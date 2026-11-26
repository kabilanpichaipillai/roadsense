"""The workflow over HTTP: roles, illegal moves, filters, photos, and the end-to-end walk of one report."""
from datetime import timedelta

import cv2
import numpy as np
import pytest
from conftest import add_event
from fastapi.testclient import TestClient

from roadsense.common.config import settings
from roadsense.common.db import now, row, session
from roadsense.core import ledger, open311, reports, workflow
from roadsense.services import agent_api

HRM = "Halifax Regional Municipality"
AUTH = {"Authorization": "Bearer test-token"}
api = TestClient(agent_api.app)
SOON = (workflow.today() + timedelta(days=5)).isoformat()
_n = {"i": 0}


def person(email, role, uid=None):
    h = {**AUTH, "X-RoadSense-User": email, "X-RoadSense-Role": role}
    return {**h, "X-RoadSense-User-Id": str(uid)} if uid is not None else h


def user(email, role) -> int:
    with session() as conn:
        conn.execute("INSERT INTO users (email, role, created_at) VALUES (?, ?, ?)", (email, role, now()))
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]


def new_report(lat=None) -> int:
    _n["i"] += 1
    eid = f"ev-api{_n['i']}"
    add_event(eid, lat or 44.60 + _n["i"] * 0.003, -63.58)
    with session() as conn:
        conn.execute("UPDATE events SET verified_confidence = 0.9 WHERE id = ?", (eid,))
        return reports.create_or_merge(conn, eid, "high", "x", HRM, 15, jurisdiction=HRM)["report"]["id"]


def payload_hash(rid):
    with session() as conn:
        r = row(conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone())
    return open311.payload_hash(open311.build_payload(r))


def jpeg(color=128, exif=False) -> bytes:
    data = cv2.imencode(".jpg", np.full((60, 80, 3), color, np.uint8))[1].tobytes()
    if exif:  # an APP1 Exif segment right after SOI, as a phone camera writes it (with a GPS tag inside)
        seg = b"Exif\x00\x00GPSLatitude=44.64"
        data = data[:2] + b"\xff\xe1" + (len(seg) + 2).to_bytes(2, "big") + seg + data[2:]
    return data


@pytest.fixture
def staff(db):
    return {"rev": person("rev@x.test", "reviewer"), "insp_id": (i := user("insp@x.test", "inspector")),
            "insp": person("insp@x.test", "inspector", i), "other": person("o@x.test", "inspector", user("o@x.test",
                                                                                                         "inspector")),
            "driver": person("d@x.test", "driver"), "viewer": person("v@x.test", "viewer")}


def test_roles_and_illegal_moves_over_http(staff):
    rid = new_report()
    h = payload_hash(rid)
    for who in ("driver", "viewer", "insp"):
        assert api.post(f"/reports/{rid}/approve", json={"payload_hash": h}, headers=staff[who]).status_code == 403
    assert api.post(f"/reports/{rid}/approve", json={"payload_hash": "stale"}, headers=staff["rev"]).status_code == 409
    r = api.post(f"/reports/{rid}/approve", json={"payload_hash": h}, headers=staff["rev"])
    assert r.status_code == 200 and r.json()["status"] == "awaiting_inspection"
    assert r.json()["allowed_actions"] == ["assign"] and r.json()["status_label"] == "Awaiting inspection"
    again = api.post(f"/reports/{rid}/approve", json={"payload_hash": h}, headers=staff["rev"])
    assert again.status_code == 409 and "awaiting inspection" in again.json()["detail"]
    assert api.post(f"/reports/{rid}/reject", headers=staff["rev"]).status_code == 409

    assert api.post(f"/reports/{rid}/assign", json={"inspector_id": staff["insp_id"]},
                    headers=staff["insp"]).status_code == 403
    r = api.post(f"/reports/{rid}/assign", json={"inspector_id": staff["insp_id"]}, headers=staff["rev"])
    assert r.json()["inspector_id"] == staff["insp_id"]

    form = {"outcome": "repair_needed", "repair_eta": SOON, "severity_band": "S3"}
    assert api.post(f"/reports/{rid}/inspection", data=form, headers=staff["other"]).status_code == 403
    assert api.post(f"/reports/{rid}/inspection", data=form, headers=staff["viewer"]).status_code == 403
    assert api.post(f"/reports/{rid}/inspection", data={**form, "repair_eta": ""},
                    headers=staff["insp"]).status_code == 422
    assert api.post(f"/reports/{rid}/inspection", data={**form, "outcome": "maybe"},
                    headers=staff["insp"]).status_code == 422
    r = api.post(f"/reports/{rid}/inspection", data=form, headers=staff["insp"])
    assert r.status_code == 200 and r.json()["status"] == "repair_needed" and r.json()["severity_band"] == "S3"
    assert set(r.json()["allowed_actions"]) == {"repaired", "reschedule"}

    later = (workflow.today() + timedelta(days=9)).isoformat()
    assert api.patch(f"/reports/{rid}/eta", json={"repair_eta": later}, headers=staff["insp"]).json()["repair_eta"] \
        == later
    r = api.post(f"/reports/{rid}/repaired", data={"notes": "hot mix"}, headers=staff["insp"])
    assert r.json()["status"] == "repaired" and r.json()["repair_notes"] == "hot mix"
    assert api.post(f"/reports/{rid}/reopen", json={"reason": ""}, headers=staff["insp"]).status_code == 422
    r = api.post(f"/reports/{rid}/reopen", json={"reason": "came back"}, headers=staff["insp"])
    assert r.json()["status"] == "reopened" and r.json()["reopen_count"] == 1


def test_report_lists_filter_by_inspector_status_and_overdue(staff):
    mine, theirs, late = new_report(), new_report(), new_report()
    past = (workflow.today() - timedelta(days=10)).isoformat() + "T12:00:00+00:00"
    with session() as conn:
        for rid in (mine, theirs, late):
            workflow.transition(conn, rid, "approve", "rev", "reviewer", at=past)
        workflow.transition(conn, mine, "assign", "rev", "reviewer", inspector_id=staff["insp_id"])
        workflow.transition(conn, late, "assign", "rev", "reviewer", inspector_id=staff["insp_id"], at=past)
        workflow.transition(conn, late, "repair_needed", "rev", "reviewer", at=past,
                            repair_eta=(workflow.today() - timedelta(days=3)).isoformat())
    me = api.get("/reports", params={"inspector_id": "me"}, headers=staff["insp"]).json()
    assert {r["id"] for r in me} == {mine, late}
    assert api.get("/reports", params={"inspector_id": "me"}, headers=staff["rev"]).status_code == 400
    overdue = api.get("/reports", params={"overdue": "true"}, headers=staff["rev"]).json()
    assert [r["id"] for r in overdue] == [late] and overdue[0]["overdue"] is True
    waiting = api.get("/reports", params={"status": "awaiting_inspection"}, headers=staff["rev"]).json()
    assert [r["id"] for r in waiting] == [theirs]
    assert api.get("/reports", params={"status": "submitted"}, headers=staff["rev"]).status_code == 400
    assert api.get("/stats", headers=AUTH).json()["overdue"] == 1
    d = api.get(f"/reports/{mine}", headers=staff["insp"]).json()
    assert d["report"]["inspector"]["email"] == "insp@x.test" and "approved_payload" not in d["report"]


def test_inspection_photo_is_checked_reencoded_and_served(staff):
    rid = new_report()
    with session() as conn:
        workflow.transition(conn, rid, "approve", "rev", "reviewer")
        workflow.transition(conn, rid, "assign", "rev", "reviewer", inspector_id=staff["insp_id"])
    url = f"/reports/{rid}/inspection"
    form = {"outcome": "no_repair_needed", "notes": "surface only"}
    before = set(settings.media_dir.glob("photo-*.jpg"))
    bad = api.post(url, data=form, files={"photo": ("x.txt", b"hello", "text/plain")}, headers=staff["insp"])
    assert bad.status_code == 415
    big = api.post(url, data=form, files={"photo": ("x.jpg", b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024), "image/jpeg")},
                   headers=staff["insp"])
    assert big.status_code == 413
    gif = api.post(url, data=form, files={"photo": ("x.gif", b"GIF89a....", "image/gif")}, headers=staff["insp"])
    assert gif.status_code == 415
    assert set(settings.media_dir.glob("photo-*.jpg")) == before  # nothing kept from refused uploads
    ok = api.post(url, data=form, files={"photo": ("site.jpg", jpeg(exif=True), "image/jpeg")}, headers=staff["insp"])
    assert ok.status_code == 200
    r = ok.json()
    kind, _, pid = r["inspection_photo"].partition(":")
    assert r["status"] == "closed" and r["closed_reason"] == "no_repair_needed" and kind == "inspection"
    stored = (settings.media_dir / f"photo-{pid}.jpg").read_bytes()
    assert stored.startswith(b"\xff\xd8") and b"Exif" not in stored and b"GPSLatitude" not in stored
    assert api.get(f"/photos/{pid}.jpg").status_code == 401
    assert api.get(f"/photos/{pid}.jpg", headers=AUTH).content == stored


def test_png_photo_on_repair_and_orphans_are_dropped(staff):
    rid = new_report()
    png = cv2.imencode(".png", np.full((40, 40, 3), 200, np.uint8))[1].tobytes()
    before = set(settings.media_dir.glob("photo-*.jpg"))
    early = api.post(f"/reports/{rid}/repaired", files={"photo": ("p.png", png, "image/png")}, headers=staff["rev"])
    assert early.status_code == 409 and set(settings.media_dir.glob("photo-*.jpg")) == before
    with session() as conn:
        workflow.transition(conn, rid, "approve", "rev", "reviewer")
        workflow.transition(conn, rid, "assign", "rev", "reviewer", inspector_id=staff["insp_id"])
        workflow.transition(conn, rid, "repair_needed", "rev", "reviewer", repair_eta=SOON)
    r = api.post(f"/reports/{rid}/repaired", data={"notes": "done"}, files={"photo": ("p.png", png, "image/png")},
                 headers=staff["rev"]).json()
    assert r["repair_photo"].startswith("repair:") and r["verification_status"] == "awaiting"


# ---------------- the web app: roles, the assign picker, photos ----------------
def signed_in(email=None, password=None):
    from roadsense.web import auth
    from roadsense.web.app import app

    auth.bootstrap_admin()
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email or settings.admin_email,
                                        "password": password or settings.admin_password})
    return c, r.json()["csrf"]


def test_web_roles_for_inspectors(db):
    admin, csrf = signed_in()
    for email, role in (("insp@roadsense.test", "inspector"), ("rev@roadsense.test", "reviewer")):
        assert admin.post("/api/users", headers={"X-CSRF-Token": csrf},
                          json={"email": email, "role": role, "password": "Inspector-Passw0rd1"}).status_code == 200
    insp, icsrf = signed_in("insp@roadsense.test", "Inspector-Passw0rd1")
    rev, _ = signed_in("rev@roadsense.test", "Inspector-Passw0rd1")
    assert "inspect" in insp.get("/api/me").json()["permissions"] and \
        "review" not in insp.get("/api/me").json()["permissions"]
    assert insp.post("/api/reports/1/approve", headers={"X-CSRF-Token": icsrf},
                     json={"payload_hash": "x"}).status_code == 403
    assert insp.post("/api/reports/1/assign", headers={"X-CSRF-Token": icsrf},
                     json={"inspector_id": 1}).status_code == 403
    picker = rev.get("/api/users", params={"role": "inspector"}).json()
    assert [u["email"] for u in picker] == ["insp@roadsense.test"] and set(picker[0]) == {"id", "email", "name", "role"}
    assert rev.get("/api/users").status_code == 403  # the full list stays admin-only
    assert insp.get("/api/users", params={"role": "inspector"}).status_code == 403
    assert TestClient(admin.app).get("/api/media/photo/abc.jpg").status_code == 401
    assert admin.get("/api/media/photo/bad$id.jpg").status_code == 400
    assert admin.get("/api/media/photo/abc123.jpg").status_code == 404


def test_sso_accepts_the_inspector_role():
    from roadsense.web import auth

    assert "inspector" in auth.ROLES and auth.allowed({"role": "inspector"}, "inspect")
    assert not auth.allowed({"role": "inspector"}, "review") and not auth.allowed({"role": "driver"}, "inspect")


# ---------------- end to end ----------------
def test_one_report_walks_the_whole_workflow_with_an_intact_ledger(staff):
    """review → assign → inspection (repair needed, ETA) → repaired → re-detection reopens → repair needed →
    repaired → verified by two clean passes; every step in the timeline; the integrity check stays green."""
    import json

    rid = new_report(lat=44.643)
    with session() as conn:
        lat, lon = conn.execute("SELECT lat, lon FROM reports WHERE id = ?", (rid,)).fetchone()
    rev, insp = staff["rev"], staff["insp"]
    assert api.post(f"/reports/{rid}/approve", json={"payload_hash": payload_hash(rid)}, headers=rev).status_code == 200
    api.post(f"/reports/{rid}/assign", json={"inspector_id": staff["insp_id"]}, headers=rev)
    api.post(f"/reports/{rid}/inspection", data={"outcome": "repair_needed", "repair_eta": SOON}, headers=insp)
    assert api.post(f"/reports/{rid}/repaired", data={"notes": "patched"}, headers=insp).json()["status"] == "repaired"

    add_event("ev-e2e-again", lat + 3 / 111_320, lon, trip="trip-later")  # a later trip sees the pothole again
    with session() as conn:
        conn.execute("UPDATE events SET verified_confidence = 0.9 WHERE id = 'ev-e2e-again'")
        out = reports.create_or_merge(conn, "ev-e2e-again", "high", "x", HRM, 15, jurisdiction=HRM,
                                      actor="agent:triage")
    assert out["action"] == "reopened" and out["report"]["status"] == "reopened"

    api.post(f"/reports/{rid}/inspection", data={"outcome": "repair_needed", "repair_eta": SOON}, headers=insp)
    assert api.get(f"/reports/{rid}", headers=insp).json()["report"]["status"] == "repair_needed"
    assert api.post(f"/reports/{rid}/repaired", headers=insp).json()["status"] == "repaired"

    from roadsense.core import repair

    for trip, south in (("trip-p1", 10), ("trip-p2", 8)):
        with session() as conn:
            conn.execute("INSERT INTO road_passes (id, trip_id, lat, lon, heading, captured_at, image_path, box, "
                         "sharpness, brightness, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (f"p-{trip}", trip, lat - south / 111_320, lon, 0.0, now(), "x.jpg", json.dumps([0, 0, 9, 9]),
                          150.0, 120.0, now()))
            repair.check_passes(conn, trip)
    d = api.get(f"/reports/{rid}", headers=rev).json()
    assert d["report"]["status_label"] == "Repaired (verified)" and d["report"]["reopen_count"] == 1
    steps = [a["action"].removeprefix("ledger_workflow_") for a in d["trail"] if a["action"].startswith("ledger_workflow_")]
    assert steps == ["approve", "assign", "repair_needed", "repaired", "reopen", "repair_needed", "repaired"]
    reopen = next(a for a in d["trail"] if a["action"] == "ledger_workflow_reopen")
    assert reopen["actor"] == "verify:repair"  # RoadSense reopened it, not the agent that triaged the sighting
    whole = api.get("/service-standards/integrity", headers=rev).json()
    mine = api.get("/service-standards/integrity", params={"report_id": rid}, headers=rev).json()
    assert whole["ok"] and mine["ok"] and mine["entries"] >= 12
    with session() as conn:
        assert ledger.verify(conn)["ok"]
        c = conn.execute("SELECT stage, reopened_count, completed_at FROM service_clocks WHERE report_id = ?",
                         (rid,)).fetchone()
    assert tuple(c)[:2] == ("closed", 1) and c[2]
