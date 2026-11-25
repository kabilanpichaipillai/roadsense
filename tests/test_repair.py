"""Independent repair verification: re-observing reports the authority closed."""
import json

import cv2
import numpy as np
import pytest
from conftest import add_event
from fastapi.testclient import TestClient

from roadsense.common.config import settings
from roadsense.common.db import now, row, session
from roadsense.core import delivery, repair, reports
from roadsense.mcp_servers import municipality_server

HRM = "Halifax Regional Municipality"
AUTH = {"Authorization": "Bearer test-token"}
LAT, LON = 44.643, -63.58
M = 1 / 111_320  # one metre of latitude


def closed_report(event_id="ev-orig1", lat=LAT, lon=LON, code="D40", road=None) -> int:
    """A report filed from a first trip, sent, then closed by the authority's status feed (follow-up)."""
    add_event(event_id, lat, lon, code=code, trip="trip-orig")
    with session() as conn:
        conn.execute("UPDATE events SET verified_confidence = 0.85 WHERE id = ?", (event_id,))
        rid = reports.create_or_merge(conn, event_id, "high", "x", HRM, 15, jurisdiction=HRM,
                                      road_name=road)["report"]["id"]
        reports.set_status(conn, rid, "submitted", "test", service_request_id=f"sr-{rid}", channel="open311")
        reports.set_status(conn, rid, "fixed", "mcp:municipality")
    return rid


def sighting(event_id, trip, lat=LAT, lon=LON, code="D40", verified=0.8, conf=0.8, road=None) -> dict:
    add_event(event_id, lat, lon, code=code, trip=trip, conf=conf)
    with session() as conn:
        conn.execute("UPDATE events SET verified_confidence = ? WHERE id = ?", (verified, event_id))
        return reports.create_or_merge(conn, event_id, "high", "x", HRM, 15, jurisdiction=HRM, road_name=road)


def add_pass(pass_id, trip, metres_south=10.0, heading=0.0, sharpness=150.0, brightness=120.0, lat=LAT, lon=LON):
    """A frame the edge kept while driving north towards the spot."""
    with session() as conn:
        conn.execute("INSERT INTO road_passes (id, trip_id, lat, lon, heading, captured_at, image_path, box, sharpness,"
                     " brightness, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pass_id, trip, lat - metres_south * M, lon, heading, now(), f"pass-{pass_id}.jpg",
                      json.dumps([0, 0, 10, 10]), sharpness, brightness, now()))


def check(trip):
    with session() as conn:
        return repair.check_passes(conn, trip)


def get(rid):
    with session() as conn:
        return row(conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone())


def hist(rid):
    with session() as conn:
        return repair.history(conn, rid)


# 1
def test_authority_closure_keeps_its_status_and_awaits_verification(db):
    rid = closed_report()
    r = get(rid)
    assert r["status"] == "fixed" and r["verification_status"] == "awaiting" and r["closed_at"]
    assert repair.lifecycle(r) == "Closed"
    with session() as conn:
        assert [w["report_id"] for w in repair.watchlist(conn)] == [rid]
        actions = [a for (a,) in conn.execute("SELECT action FROM audit WHERE subject = ?", (f"report:{rid}",))]
    assert "status_fixed" in actions and "repair_awaiting_verification" in actions


# 2 + 3
def test_same_pothole_seen_again_reopens_the_report(db):
    rid = closed_report()
    out = sighting("ev-again1", "trip-2", lat=LAT + 4 * M)  # ~4 m away: GPS drift between trips
    assert out["action"] == "reopened" and out["report"]["id"] == rid
    r = get(rid)
    assert r["verification_status"] == "reopened" and r["reopen_event_id"] == "ev-again1"
    assert "still there" in r["reopen_reason"] and r["status"] == "fixed"  # the authority's status is kept
    assert repair.lifecycle(r) == "Reopened"
    (h,) = hist(rid)
    assert h["result"] == "not_verified" and h["observed_class"] == "D40" and 3 < h["distance_m"] < 6
    assert h["original_image"] == "event:ev-orig1" and h["followup_image"] == "event:ev-again1"
    assert len(h["evidence_hash"]) == 64
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1  # no duplicate report was created
        linked = conn.execute("SELECT report_id, status FROM events WHERE id = 'ev-again1'").fetchone()
        actions = {a for (a,) in conn.execute("SELECT action FROM audit WHERE subject = ?", (f"report:{rid}",))}
    assert tuple(linked) == (rid, "processed")
    assert {"repair_verification_started", "repair_verification_completed", "repair_not_verified",
            "report_reopened"} <= actions


# 4 + 5
def test_repair_is_verified_only_after_enough_independent_clean_passes(db):
    rid = closed_report()
    add_pass("p-trip2", "trip-2")
    first = check("trip-2")
    assert first["verified"] == [] and get(rid)["verification_status"] == "awaiting"
    assert hist(rid)[0]["result"] == "inconclusive" and "1 of 2" in hist(rid)[0]["reason"]

    add_pass("p-trip2b", "trip-2", metres_south=12)  # the same trip again is not independent evidence
    check("trip-2")
    assert get(rid)["verification_status"] == "awaiting"

    add_pass("p-trip3", "trip-3", metres_south=8)
    assert check("trip-3")["verified"] == [rid]
    r = get(rid)
    assert r["verification_status"] == "verified" and r["status"] == "fixed"
    assert hist(rid)[-1]["result"] == "verified" and hist(rid)[-1]["followup_image"] == "pass:p-trip3"


# 6
def test_low_confidence_redetection_is_inconclusive(db):
    rid = closed_report()
    out = sighting("ev-weak1", "trip-2", verified=0.3, conf=0.3)
    assert out["action"] == "reobserved"
    assert get(rid)["verification_status"] == "awaiting"
    (h,) = hist(rid)
    assert h["result"] == "inconclusive" and "below" in h["reason"]
    # and a clean-looking frame from that same trip does not count: the detector did see something there
    add_pass("p-weak", "trip-2")
    check("trip-2")
    assert hist(rid)[-1]["result"] == "inconclusive" and hist(rid)[-1]["clean"] == 0


def test_poor_frames_are_inconclusive_not_verified(db):
    rid = closed_report()
    add_pass("p-blur", "trip-2", sharpness=10)
    add_pass("p-dark", "trip-3", brightness=8)
    add_pass("p-away", "trip-4", heading=180)  # driving away: the spot is behind the camera
    for t in ("trip-2", "trip-3", "trip-4"):
        check(t)
    reasons = [h["reason"] for h in hist(rid)]
    assert [h["result"] for h in hist(rid)] == ["inconclusive"] * 3 and all(h["clean"] == 0 for h in hist(rid))
    assert "blurred" in reasons[0] and "dark" in reasons[1] and "faced" in reasons[2]
    assert get(rid)["verification_status"] == "awaiting"


# 7
def test_observations_outside_the_tolerance_are_ignored(db):
    rid = closed_report()
    out = sighting("ev-far1", "trip-2", lat=LAT + 40 * M)  # 40 m away: another defect
    assert out["action"] == "created" and out["report"]["id"] != rid
    add_pass("p-far", "trip-2", metres_south=60)  # too far to see the road surface
    check("trip-2")
    assert hist(rid) == [] and get(rid)["verification_status"] == "awaiting"


# 8
def test_a_different_damage_class_is_not_matched(db):
    rid = closed_report(code="D40")
    out = sighting("ev-crack1", "trip-2", code="D00")
    assert out["action"] == "created" and out["report"]["id"] != rid
    assert hist(rid) == [] and get(rid)["verification_status"] == "awaiting"


def test_a_different_road_is_not_matched(db):
    rid = closed_report(road="Spring Garden Road")
    out = sighting("ev-cross1", "trip-2", lat=LAT + 5 * M, road="Queen Street")  # the intersecting street
    assert out["action"] == "created" and hist(rid) == []


# 9
def test_nearby_reports_are_each_judged_on_their_own(db):
    near = closed_report("ev-orig-a", lat=LAT, code="D40")
    other = closed_report("ev-orig-b", lat=LAT + 8 * M, code="D20")  # an alligator crack 8 m further on
    assert near != other
    # the edge says pothole, the verifier says alligator crack: both closed reports are candidates
    add_event("ev-again-a", LAT + 2 * M, LON, code="D40", trip="trip-2")
    with session() as conn:
        conn.execute("UPDATE events SET verified_confidence = 0.8, verifier_class = 'D20' WHERE id = 'ev-again-a'")
        out = reports.create_or_merge(conn, "ev-again-a", "high", "x", HRM, 15, jurisdiction=HRM)
    assert out["action"] == "reopened" and out["report"]["id"] == near  # the nearest defect only
    assert get(other)["verification_status"] == "awaiting" and hist(other) == []
    add_pass("p-both", "trip-2", metres_south=10)
    check("trip-2")  # the frame sees both spots; the reopened one is no longer awaiting
    assert [h["report_id"] for h in hist(other)] == [other] and len(hist(near)) == 1


# 10
def test_repeated_observations_do_not_duplicate_history(db):
    rid = closed_report()
    sighting("ev-again1", "trip-2")
    with session() as conn:  # re-processing the same event (e.g. "Process now")
        again = reports.create_or_merge(conn, "ev-again1", "high", "x", HRM, 15, jurisdiction=HRM)
    assert again["action"] == "already_linked"
    rid2 = closed_report("ev-orig2", lat=LAT + 100 * M)
    add_pass("p-r2", "trip-3", lat=LAT + 100 * M)
    check("trip-3")
    check("trip-3")
    assert len(hist(rid)) == 1 and len(hist(rid2)) == 1
    with session() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE repair_verifications SET result = 'verified'")


# 11
def test_original_photo_and_history_are_preserved(db):
    rid = closed_report()
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    photo = settings.media_dir / "ev-orig1.jpg"
    photo.write_bytes(b"\xff\xd8original")
    before = get(rid)
    sighting("ev-again1", "trip-2")
    after = get(rid)
    for k in ("best_event_id", "class_code", "lat", "lon", "status", "closed_at", "service_request_id",
              "created_at", "trip_id"):
        assert after[k] == before[k], k
    assert photo.read_bytes() == b"\xff\xd8original"
    with session() as conn:
        orig = conn.execute("SELECT report_id, status FROM events WHERE id = 'ev-orig1'").fetchone()
        closure = conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'status_fixed' AND subject = ?",
                               (f"report:{rid}",)).fetchone()[0]
        s = repair.summary(conn, after)
    assert tuple(orig) == (rid, "processed") and closure == 1
    assert s["original"]["event_id"] == "ev-orig1" and s["latest"]["observation_id"] == "ev-again1"
    assert s["authority_status"] == "fixed" and s["state"] == "reopened"


def test_observations_before_the_closure_do_not_count(db):
    rid = closed_report()
    with session() as conn:
        conn.execute("UPDATE reports SET closed_at = '2999-01-01T00:00:00+00:00' WHERE id = ?", (rid,))
    out = sighting("ev-early", "trip-2")
    add_pass("p-early", "trip-2")
    check("trip-2")
    assert out["action"] != "reopened" and hist(rid) == []


def test_metrics_do_not_count_unobserved_repairs_as_failures(db):
    verified = closed_report("ev-m1", lat=LAT)
    reopened = closed_report("ev-m2", lat=LAT + 200 * M)
    closed_report("ev-m3", lat=LAT + 400 * M)          # nobody drove past it
    inconclusive = closed_report("ev-m4", lat=LAT + 600 * M)
    for t in ("t-a", "t-b"):
        add_pass(f"p-{t}", t)
        check(t)
    sighting("ev-m2-again", "t-c", lat=LAT + 200 * M)
    add_pass("p-blurry", "t-d", lat=LAT + 600 * M, sharpness=1)
    check("t-d")
    with session() as conn:
        m = repair.metrics(conn)
    assert get(verified)["verification_status"] == "verified" and get(reopened)["verification_status"] == "reopened"
    assert get(inconclusive)["verification_status"] == "awaiting"
    assert m["closed_by_authority"] == 4 and m["verified"] == 1 and m["reopened"] == 1 and m["eligible"] == 2
    assert m["verification_rate"] == 50.0  # 1 / (1 verified + 1 reopened), not 1 / 4
    assert m["awaiting_verification"] == 2 and m["not_yet_reobserved"] == 1 and m["awaiting_inconclusive"] == 1
    assert m["inconclusive_verifications"] == 2  # trip t-a's first clean pass, and the blurry frame


# 12
def test_follow_up_status_feed_still_closes_reports(db, monkeypatch):
    add_event("ev-fu1", LAT, LON, trip="trip-orig")
    with session() as conn:
        rid = reports.create_or_merge(conn, "ev-fu1", "high", "x", HRM, 15, jurisdiction=HRM)["report"]["id"]
        reports.set_status(conn, rid, "submitted", "test", service_request_id="77", channel="open311")
    monkeypatch.setattr(delivery, "status", lambda *a, **k: {"status": "open"})
    assert municipality_server.request_status(rid)["fixed"] is False and get(rid)["verification_status"] is None
    monkeypatch.setattr(delivery, "status", lambda *a, **k: {"status": "closed", "status_notes": "patched"})
    out = municipality_server.request_status(rid)
    assert out["fixed"] and out["newly_fixed"] and out["city_status"] == "closed"
    assert get(rid)["status"] == "fixed" and get(rid)["verification_status"] == "awaiting"
    again = municipality_server.request_status(rid)
    assert again["fixed"] and not again["newly_fixed"]


def test_pass_upload_api_and_report_detail(db):
    from roadsense.services import agent_api

    rid = closed_report()
    c = TestClient(agent_api.app)
    watch = c.get("/verification/watchlist", headers=AUTH).json()
    assert watch == [{"report_id": rid, "lat": pytest.approx(LAT), "lon": pytest.approx(LON), "class_code": "D40"}]
    img = cv2.imencode(".jpg", np.full((80, 80, 3), 128, np.uint8))[1].tobytes()
    p = {"id": "trip-9-r1", "trip_id": "trip-9", "report_id": rid, "lat": LAT - 10 * M, "lon": LON, "heading": 0,
         "captured_at": now(), "box": [20, 20, 60, 60], "sharpness": 150, "brightness": 120}
    r = c.post("/passes", data={"road_pass": json.dumps(p)}, files={"image": ("p.jpg", img, "image/jpeg")},
               headers=AUTH)
    assert r.json() == {"accepted": "trip-9-r1"}
    dup = c.post("/passes", data={"road_pass": json.dumps(p)}, files={"image": ("p.jpg", img, "image/jpeg")},
                 headers=AUTH)
    assert dup.json()["duplicate"] is True
    assert c.post("/passes", data={"road_pass": json.dumps({**p, "lat": 999})},
                  files={"image": ("p.jpg", img, "image/jpeg")}, headers=AUTH).status_code == 422
    assert c.get("/passes/trip-9-r1.jpg").status_code == 401  # never public, unlike the authority's evidence link
    assert c.get("/passes/trip-9-r1.jpg", headers=AUTH).status_code == 200
    check("trip-9")
    d = c.get(f"/reports/{rid}", headers=AUTH).json()
    v = d["verification"]
    assert d["report"]["lifecycle"] == "Closed" and v["state"] == "awaiting" and v["latest"]["image"] == "pass:trip-9-r1"
    assert v["original"]["image"] == "event:ev-orig1" and len(v["history"]) == 1
    assert c.get("/stats", headers=AUTH).json()["repair_verification"]["awaiting_verification"] == 1
    assert {x["lifecycle"] for x in c.get("/reports", headers=AUTH).json()} == {"Closed"}


def test_retention_clears_pass_positions(db):
    from roadsense.core import privacy

    add_pass("p-old", "trip-old")
    with session() as conn:
        conn.execute("UPDATE road_passes SET created_at = '2020-01-01T00:00:00+00:00'")
        privacy.purge(conn)
        assert tuple(conn.execute("SELECT lat, lon, heading FROM road_passes").fetchone()) == (None, None, None)


def test_edge_keeps_one_frame_per_watched_spot(tmp_path):
    from datetime import datetime, timezone

    from roadsense.edge.reobserve import PassCollector

    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)  # textured: sharp
    flat = np.full((720, 1280, 3), 120, np.uint8)                  # featureless: blurred
    c = PassCollector([{"report_id": 7, "lat": LAT, "lon": LON, "class_code": "D40"},
                       {"report_id": 8, "lat": LAT + 500 * M, "lon": LON, "class_code": "D40"}])
    when = datetime.now(timezone.utc)
    c.update(flat, LAT - 15 * M, LON, 0.0, when)
    c.update(frame, LAT - 10 * M, LON, 0.0, when)
    c.update(frame, LAT - 10 * M, LON, 180.0, when)  # facing away: ignored
    (p,) = c.flush("trip-edge", tmp_path)
    assert p.report_id == 7 and p.sharpness > 1000 and p.heading == 0.0
    assert (tmp_path / f"pass-{p.id}.jpg").is_file() and (tmp_path / f"pass-{p.id}.json").is_file()


# authority reopens a closed request, then closes it again
def test_authority_reopen_then_reclosure_starts_a_fresh_check(db, monkeypatch):
    import time

    rid = closed_report()
    add_pass("p-cycle1", "trip-2")
    check("trip-2")  # one clean pass in the first cycle (1 of 2)
    first_closure = get(rid)["closed_at"]
    replies = iter([{"status": "open", "status_notes": "Reopened: patch failed"},
                    {"status": "closed", "status_notes": "Repaired again"}])
    monkeypatch.setattr(delivery, "status", lambda *a, **k: next(replies))
    assert municipality_server.request_status(rid)["reopened"]
    r = get(rid)
    assert r["status"] == "submitted" and r["verification_status"] is None and r["closed_at"] is None
    assert repair.lifecycle(r) == "In Progress"
    with session() as conn:
        assert repair.watchlist(conn) == []  # not watched while the authority is working on it again
        reset = conn.execute("SELECT detail FROM audit WHERE action = 'repair_verification_reset'").fetchone()
    assert json.loads(reset[0])["previous"]["closed_at"] == first_closure  # the old cycle stays on record

    time.sleep(1.1)  # closure times have one-second resolution
    assert municipality_server.request_status(rid)["newly_fixed"]
    r = get(rid)
    assert r["verification_status"] == "awaiting" and r["closed_at"] > first_closure
    add_pass("p-cycle2", "trip-3")
    assert check("trip-3")["verified"] == []  # the first cycle's pass no longer counts towards this closure
    assert "1 of 2" in hist(rid)[-1]["reason"] and len(hist(rid)) == 2  # history of both cycles is kept
    add_pass("p-cycle2b", "trip-4", metres_south=8)
    assert check("trip-4")["verified"] == [rid]


def test_authority_reopen_clears_a_roadsense_reopening(db):
    rid = closed_report()
    sighting("ev-again-r", "trip-2", lat=LAT + 3 * M)
    assert get(rid)["verification_status"] == "reopened"
    with session() as conn:
        reports.set_status(conn, rid, "submitted", "mcp:municipality")  # the authority acts on it
        prev = json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'repair_verification_reset'")
                          .fetchone()[0])["previous"]
        reports.set_status(conn, rid, "fixed", "mcp:municipality")
    assert prev["verification_status"] == "reopened" and prev["reopen_event_id"] == "ev-again-r"
    r = get(rid)
    assert r["verification_status"] == "awaiting" and r["reopen_event_id"] is None and repair.lifecycle(r) == "Closed"
