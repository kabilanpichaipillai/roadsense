"""Duty-of-care ledger: the report-level history of what happened, when, and against which service standard.

It is not a second audit system. Ledger entries are rows of the existing append-only `audit` table (action
`ledger_<event>`, subject `report:<id>`), so they show up in the activity log and inherit its UPDATE/DELETE triggers.
What a ledger row adds is integrity:

    hash      = SHA-256 of the canonical entry (open311.payload_hash: sorted-key JSON), covering
                ts, actor, action, subject, the exact stored detail text and prev_hash
    prev_hash = the hash of the previous ledger row of the same report ("0" * 64 for the first)

so each report's history is a hash chain. A unique index on (subject, prev_hash) refuses a fork, and
verify() recomputes every hash and link. The service clock keeps the chain head, so removing the newest entries
is detected too.

SHA-256 does not make the database immutable on its own: the triggers stop casual edits through SQLite, the file
should be writable by the service account only, and the chain head should be copied somewhere RoadSense cannot
write (an export, an email to the records office) when the evidence matters. See docs/service-standards.md.
"""
from __future__ import annotations

import json
import sqlite3

from ..common.db import now
from .open311 import payload_hash

GENESIS = "0" * 64
PREFIX = "ledger_"


def entry_hash(ts: str, actor: str, action: str, subject: str, detail: str, prev_hash: str) -> str:
    return payload_hash({"ts": ts, "actor": actor, "action": action, "subject": subject, "detail": detail,
                         "prev_hash": prev_hash})


def head(conn: sqlite3.Connection, subject: str) -> str:
    r = conn.execute("SELECT hash FROM audit WHERE subject = ? AND hash IS NOT NULL ORDER BY id DESC LIMIT 1",
                     (subject,)).fetchone()
    return r[0] if r else GENESIS


def append(conn: sqlite3.Connection, report_id: int, event_type: str, actor: str, detail: dict,
           ts: str | None = None) -> dict:
    """Add one entry to a report's chain. Takes the write lock first, so two processes cannot both extend the same
    head (the unique index would refuse the second one anyway)."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    subject = f"report:{report_id}"
    prev = head(conn, subject)
    ts = ts or now()
    action = PREFIX + event_type
    text = json.dumps(detail, sort_keys=True, default=str)  # stored exactly as hashed: never truncated
    digest = entry_hash(ts, actor, action, subject, text, prev)
    cur = conn.execute("INSERT INTO audit (ts, actor, action, subject, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                       (ts, actor, action, subject, text, prev, digest))
    return {"id": cur.lastrowid, "ts": ts, "actor": actor, "event_type": event_type, "hash": digest,
            "prev_hash": prev, **detail}


def entries(conn: sqlite3.Connection, report_id: int) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM audit WHERE subject = ? AND hash IS NOT NULL ORDER BY id",
                          (f"report:{report_id}",)):
        d = dict(r)
        d["event_type"] = d["action"].removeprefix(PREFIX)
        try:
            d["data"] = json.loads(d["detail"])
        except (TypeError, ValueError):
            d["data"] = None
        out.append(d)
    return out


def verify(conn: sqlite3.Connection, report_id: int | None = None) -> dict:
    """Recompute every hash and link (of one report, or all). Returns ok plus each problem found."""
    sql = "SELECT * FROM audit WHERE hash IS NOT NULL"
    args: tuple = ()
    if report_id is not None:
        sql += " AND subject = ?"
        args = (f"report:{report_id}",)
    expected: dict[str, str] = {}
    problems, count = [], 0
    for r in conn.execute(sql + " ORDER BY id", args):
        count += 1
        subject = r["subject"]
        if r["prev_hash"] != expected.get(subject, GENESIS):
            problems.append({"id": r["id"], "subject": subject, "problem": "broken link: an entry before this one "
                             "is missing or was changed"})
        if entry_hash(r["ts"], r["actor"], r["action"], subject, r["detail"], r["prev_hash"]) != r["hash"]:
            problems.append({"id": r["id"], "subject": subject, "problem": "hash mismatch: this entry was altered"})
        expected[subject] = r["hash"]
    # the clock remembers each chain's head: entries removed from the end break this check
    clocks = conn.execute("SELECT report_id, ledger_head FROM service_clocks" +
                          (" WHERE report_id = ?" if report_id is not None else ""),
                          (report_id,) if report_id is not None else ()).fetchall()
    for c in clocks:
        subject = f"report:{c['report_id']}"
        if c["ledger_head"] and expected.get(subject, GENESIS) != c["ledger_head"]:
            problems.append({"id": None, "subject": subject, "problem": "the newest ledger entries are missing"})
    return {"ok": not problems, "entries": count, "reports": len({*expected, *(f"report:{c[0]}" for c in clocks)}),
            "problems": problems, "head": expected.get(f"report:{report_id}") if report_id is not None else None,
            "algorithm": "SHA-256 over sorted-key JSON of ts, actor, action, subject, detail, prev_hash"}
