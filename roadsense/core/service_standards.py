"""Service-standard tracking: which published standard applies to a filed report, its deadlines, and how the
authority is doing against them.

This is a compliance-monitoring and evidence tool. Meeting (or missing) a published standard does not by itself
settle legal liability; the dashboard says so.

    rules      service_standards table: per authority, optionally narrowed by road category, damage category
               (class code) and priority (severity band), with response / completion targets in business or
               calendar days, effective dates, the official source and a verification status
    clock      service_clocks table: started when a report is filed. The applicable rule is copied into the clock
               (rule_snapshot, rule_hash) and the deadlines are computed once; a trigger refuses later changes, so
               editing a rule never moves an existing report's deadlines
    events     report_submitted, acknowledged, inspection_started, repair_started, repair_completed, report_closed,
               report_reopened (+ authority_status_changed, deadline_approaching, deadline_passed, standard_verified),
               each written to the hash-chained ledger (core.ledger)
    status     compliance() turns a clock into one tracking status at a given moment (STATUS_RULES below)

The authority's own status (open / closed and its notes) is stored as reported and kept apart from RoadSense's
lifecycle stage and tracking status.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..common.db import audit, now, row
from . import ledger, open311
from .business_days import UNITS, Calendar, parse_ts

log = logging.getLogger("roadsense.service_standards")
SEED = Path(__file__).with_name("service_standards.json")
VERIFICATIONS = ("unverified", "verified", "approved")
USABLE = ("verified", "approved")  # verified: checked against the official source; approved: adopted by an admin
PRIORITIES = ("S1", "S2", "S3", "S4")
DAMAGE = ("D00", "D10", "D20", "D40")
RULE_FIELDS = ("authority", "jurisdiction", "road_category", "damage_category", "priority", "response_days",
               "completion_days", "time_unit", "timezone", "effective_from", "effective_to", "source_url",
               "verification", "last_reviewed", "notes")
TIMING_FIELDS = ("authority", "road_category", "damage_category", "priority", "response_days", "completion_days",
                 "time_unit", "timezone")

# RoadSense lifecycle stages, in order. "reopened" ranks with "acknowledged": the authority is working on it again.
STAGES = ("submitted", "acknowledged", "inspection", "repair_started", "repair_completed", "closed")
RANK = {**{s: i for i, s in enumerate(STAGES)}, "reopened": 1}
EVENT_STAGE = {"acknowledged": "acknowledged", "inspection_started": "inspection", "repair_started": "repair_started",
               "repair_completed": "repair_completed", "report_closed": "closed"}
EVENT_FIELD = {"acknowledged": "acknowledged_at", "inspection_started": "inspection_at",
               "repair_started": "repair_started_at", "repair_completed": "completed_at", "report_closed": "closed_at"}
LIFECYCLE_EVENTS = ("report_submitted", *EVENT_STAGE, "report_reopened")
MANUAL_EVENTS = (*EVENT_STAGE, "report_reopened")

WITHIN, APPROACHING, OVERDUE = "within_standard", "approaching_deadline", "overdue"
MET, MISSED = "completed_within_target", "completed_after_target"
AWAITING, UNKNOWN = "awaiting_response", "unable_to_determine"
STATUSES = (OVERDUE, APPROACHING, AWAITING, WITHIN, UNKNOWN, MISSED, MET)
STATUS_RULES = {
    UNKNOWN: "No service standard matches the report, or the matching standard is unverified (an administrator must "
             "verify its source or approve it), or the report was closed and the standard sets no completion "
             "target. Provisional deadlines are shown but not counted.",
    OVERDUE: "Open, and the completion deadline has passed; or the authority has not acknowledged the report and "
             "the response deadline has passed. Acknowledgement does not stop the completion clock.",
    APPROACHING: "Open, not overdue, and inside the warning window (the last N days before the next deadline).",
    AWAITING: "Open, no deadline close or passed, and the authority has not acknowledged the report yet.",
    WITHIN: "Open, acknowledged by the authority, and no deadline close or passed.",
    MET: "Repaired (or closed) on or before the completion deadline.",
    MISSED: "Repaired (or closed) after the completion deadline.",
}
DISCLAIMER = ("Compliance monitoring and evidence management only. Whether a published standard was met does not by "
              "itself establish or remove legal liability.")


@lru_cache(maxsize=1)
def config() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


def _ts(value: str | None) -> datetime | None:
    return parse_ts(value) if value else None


def canonical_authority(name: str | None) -> str | None:
    info = open311.service_info(name)
    return info["authority"] if info else (name or None)


# ---------------- rules and holidays ----------------
def ensure_seeded(conn: sqlite3.Connection) -> None:
    """Load the seed rules and holidays into empty tables (once per database)."""
    cfg = config()
    if not conn.execute("SELECT 1 FROM service_standards LIMIT 1").fetchone():
        ts, n = now(), 0
        for rule in cfg.get("rules", []):
            priorities = rule.get("priority")
            for p in (priorities if isinstance(priorities, list) else [priorities]):
                conn.execute(
                    "INSERT INTO service_standards (authority, jurisdiction, road_category, damage_category, priority,"
                    " response_days, completion_days, time_unit, timezone, effective_from, effective_to, source_url,"
                    " verification, notes, created_by, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rule["authority"], rule.get("jurisdiction"), rule.get("road_category"),
                     rule.get("damage_category"), p, rule.get("response_days"), rule.get("completion_days"),
                     rule.get("time_unit", "business_days"), rule.get("timezone", cfg["timezone"]),
                     rule.get("effective_from", "2025-01-01"), rule.get("effective_to"), rule.get("source_url"),
                     "unverified", rule.get("notes"), "seed", ts, ts))
                n += 1
        audit(conn, "seed", "service_standards_seeded", None, {"rules": n, "verification": "unverified"})
    if not conn.execute("SELECT 1 FROM holidays LIMIT 1").fetchone():
        for day, name in cfg.get("holidays", {}).get("all", []):
            conn.execute("INSERT OR IGNORE INTO holidays (day, authority, name) VALUES (?, '', ?)", (day, name))


def calendar(conn: sqlite3.Connection, authority: str | None, tz: str | None = None) -> Calendar:
    days = [r[0] for r in conn.execute("SELECT day FROM holidays WHERE authority = '' OR authority = ?",
                                       (authority or "",))]
    return Calendar(tz or config()["timezone"], days)


def _timing_hash(rule: dict) -> str:
    return open311.payload_hash({k: rule.get(k) for k in TIMING_FIELDS})


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    ensure_seeded(conn)
    used = dict(conn.execute("SELECT rule_id, COUNT(*) FROM service_clocks WHERE rule_id IS NOT NULL "
                             "GROUP BY rule_id").fetchall())
    return [{**dict(r), "usable": r["verification"] in USABLE, "reports": used.get(r["id"], 0)}
            for r in conn.execute("SELECT * FROM service_standards ORDER BY authority, effective_from DESC, id")]


def _clean(data: dict, current: dict | None = None) -> dict:
    """Validate a rule (new, or `current` with the changes in `data`). Raises ValueError with a readable reason."""
    out = {k: (current or {}).get(k) for k in RULE_FIELDS}
    for k in RULE_FIELDS:
        if k in data:
            v = data[k]
            out[k] = v.strip() if isinstance(v, str) else v
            if out[k] == "":
                out[k] = None
    if not current:
        out["time_unit"] = out["time_unit"] or "business_days"
        out["timezone"] = out["timezone"] or config()["timezone"]
        out["verification"] = out["verification"] or "unverified"
    info = open311.service_info(out["authority"])
    if info is None:
        raise ValueError(f"unknown authority {out['authority']!r}: add it to the authority directory first")
    out["authority"] = info["authority"]
    if out["time_unit"] not in UNITS:
        raise ValueError(f"time_unit must be one of {UNITS}")
    for k in ("response_days", "completion_days"):
        if out[k] is not None:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError) as e:
                raise ValueError(f"{k} must be a whole number of days") from e
            if not 0 <= out[k] <= 3650:
                raise ValueError(f"{k} must be between 0 and 3650")
    if out["response_days"] is None and out["completion_days"] is None:
        raise ValueError("a standard needs a response target, a completion target, or both")
    if out["priority"] is not None and out["priority"] not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES} (severity band), or empty for any")
    if out["damage_category"] is not None and out["damage_category"] not in DAMAGE:
        raise ValueError(f"damage_category must be one of {DAMAGE}, or empty for any")
    try:
        ZoneInfo(out["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone {out['timezone']!r}") from e
    for k in ("effective_from", "effective_to", "last_reviewed"):
        if out[k] is not None:
            try:
                out[k] = date.fromisoformat(str(out[k])[:10]).isoformat()
            except ValueError as e:
                raise ValueError(f"{k} must be a date (YYYY-MM-DD)") from e
    if not out["effective_from"]:
        raise ValueError("effective_from is required")
    if out["effective_to"] and out["effective_to"] < out["effective_from"]:
        raise ValueError("effective_to is before effective_from")
    if out["verification"] not in VERIFICATIONS:
        raise ValueError(f"verification must be one of {VERIFICATIONS}")
    if out["source_url"] and not str(out["source_url"]).startswith("https://"):
        raise ValueError("source_url must be an https:// link to the official publication")
    if out["verification"] == "verified" and not out["source_url"]:
        raise ValueError("a verified standard needs the official source URL (use 'approved' for an internal policy)")
    return out


def create_rule(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    ensure_seeded(conn)
    rule = _clean(data)
    ts = now()
    if rule["verification"] in USABLE:
        rule["last_reviewed"] = rule["last_reviewed"] or ts[:10]
    cur = conn.execute(
        f"INSERT INTO service_standards ({', '.join(RULE_FIELDS)}, approved_by, created_by, created_at, updated_at) "
        f"VALUES ({', '.join('?' * (len(RULE_FIELDS) + 4))})",
        [*(rule[k] for k in RULE_FIELDS), actor if rule["verification"] in USABLE else None, actor, ts, ts])
    audit(conn, actor, "service_standard_created", f"standard:{cur.lastrowid}", rule)
    return row(conn.execute("SELECT * FROM service_standards WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_rule(conn: sqlite3.Connection, rule_id: int, data: dict, actor: str) -> dict:
    """Change a rule. Reports already filed keep the snapshot and deadlines they started with; a verification that
    leaves the timing untouched is recorded on the open reports that use exactly this rule."""
    current = row(conn.execute("SELECT * FROM service_standards WHERE id = ?", (rule_id,)).fetchone())
    if current is None:
        raise LookupError(f"unknown service standard {rule_id}")
    rule = _clean(data, current)
    ts = now()
    newly_usable = rule["verification"] in USABLE and current["verification"] not in USABLE
    if rule["verification"] != current["verification"]:
        rule["last_reviewed"] = data.get("last_reviewed") or ts[:10]
    approved_by = actor if rule["verification"] in USABLE and rule["verification"] != current["verification"] \
        else current["approved_by"] if rule["verification"] in USABLE else None
    conn.execute(f"UPDATE service_standards SET {', '.join(f'{k} = ?' for k in RULE_FIELDS)}, approved_by = ?, "
                 "updated_at = ? WHERE id = ?", [*(rule[k] for k in RULE_FIELDS), approved_by, ts, rule_id])
    changes = {k: {"from": current[k], "to": rule[k]} for k in RULE_FIELDS if current[k] != rule[k]}
    audit(conn, actor, "service_standard_updated", f"standard:{rule_id}", changes or {"unchanged": True})
    if newly_usable:
        digest = _timing_hash(rule)
        for c in conn.execute("SELECT * FROM service_clocks WHERE rule_id = ? AND rule_hash = ? AND "
                              "rule_verified_at IS NULL", (rule_id, digest)).fetchall():
            conn.execute("UPDATE service_clocks SET rule_verified_at = ?, updated_at = ? WHERE report_id = ?",
                         (ts, ts, c["report_id"]))
            _ledger(conn, dict(c) | {"rule_verified_at": ts}, "standard_verified", actor, "admin",
                    previous=c["stage"], note=f"service standard {rule_id} {rule['verification']} by {actor}; "
                                              "deadlines unchanged")
    return row(conn.execute("SELECT * FROM service_standards WHERE id = ?", (rule_id,)).fetchone())


def list_holidays(conn: sqlite3.Connection) -> list[dict]:
    ensure_seeded(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM holidays ORDER BY day, authority")]


def add_holiday(conn: sqlite3.Connection, day: str, name: str, authority: str | None, actor: str) -> dict:
    try:
        d = date.fromisoformat(str(day)[:10]).isoformat()
    except ValueError as e:
        raise ValueError("day must be a date (YYYY-MM-DD)") from e
    if not (name or "").strip():
        raise ValueError("a holiday needs a name")
    scope = canonical_authority(authority) if authority else ""
    if authority and open311.service_info(authority) is None:
        raise ValueError(f"unknown authority {authority!r}")
    conn.execute("INSERT OR REPLACE INTO holidays (day, authority, name) VALUES (?, ?, ?)", (d, scope, name[:80]))
    audit(conn, actor, "holiday_added", f"holiday:{d}", {"authority": scope or "all", "name": name[:80],
                                                         "note": "existing deadlines are not recalculated"})
    return {"day": d, "authority": scope, "name": name[:80]}


def remove_holiday(conn: sqlite3.Connection, day: str, authority: str | None, actor: str) -> bool:
    scope = canonical_authority(authority) if authority else ""
    n = conn.execute("DELETE FROM holidays WHERE day = ? AND authority = ?", (day, scope)).rowcount
    if n:
        audit(conn, actor, "holiday_removed", f"holiday:{day}", {"authority": scope or "all"})
    return bool(n)


def select_rule(conn: sqlite3.Connection, authority: str | None, class_code: str | None, band: str | None,
                road_class: str | None, on: date) -> dict | None:
    """The applicable standard: in force on `on`, every narrowing field either empty (any) or equal to the report's,
    most specific first. Verification only breaks ties: an unverified specific rule is still the applicable one,
    and the report is then flagged instead of silently measured against a looser verified rule."""
    ensure_seeded(conn)
    if not authority:
        return None
    day, best, best_key = on.isoformat(), None, None
    for r in conn.execute("SELECT * FROM service_standards WHERE lower(authority) = lower(?)", (authority,)):
        if r["effective_from"] > day or (r["effective_to"] and r["effective_to"] < day):
            continue
        spec, fits = 0, True
        for field, value in (("road_category", road_class), ("damage_category", class_code), ("priority", band)):
            if r[field]:
                if r[field] != value:
                    fits = False
                    break
                spec += 1
        if fits:
            key = (spec, r["verification"] in USABLE, r["effective_from"], r["id"])
            if best_key is None or key > best_key:
                best, best_key = dict(r), key
    return best


# ---------------- the service clock ----------------
def get_clock(conn: sqlite3.Connection, report_id: int) -> dict | None:
    return row(conn.execute("SELECT * FROM service_clocks WHERE report_id = ?", (report_id,)).fetchone())


def _neighbourhood(conn: sqlite3.Connection, report: dict) -> str | None:
    """From the geocoder cache only (no network call on the delivery path)."""
    hit = conn.execute("SELECT value FROM geocache WHERE key = ?",
                       (f"v3:{report['lat']:.4f},{report['lon']:.4f}",)).fetchone()
    try:
        return (json.loads(hit[0]).get("suburb") or None) if hit else None
    except ValueError:
        return None


def _verified(clock: dict) -> bool:
    snap = json.loads(clock["rule_snapshot"]) if clock.get("rule_snapshot") else {}
    return snap.get("verification") in USABLE or bool(clock.get("rule_verified_at"))


def _evidence(report: dict) -> dict:
    return {"best_event_id": report.get("best_event_id"), "service_request_id": report.get("service_request_id"),
            "submitted_payload_hash": report.get("submitted_payload_hash"), "channel": report.get("channel")}


def _ledger(conn, clock: dict, event_type: str, actor: str, source: str, previous: str | None = None,
            at: str | None = None, before: dict | None = None, note: str | None = None,
            report: dict | None = None) -> dict:
    report = report or row(conn.execute("SELECT * FROM reports WHERE id = ?", (clock["report_id"],)).fetchone()) or {}
    after = compliance(clock, at)
    snap = json.loads(clock["rule_snapshot"]) if clock.get("rule_snapshot") else None
    entry = ledger.append(conn, clock["report_id"], event_type, actor, {
        "report_id": clock["report_id"], "authority": clock["authority"], "event_type": event_type,
        "event_at": at or now(), "previous_status": previous, "new_status": clock["stage"],
        "tracking_before": before["status"] if before else None, "tracking_after": after["status"],
        "source": source, "responsible": actor, "evidence": _evidence(report),
        "rule": {"id": clock.get("rule_id"), "hash": clock.get("rule_hash"),
                 "verification": snap.get("verification") if snap else None,
                 "verified_later": bool(clock.get("rule_verified_at"))} if snap else None,
        "deadlines": {"response_due": clock.get("response_due"), "completion_due": clock.get("completion_due")},
        "authority_status": clock.get("authority_status"), "authority_notes": clock.get("authority_notes"),
        "note": note})
    conn.execute("UPDATE service_clocks SET ledger_head = ?, last_flag = ? WHERE report_id = ?",
                 (entry["hash"], after["provisional"] or after["status"], clock["report_id"]))
    return entry


def start_clock(conn: sqlite3.Connection, report: dict, actor: str, submitted_at: str | None = None,
                source: str = "roadsense") -> dict:
    """Start tracking a report that was just filed. Idempotent: a second call returns the existing clock."""
    existing = get_clock(conn, report["id"])
    if existing:
        return existing
    ts = submitted_at or now()
    authority = canonical_authority(report.get("jurisdiction"))
    tz = config()["timezone"]
    rule = select_rule(conn, authority, report.get("class_code"), report.get("severity_band"),
                       report.get("road_class"), Calendar(tz).local_date(ts))
    fields = {"report_id": report["id"], "authority": authority, "timezone": tz, "submitted_at": ts,
              "stage": "submitted", "neighbourhood": _neighbourhood(conn, report), "updated_at": now()}
    if rule:
        cal = calendar(conn, authority, rule["timezone"])
        unit, warn = rule["time_unit"], int(config().get("warn_days", 2))
        due = {k: cal.deadline(ts, rule[f"{k}_days"], unit) if rule[f"{k}_days"] is not None else None
               for k in ("response", "completion")}
        last = max((d for d in due.values() if d), default=parse_ts(ts))
        snapshot = {**{k: rule.get(k) for k in RULE_FIELDS}, "id": rule["id"], "approved_by": rule["approved_by"],
                    "calendar": {"timezone": rule["timezone"], "weekend": ["Sat", "Sun"],
                                 "holidays_in_window": cal.holidays_between(cal.local_date(ts), cal.local_date(last))}}
        fields.update(rule_id=rule["id"], rule_snapshot=json.dumps(snapshot, sort_keys=True),
                      rule_hash=_timing_hash(rule), timezone=rule["timezone"],
                      response_due=due["response"] and due["response"].isoformat(timespec="seconds"),
                      completion_due=due["completion"] and due["completion"].isoformat(timespec="seconds"),
                      response_warn_at=due["response"] and cal.warning(due["response"], warn, unit).isoformat(
                          timespec="seconds"),
                      completion_warn_at=due["completion"] and cal.warning(due["completion"], warn, unit).isoformat(
                          timespec="seconds"),
                      note=None if rule["verification"] in USABLE else
                      f"service standard {rule['id']} is unverified: deadlines are provisional until an "
                      "administrator verifies or approves it")
    else:
        fields["note"] = (f"no service standard configured for {authority or 'an unknown authority'} "
                          f"({report.get('class_code')}, band {report.get('severity_band') or '?'}, road "
                          f"{report.get('road_class') or '?'}): an administrator must add one")
    conn.execute(f"INSERT INTO service_clocks ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                 list(fields.values()))
    clock = get_clock(conn, report["id"])
    _ledger(conn, clock, "report_submitted", actor, source, at=ts, note=fields["note"], report=report)
    return get_clock(conn, report["id"])


def start_clock_safely(conn: sqlite3.Connection, report: dict, actor: str) -> dict | None:
    """For the delivery path: the report has already reached the authority, so a tracking error must never undo
    that. The error is audited and the next sweep backfills the clock."""
    try:
        return start_clock(conn, report, actor)
    except Exception as e:  # noqa: BLE001
        log.exception("service clock for report %s failed", report.get("id"))
        audit(conn, actor, "service_clock_failed", f"report:{report.get('id')}", str(e)[:500])
        return None


def record_event(conn: sqlite3.Connection, report_id: int, event_type: str, actor: str, source: str,
                 at: str | None = None, authority_status: str | None = None, authority_notes: str | None = None,
                 note: str | None = None) -> dict | None:
    """Move a report through its lifecycle and write the ledger entry. Returns None when the event does not change
    anything (already at or past that stage), so polling the authority every hour adds nothing."""
    clock = get_clock(conn, report_id)
    if clock is None:
        return None
    at = at or now()
    before = compliance(clock, at)
    prev = clock["stage"]
    if event_type == "report_reopened":
        if prev not in ("repair_completed", "closed"):
            return None
        updates = {"stage": "reopened", "completed_at": None, "closed_at": None,
                   "reopened_count": clock["reopened_count"] + 1}
    elif event_type in EVENT_STAGE:
        stage = EVENT_STAGE[event_type]
        if RANK[stage] <= RANK[prev]:
            return None
        updates = {"stage": stage, EVENT_FIELD[event_type]: at}
        if not clock["acknowledged_at"]:
            updates["acknowledged_at"] = at  # any engagement by the authority implies it received the report
    else:
        raise ValueError(f"event must be one of {MANUAL_EVENTS}")
    if authority_status is not None:
        updates["authority_status"] = authority_status
    if authority_notes is not None:
        updates["authority_notes"] = authority_notes
    updates["updated_at"] = now()
    conn.execute(f"UPDATE service_clocks SET {', '.join(f'{k} = ?' for k in updates)} WHERE report_id = ?",
                 [*updates.values(), report_id])
    return _ledger(conn, {**clock, **updates}, event_type, actor, source, previous=prev, at=at, before=before,
                   note=note)


def _has(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)


def observe_authority(conn: sqlite3.Connection, report_id: int, status: str | None, notes: str | None, actor: str,
                      source: str) -> list[dict]:
    """Translate what the authority's system reports into lifecycle events (keywords in service_standards.json).
    'closed' means repaired unless the notes say otherwise (duplicate, not found...); anything else after a
    closure is a reopening. An automatic receipt ("Received") is not an acknowledgement."""
    clock = get_clock(conn, report_id)
    if clock is None:
        return []
    kw = config()["status_keywords"]
    s, n = (status or "").strip().lower(), (notes or "").strip().lower()
    events: list[tuple[str, str | None]] = []
    if s == "closed":
        if clock["stage"] not in ("repair_completed", "closed"):
            if _has(n, kw["closed_without_repair"]):
                events.append(("report_closed", "closed without a repair"))
            else:
                events += [("repair_completed", "repair inferred from the authority closing the request"),
                           ("report_closed", None)]
    elif s:
        if clock["stage"] in ("repair_completed", "closed"):
            events.append(("report_reopened", f"authority status changed from closed to {status}"))
        text = f"{s} {n}"
        for ev in ("repair_started", "inspection_started", "acknowledged"):
            if _has(text, kw[{"inspection_started": "inspection"}.get(ev, ev)]):
                events.append((ev, None))
                break
    out = []
    for ev, note in events:
        e = record_event(conn, report_id, ev, actor, source, authority_status=status, authority_notes=notes,
                         note=note)
        if e:
            out.append(e)
    clock = get_clock(conn, report_id)
    if not out and (status, notes) != (clock["authority_status"], clock["authority_notes"]):
        conn.execute("UPDATE service_clocks SET authority_status = ?, authority_notes = ?, updated_at = ? "
                     "WHERE report_id = ?", (status, notes, now(), report_id))
        out.append(_ledger(conn, {**clock, "authority_status": status, "authority_notes": notes},
                           "authority_status_changed", actor, source, previous=clock["stage"],
                           before=compliance(clock), note="authority status changed without a lifecycle step"))
    return out


# ---------------- tracking status ----------------
def compliance(clock: dict, at: str | datetime | None = None) -> dict:
    """The tracking status of one clock at `at` (default now). See STATUS_RULES."""
    t = parse_ts(at) if at else datetime.now(timezone.utc)
    resp, comp = _ts(clock.get("response_due")), _ts(clock.get("completion_due"))
    ack = _ts(clock.get("acknowledged_at"))
    done = _ts(clock.get("completed_at")) or _ts(clock.get("closed_at"))
    out = {"response_on_time": None if not (resp and ack) else ack <= resp, "next_due": None, "next_due_kind": None}
    if done:
        if comp is None:
            status, reason = UNKNOWN, "closed, but the standard sets no completion target"
        elif done <= comp:
            status, reason = MET, "completed on or before the completion deadline"
        else:
            status, reason = MISSED, "completed after the completion deadline"
    elif not clock.get("rule_id"):
        status, reason = UNKNOWN, clock.get("note") or "no service standard applies"
    else:
        pending = [(comp, "completion", _ts(clock.get("completion_warn_at")))] if comp else []
        if resp and not ack:
            pending.insert(0, (resp, "response", _ts(clock.get("response_warn_at"))))
        nxt = min(pending, key=lambda p: p[0]) if pending else None
        if nxt:
            out.update(next_due=nxt[0].isoformat(timespec="seconds"), next_due_kind=nxt[1])
        if comp and t > comp:
            status, reason = OVERDUE, "the completion deadline has passed" + (" (acknowledged)" if ack else "")
        elif resp and not ack and t > resp:
            status, reason = OVERDUE, "no acknowledgement by the response deadline"
        elif any(w and t > w for _, _, w in pending):
            status, reason = APPROACHING, f"inside the warning window before the {nxt[1]} deadline"
        elif not ack:
            status, reason = AWAITING, "the authority has not acknowledged the report yet"
        else:
            status, reason = WITHIN, "acknowledged and within the service standard"
    if clock.get("rule_id") and not _verified(clock) and status != UNKNOWN:
        out.update(status=UNKNOWN, provisional=status,
                   reason=f"provisional: {reason}; the applicable standard is unverified, so it is not counted")
        return out
    out.update(status=status, provisional=None, reason=reason)
    return out


def sweep(conn: sqlite3.Connection, actor: str = "scheduler", at: str | None = None) -> dict:
    """Backfill clocks for reports filed before tracking existed, then record deadline transitions (approaching,
    passed) in the ledger. Runs with every follow-up."""
    started = 0
    for r in conn.execute("SELECT r.* FROM reports r LEFT JOIN service_clocks c ON c.report_id = r.id "
                          "WHERE r.status IN ('submitted', 'fixed') AND c.report_id IS NULL").fetchall():
        r = dict(r)
        sent = conn.execute("SELECT ts FROM audit WHERE subject = ? AND action = 'status_submitted' ORDER BY id "
                            "LIMIT 1", (f"report:{r['id']}",)).fetchone()
        start_clock(conn, r, actor, submitted_at=sent[0] if sent else r["updated_at"], source="backfill")
        if r["status"] == "fixed":
            fixed = conn.execute("SELECT ts FROM audit WHERE subject = ? AND action = 'status_fixed' ORDER BY id "
                                 "LIMIT 1", (f"report:{r['id']}",)).fetchone()
            for ev in ("repair_completed", "report_closed"):
                record_event(conn, r["id"], ev, actor, "backfill", at=fixed[0] if fixed else r["updated_at"],
                             note="recorded before service-standard tracking existed")
        started += 1
    flagged = []
    for c in conn.execute("SELECT * FROM service_clocks WHERE completed_at IS NULL AND closed_at IS NULL").fetchall():
        c = dict(c)
        state = compliance(c, at)
        shown = state["provisional"] or state["status"]
        if shown == c["last_flag"]:
            continue
        if shown in (APPROACHING, OVERDUE):
            ev = "deadline_approaching" if shown == APPROACHING else "deadline_passed"
            _ledger(conn, c, ev, actor, "roadsense", previous=c["stage"], at=at, note=state["reason"])
            flagged.append({"report_id": c["report_id"], "event": ev})
        else:
            conn.execute("UPDATE service_clocks SET last_flag = ? WHERE report_id = ?", (shown, c["report_id"]))
    return {"backfilled": started, "flagged": flagged}


# ---------------- dashboard ----------------
def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def dashboard(conn: sqlite3.Connection, filters: dict | None = None, at: str | None = None) -> dict:
    ensure_seeded(conn)
    f = {k: v for k, v in (filters or {}).items() if v}
    t = at or now()
    items, cals = [], {}
    for c in conn.execute("SELECT c.*, r.class_code, r.class_name, r.severity_band, r.road_class, r.road_name, "
                          "r.address, r.status AS report_status FROM service_clocks c "
                          "JOIN reports r ON r.id = c.report_id ORDER BY c.submitted_at DESC"):
        c = dict(c)
        state = compliance(c, t)
        snap = json.loads(c["rule_snapshot"]) if c["rule_snapshot"] else None
        unit = snap["time_unit"] if snap else "business_days"
        cal = cals.setdefault((c["authority"], c["timezone"]), calendar(conn, c["authority"], c["timezone"]))
        days_left = None
        if state["next_due"]:
            due = state["next_due"]
            days_left = cal.elapsed(t, due, unit) if parse_ts(due) >= parse_ts(t) else -cal.elapsed(due, t, unit)
        ack, done = c["acknowledged_at"], c["completed_at"]
        items.append({
            "report_id": c["report_id"], "authority": c["authority"], "neighbourhood": c["neighbourhood"],
            "class_code": c["class_code"], "class_name": c["class_name"], "priority": c["severity_band"],
            "road_class": c["road_class"], "address": c["address"], "report_status": c["report_status"],
            "stage": c["stage"], "authority_status": c["authority_status"], "authority_notes": c["authority_notes"],
            "submitted_at": c["submitted_at"], "response_due": c["response_due"],
            "completion_due": c["completion_due"], "acknowledged_at": ack, "completed_at": done,
            "closed_at": c["closed_at"], "reopened_count": c["reopened_count"],
            "status": state["status"], "provisional": state["provisional"], "reason": state["reason"],
            "next_due": state["next_due"], "next_due_kind": state["next_due_kind"], "days_left": days_left,
            "time_unit": unit, "response_on_time": state["response_on_time"],
            "rule": {"id": c["rule_id"], "verification": snap.get("verification") if snap else None,
                     "verified_later": bool(c["rule_verified_at"]),
                     "response_days": snap.get("response_days") if snap else None,
                     "completion_days": snap.get("completion_days") if snap else None},
            "response_days_taken": cal.elapsed(c["submitted_at"], ack, unit) if ack else None,
            "response_hours": round((parse_ts(ack) - parse_ts(c["submitted_at"])).total_seconds() / 3600, 1)
            if ack else None,
            "completion_days_taken": cal.elapsed(c["submitted_at"], done, unit) if done else None,
            "completion_hours": round((parse_ts(done) - parse_ts(c["submitted_at"])).total_seconds() / 3600, 1)
            if done else None,
        })
    options = {k: sorted({i[k] for i in items if i[k]}) for k in ("authority", "neighbourhood", "class_code",
                                                                  "priority")}

    def keep(i: dict) -> bool:
        local = i["submitted_at"]  # ISO UTC: the date filter compares local submission dates
        day = Calendar(config()["timezone"]).local_date(local).isoformat()
        return all([
            not f.get("authority") or i["authority"] == f["authority"],
            not f.get("neighbourhood") or (i["neighbourhood"] or "") == f["neighbourhood"],
            not f.get("damage") or i["class_code"] == f["damage"],
            not f.get("priority") or i["priority"] == f["priority"],
            not f.get("status") or i["status"] == f["status"],
            not f.get("date_from") or day >= f["date_from"],
            not f.get("date_to") or day <= f["date_to"]])

    shown = [i for i in items if keep(i)]
    count = {s: sum(i["status"] == s for i in shown) for s in STATUSES}
    responded = [i for i in shown if i["response_hours"] is not None]
    repaired = [i for i in shown if i["completion_hours"] is not None]  # unresolved reports never count here
    order = {s: n for n, s in enumerate(STATUSES)}
    shown.sort(key=lambda i: (order[i["status"]], i["next_due"] or "9999", -i["report_id"]))
    return {
        "generated_at": t, "filters": f, "options": options, "items": shown,
        "metrics": {
            "total": len(shown),
            "active": sum(not (i["completed_at"] or i["closed_at"]) for i in shown),
            **count,
            "response_late": sum(i["response_on_time"] is False for i in shown),
            "avg_response_hours": _avg([i["response_hours"] for i in responded]),
            "avg_response_days": _avg([i["response_days_taken"] for i in responded]),
            "responded": len(responded),
            "avg_completion_hours": _avg([i["completion_hours"] for i in repaired]),
            "avg_completion_days": _avg([i["completion_days_taken"] for i in repaired]),
            "repaired": len(repaired),
        },
        "definitions": STATUS_RULES, "disclaimer": DISCLAIMER,
        "method": ("Deadlines: end of the N-th business (or calendar) day after the local day of submission, in the "
                   "authority's timezone, skipping weekends and configured holidays. Averages use only reports that "
                   "reached that step; days are counted the same way as deadlines."),
    }


def timeline(conn: sqlite3.Connection, report_id: int, at: str | None = None) -> dict:
    """Everything the duty-of-care view shows for one report: clock, status, rule snapshot, ledger and integrity."""
    clock = get_clock(conn, report_id)
    report = row(conn.execute("SELECT id, class_code, class_name, severity_band, road_class, address, jurisdiction, "
                              "status, service_request_id, channel, best_event_id, submitted_payload_hash "
                              "FROM reports WHERE id = ?", (report_id,)).fetchone())
    if report is None:
        raise LookupError(f"unknown report {report_id}")
    return {"report": report, "clock": clock, "tracking": compliance(clock, at) if clock else None,
            "rule": json.loads(clock["rule_snapshot"]) if clock and clock["rule_snapshot"] else None,
            "ledger": [{k: e[k] for k in ("id", "ts", "actor", "event_type", "data", "hash", "prev_hash")}
                       for e in ledger.entries(conn, report_id)],
            "integrity": ledger.verify(conn, report_id), "manual_events": MANUAL_EVENTS,
            "disclaimer": DISCLAIMER}
