"""Freeze-thaw pothole forecast: weather, freeze-thaw cycles, scoring, storage, evaluation and API.

ECCC is never called: every test that needs weather hands the code an httpx.MockTransport with recorded-shape replies.
"""
import asyncio
import json
import math
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import add_event
from fastapi.testclient import TestClient

from roadsense.common.db import session
from roadsense.core import forecast, freezethaw, weather

AUTH = {"Authorization": "Bearer test-token"}
AS_OF = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)  # a Wednesday
TODAY = AS_OF.date()
BEDFORD = (44.6720, -63.6450)


# ---------------- mocked ECCC ----------------
def climate_feature(sid, name, lon, lat, day, t_min, t_max, precip=None, snow=None):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"CLIMATE_IDENTIFIER": sid, "STATION_NAME": name, "LOCAL_DATE": f"{day.isoformat()} 00:00:00",
                           "MIN_TEMPERATURE": t_min, "MAX_TEMPERATURE": t_max, "MEAN_TEMPERATURE": None,
                           "TOTAL_PRECIPITATION": precip, "TOTAL_SNOW": snow}}


def cold_month(days=30, end=TODAY - timedelta(days=1)):
    """Every other day crosses zero; rain every third day. A far station with less data must not be chosen."""
    out = []
    for i in range(days):
        d = end - timedelta(days=i)
        crossing = i % 2 == 0
        out.append(climate_feature("8202251", "HALIFAX STANFIELD INT'L A", -63.50, 44.88, d,
                                   -4.0 if crossing else 2.0, 5.0, precip=3.0 if i % 3 == 0 else 0.0))
        if i < 5:
            out.append(climate_feature("8205091", "SHEARWATER AUTO", -63.51, 44.63, d, -1.0, 1.0))
    # the current day is published empty (ECCC fills it in a day later)
    out.append(climate_feature("8202251", "HALIFAX STANFIELD INT'L A", -63.50, 44.88, TODAY, None, None))
    return out


def period(value, cls, temp, precip=False, cloud=""):
    p = {"period": {"textForecastName": {"en": value}, "value": {"en": value}},
         "temperatures": {"temperature": [{"class": {"en": cls}, "value": {"en": temp}, "units": {"en": "C"}}]},
         "cloudPrecip": {"en": cloud}}
    if precip:
        p["precipitation"] = {"precipPeriods": [{"start": {"en": 0}, "end": {"en": 12}, "value": {"en": "rain"}}]}
    return p


def city_feature():
    return {"type": "Feature", "id": "ns-40", "geometry": {"type": "Point", "coordinates": [-63.51, 44.63]},
            "properties": {"name": {"en": "Halifax (Shearwater)"}, "forecastGroup": {
                "timestamp": {"en": "2026-11-25T20:00:00Z", "fr": "2026-11-25T20:00:00Z"},  # bilingual, as ECCC sends it
                "forecasts": [period("Wednesday night", "low", -5),
                              period("Thursday", "high", 5, precip=True),
                              period("Thursday night", "low", -4, cloud="Cloudy with 60 percent chance of flurries."),
                              period("Friday", "high", 3)]}}}


class ECCC:
    """Mock ECCC: counts calls and records the query of each one."""

    def __init__(self, climate=None, city=None, fail=False):
        self.climate = cold_month() if climate is None else climate
        self.city = [city_feature()] if city is None else city
        self.fail, self.calls = fail, []

    def handler(self, request: httpx.Request):
        self.calls.append(request)
        if self.fail:
            return httpx.Response(503, text="service unavailable")
        if "climate-daily" in request.url.path:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": self.climate})
        if "citypageweather" in request.url.path:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": self.city})
        return httpx.Response(404)

    @property
    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fdb(db):
    weather.reset()
    yield
    weather.reset()


def crack(event_id, lat=BEDFORD[0], lon=BEDFORD[1], code="D20", trip="trip1", when=None, conf=0.9,
          road="Bedford Highway", road_class="primary", area=0.03, status="verified"):
    add_event(event_id, lat, lon, code=code, trip=trip, conf=conf, area=area, status=status)
    when = when or AS_OF - timedelta(days=10)
    with session() as conn:
        conn.execute("UPDATE events SET captured_at = ?, road_name = ?, road_class = ? WHERE id = ?",
                     (when.isoformat(timespec="seconds"), road, road_class, event_id))


def run(eccc=None, **kw):
    eccc = eccc or ECCC()
    with session() as conn:
        return forecast.run_forecast(conn, as_of=kw.pop("as_of", AS_OF), client=eccc.client, **kw)


def predictions():
    with session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM risk_predictions ORDER BY score DESC")]


# ---------------- 1-4. freeze-thaw identification, temperature crossings, missing data, precipitation ----------------
def test_freeze_thaw_day_classification():
    obs = lambda lo, hi, **k: {"kind": "observed", "t_min": lo, "t_max": hi, **k}
    assert freezethaw.classify_day(obs(-3, 4)) == "confirmed"
    assert freezethaw.classify_day({**obs(-3, 4), "kind": "forecast"}) == "potential"  # a forecast is not a fact
    assert freezethaw.classify_day(obs(-8, -1)) == "none"   # stayed frozen
    assert freezethaw.classify_day(obs(1, 9)) == "none"     # never froze
    assert freezethaw.classify_day(obs(0, 0)) == "confirmed"  # touching the threshold counts at margin 0


def test_temperature_crossing_thresholds_and_margin():
    obs = {"kind": "observed", "t_min": -0.5, "t_max": 0.5}
    assert freezethaw.classify_day(obs) == "confirmed"
    assert freezethaw.classify_day(obs, margin_c=1.0) == "none"  # a shallow crossing doesn't count with a margin
    assert freezethaw.classify_day({"kind": "observed", "t_min": 1.5, "t_max": 6}, freeze_c=2.0) == "confirmed"


def test_missing_weather_is_missing_not_invented():
    assert freezethaw.classify_day(None) == "missing"
    assert freezethaw.classify_day({"kind": "observed", "t_min": None, "t_max": None}) == "missing"
    assert freezethaw.classify_day({"kind": "observed", "t_min": float("nan"), "t_max": None}) == "missing"
    # one bound known: it can only rule a crossing out, never confirm one
    assert freezethaw.classify_day({"kind": "observed", "t_min": -5, "t_max": None}) == "potential"
    assert freezethaw.classify_day({"kind": "observed", "t_min": 3, "t_max": None}) == "none"
    start = date(2026, 11, 1)
    s = freezethaw.summarise([{"date": "2026-11-02", "kind": "observed", "t_min": -2, "t_max": 3}], start,
                             start + timedelta(days=4))
    assert (s["days"], s["confirmed_cycles"], s["missing_days"], s["coverage"]) == (5, 1, 4, 0.2)


def test_precipitation_handling():
    assert freezethaw.is_wet({"precip_mm": 0.1}) is False      # trace below the threshold
    assert freezethaw.is_wet({"precip_mm": 4.0}) is True
    assert freezethaw.is_wet({"precip_mm": 0.0, "snow_cm": 2}) is True
    assert freezethaw.is_wet({"precip_chance": 60}) is True
    assert freezethaw.is_wet({"precip_chance": 30, "precip_expected": False}) is False
    assert freezethaw.is_wet({"t_min": -3}) is None             # unknown, not dry
    start = date(2026, 11, 1)
    recs = [{"date": "2026-11-01", "kind": "observed", "t_min": -3, "t_max": 2, "precip_mm": 0},
            {"date": "2026-11-02", "kind": "observed", "t_min": 1, "t_max": 4, "precip_mm": 6},
            {"date": "2026-11-03", "kind": "observed", "t_min": 1, "t_max": 4}]
    s = freezethaw.summarise(recs, start, start + timedelta(days=2))
    assert (s["wet_days"], s["precip_known_days"], s["moist_cycles"]) == (1, 2, 1)  # rain the day after the cycle


# ---------------- ECCC client ----------------
def test_observed_weather_picks_the_most_complete_station_and_caches(fdb):
    eccc = ECCC()
    start, end = TODAY - timedelta(days=30), TODAY - timedelta(days=1)
    with session() as conn:
        r = weather.observed(conn, *BEDFORD, start, end, eccc.client)
        assert r["available"] and "STANFIELD" in r["station"] and "ECCC climate-daily" in r["source"]
        assert len(r["records"]) == 30 and all(x["fetched_at"] and x["source"] for x in r["records"])
        assert eccc.calls[0].url.params["datetime"] == f"{start.isoformat()}/{end.isoformat()}"
        again = weather.observed(conn, *BEDFORD, start, end, eccc.client)
    assert again["cached"] and len(eccc.calls) == 1  # served from SQLite, no second request


def test_city_forecast_is_parsed_into_calendar_days(fdb):
    days = weather.parse_city_forecast(city_feature())
    by = {d["date"]: d for d in days}
    thu, fri = by["2026-11-26"], by["2026-11-27"]
    assert (thu["t_min"], thu["t_max"], thu["precip_expected"]) == (-5, 5, True)  # Wednesday night's low is Thursday's
    assert (fri["t_min"], fri["t_max"], fri["precip_chance"]) == (-4, 3, 60)
    assert "2026-12-05" not in by  # nothing beyond what ECCC published


# ---------------- 11. weather API failure ----------------
def test_weather_api_failure_is_recorded_and_never_filled_in(fdb):
    good, bad = ECCC(), ECCC(fail=True)
    start, end = TODAY - timedelta(days=5), TODAY - timedelta(days=1)
    with session() as conn:
        r = weather.observed(conn, *BEDFORD, start, end, bad.client)
        assert not r["available"] and r["records"] == [] and "unavailable" in r["error"]
        assert len(bad.calls) == 2  # one retry, then give up
        assert conn.execute("SELECT COUNT(*) FROM weather_daily").fetchone()[0] == 0
        fail = conn.execute("SELECT * FROM weather_fetches WHERE kind = 'observed'").fetchone()
        assert fail["ok"] == 0 and "503" in fail["error"]
        f = weather.forecast(conn, *BEDFORD, bad.client)
        assert not f["available"] and f["records"] == []
        # with stored data from an earlier success, a later outage serves it and says so
        weather.observed(conn, *BEDFORD, start - timedelta(days=30), end, good.client)
        conn.execute("UPDATE weather_fetches SET fetched_at = '2020-01-01T00:00:00+00:00'")
        stale = weather.observed(conn, *BEDFORD, start, end, bad.client)
    assert stale["available"] and stale["error"] and len(stale["records"]) == 5


# ---------------- 5-7. crack severity, risk score, categories ----------------
def test_crack_severity_ranks_class_confidence_and_repeat_sightings():
    r = forecast.rules()
    ev = lambda code, conf=0.9, trip="t1": {"class_code": code, "confidence": conf, "trip_id": trip, "width_m": None,
                                             "box_area_ratio": 0.03, "jolt_g": None}
    alligator = forecast.crack_severity_factor([ev("D20")], r)["value"]
    longitudinal = forecast.crack_severity_factor([ev("D00")], r)["value"]
    unsure = forecast.crack_severity_factor([ev("D20", conf=0.3)], r)["value"]
    repeated = forecast.crack_severity_factor([ev("D20", 0.6, "t1"), ev("D20", 0.6, "t2"), ev("D20", 0.6, "t3")], r)
    assert alligator > longitudinal and alligator > unsure
    assert repeated["value"] > forecast.crack_severity_factor([ev("D20", 0.6)], r)["value"]
    assert repeated["raw"]["trips"] == 3
    assert not forecast.crack_severity_factor([ev("D40")], r)["available"]  # a pothole is not a crack


def test_risk_score_is_the_weighted_sum_and_missing_data_lowers_quality():
    r = forecast.rules()
    f = lambda name, v, cov=1.0, avail=True, applicable=True: forecast._factor(name, v, {}, cov, avail, applicable)
    factors = [f("crack_severity", 1.0), f("recent_freeze_thaw", 0.5), f("forecast_freeze_thaw", 0.0, 0.5),
               f("moisture", 1.0), f("road_class", 0, 0, avail=False),
               f("pothole_history", 0, 0, avail=False, applicable=False)]
    s = forecast.score(factors, r)
    # pothole_history does not apply, so the other weights are the documented 30/25/20/15/10
    assert s["score"] == pytest.approx(0.30 + 0.25 * 0.5 + 0.15)
    assert s["data_quality"] == pytest.approx(0.30 + 0.25 + 0.20 * 0.5 + 0.15)
    assert {x["factor"]: x["weight"] for x in factors}["road_class"] == pytest.approx(0.10)
    assert {x["factor"]: x["contribution"] for x in factors}["road_class"] == 0


def test_risk_categories_and_the_data_quality_cap():
    r = forecast.rules()
    assert forecast.categorise(0.39, 1, r) == ("low", False)
    assert forecast.categorise(0.40, 1, r) == ("medium", False)
    assert forecast.categorise(0.70, 1, r) == ("high", False)
    assert forecast.categorise(0.95, 0.4, r) == ("medium", True)  # incomplete inputs never produce a High
    assert forecast.categorise(0.2, 0.4, r) == ("low", True)


# ---------------- 12. forecast generation end to end ----------------
def test_forecast_generation_explains_and_stores_each_prediction(fdb):
    crack("e1", trip="t1")
    crack("e2", lat=BEDFORD[0] + 0.0001, trip="t2", when=AS_OF - timedelta(days=4))
    crack("far", lat=44.6420, lon=-63.5750, code="D00", road="Spring Garden Road", road_class="tertiary", conf=0.4)
    res = run()
    assert res["segments"] == 2 and res["weather_status"] == "ok" and not res["skipped"]
    preds = predictions()
    top = preds[0]
    assert top["category"] == "high" and top["data_quality"] >= 0.6
    assert top["period_start"] == "2026-11-25" and top["period_end"] == "2026-12-08"  # 14-day default horizon
    assert "alligator crack" in top["explanation"] and "freeze-thaw" in top["explanation"]
    sources = json.loads(top["data_sources"])
    assert "ECCC climate-daily" in sources["weather_observed"] and "ECCC city forecast" in sources["weather_forecast"]
    with session() as conn:
        factors = {f["factor"]: dict(f) for f in conn.execute(
            "SELECT * FROM prediction_factors WHERE prediction_id = ?", (top["id"],))}
        ind = {i["span"]: dict(i) for i in conn.execute(
            "SELECT * FROM freeze_thaw_indicators WHERE segment_id = ?", (top["segment_id"],))}
        latest = forecast.latest(conn)
    assert set(factors) == set(forecast.FACTOR_LABELS)
    assert ind["history"]["confirmed_cycles"] == 15 and ind["history"]["source"].startswith("ECCC")
    assert ind["forecast"]["potential_cycles"] == 2 and ind["forecast"]["missing_days"] == 12  # ECCC covers ~6 days
    assert latest["summary"]["total"] == 2 and latest["model_kind"] == "heuristic" and "not" in latest["disclaimer"]


def test_weather_outage_cannot_produce_a_high_risk(fdb):
    crack("e1", trip="t1")
    crack("e2", trip="t2")
    res = run(ECCC(fail=True))
    p = predictions()[0]
    assert res["weather_status"] == "unavailable" and res["weather_errors"]
    assert p["category"] != "high" and p["insufficient_data"] == 1
    assert "no weather data" in p["explanation"]


# ---------------- 8. missing road classification ----------------
def test_missing_road_class_is_explicit(fdb):
    crack("e1", road_class=None)
    run()
    p = predictions()[0]
    with session() as conn:
        f = dict(conn.execute("SELECT * FROM prediction_factors WHERE prediction_id = ? AND factor = 'road_class'",
                              (p["id"],)).fetchone())
    assert f["available"] == 0 and f["contribution"] == 0 and "unknown" in f["raw"]
    assert "road class unknown" in p["explanation"] and json.loads(p["data_sources"])["road_class"] == "unknown"


# ---------------- 9. several observations on one segment ----------------
def test_sightings_on_one_segment_are_grouped_not_counted_as_segments(fdb):
    crack("a", trip="t1")
    crack("b", lat=BEDFORD[0] + 0.0001, trip="t2")
    crack("c", lat=BEDFORD[0] + 0.00015, trip="t2", code="D10")
    crack("other-road", road="Kearney Lake Road", road_class="secondary")
    run()
    preds = predictions()
    assert len(preds) == 2
    with session() as conn:
        bedford = [p for p in preds if conn.execute("SELECT road_name FROM road_segments WHERE id = ?",
                                                    (p["segment_id"],)).fetchone()[0] == "Bedford Highway"][0]
        raw = json.loads(conn.execute("SELECT raw FROM prediction_factors WHERE prediction_id = ? AND "
                                      "factor = 'crack_severity'", (bedford["id"],)).fetchone()[0])
    assert (raw["observations"], raw["trips"]) == (3, 2)


def test_a_crack_on_a_100m_bin_edge_stays_one_segment():
    lat = 44.6720  # east-west axis: bins are along longitude
    k = 111_000 * math.cos(math.radians(lat))
    edge = (math.floor(-63.6450 * k / 100) * 100) / k  # longitude of a bin boundary
    t = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ev = lambda i, lon: {"id": i, "lat": lat, "lon": lon, "road_name": "Bedford Highway", "class_code": "D20",
                         "trip_id": "t", "captured_at": t.isoformat(), "at": t}
    segs = forecast.build_segments([ev("w", edge - 0.00005), ev("e", edge + 0.00005)], tolerance_m=25)
    assert len(segs) == 1
    assert len(forecast.build_segments([ev("w", edge - 0.00005), ev("e", edge + 0.00005)], tolerance_m=1)) == 2


def test_invalid_gps_and_confidence_are_skipped_not_scored(fdb):
    crack("ok")
    crack("bad-gps", lat=999.0)
    crack("null-island", lat=0.0, lon=0.0)
    crack("bad-conf", conf=1.7)
    res = run()
    assert res["segments"] == 1
    assert res["skipped_inputs"] == {"invalid_gps": 2, "invalid_confidence": 1}


# ---------------- 10. duplicate predictions ----------------
def test_a_rerun_with_the_same_inputs_is_skipped_and_never_duplicates(fdb):
    crack("e1")
    eccc = ECCC()
    first = run(eccc)
    again = run(eccc)
    assert again["skipped"] and again["run_id"] == first["run_id"]
    forced = run(eccc, force=True)
    assert forced["run_id"] != first["run_id"] and len(predictions()) == 1  # same segment, day and rules: updated
    crack("e2", trip="t2")
    assert not run(eccc)["skipped"]  # a new sighting is new input


# ---------------- 13. deterioration history ----------------
def test_deterioration_from_crack_to_pothole():
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    ev = lambda i, code, days, trip: {"id": i, "class_code": code, "trip_id": trip, "confidence": 0.8,
                                      "report_id": None, "at": t0 + timedelta(days=days),
                                      "captured_at": (t0 + timedelta(days=days)).isoformat()}
    h = forecast.deterioration([ev("a", "D10", 0, "t1"), ev("b", "D20", 10, "t2"), ev("c", "D40", 21, "t3")])
    assert h["days_crack_to_pothole"] == 21 and [c["class_code"] for c in h["class_changes"]] == ["D10", "D20", "D40"]
    assert h["sufficient"] and h["trips"] == 3
    single = forecast.deterioration([ev("x", "D00", 0, "t1")])
    assert not single["sufficient"] and single["first_pothole_at"] is None
    summary = forecast.history_summary([{"events": [ev("a", "D10", 0, "t1"), ev("c", "D40", 21, "t3")]}])
    assert summary["crack_to_pothole_pairs"] == 1 and not summary["sufficient"] and "Not enough history" in summary["message"]


# ---------------- 14. outcome evaluation and backtesting ----------------
def test_outcomes_compare_forecasts_with_later_trips(fdb):
    crack("hit", trip="t1")
    crack("hit2", trip="t2")
    crack("quiet", lat=44.6420, lon=-63.5750, road="Spring Garden Road", road_class="tertiary")
    crack("seen", lat=44.6900, lon=-63.6600, road="Kearney Lake Road", road_class="secondary")
    run()
    # after the forecast: a pothole where we predicted risk, and a later pass that saw only the crack again
    crack("pothole", code="D40", trip="t9", when=AS_OF + timedelta(days=3))
    crack("seen-again", lat=44.6900, lon=-63.6600, road="Kearney Lake Road", trip="t9", when=AS_OF + timedelta(days=5))
    with session() as conn:
        during = forecast.evaluate_outcomes(conn, now_dt=AS_OF + timedelta(days=4))
        assert during == {"evaluated": 1, "pothole_confirmed": 1}  # the rest is still inside the period
        after = forecast.evaluate_outcomes(conn, now_dt=AS_OF + timedelta(days=30))
        summary = forecast.evaluation_summary(conn)
    assert after == {"evaluated": 2, "no_pothole_observed": 1, "not_observed": 1}
    assert summary["confirmed_potholes"] == 1 and summary["not_observed"] == 1 and summary["pending"] == 0
    assert summary["high_predictions"] == sum(p["category"] == "high" for p in predictions())
    assert not summary["validated"] and summary["high_precision"] is not None and "Not evaluated yet" in summary["message"]


def test_backtest_uses_only_what_was_known_at_the_time(fdb):
    as_of = datetime(2026, 11, 10, tzinfo=timezone.utc)
    crack("old", trip="t1", when=as_of - timedelta(days=5))
    crack("future", lat=44.6420, lon=-63.5750, road="Spring Garden Road", when=as_of + timedelta(days=2))
    crack("pothole", code="D40", trip="t2", when=as_of + timedelta(days=6))
    eccc = ECCC(climate=cold_month(end=as_of.date() + timedelta(days=10)))
    with session() as conn:
        before = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    with session() as conn:
        res = forecast.backtest(conn, [as_of.date()], client=eccc.client)
    run_ = res["runs"][0]
    assert run_["segments"] == 1  # the sighting from after the forecast date is not used
    assert all("citypageweather" not in c.url.path for c in eccc.calls)  # no forecast for a past date
    assert all(c.url.params["datetime"].split("/")[1] < as_of.date().isoformat() for c in eccc.calls)
    with session() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM weather_daily")]
        f = dict(conn.execute("SELECT * FROM prediction_factors WHERE factor = 'forecast_freeze_thaw'").fetchone())
        after = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    assert f["available"] == 0
    assert res["evaluation"]["confirmed_potholes"] == 1  # the later pothole is the outcome, never an input
    assert after == before  # forecasting never changes detections
    assert rows  # observed weather was stored for reuse


# ---------------- scheduler, job and API ----------------
def test_scheduler_queues_the_forecast_and_the_job_runs_it(fdb, monkeypatch):
    from roadsense.services.jobs import Worker

    crack("e1")
    w = Worker()
    assert "forecast_potholes" in w.schedule()
    assert "forecast_potholes" not in w.schedule()  # not again before the period passes
    eccc = ECCC()
    real = forecast.run_forecast
    monkeypatch.setattr(forecast, "run_forecast", lambda conn, **kw: real(conn, as_of=AS_OF, client=eccc.client, **kw))
    out = asyncio.run(w.execute({"kind": "forecast_potholes", "args": {}, "actor": "scheduler"}))
    assert out["segments"] == 1 and "outcomes" in out


def test_forecast_api(fdb):
    from roadsense.services import agent_api

    crack("e1")
    run()
    c = TestClient(agent_api.app)
    assert c.get("/forecast").status_code == 401
    f = c.get("/forecast", headers=AUTH).json()
    assert f["summary"]["total"] == 1 and f["segments"][0]["main_factors"] and f["model_kind"] == "heuristic"
    seg = f["segments"][0]["segment_id"]
    d = c.get(f"/forecast/segments/{seg}", headers=AUTH).json()
    assert d["events"][0]["id"] == "e1" and d["prediction"]["factors"] and d["weather"]
    assert c.get("/forecast/segments/nope", headers=AUTH).status_code == 404
    assert c.get("/forecast/evaluation", headers=AUTH).json()["validated"] is False
    assert c.get("/forecast/history", headers=AUTH).json()["sufficient"] is False
    viewer = {**AUTH, "X-RoadSense-User": "v@roadsense.test", "X-RoadSense-Role": "viewer"}
    assert c.post("/forecast/run", headers=viewer).status_code == 403


def test_forecast_rules_are_valid_and_hashed():
    r = forecast.rules()
    assert r["model_kind"] == "heuristic"
    assert sum(v for k, v in r["weights"].items() if k != "pothole_history") == pytest.approx(1.0)
    assert forecast.config_hash(r, 14) != forecast.config_hash(r, 7)
