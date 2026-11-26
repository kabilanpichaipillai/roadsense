"""Service-standard tracking and the duty-of-care ledger: deadlines, statuses, authority updates and integrity."""
import json
import sqlite3
from datetime import date, datetime, timezone

import pytest
from conftest import add_event
from fastapi.testclient import TestClient

from roadsense.common.db import row, session
from roadsense.core import delivery, ledger, open311, reports, workflow
from roadsense.core import service_standards as ss
from roadsense.core.business_days import BUSINESS, CALENDAR, Calendar
from roadsense.mcp_servers import municipality_server

HRM, NSPW = "Halifax Regional Municipality", "Nova Scotia Public Works"
AUTH = {"Authorization": "Bearer test-token"}
MON = "2026-11-23T14:00:00+00:00"  # Monday 23 Nov 2026, 10:00 in Halifax (AST, UTC-4)
_n = {"i": 0}


def filed(code="D40", band="S4", jurisdiction=HRM, at=MON, road_class=None, start=True):
    """A report approved at `at` and referred to its authority (each one far enough from the others not to merge).
    start=False: approved only, with no clock (as before tracking existed)."""
    _n["i"] += 1
    event_id = f"ev-ss{_n['i']}"
    add_event(event_id, 44.60 + _n["i"] * 0.002, -63.58, code=code)
    with session() as conn:
        r = reports.create_or_merge(conn, event_id, "high", "x", jurisdiction, 15, jurisdiction=jurisdiction,
                                    band=band, road_class=road_class)["report"]
        if not start:  # an old database: approved before tracking existed (no workflow step, no clock)
            p = open311.build_payload(r)
            conn.execute("UPDATE reports SET status = 'awaiting_inspection', approved_at = ?, approved_payload = ?, "
                         "approved_payload_hash = ? WHERE id = ?",
                         (at, json.dumps(p, sort_keys=True), open311.payload_hash(p), r["id"]))
            return row(conn.execute("SELECT * FROM reports WHERE id = ?", (r["id"],)).fetchone())
        r = workflow.transition(conn, r["id"], "approve", "reviewer@roadsense.test", "reviewer", at=at)
        r = workflow.transition(conn, r["id"], "refer", "mcp:municipality", "system", at=at,
                                service_request_id=str(r["id"]), channel="open311")
        return ss.start_clock(conn, r, "mcp:municipality", submitted_at=at)


def approve_rules(authority=HRM):
    with session() as conn:
        for rule in ss.list_rules(conn):
            if rule["authority"] == authority:
                ss.update_rule(conn, rule["id"], {"verification": "approved"}, "admin@roadsense.test")


def clock(report_id):
    with session() as conn:
        return ss.get_clock(conn, report_id)


def status(report_id, at):
    return ss.compliance(clock(report_id), at)["status"]


def ledger_types(report_id):
    """The service-clock entries of a report's ledger (the workflow's own entries are in the same chain)."""
    with session() as conn:
        return [e["event_type"] for e in ledger.entries(conn, report_id) if not e["event_type"].startswith("workflow_")]


# ---------------- business-day calculation ----------------
def test_business_days_skip_weekends():
    cal = Calendar("America/Halifax")
    # Mon 23 Nov + 7 business days = Wed 2 Dec, end of day in Halifax (23:59:59 AST = 03:59:59 UTC next day)
    assert cal.deadline(MON, 7, BUSINESS).isoformat() == "2026-12-03T03:59:59+00:00"
    # filed on a Saturday: counting starts Monday; filed Friday evening: Monday is day 1
    assert cal.local_date(cal.deadline("2026-11-28T15:00:00+00:00", 1, BUSINESS)) == date(2026, 11, 30)
    assert cal.local_date(cal.deadline("2026-11-28T01:30:00+00:00", 1, BUSINESS)) == date(2026, 11, 30)  # Fri 21:30
    assert cal.local_date(cal.deadline(MON, 0, BUSINESS)) == date(2026, 11, 23)


def test_business_days_skip_configured_holidays():
    tue = "2026-11-10T15:00:00+00:00"  # the day before Remembrance Day
    assert Calendar("America/Halifax").local_date(
        Calendar("America/Halifax").deadline(tue, 1, BUSINESS)) == date(2026, 11, 11)
    cal = Calendar("America/Halifax", ["2026-11-11"])
    assert cal.local_date(cal.deadline(tue, 1, BUSINESS)) == date(2026, 11, 12)
    assert cal.elapsed(tue, "2026-11-13T15:00:00+00:00", BUSINESS) == 2  # Thu, Fri (Wed is the holiday)


def test_calendar_days_count_weekends_and_holidays():
    cal = Calendar("America/Halifax", ["2026-11-11"])
    assert cal.local_date(cal.deadline("2026-11-07T15:00:00+00:00", 7, CALENDAR)) == date(2026, 11, 14)
    assert cal.elapsed("2026-11-07T15:00:00+00:00", "2026-11-14T15:00:00+00:00", CALENDAR) == 7


def test_deadlines_use_the_authority_timezone_not_utc():
    cal = Calendar("America/Halifax")
    late_monday = "2026-11-24T02:30:00+00:00"  # Tuesday in UTC, but 22:30 Monday in Halifax
    assert cal.local_date(late_monday) == date(2026, 11, 23)
    assert cal.deadline(late_monday, 1, BUSINESS).isoformat() == "2026-11-25T03:59:59+00:00"
    # across the spring daylight-saving change (8 Mar 2026) the deadline is still 23:59:59 local, now ADT (UTC-3)
    assert cal.deadline("2026-03-06T15:00:00+00:00", 2, BUSINESS).isoformat() == "2026-03-11T02:59:59+00:00"
    # an authority in another timezone (Regina: UTC-6, no daylight saving) gets its own end of day
    assert Calendar("America/Regina").deadline(MON, 1, BUSINESS).isoformat() == "2026-11-25T05:59:59+00:00"
    with pytest.raises(ValueError, match="no timezone"):
        cal.deadline(datetime(2026, 11, 23, 10), 1, BUSINESS)  # a naive timestamp would silently move deadlines


# ---------------- authority and rule selection ----------------
def test_rule_selection_follows_the_authority(db):
    hrm = clock(filed(jurisdiction="Dartmouth", band="S2", code="D20")["report_id"])  # alias of HRM
    prov = clock(filed(jurisdiction=NSPW, band="S2", code="D20")["report_id"])
    assert hrm["authority"] == HRM and json.loads(hrm["rule_snapshot"])["completion_days"] == 30
    assert prov["authority"] == NSPW and json.loads(prov["rule_snapshot"])["completion_days"] == 120


def test_different_damage_categories_get_different_rules(db):
    urgent_pothole = clock(filed(code="D40", band="S4")["report_id"])
    minor_pothole = clock(filed(code="D40", band="S1")["report_id"])
    urgent_crack = clock(filed(code="D20", band="S4")["report_id"])
    assert json.loads(urgent_pothole["rule_snapshot"])["completion_days"] == 7
    assert urgent_pothole["completion_due"] == "2026-12-03T03:59:59+00:00"
    assert json.loads(minor_pothole["rule_snapshot"])["completion_days"] == 30  # not every pothole is 7 days
    assert json.loads(urgent_crack["rule_snapshot"])["completion_days"] == 30
    # the 30-business-day clock skips Christmas, the observed Boxing Day and New Year's Day
    assert urgent_crack["response_due"] == "2026-12-08T03:59:59+00:00"
    assert urgent_crack["completion_due"] == "2027-01-08T03:59:59+00:00"


def test_most_specific_rule_wins_and_road_category_narrows(db):
    with session() as conn:
        ss.create_rule(conn, {"authority": HRM, "road_category": "primary", "damage_category": "D40",
                              "priority": "S4", "completion_days": 2, "effective_from": "2026-01-01",
                              "verification": "approved"}, "admin@roadsense.test")
    on_primary = clock(filed(road_class="primary")["report_id"])
    on_residential = clock(filed(road_class="residential")["report_id"])
    assert json.loads(on_primary["rule_snapshot"])["completion_days"] == 2
    assert json.loads(on_residential["rule_snapshot"])["completion_days"] == 7


def test_rules_outside_their_effective_dates_are_ignored(db):
    with session() as conn:
        ss.ensure_seeded(conn)
        ss.create_rule(conn, {"authority": HRM, "damage_category": "D40", "priority": "S4", "road_category": "x",
                              "completion_days": 1, "effective_from": "2027-01-01"}, "admin@roadsense.test")
        future = ss.select_rule(conn, HRM, "D40", "S4", "x", date(2026, 11, 23))
        later = ss.select_rule(conn, HRM, "D40", "S4", "x", date(2027, 2, 1))
    assert future["completion_days"] == 7 and later["completion_days"] == 1


def test_missing_rule_is_unable_to_determine(db):
    c = filed(jurisdiction="Town of Truro")
    state = ss.compliance(c, "2026-11-24T12:00:00+00:00")
    assert c["rule_id"] is None and c["completion_due"] is None
    assert state["status"] == ss.UNKNOWN and "no service standard configured" in state["reason"]


def test_unverified_rule_gives_provisional_deadlines_only(db):
    c = filed()  # the seeded rules are unverified until an admin acts
    state = ss.compliance(c, "2026-12-10T12:00:00+00:00")
    assert c["completion_due"] and "unverified" in c["note"]
    assert state["status"] == ss.UNKNOWN and state["provisional"] == ss.OVERDUE
    with session() as conn, pytest.raises(ValueError, match="official source URL"):
        ss.create_rule(conn, {"authority": NSPW, "completion_days": 5, "effective_from": "2026-01-01",
                              "verification": "verified"}, "admin@roadsense.test")
    # approving the same rule later counts the report without moving its deadline
    approve_rules()
    after = clock(c["report_id"])
    assert after["completion_due"] == c["completion_due"] and after["rule_verified_at"]
    assert ss.compliance(after, "2026-12-10T12:00:00+00:00")["status"] == ss.OVERDUE
    assert "standard_verified" in ledger_types(c["report_id"])


# ---------------- tracking status ----------------
def test_awaiting_then_approaching_then_overdue(db):
    approve_rules()
    rid = filed()["report_id"]  # 7 business days: due end of Wed 2 Dec, warning from end of Mon 30 Nov
    assert status(rid, "2026-11-24T15:00:00+00:00") == ss.AWAITING
    assert status(rid, "2026-12-01T15:00:00+00:00") == ss.APPROACHING
    assert status(rid, "2026-12-03T05:00:00+00:00") == ss.OVERDUE


def test_overdue_even_after_acknowledgement(db):
    approve_rules()
    rid = filed()["report_id"]
    with session() as conn:
        ss.record_event(conn, rid, "acknowledged", "manual@roadsense.test", "manual", at="2026-11-24T15:00:00+00:00")
    assert status(rid, "2026-11-25T15:00:00+00:00") == ss.WITHIN
    assert status(rid, "2026-12-04T15:00:00+00:00") == ss.OVERDUE  # acknowledgement does not stop the clock


def test_no_acknowledgement_by_the_response_deadline_is_overdue(db):
    approve_rules()
    rid = filed(code="D20", band="S2")["report_id"]  # 10-day response, 30-day completion
    assert status(rid, "2026-12-08T12:00:00+00:00") == ss.OVERDUE
    with session() as conn:
        ss.record_event(conn, rid, "acknowledged", "manual@roadsense.test", "manual", at="2026-12-08T13:00:00+00:00")
    state = ss.compliance(clock(rid), "2026-12-09T12:00:00+00:00")
    assert state["status"] == ss.WITHIN and state["response_on_time"] is False  # late response stays visible


def test_completion_within_and_after_target(db):
    approve_rules()
    on_time, late = filed()["report_id"], filed()["report_id"]
    with session() as conn:
        ss.record_event(conn, on_time, "repair_completed", "manual@roadsense.test", "manual",
                        at="2026-12-02T18:00:00+00:00")
        ss.record_event(conn, late, "repair_completed", "manual@roadsense.test", "manual",
                        at="2026-12-04T18:00:00+00:00")
    assert status(on_time, "2026-12-20T12:00:00+00:00") == ss.MET
    assert status(late, "2026-12-20T12:00:00+00:00") == ss.MISSED


# ---------------- authority status updates and reopening ----------------
def test_authority_status_updates_drive_the_lifecycle(db):
    approve_rules()
    rid = filed()["report_id"]
    with session() as conn:
        ss.observe_authority(conn, rid, "open", "Received", "mcp:municipality", "authority:open311")
        ss.observe_authority(conn, rid, "open", "Received", "mcp:municipality", "authority:open311")  # no change
        assert ss.get_clock(conn, rid)["acknowledged_at"] is None  # an automatic receipt is not a response
        ss.observe_authority(conn, rid, "open", "Acknowledged by Public Works", "mcp:municipality",
                             "authority:open311")
        ss.observe_authority(conn, rid, "open", "Repair in progress", "mcp:municipality", "authority:open311")
        ss.observe_authority(conn, rid, "closed", "Repaired", "mcp:municipality", "authority:open311")
        c = ss.get_clock(conn, rid)
    assert ledger_types(rid) == ["report_submitted", "authority_status_changed", "acknowledged", "repair_started",
                                 "repair_completed", "report_closed"]
    assert c["stage"] == "closed" and c["authority_status"] == "closed" and c["authority_notes"] == "Repaired"


def test_closed_without_repair_is_not_a_repair(db):
    approve_rules()
    rid = filed()["report_id"]
    with session() as conn:
        ss.observe_authority(conn, rid, "closed", "Duplicate of #123", "mcp:municipality", "authority:open311")
        c = ss.get_clock(conn, rid)
    assert c["closed_at"] and c["completed_at"] is None and "repair_completed" not in ledger_types(rid)


def test_reopening_preserves_both_events_and_restarts_tracking(db, monkeypatch):
    approve_rules()
    rid = filed()["report_id"]
    replies = iter([{"status": "closed", "status_notes": "Repaired"},
                    {"status": "open", "status_notes": "Reopened: repair in progress"}])
    monkeypatch.setattr(delivery, "status", lambda *a, **k: next(replies))
    assert municipality_server.request_status(rid)["newly_fixed"]
    out = municipality_server.request_status(rid)
    with session() as conn:
        report = row(conn.execute("SELECT status FROM reports WHERE id = ?", (rid,)).fetchone())
    c = clock(rid)
    assert out["reopened"] and report["status"] == "closed"  # referred: only the other authority's clock moves
    types = ledger_types(rid)
    assert types.index("report_closed") < types.index("report_reopened") < types.index("repair_started")
    assert c["reopened_count"] == 1 and c["completed_at"] is None and c["stage"] == "repair_started"
    assert c["completion_due"] == "2026-12-03T03:59:59+00:00"  # the original deadline still applies


def test_delivery_starts_the_clock(db, monkeypatch):
    approve_rules()
    r = filed(start=False)  # approved, as the reviewer leaves it
    monkeypatch.setattr(delivery, "submit", lambda *a, **k: {"channel": "open311", "service_request_id": "55"})
    municipality_server.deliver(r["id"])
    c = clock(r["id"])
    assert c and c["rule_id"] and c["completion_due"] and ledger_types(r["id"]) == ["report_submitted"]
    with session() as conn:
        entry = next(e["data"] for e in ledger.entries(conn, r["id"]) if e["event_type"] == "report_submitted")
    assert entry["evidence"]["service_request_id"] == "55" and entry["deadlines"]["completion_due"] == c["completion_due"]


def test_sweep_backfills_and_records_deadlines_once(db):
    approve_rules()
    old = filed(start=False)  # filed before tracking existed
    with session() as conn:
        first = ss.sweep(conn, at="2026-12-10T12:00:00+00:00")
        again = ss.sweep(conn, at="2026-12-10T13:00:00+00:00")
    assert first["backfilled"] == 1 and clock(old["id"])
    assert first["flagged"] == [{"report_id": old["id"], "event": "deadline_passed"}] and again["flagged"] == []


# ---------------- ledger: append-only and tamper-evident ----------------
def test_ledger_is_append_only(db):
    rid = filed()["report_id"]
    with session() as conn:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE audit SET detail = '{}' WHERE action = 'ledger_report_submitted'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM audit WHERE action = 'ledger_report_submitted'")
        with pytest.raises(sqlite3.DatabaseError, match="frozen"):
            conn.execute("UPDATE service_clocks SET completion_due = '2030-01-01' WHERE report_id = ?", (rid,))
        with pytest.raises(sqlite3.DatabaseError, match="cannot be deleted"):
            conn.execute("DELETE FROM service_clocks")
        with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
            conn.execute("DELETE FROM service_standards")


def test_ledger_hashes_reuse_payload_hashing_and_chain(db):
    rid = filed()["report_id"]
    with session() as conn:
        ss.record_event(conn, rid, "acknowledged", "manual@roadsense.test", "manual")
        rows = conn.execute("SELECT * FROM audit WHERE hash IS NOT NULL ORDER BY id").fetchall()
        assert ledger.verify(conn, rid)["ok"]
    assert rows[0]["prev_hash"] == ledger.GENESIS  # the workflow's entries and the clock's share one chain
    assert all(b["prev_hash"] == a["hash"] for a, b in zip(rows, rows[1:]))
    first, second = rows[0], rows[-1]
    assert first["hash"] == open311.payload_hash({k: first[k] for k in ("ts", "actor", "action", "subject",
                                                                         "detail", "prev_hash")})
    data = json.loads(second["detail"])
    for field in ("report_id", "authority", "event_type", "event_at", "previous_status", "new_status", "source",
                  "responsible", "evidence", "rule", "deadlines"):
        assert field in data
    assert data["previous_status"] == "submitted" and data["new_status"] == "acknowledged"


def test_integrity_check_detects_tampering(db):
    rid = filed()["report_id"]
    with session() as conn:
        ss.record_event(conn, rid, "acknowledged", "manual@roadsense.test", "manual")
        ss.record_event(conn, rid, "repair_completed", "manual@roadsense.test", "manual")
    with session() as conn:  # someone with raw access to the file drops the trigger and rewrites history
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit SET detail = replace(detail, 'manual@roadsense.test', 'someone@else') "
                     "WHERE action = 'ledger_acknowledged'")
        bad = ledger.verify(conn, rid)
    assert not bad["ok"] and any("altered" in p["problem"] for p in bad["problems"])
    with session() as conn:
        conn.execute("DROP TRIGGER audit_no_delete")
        conn.execute("DELETE FROM audit WHERE action = 'ledger_repair_completed'")  # remove the newest entry
        gone = ledger.verify(conn, rid)
    assert any("newest ledger entries are missing" in p["problem"] for p in gone["problems"])


def test_a_forked_chain_is_refused(db):
    rid = filed()["report_id"]
    with session() as conn:
        first = conn.execute("SELECT * FROM audit WHERE hash IS NOT NULL").fetchone()
        with pytest.raises(sqlite3.IntegrityError):  # a second entry claiming the same predecessor
            conn.execute("INSERT INTO audit (ts, actor, action, subject, detail, prev_hash, hash) VALUES "
                         "(?, 'x', 'ledger_x', ?, '{}', ?, 'f')", (first["ts"], f"report:{rid}", first["prev_hash"]))


# ---------------- rules change without rewriting history ----------------
def test_updating_a_rule_keeps_historical_deadlines(db):
    approve_rules()
    before = filed()
    with session() as conn:
        rule_id = before["rule_id"]
        ss.update_rule(conn, rule_id, {"completion_days": 3}, "admin@roadsense.test")
        actions = [a for (a,) in conn.execute("SELECT action FROM audit WHERE subject = ?", (f"standard:{rule_id}",))]
    after = filed()
    assert clock(before["report_id"])["completion_due"] == before["completion_due"] == "2026-12-03T03:59:59+00:00"
    assert json.loads(clock(before["report_id"])["rule_snapshot"])["completion_days"] == 7
    assert after["completion_due"] == "2026-11-27T03:59:59+00:00" and "service_standard_updated" in actions


def test_rule_validation(db):
    with session() as conn:
        for bad, why in (({"authority": "Atlantis", "completion_days": 5}, "unknown authority"),
                         ({"authority": HRM, "completion_days": 5, "time_unit": "weeks"}, "time_unit"),
                         ({"authority": HRM}, "response target"),
                         ({"authority": HRM, "completion_days": 5, "priority": "urgent"}, "priority"),
                         ({"authority": HRM, "completion_days": 5, "timezone": "Mars/Base"}, "timezone"),
                         ({"authority": HRM, "completion_days": 5, "effective_from": "2026-02-01",
                           "effective_to": "2026-01-01"}, "before")):
            with pytest.raises(ValueError, match=why):
                ss.create_rule(conn, {"effective_from": "2026-01-01", **bad}, "admin@roadsense.test")


# ---------------- dashboard and API ----------------
def test_dashboard_metrics_exclude_unresolved_reports(db):
    approve_rules()
    done, open_ = filed()["report_id"], filed()["report_id"]
    filed(jurisdiction=NSPW)
    with session() as conn:
        ss.record_event(conn, done, "repair_completed", "manual@roadsense.test", "manual",
                        at="2026-11-25T14:00:00+00:00")  # 48 hours after filing
        ss.record_event(conn, open_, "acknowledged", "manual@roadsense.test", "manual",
                        at="2026-11-24T14:00:00+00:00")
        d = ss.dashboard(conn, at="2026-11-26T12:00:00+00:00")
        hrm = ss.dashboard(conn, {"authority": HRM}, at="2026-11-26T12:00:00+00:00")
        overdue = ss.dashboard(conn, {"status": ss.OVERDUE}, at="2026-11-26T12:00:00+00:00")
    m = d["metrics"]
    assert m["total"] == 3 and m["active"] == 2 and m[ss.MET] == 1 and m[ss.UNKNOWN] == 1  # province unverified
    assert m["repaired"] == 1 and m["avg_completion_hours"] == 48.0 and m["avg_completion_days"] == 2
    assert m["responded"] == 2 and m["avg_response_hours"] == 36.0  # 24 h and the 48 h repair (implied response)
    assert hrm["metrics"]["total"] == 2 and overdue["metrics"]["total"] == 0
    assert set(d["options"]["authority"]) == {HRM, NSPW} and "lat" not in d["items"][0]


def test_api_permissions(db):
    from roadsense.services import agent_api

    c = TestClient(agent_api.app)
    viewer = {**AUTH, "X-RoadSense-User": "v@roadsense.test", "X-RoadSense-Role": "viewer"}
    admin = {**AUTH, "X-RoadSense-User": "a@roadsense.test", "X-RoadSense-Role": "admin"}
    reviewer = {**AUTH, "X-RoadSense-User": "r@roadsense.test", "X-RoadSense-Role": "reviewer"}
    rid = filed()["report_id"]
    assert c.get("/service-standards/dashboard").status_code == 401
    assert c.get("/service-standards/dashboard", headers=viewer).json()["metrics"]["total"] == 1
    rule = {"authority": HRM, "completion_days": 5, "effective_from": "2026-01-01"}
    assert c.post("/service-standards/rules", json=rule, headers=viewer).status_code == 403
    assert c.post("/service-standards/rules", json=rule, headers=reviewer).status_code == 403
    assert c.post("/service-standards/rules", json=rule, headers=admin).status_code == 200
    assert c.post("/service-standards/rules", json={**rule, "time_unit": "x"}, headers=admin).status_code == 422
    assert c.get(f"/service-standards/reports/{rid}", headers=viewer).status_code == 403
    t = c.get(f"/service-standards/reports/{rid}", headers=reviewer).json()
    clock_entries = [e for e in t["ledger"] if not e["event_type"].startswith("workflow_")]
    assert t["integrity"]["ok"] and clock_entries[0]["event_type"] == "report_submitted"
    ev = c.post(f"/service-standards/reports/{rid}/events", json={"event": "acknowledged", "note": "phoned 311"},
                headers=reviewer)
    assert ev.status_code == 200 and ev.json()["actor"] == "r@roadsense.test"
    assert c.post(f"/service-standards/reports/{rid}/events", json={"event": "acknowledged"},
                  headers=reviewer).status_code == 409  # already acknowledged: nothing new to record
    assert c.post(f"/service-standards/reports/{rid}/events", json={"event": "repair_completed",
                  "at": "2030-01-01T00:00:00+00:00"}, headers=reviewer).status_code == 422  # not in the future
    assert c.get("/service-standards/integrity", headers=reviewer).json()["ok"]


def test_web_app_requires_sign_in_for_service_standards():
    from roadsense.web.app import app

    assert TestClient(app).get("/api/service-standards").status_code == 401
    assert TestClient(app).post("/api/service-standards/rules", json={}).status_code == 401


def test_now_is_timezone_aware():
    assert datetime.fromisoformat(ss.now()).tzinfo == timezone.utc


def test_repair_verification_outcomes_are_ledger_evidence(db):
    rid = filed()["report_id"]
    with session() as conn:
        ss.observe_authority(conn, rid, "closed", "Repaired", "mcp:municipality", "authority:open311")
        entry = ss.note_repair_check(conn, rid, "repair_not_verified", "verify:repair", "pothole seen again",
                                     "event:ev-later")
        c = ss.get_clock(conn, rid)
        assert ledger.verify(conn, rid)["ok"]
    assert entry["event_type"] == "repair_not_verified" and "event:ev-later" in entry["note"]
    assert c["stage"] == "closed"  # RoadSense's own check never rewrites what the authority reported


# ---------------- one deadline source ----------------
def test_the_repair_target_is_the_service_standard(db):
    unfiled = filed(band="S4", start=False)
    with session() as conn:
        conn.execute("UPDATE reports SET status = 'pending_approval' WHERE id = ?", (unfiled["id"],))
        before = ss.target(conn, unfiled)
    assert before["source"] == "applicable rule" and before["completion_days"] == 7 and not before["usable"]
    approve_rules()
    clock = filed(band="S4")
    with session() as conn:
        t = ss.target(conn, {"id": clock["report_id"]})
    assert t["source"] == "filed" and t["usable"] and t["completion_due"] == clock["completion_due"]
    assert t["completion_days"] == json.loads(clock["rule_snapshot"])["completion_days"]


def test_the_authority_payload_carries_no_roadsense_deadline(db):
    r = filed(band="S4", start=False)
    payload = open311.build_payload(r)
    assert "repair_target_days" not in payload["attributes"] and payload["attributes"]["severity_band"] == "S4"


def test_report_page_shows_the_service_standard_target(db):
    from roadsense.services import agent_api

    r = filed(band="S4", start=False)
    d = TestClient(agent_api.app).get(f"/reports/{r['id']}", headers=AUTH).json()
    assert "repair_target_days" not in d["report"] and d["report"]["service_target"]["completion_days"] == 7


def test_reclassifying_recomputes_the_band_and_level(db):
    add_event("ev-rc1", 44.7, -63.58, code="D20", area=0.07)
    with session() as conn:
        r = reports.create_or_merge(conn, "ev-rc1", "medium", "x", HRM, 15, jurisdiction=HRM, band="S4")["report"]
        assert r["severity"] == "high"  # the passed level is ignored: the band names it
        r = reports.reclassify(conn, r["id"], "D00", "reviewer@roadsense.test")
    assert (r["severity_band"], r["severity"]) == ("S2", "medium")  # a line crack is never urgent


def test_old_contradictory_levels_are_fixed_only_before_review(db):
    pending, sent = filed(band="S4", start=False), filed(band="S4", start=False)
    with session() as conn:
        conn.execute("UPDATE reports SET status = 'pending_approval', severity = 'medium' WHERE id = ?",
                     (pending["id"],))
        conn.execute("UPDATE reports SET severity = 'medium' WHERE id = ?", (sent["id"],))  # already submitted
        assert reports.normalise_severity(conn) == 1
        levels = dict(conn.execute("SELECT id, severity FROM reports WHERE id IN (?, ?)", (pending["id"], sent["id"])))
    assert levels == {pending["id"]: "high", sent["id"]: "medium"}  # what the authority received is not rewritten


# ---------------- the municipal workflow drives the clock ----------------
def _inspector():
    with session() as conn:
        conn.execute("INSERT OR IGNORE INTO users (email, role, created_at) VALUES ('i@x.test', 'inspector', ?)", (MON,))
        return conn.execute("SELECT id FROM users WHERE email = 'i@x.test'").fetchone()[0]


def _own_report(band="S4"):
    _n["i"] += 1
    event_id = f"ev-wfc{_n['i']}"
    add_event(event_id, 44.60 + _n["i"] * 0.002, -63.58)
    with session() as conn:
        return reports.create_or_merge(conn, event_id, "high", "x", HRM, 15, jurisdiction=HRM, band=band)["report"]


def _step(rid, action, at, **fields):
    with session() as conn:
        return workflow.transition(conn, rid, action, "staff@roadsense.test", "admin", at=at, **fields)


def test_the_workflow_moves_the_clock_from_approval_to_repair(db):
    approve_rules()
    rid, insp = _own_report()["id"], _inspector()
    _step(rid, "approve", MON)
    c = clock(rid)
    assert c["submitted_at"] == MON and c["completion_due"] == "2026-12-03T03:59:59+00:00"  # S4: 7 business days
    _step(rid, "assign", "2026-11-23T15:00:00+00:00", inspector_id=insp)
    _step(rid, "repair_needed", "2026-11-24T15:00:00+00:00", repair_eta="2026-11-30")
    assert status(rid, "2026-11-25T12:00:00+00:00") == ss.WITHIN
    _step(rid, "repaired", "2026-11-27T15:00:00+00:00")
    c = clock(rid)
    assert ledger_types(rid) == ["report_submitted", "acknowledged", "inspection_started", "repair_completed"]
    assert c["stage"] == "repair_completed" and c["acknowledged_at"] == "2026-11-23T15:00:00+00:00"
    assert c["inspection_at"] == "2026-11-24T15:00:00+00:00" and status(rid, "2026-12-20T00:00:00+00:00") == ss.MET

    _step(rid, "reopen", "2026-12-01T15:00:00+00:00", reason="patch failed")
    assert clock(rid)["reopened_count"] == 1 and clock(rid)["completed_at"] is None
    _step(rid, "repair_needed", "2026-12-01T16:00:00+00:00", repair_eta="2026-12-10")
    _step(rid, "repaired", "2026-12-08T15:00:00+00:00")
    assert ledger_types(rid)[-2:] == ["report_reopened", "repair_completed"]
    assert status(rid, "2026-12-20T00:00:00+00:00") == ss.MISSED  # the second repair came after the deadline
    with session() as conn:
        assert ledger.verify(conn, rid)["ok"]


def test_no_repair_needed_closes_the_clock_without_a_repair(db):
    approve_rules()
    rid, insp = _own_report()["id"], _inspector()
    _step(rid, "approve", MON)
    _step(rid, "assign", MON, inspector_id=insp)
    _step(rid, "no_repair_needed", "2026-11-24T15:00:00+00:00", notes="surface wear only")
    c = clock(rid)
    assert c["closed_at"] and c["completed_at"] is None and "repair_completed" not in ledger_types(rid)


def test_rejected_reports_never_get_a_clock(db):
    rid = _own_report()["id"]
    _step(rid, "reject", MON)
    assert clock(rid) is None
