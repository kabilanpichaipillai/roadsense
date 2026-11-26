"""Conversations, scoped to their owner. Every read and delete checks the owner: one person never sees another's."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3

from ..common.db import now

ID_RE = re.compile(r"^[a-f0-9]{16}$")
MAX_MESSAGES = 200       # per conversation; older turns stay, new ones are refused past this
MAX_CONVERSATIONS = 50   # listed per person


def valid_id(cid: str | None) -> bool:
    return bool(cid and ID_RE.match(cid))


def get(conn: sqlite3.Connection, cid: str, owner: str) -> dict | None:
    if not valid_id(cid):
        return None
    r = conn.execute("SELECT * FROM assistant_conversations WHERE id = ? AND owner = ?", (cid, owner)).fetchone()
    return dict(r) if r else None


def create(conn: sqlite3.Connection, owner: str, title: str) -> dict:
    cid = secrets.token_hex(8)
    ts = now()
    conn.execute("INSERT INTO assistant_conversations (id, owner, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                 (cid, owner, title[:80], ts, ts))
    return {"id": cid, "owner": owner, "title": title[:80], "created_at": ts, "updated_at": ts}


def count_messages(conn: sqlite3.Connection, cid: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM assistant_messages WHERE conversation_id = ?", (cid,)).fetchone()[0]


def add(conn: sqlite3.Connection, cid: str, role: str, content: str, **meta) -> int:
    enc = {k: json.dumps(v, default=str) if isinstance(v, (list, dict)) else v for k, v in meta.items()}
    cur = conn.execute(
        "INSERT INTO assistant_messages (conversation_id, role, content, intent, spec, result_ids, segment_ids, "
        "warnings, understanding, retrieved_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, role, content, enc.get("intent"), enc.get("spec"), enc.get("result_ids"), enc.get("segment_ids"),
         enc.get("warnings"), enc.get("understanding"), enc.get("retrieved_at"), now()))
    conn.execute("UPDATE assistant_conversations SET updated_at = ? WHERE id = ?", (now(), cid))
    return cur.lastrowid


def _decode(m: dict) -> dict:
    for k in ("spec", "result_ids", "segment_ids", "warnings"):
        if m.get(k):
            try:
                m[k] = json.loads(m[k])
            except ValueError:
                m[k] = None
    return m


def last_answer(conn: sqlite3.Connection, cid: str) -> dict | None:
    """The previous assistant turn: what a follow-up ("those roads", "the first three") refers to."""
    r = conn.execute("SELECT * FROM assistant_messages WHERE conversation_id = ? AND role = 'assistant' "
                     "AND intent NOT IN ('unknown', 'unsupported_action') ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    return _decode(dict(r)) if r else None


def messages(conn: sqlite3.Connection, cid: str) -> list[dict]:
    return [_decode(dict(m)) for m in conn.execute(
        "SELECT id, role, content, intent, spec, result_ids, segment_ids, warnings, understanding, retrieved_at, "
        "created_at FROM assistant_messages WHERE conversation_id = ? ORDER BY id", (cid,))]


def listing(conn: sqlite3.Connection, owner: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT c.id, c.title, c.created_at, c.updated_at, (SELECT COUNT(*) FROM assistant_messages m "
        "WHERE m.conversation_id = c.id AND m.role = 'user') AS questions FROM assistant_conversations c "
        "WHERE c.owner = ? ORDER BY c.updated_at DESC LIMIT ?", (owner, MAX_CONVERSATIONS))]


def delete(conn: sqlite3.Connection, cid: str, owner: str) -> bool:
    if not get(conn, cid, owner):
        return False
    conn.execute("DELETE FROM assistant_messages WHERE conversation_id = ?", (cid,))
    conn.execute("DELETE FROM assistant_conversations WHERE id = ? AND owner = ?", (cid, owner))
    return True
