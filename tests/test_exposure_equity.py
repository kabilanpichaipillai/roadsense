"""Exposure-weighted risk (OSM infrastructure near a defect) and the Repair Equity Audit (repair times by neighbourhood).

OpenStreetMap (Overpass) and the city's ArcGIS layer are never called: tests hand the code an httpx.MockTransport.
"""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import add_event
from fastapi.testclient import TestClient

from roadsense.common.db import session
from roadsense.core import equity, exposure, external, reports

AUTH = {"Authorization": "Bearer test-token"}
HRM = "Halifax Regional Municipality"
LAT, LON = 44.6500, -63.5800
M = 1 / 111_000  # one metre of latitude


# ---------------- mocked OpenStreetMap ----------------
def node(i, dlat_m=0.0, **tags):
    return {"type": "node", "id": i, "lat": LAT + dlat_m * M, "lon": LON, "tags": tags}


def way(i, pts_m, **tags):
    """A way through points given as metres north of the defect, offset east-west by ±30 m."""
    return {"type": "way", "id": i, "tags": tags,
            "geometry": [{"lat": LAT + dy * M, "lon": LON + dx * M / 0.71} for dx, dy in pts_m]}


ROAD_THROUGH = [(-30, 0), (30, 0)]  # an east-west road passing right through the defect


class OSM:
    def __init__(self, elements=None, fail=False):
        self.elements, self.fail, self.calls = elements or [], fail, []

    def handler(self, request):
        self.calls.append(request)
        if self.fail:
            return httpx.Response(504, text="gateway timeout")
        return httpx.Response(200, json={"elements": self.elements})

    @property
    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def edb(db):
    exposure.reset()
    yield
    exposure.reset()


def report(event_id, lat=LAT, lon=LON, code="D40", band="S2", width=0.20, road="Main Street", road_class="residential",
           confirmations=1):
    add_event(event_id, lat, lon, code=code)
    with session() as conn:
        conn.execute("UPDATE events SET width_m = ? WHERE id = ?", (width, event_id))
        r = reports.create_or_merge(conn, event_id, "medium", f"{road} near x", HRM, 15, jurisdiction=HRM, band=band,
                                    road_class=road_class, road_name=road)["report"]
        conn.execute("UPDATE reports SET confirmations = ?, width_m = ? WHERE id = ?", (confirmations, width, r["id"]))
        return dict(conn.execute("SELECT * FROM reports WHERE id = ?", (r["id"],)).fetchone())


def score(rep, osm, **kw):
    with session() as conn:
        return exposure.compute(conn, rep, osm.client, **kw)


def factors(res):
    return {f["factor"]: f for f in json.loads(res["factors"])}


# ---------------- 1-5. infrastructure near a pothole ----------------
def test_pothole_in_a_cycle_lane_outranks_the_same_pothole_on_a_quiet_street(edb):
    lane = report("lane")
    quiet = report("quiet", lat=LAT + 0.01)
    res_lane = score(lane, OSM([way(1, ROAD_THROUGH, highway="secondary", name="Main Street", cycleway="lane")]))
    res_quiet = score(quiet, OSM([way(2, ROAD_THROUGH, highway="residential", name="Main Street")]))
    f = factors(res_lane)
    assert f["cyclist"]["value"] == pytest.approx(1.0, abs=0.05)
    assert f["cyclist"]["raw"]["relation"] == "bike lane on this road"
    assert res_lane["physical_score"] == res_quiet["physical_score"]  # same pothole: same physical severity
    assert res_lane["overall_score"] > res_quiet["overall_score"]
    assert "bike lane on this road" in res_lane["explanation"] and "proxy" in res_lane["explanation"]


def test_a_bike_lane_on_another_road_is_nearby_not_on_this_road(edb):
    rep = report("x")
    res = score(rep, OSM([way(1, [(-30, 12), (30, 12)], highway="secondary", name="Other Street", cycleway="lane")]))
    cyc = factors(res)["cyclist"]
    assert cyc["raw"]["relation"] == "bike lane on a nearby road"
    assert cyc["value"] < 0.6  # the closest lane is not assumed to be on the damaged road


def test_pothole_near_a_pedestrian_crossing(edb):
    res = score(report("x"), OSM([node(5, 10, highway="crossing", crossing="marked")]))
    ped = factors(res)["pedestrian"]
    assert ped["value"] == pytest.approx(1 - 10 / 30, abs=0.03)
    assert ped["raw"]["top_feature"] == "Pedestrian crossing"


def test_pothole_near_a_school_is_a_proxy_not_a_count(edb):
    school = way(7, [(-20, 70), (20, 70), (20, 110), (-20, 110), (-20, 70)], amenity="school", name="Oxford School")
    res = score(report("x"), OSM([school]))
    ped = factors(res)["pedestrian"]
    assert 0 < ped["value"] < 0.7 and ped["raw"]["proxy"] is True
    assert "not a count" in ped["raw"]["relation"]
    with session() as conn:
        f = dict(conn.execute("SELECT * FROM exposure_features WHERE report_id = ?", (res["report_id"],)).fetchone())
    assert f["category"] == "school" and 65 <= f["distance_m"] <= 75


def test_pothole_near_a_bus_stop(edb):
    res = score(report("x"), OSM([node(9, 5, highway="bus_stop", name="Spring Garden Rd [7040]")]))
    tr = factors(res)["transit"]
    assert tr["value"] == pytest.approx(1 - 5 / 30, abs=0.03) and tr["raw"]["name"] == "Spring Garden Rd [7040]"


def test_pothole_with_no_nearby_infrastructure(edb):
    res = score(report("x"), OSM([way(1, ROAD_THROUGH, highway="residential", name="Main Street")]))
    f = factors(res)
    assert f["cyclist"]["value"] == f["pedestrian"]["value"] == f["transit"]["value"] == 0
    assert res["exposure_score"] is not None and res["osm_status"] == "ok"
    assert "no cycling, pedestrian or bus infrastructure mapped nearby" in res["explanation"]


# ---------------- 6-7. missing OSM, invalid GPS ----------------
def test_missing_osm_marks_exposure_unknown_and_never_guesses(edb):
    res = score(report("x"), OSM(fail=True))
    f = factors(res)
    assert res["osm_status"] == "unavailable" and res["exposure_score"] is None
    assert not f["cyclist"]["available"] and f["cyclist"]["contribution"] == 0
    assert res["data_quality"] == pytest.approx(0.5 * 1.0 + 0.05)  # physical + road class only
    assert "physical severity only" in res["explanation"]


def test_stale_osm_is_used_but_flagged_when_the_refresh_fails(edb):
    rep = report("x")
    score(rep, OSM([node(9, 5, highway="bus_stop")]))
    with session() as conn:
        conn.execute("UPDATE geocache SET value = json_set(value, '$.fetched_at', '2020-01-01T00:00:00+00:00') "
                     "WHERE key LIKE 'osm:%'")
    res = score(rep, OSM(fail=True))
    assert res["osm_status"] == "stale" and factors(res)["transit"]["value"] > 0
    assert "older than the refresh period" in res["explanation"]


def test_invalid_gps_is_not_sent_to_osm(edb):
    rep = report("x")
    rep["lat"] = 999.0
    osm = OSM([node(9, 5, highway="bus_stop")])
    res = score(rep, osm)
    assert osm.calls == [] and res["osm_status"] == "invalid_gps" and res["exposure_score"] is None
    assert "invalid GPS position" in res["explanation"]


# ---------------- 8-11. physical severity, score, categories, weights ----------------
def test_physical_severity_follows_the_existing_severity_band():
    r = exposure.rules()
    p = lambda **k: exposure.physical_factor({"class_code": "D40", "confirmations": 1, **k}, r)["value"]
    assert p(severity_band="S1") < p(severity_band="S2") < p(severity_band="S3") < p(severity_band="S4") == 1.0
    crack = exposure.physical_factor({"class_code": "D00", "severity_band": "S2", "confirmations": 1}, r)
    assert crack["value"] < p(severity_band="S2")
    seen_twice = exposure.physical_factor({"class_code": "D40", "severity_band": "S2", "confirmations": 3}, r)
    assert seen_twice["value"] == pytest.approx(0.5 + 0.1) and seen_twice["raw"]["depth"] == "not measured"


def test_overall_score_is_the_documented_weighted_sum(edb):
    rep = report("x", band="S3", road_class="primary")
    res = score(rep, OSM([way(1, ROAD_THROUGH, highway="primary", name="Main Street", cycleway="lane"),
                          node(5, 15, highway="crossing")]))
    f = factors(res)
    expected = 0.5 * 0.75 + 0.2 * f["cyclist"]["value"] + 0.15 * f["pedestrian"]["value"] + 0.1 * 0 + 0.05 * 0.9
    assert res["overall_score"] == pytest.approx(expected, abs=1e-3)
    assert res["physical_score"] == 0.75  # kept separate from exposure and overall
    assert res["exposure_score"] == pytest.approx((expected - 0.5 * 0.75) / 0.5, abs=1e-3)
    with session() as conn:  # the detector's confidence is untouched
        assert conn.execute("SELECT confidence FROM events WHERE id = 'x'").fetchone()[0] == 0.8


def test_risk_categories_and_critical_needs_the_urgent_band():
    r = exposure.rules()
    fs = lambda phys, cyc: [{"factor": "physical", "value": phys, "available": True, "coverage": 1.0},
                            {"factor": "cyclist", "value": cyc, "available": True, "coverage": 1.0},
                            {"factor": "pedestrian", "value": 1.0, "available": True, "coverage": 1.0},
                            {"factor": "transit", "value": 1.0, "available": True, "coverage": 1.0},
                            {"factor": "road_context", "value": 1.0, "available": True, "coverage": 1.0}]
    assert exposure.combine(fs(0.1, 0), "S1", r)["category"] == "medium"   # 0.05 + 0.30 = 0.35
    assert exposure.combine(fs(0.0, 0), "S1", r)["category"] == "low"      # 0.30
    assert exposure.combine(fs(1.0, 1), "S4", r)["category"] == "critical"
    assert exposure.combine(fs(1.0, 1), "S3", r)["category"] == "high"     # exposure alone never makes it Critical


def test_weights_are_configurable_and_validated():
    base = exposure.rules()
    heavy = json.loads(json.dumps(base))
    heavy["weights"] = {"physical": 0.2, "cyclist": 0.5, "pedestrian": 0.1, "transit": 0.1, "road_context": 0.1}
    fs = lambda: [{"factor": "physical", "value": 0.5, "available": True, "coverage": 1.0},
                  {"factor": "cyclist", "value": 1.0, "available": True, "coverage": 1.0},
                  {"factor": "pedestrian", "value": 0.0, "available": True, "coverage": 1.0},
                  {"factor": "transit", "value": 0.0, "available": True, "coverage": 1.0},
                  {"factor": "road_context", "value": 0.4, "available": True, "coverage": 1.0}]
    assert exposure.combine(fs(), "S2", exposure.rules(heavy))["overall"] > exposure.combine(fs(), "S2", base)["overall"]
    assert exposure.rules_hash(heavy) != exposure.rules_hash(base)
    bad = json.loads(json.dumps(base))
    bad["weights"]["cyclist"] = -1
    with pytest.raises(ValueError):
        exposure.rules(bad)


# ---------------- 12. no duplicate exposure ----------------
def test_features_and_calculations_are_never_duplicated(edb):
    stop = node(9, 5, highway="bus_stop", public_transport="platform", name="Stop A")
    platform = way(10, [(-3, 6), (3, 6)], public_transport="platform", bus="yes", name="Stop A")
    osm = OSM([stop, stop, platform, node(5, 8, highway="crossing")])  # the same element twice, and a twin
    rep = report("x")
    first = score(rep, osm)
    with session() as conn:
        rows = conn.execute("SELECT category, COUNT(*) FROM exposure_features GROUP BY category").fetchall()
    assert dict(rows) == {"bus_stop": 1, "crossing": 1}
    again = score(rep, osm)
    assert first["recomputed"] and not again["recomputed"]  # unchanged inputs: stored score reused
    assert len(osm.calls) == 1  # OSM answer served from the cache
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_risk").fetchone()[0] == 1
        rep2 = dict(conn.execute("SELECT * FROM reports WHERE id = ?", (rep["id"],)).fetchone())
    rep2["severity_band"] = "S4"  # a change the score depends on
    assert score(rep2, osm)["recomputed"]


def test_batch_scoring_and_listing_filters(edb):
    report("a")
    report("b", lat=LAT + 0.01, code="D20", road_class="primary")
    osm = OSM([node(9, 5, highway="bus_stop")])
    with session() as conn:
        assert exposure.run(conn, osm.client)["scored"] == 2
        assert exposure.run(conn, osm.client)["scored"] == 0  # nothing left to do
        listing = exposure.listing(conn)
        # the mocked stop is 5 m from report a and about 1.1 km from report b
        assert listing["summary"]["near_bus_stop"] == 1 and listing["summary"]["defects"] == 2
        assert [x["class_code"] for x in exposure.listing(conn, damage="D20")["reports"]] == ["D20"]
        assert len(exposure.listing(conn, road_class="primary")["reports"]) == 1
        assert len(exposure.listing(conn, infrastructure="cycling")["reports"]) == 0
        assert len(exposure.listing(conn, neighbourhood="Unassigned")["reports"]) == 2


# ---------------- repair equity ----------------
T0 = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)


def city_sr(conn, rid, days=None, lat=LAT, lon=LON, reported=T0, kind="Pothole", raw=None, status=None):
    closed = reported + timedelta(days=days) if days is not None else None
    external._store(conn, {"id": f"city:{rid}", "source": "city_sr", "kind": kind,
                           "status": status or ("closed" if days is not None else "open"), "lat": lat, "lon": lon,
                           "description": kind, "reported_at": reported.isoformat(),
                           "closed_at": closed.isoformat() if closed else None, "raw": raw or {}})


def suburb(conn, lat, lon, name):
    conn.execute("INSERT OR REPLACE INTO geocache (key, value) VALUES (?, ?)",
                 (f"v3:{lat:.4f},{lon:.4f}", json.dumps({"suburb": name, "road_class": "residential",
                                                          "source": "openstreetmap_nominatim"})))


def by_id(recs):
    return {r["id"]: r for r in recs}


def test_neighbourhood_comes_from_evidence(db):
    with session() as conn:
        city_sr(conn, 1, 5, raw={"COMMUNITY": "DARTMOUTH"})
        suburb(conn, LAT + 0.01, LON, "North End")
        city_sr(conn, 2, 5, lat=LAT + 0.01)
        recs = by_id(equity.records(conn)[0])
        stored = dict(conn.execute("SELECT * FROM neighbourhood_assignments WHERE record_id = 'city:1'").fetchone())
    assert recs["city:1"]["neighbourhood"] == "Dartmouth" and "COMMUNITY" in recs["city:1"]["neighbourhood_source"]
    assert recs["city:2"]["neighbourhood"] == "North End" and "OpenStreetMap" in recs["city:2"]["neighbourhood_source"]
    assert stored["neighbourhood"] == "Dartmouth"


def test_records_without_evidence_are_unassigned(db):
    with session() as conn:
        city_sr(conn, 1, 5, lat=LAT + 0.2)
        # the offline fixture's place has no suburb and is not OSM evidence
        conn.execute("INSERT INTO geocache (key, value) VALUES (?, ?)", (f"v3:{LAT + 0.3:.4f},{LON:.4f}", json.dumps(
            {"suburb": "Somewhere", "source": "offline_fixture"})))
        city_sr(conn, 2, 5, lat=LAT + 0.3)
        recs = by_id(equity.records(conn)[0])
        s = equity.summary(conn)
    assert recs["city:1"]["neighbourhood"] == recs["city:2"]["neighbourhood"] == "Unassigned"
    assert s["data_quality"]["unassigned_neighbourhood"] == 2
    assert s["neighbourhoods"][0]["comparison"]["status"] == "not_compared"


def test_repair_response_and_inspection_times_are_kept_apart(db):
    with session() as conn:
        city_sr(conn, 1, 10, raw={"DateAcknowledged": int((T0 + timedelta(days=1)).timestamp() * 1000),
                                  "InspectionDate": (T0 + timedelta(days=3)).isoformat()})
        city_sr(conn, 2, 4, reported=T0 + timedelta(hours=1))
        recs = by_id(equity.records(conn)[0])
    a = recs["city:1"]
    assert (a["repair_days"], a["response_days"], a["inspection_days"]) == (10.0, 1.0, 3.0)
    assert recs["city:2"]["response_days"] is None  # no acknowledgement timestamp: no response time


def test_missing_completion_timestamp_and_unresolved(db):
    with session() as conn:
        city_sr(conn, 1, None, status="closed")  # closed, but no closing date
        city_sr(conn, 2, None, reported=T0 + timedelta(hours=1))  # still open
        recs, quality = equity.records(conn)
    r = by_id(recs)
    assert r["city:1"]["status"] == "completed" and r["city:1"]["repair_days"] is None
    assert r["city:2"]["status"] == "unresolved"
    assert quality["completed_without_timestamp"] == 1


def test_duplicate_city_requests_count_once(db):
    with session() as conn:
        city_sr(conn, 1, 5)
        city_sr(conn, 1, 5)          # synced twice: one row
        city_sr(conn, "1b", 5)       # republished under another id: same place, time and kind
        city_sr(conn, 2, 5, reported=T0 + timedelta(days=1))
        recs, quality = equity.records(conn)
    assert len(recs) == 2 and quality["duplicates_removed"] == 1


def test_average_and_median_repair_time(db):
    with session() as conn:
        for i, d in enumerate([1, 2, 3, 10]):
            city_sr(conn, i, d, reported=T0 + timedelta(hours=i))
        s = equity.summary(conn)
    rep = s["overall"]["repair"]
    assert (rep["n"], rep["mean"], rep["median"], rep["min"], rep["max"]) == (4, 4.0, 2.5, 1.0, 10.0)
    assert rep["median"] < rep["mean"]  # one slow repair pulls the average, not the median


def _two_neighbourhoods(conn, slow_days=20, fast_days=5, n=12, category="Pothole"):
    suburb(conn, LAT, LON, "Area A")
    suburb(conn, LAT + 0.01, LON, "Area B")
    for i in range(n):
        city_sr(conn, f"a{i}{category}", slow_days + i % 3, reported=T0 + timedelta(hours=i), kind=category)
        city_sr(conn, f"b{i}{category}", fast_days + i % 3, lat=LAT + 0.01, reported=T0 + timedelta(hours=i),
                kind=category)


def test_filtering_by_neighbourhood_and_date_range(db):
    with session() as conn:
        _two_neighbourhoods(conn, n=4)
        city_sr(conn, "late", 3, reported=datetime(2026, 6, 1, tzinfo=timezone.utc))
        only_a = equity.summary(conn, neighbourhood="Area A")
        march = equity.summary(conn, start=date(2026, 3, 1), end=date(2026, 3, 31))
        june = equity.summary(conn, start=date(2026, 6, 1))
        rows = equity.neighbourhood_records(conn, "Area B")
    assert [x["neighbourhood"] for x in only_a["neighbourhoods"]] == ["Area A"]
    assert march["overall"]["requests"] == 8 and june["overall"]["requests"] == 1
    assert len(rows) == 4 and all("trip_id" not in r and "approved_by" not in r for r in rows)  # no driver/trip data


def test_small_samples_are_flagged_not_compared(db):
    with session() as conn:
        _two_neighbourhoods(conn, n=3)
        s = equity.summary(conn)
    a = next(x for x in s["neighbourhoods"] if x["neighbourhood"] == "Area A")
    assert a["insufficient_data"] and a["comparison"]["status"] == "insufficient_data"
    assert "Too few completed repairs" in a["comparison"]["message"] and s["flagged"] == []


def test_a_slower_neighbourhood_is_a_possible_difference_within_similar_damage(db):
    with session() as conn:
        _two_neighbourhoods(conn, n=12)
        _two_neighbourhoods(conn, slow_days=6, fast_days=6, n=12, category="Crack")  # no difference for cracks
        s = equity.summary(conn)
    a = next(x for x in s["neighbourhoods"] if x["neighbourhood"] == "Area A")
    assert a["comparison"]["status"] == "possible_difference" and s["flagged"] == ["Area A"]
    assert a["comparison"]["p_value"] < 0.05 and a["comparison"]["ratio"] >= 1.25
    pothole = next(x for x in s["similar_damage"] if x["category"] == "Pothole")
    crack = next(x for x in s["similar_damage"] if x["category"] == "Crack")
    assert next(c for c in pothole["neighbourhoods"] if c["neighbourhood"] == "Area A")["status"] == "possible_difference"
    assert all(c["status"] == "no_clear_difference" for c in crack["neighbourhoods"])
    assert a["holds_within_similar_damage"] is True
    assert a["context"]["pothole_share"] == 0.5 and "not proof" in s["caveat"]
    assert s["longest"][0]["neighbourhood"] == "Area A"


def test_mann_whitney_sanity():
    assert equity.mann_whitney_p([1, 2, 3, 4, 5] * 3, [1, 2, 3, 4, 5] * 3) == 1.0
    assert equity.mann_whitney_p([20, 21, 22, 23] * 3, [1, 2, 3, 4] * 3) < 0.001


def test_roadsense_reports_count_once_with_their_city_request(db):
    from roadsense.core import repair

    rep = report("rs1")
    rep2 = report("rs2", lat=LAT + 0.02)
    with session() as conn:
        for r in (rep, rep2):
            reports.set_status(conn, r["id"], "submitted", "test", service_request_id=f"sr{r['id']}")
            reports.set_status(conn, r["id"], "fixed", "test")
            repair.mark_awaiting(conn, r["id"], "test")
        city_sr(conn, "linked", 3)
        conn.execute("UPDATE external_requests SET report_id = ? WHERE id = 'city:linked'", (rep["id"],))
        recs, quality = equity.records(conn)
    kinds = sorted(r["record_kind"] for r in recs)
    assert kinds == ["city_sr", "roadsense"] and quality["linked_to_city_request"] == 1
    own = next(r for r in recs if r["record_kind"] == "roadsense")
    assert own["status"] == "completed" and own["repair_days"] is not None and own["category"] == "Pothole"


# ---------------- 24. the existing Cityworks/ArcGIS sync feeds the audit ----------------
def test_city_layer_sync_feeds_the_equity_audit(db):
    ms = lambda d: int(d.timestamp() * 1000)
    feature = lambda rid, days, h: {"type": "Feature", "geometry": {"type": "Point", "coordinates": [LON, LAT]},
                                    "properties": {"RequestId": rid, "Problem": "Pothole on road", "Status": "Closed",
                                                   "InitiatedDate": ms(T0 + timedelta(hours=h)),
                                                   "ClosedDate": ms(T0 + timedelta(hours=h, days=days)),
                                                   "Community": "Halifax Peninsula"}}
    body = {"type": "FeatureCollection", "features": [feature("SR-1", 7, 0), feature("SR-2", 9, 2)]}
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))
    with session() as conn:
        res = external.sync_city_requests(conn, layer_url="https://city.example/FeatureServer/0", client=client)
        again = external.sync_city_requests(conn, layer_url="https://city.example/FeatureServer/0", client=client)
        s = equity.summary(conn)
    assert res["added"] == 2 and again["added"] == 0 and again["updated"] == 2  # re-sync does not duplicate
    assert s["overall"]["completed"] == 2 and s["overall"]["repair"]["median"] == 8.0
    assert s["neighbourhoods"][0]["neighbourhood"] == "Halifax Peninsula"


# ---------------- API and scheduler ----------------
def test_risk_and_equity_api(edb):
    from roadsense.services import agent_api

    rep = report("x")
    with session() as conn:
        exposure.compute(conn, rep, OSM([node(9, 5, highway="bus_stop")]).client)
        city_sr(conn, 1, 5, raw={"Community": "Fairview"})
    c = TestClient(agent_api.app)
    assert c.get("/risk").status_code == 401
    listing = c.get("/risk", headers=AUTH).json()
    assert listing["summary"]["near_bus_stop"] == 1 and listing["reports"][0]["category"]
    assert c.get("/risk?category=extreme", headers=AUTH).status_code == 400
    detail = c.get(f"/reports/{rep['id']}", headers=AUTH).json()
    assert detail["risk"]["features"][0]["category"] == "bus_stop" and detail["risk"]["physical_score"] > 0
    eq = c.get("/equity?start=2026-01-01", headers=AUTH).json()
    assert eq["overall"]["requests"] == 1 and eq["neighbourhoods"][0]["neighbourhood"] == "Fairview"
    assert c.get("/equity?start=03-2026", headers=AUTH).status_code == 400
    assert len(c.get("/equity/records?neighbourhood=Fairview", headers=AUTH).json()) == 1
    viewer = {**AUTH, "X-RoadSense-User": "v@roadsense.test", "X-RoadSense-Role": "viewer"}
    assert c.post("/risk/run", headers=viewer).status_code == 403


def test_scheduler_queues_scoring_only_when_there_is_work(edb):
    from roadsense.services.jobs import Worker

    w = Worker()
    assert "score_exposure" not in w.schedule()
    report("x")
    assert "score_exposure" in Worker().schedule()


def test_rules_are_documented_and_hashed():
    r = exposure.rules()
    assert sum(r["weights"].values()) == pytest.approx(1.0)
    assert r["weights"] == {"physical": 0.5, "cyclist": 0.2, "pedestrian": 0.15, "transit": 0.1, "road_context": 0.05}
