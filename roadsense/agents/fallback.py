"""Deterministic pipeline over the same MCP tools and step allow-lists: the safety net. It runs after every trip and
finishes any event the workflow left undecided or unreported (none, when the workflow completed), and the number it
had to finish is reported as the fallback rate. Filing and follow-up are this code alone: they have nothing to judge."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..common.config import settings
from ..common.db import audit, row, session
from ..core import service_standards
from .mcp_client import Toolbox
from .stages import use


def _audit(action: str, subject: str, detail=None) -> None:
    with session() as conn:
        audit(conn, "fallback", action, subject, detail)


def _status(event_id: str) -> str:
    with session() as conn:
        return conn.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()["status"]


async def check_event(toolbox: Toolbox, event_id: str) -> dict | None:
    """verify → (re-check), each tried twice. None when the verifier stays unavailable: a tool failure (out of
    memory, missing weights) is not evidence, so it must never turn real damage into a "false alarm"."""
    async def call(tool: str) -> dict | None:
        for _ in range(2):
            out = await use(toolbox, "vision", tool, {"event_id": event_id})
            if not out.get("error") and out.get("suggested_verdict"):
                return out
        _audit("verifier_unavailable", f"event:{event_id}", {"tool": tool, "error": out.get("error")})
        return None

    check = await call("detector_verify")
    if check and check["suggested_verdict"] == "recheck":
        check = await call("detector_recheck")
        return {**check, "rechecked": True} if check else None
    return {**check, "rechecked": False} if check else None


async def decide_event(toolbox: Toolbox, event_id: str) -> str:
    """verify → (re-check) → verdict by the rule in core.reports.suggested_verdict. Returns the final status."""
    check = await check_event(toolbox, event_id)
    if check is None:
        return _status(event_id)  # stays received/checked; "Process now" retries it
    await use(toolbox, "vision", "detector_record_verdict", {"event_id": event_id, "verdict": check["suggested_verdict"],
                                                   "rationale": f"rule: {check.get('reason', '')}"})
    return _status(event_id)


async def triage_event(toolbox: Toolbox, event: dict) -> dict:
    sev = await use(toolbox, "vision", "detector_severity", {"event_id": event["id"]})
    place = await use(toolbox, "location", "geo_find_jurisdiction", {"lat": event["lat"], "lon": event["lon"]})
    best = (place.get("candidates") or [{"authority": place.get("municipality"), "evidence": "no candidates"}])[0]
    return await use(toolbox, "triage", "reports_create_or_merge", {
        "event_id": event["id"], "severity": sev.get("severity", "medium"),
        "jurisdiction": best["authority"] or "unknown", "rationale": f"rule: top candidate ({best['evidence']})"})


async def complete_trip(toolbox: Toolbox, trip_id: str) -> int:
    """Finish every event of the trip that the agents left undecided or unreported. Returns how many it touched."""
    with session() as conn:
        pending = [dict(r) for r in conn.execute(
            "SELECT id, lat, lon, status FROM events WHERE trip_id = ? AND status IN ('received', 'checked', "
            "'verified')", (trip_id,))]
    for e in pending:
        status = e["status"]
        if status in ("received", "checked"):
            status = await decide_event(toolbox, e["id"])
        if status == "verified":
            result = await triage_event(toolbox, e)
            _audit("completed_event", f"event:{e['id']}", {"result": result.get("action", result.get("error"))})
    return len(pending)


def trip_summary(trip_id: str) -> str:
    with session() as conn:
        n = dict(conn.execute("SELECT status, COUNT(*) FROM events WHERE trip_id = ? GROUP BY status",
                              (trip_id,)).fetchall())
        disputed = conn.execute("SELECT COUNT(*) FROM events WHERE trip_id = ? AND class_disputed = 1",
                                (trip_id,)).fetchone()[0]
        new = conn.execute("SELECT COUNT(*) FROM reports WHERE trip_id = ? AND status = 'pending_approval'",
                           (trip_id,)).fetchone()[0]
        merged = conn.execute("SELECT COUNT(*) FROM events e JOIN reports r ON e.report_id = r.id "
                              "WHERE e.trip_id = ? AND r.trip_id != ?", (trip_id, trip_id)).fetchone()[0]
        provincial = conn.execute("SELECT COUNT(*) FROM reports WHERE trip_id = ? AND jurisdiction LIKE 'Nova Scotia%'",
                                  (trip_id,)).fetchone()[0]
    return (f"{n.get('processed', 0)} defect(s) confirmed ({disputed} with a class the two models disagree on) and "
            f"{n.get('rejected', 0)} false alarm(s) dropped. {merged} matched damage other drivers already reported. "
            f"{new} new report(s) await your approval" + (f", {provincial} of them for the province." if provincial
                                                          else "."))


async def file_report(toolbox: Toolbox, report_id: int) -> dict:
    with session() as conn:
        r = row(conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone())
    if r is None:
        return {"error": f"unknown report {report_id}"}
    if r["closed_reason"] == "referred":
        return {"ok": True, "service_request_id": r["service_request_id"], "channel": r["channel"], "by": "already sent"}
    result = await use(toolbox, "filing", "city_submit_request", {"report_id": report_id})
    _audit("filed" if result.get("ok") else "file_failed", f"report:{report_id}", result)
    return {**result, "by": "workflow"}


async def follow_up(toolbox: Toolbox) -> dict:
    with session() as conn:
        # referred reports only: a report on the municipality's own roads is never sent anywhere
        api = "r.closed_reason = 'referred' AND r.channel IN ('open311', 'cityworks', 'arcgis')"
        submitted = [r["id"] for r in conn.execute(
            f"SELECT r.id FROM reports r LEFT JOIN service_clocks c ON c.report_id = r.id WHERE {api} AND "
            "c.completed_at IS NULL AND c.closed_at IS NULL")]
        # recently repaired referrals are re-checked for a while: an authority can reopen a request it closed
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.reopen_watch_days)).isoformat(
            timespec="seconds")
        recheck = [r["id"] for r in conn.execute(
            f"SELECT r.id FROM reports r JOIN service_clocks c ON c.report_id = r.id WHERE {api} AND "
            "COALESCE(c.closed_at, c.completed_at, '') >= ?", (cutoff,))]
        fixed_unnotified = [dict(r) for r in conn.execute(
            "SELECT * FROM reports r WHERE status = 'repaired' AND NOT EXISTS "
            "(SELECT 1 FROM notifications n WHERE n.report_id = r.id)")]
    fixed = []
    for rid in submitted:
        s = await use(toolbox, "follow_up", "city_request_status", {"report_id": rid})
        if s.get("fixed"):
            fixed.append(rid)
    reopened = []
    for rid in recheck:
        s = await use(toolbox, "follow_up", "city_request_status", {"report_id": rid})
        if s.get("reopened"):
            reopened.append(rid)
    with session() as conn:
        tracking = service_standards.sweep(conn)
    with session() as conn:
        to_notify = [dict(r) for r in conn.execute(
            "SELECT * FROM reports r WHERE status = 'repaired' AND NOT EXISTS "
            "(SELECT 1 FROM notifications n WHERE n.report_id = r.id)")]
    for r in to_notify:
        await use(toolbox, "follow_up", "reports_notify_driver", {"report_id": r["id"], "message":
                           f"Good news: the {r['class_name'].lower()} you reported at {r['address']} has been "
                           "repaired. Thanks for helping keep the roads safe!"})
    return {"checked": len(submitted), "fixed": sorted(set(fixed) | {r["id"] for r in fixed_unnotified}),
            "reopened": reopened, "service_standards": tracking}
