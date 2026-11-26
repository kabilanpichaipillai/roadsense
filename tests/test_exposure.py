"""Exposure-weighted risk (OSM infrastructure near a defect).

OpenStreetMap (Overpass) is never called: tests hand the code an httpx.MockTransport.
"""
import json

import httpx
import pytest
from conftest import add_event
from fastapi.testclient import TestClient

from roadsense.common.db import session
from roadsense.core import exposure, reports

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


# ---------------- API and scheduler ----------------
def test_risk_api(edb):
    from roadsense.services import agent_api

    rep = report("x")
    with session() as conn:
        exposure.compute(conn, rep, OSM([node(9, 5, highway="bus_stop")]).client)
    c = TestClient(agent_api.app)
    assert c.get("/risk").status_code == 401
    listing = c.get("/risk", headers=AUTH).json()
    assert listing["summary"]["near_bus_stop"] == 1 and listing["reports"][0]["category"]
    assert c.get("/risk?category=extreme", headers=AUTH).status_code == 400
    detail = c.get(f"/reports/{rep['id']}", headers=AUTH).json()
    assert detail["risk"]["features"][0]["category"] == "bus_stop" and detail["risk"]["physical_score"] > 0
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
