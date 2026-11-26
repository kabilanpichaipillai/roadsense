#!/usr/bin/env python
"""Keyless demo: replay a real, recorded run of the RoadSense agents on a fixed demo trip.

    python scripts/demo.py               # replay demo/llm_cache.json: no API key, no network calls to an LLM
    python scripts/demo.py --record      # live run with OPENROUTER_API_KEY (~17 requests), saves demo/llm_cache.json
    python scripts/demo.py --no-llm      # rule engine only (plumbing check)

The demo trip (demo/trip/, built by scripts/build_demo_trip.py) has six events with fixed ids covering every agent
decision: real damage on Halifax streets, Highway 102 (provincial), Bedford Highway (ambiguous numbered route), a
likely false alarm, and a second driver's sighting that must merge. It then adds a demo reviewer and inspector and
moves eight more reports through the municipal workflow, so every status is on screen (one overdue, one reopened
by a later trip). Each run starts from a fresh data/demo folder
with offline geocoding, so the agents see exactly the same tool results as when the run was recorded, and the
replayed answers match. To browse the result afterwards:

    ROADSENSE_DATA_DIR=data/demo ROADSENSE_OFFLINE_GEO=1 python scripts/run_all.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
DATA = ROOT / "data" / "demo"
CACHE = DEMO / "llm_cache.json"
TRIPS = ("demo-halifax", "demo-halifax-2")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--record", action="store_true", help="live LLM run; saves the responses for replay")
    p.add_argument("--no-llm", action="store_true", help="rule engine only")
    args = p.parse_args()
    if DATA.exists():
        shutil.rmtree(DATA)
    os.environ.update(ROADSENSE_DATA_DIR=str(DATA), ROADSENSE_OFFLINE_GEO="1",
                      ROADSENSE_LLM_MODE="cache" if args.record else "replay",
                      ROADSENSE_USE_LLM="0" if args.no_llm else "1")
    sys.path.insert(0, str(ROOT))
    from roadsense.agents import orchestrator
    from roadsense.agents.llm import CACHE_SCHEMA
    from roadsense.agents.mcp_client import Toolbox
    from roadsense.common.config import settings
    from roadsense.common.db import now, session

    if not args.record and not args.no_llm and not CACHE.is_file():
        raise SystemExit("demo/llm_cache.json not found: record it first with --record (needs an OpenRouter key)")
    with session() as conn:
        conn.execute(CACHE_SCHEMA)
        if not args.record and CACHE.is_file():
            rows = json.loads(CACHE.read_text(encoding="utf-8"))
            conn.executemany("INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?, ?)",
                             [(r["key"], r["model"], r["message"], r["usage"]) for r in rows])
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        for f in sorted((DEMO / "trip").glob("demo-*.json")):
            e = json.loads(f.read_text(encoding="utf-8"))
            image = settings.media_dir / f"{e['id']}.jpg"
            shutil.copy(f.with_suffix(".jpg"), image)
            conn.execute(
                "INSERT INTO events (id, trip_id, track_id, class_code, class_name, confidence, frames_seen,"
                " box_area_ratio, box, lat, lon, heading, captured_at, image_path, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e["id"], e["trip_id"], e["track_id"], e["class_code"], e["class_name"], e["confidence"],
                 e["frames_seen"], e["box_area_ratio"], json.dumps(e["box"]), e["lat"], e["lon"], e["heading"],
                 e["captured_at"], str(image), now()))

    async def run():
        async with Toolbox() as tb:
            return [await orchestrator.process_trip(tb, t, settings.llm_enabled) for t in TRIPS]

    results = asyncio.run(run())
    for r in results:
        print(f"\n=== {r['trip_id']}: mode {r['mode']}, fallback rate {r.get('fallback_rate')}, "
              f"{r.get('seconds')} s, llm {r.get('llm')}")
        print(r["summary"])
    replay_ok = all(r["mode"] == "agents" and r.get("fallback_rate") == 0 for r in results)
    seed_workflow()
    if args.record:
        with session() as conn:
            rows = [dict(x) for x in conn.execute("SELECT key, model, message, usage FROM llm_cache")]
        if not rows or not replay_ok:  # a failed run (no key, verifier out of memory) must not erase a good recording
            raise SystemExit(f"\nnot saved: the live run was incomplete ({len(rows)} responses, rule engine had to "
                             f"step in); {CACHE.relative_to(ROOT)} is unchanged")
        CACHE.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\nsaved {len(rows)} recorded responses to {CACHE.relative_to(ROOT)}")
    if not args.record and not args.no_llm:
        print("\nreplay:", "complete (every agent step served from the recording)" if replay_ok else
              "INCOMPLETE: some steps were not in the recording and fell back to the rule engine")
        sys.exit(0 if replay_ok else 1)


# ---------------- the municipal workflow: staff accounts and a report in every status ----------------
# extra potholes on downtown Halifax streets, shown with the demo trip's images: (scenario, lat, lon, image)
WORKFLOW_SPOTS = [
    ("awaiting_inspection", 44.64755, -63.57290, "demo-01"), ("inspection_assigned", 44.64880, -63.58010, "demo-02"),
    ("repair_needed", 44.65105, -63.59250, "demo-04"), ("overdue", 44.64600, -63.56900, "demo-01"),
    ("repaired", 44.65300, -63.58700, "demo-02"), ("reopened", 44.64200, -63.58100, "demo-04"),
    ("no_repair_needed", 44.65450, -63.57600, "demo-02"), ("rejected", 44.64990, -63.56650, "demo-01"),
]
EVENT_SQL = ("INSERT INTO events (id, trip_id, track_id, class_code, class_name, confidence, frames_seen, box_area_ratio,"
             " box, lat, lon, heading, captured_at, image_path, status, verified_confidence, created_at)"
             " VALUES (?, ?, ?, 'D40', 'Pothole', ?, 6, 0.04, '[300, 220, 520, 400]', ?, ?, 90.0, ?, ?, 'verified', ?, ?)")


def seed_workflow() -> None:
    """Demo staff (a reviewer and an inspector; passwords in data/demo/demo_users.txt) and one report in every
    workflow status, moved there through core.workflow with back-dated steps: one overdue, one reopened because a
    later trip saw the patched pothole again."""
    import secrets
    from datetime import datetime, timedelta, timezone

    from roadsense.common.config import settings
    from roadsense.common.db import now, session
    from roadsense.core import reports, service_standards, workflow
    from roadsense.web.auth import hash_password

    hrm = "Halifax Regional Municipality"
    t0 = datetime.now(timezone.utc)

    def ago(days):
        return (t0 - timedelta(days=days)).isoformat(timespec="seconds")

    def in_days(days):
        return (t0 + timedelta(days=days)).date().isoformat()

    from roadsense.web.auth import bootstrap_admin

    bootstrap_admin()  # the first admin first: it is only created while there are no users
    lines, ids, made = [], {}, {}
    with session() as conn:
        for email, name, role in (("reviewer@roadsense.local", "Riley Reviewer", "reviewer"),
                                  ("inspector@roadsense.local", "Ira Inspector", "inspector")):
            password = secrets.token_urlsafe(10) + "Aa1"
            conn.execute("INSERT OR IGNORE INTO users (email, name, role, password_hash, created_at) "
                         "VALUES (?, ?, ?, ?, ?)", (email, name, role, hash_password(password), now()))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
            lines.append(f"{role}: {email} / {password}")
        for rule in service_standards.list_rules(conn):  # an admin adopted the city's published standards
            if rule["authority"] == hrm and rule["verification"] == "unverified":
                service_standards.update_rule(conn, rule["id"], {"verification": "approved"}, "demo-setup")
        for n, (kind, lat, lon, image) in enumerate(WORKFLOW_SPOTS, 1):
            eid = f"demo-wf-{n:02d}"
            photo = settings.media_dir / f"{eid}.jpg"
            photo.write_bytes((settings.media_dir / f"{image}.jpg").read_bytes())
            seen = ago(22 - n)
            conn.execute(EVENT_SQL, (eid, "demo-earlier", n, 0.82, lat, lon, seen, str(photo), 0.86, seen))
            est = reports.estimate_severity("D40", 0.04, 0.86, 6, 0.3)
            made[kind] = reports.create_or_merge(
                conn, eid, est["severity"], f"Halifax street near {lat:.4f}, {lon:.4f}", hrm, settings.dedupe_radius_m,
                actor="demo-setup", jurisdiction=hrm, band=est["band"])["report"]["id"]
        insp = {"inspector_id": ids["inspector"]}
        plan = {  # scenario: (action, days ago, fields); the inspector records outcomes and repairs
            "awaiting_inspection": [("approve", 3, {})],
            "inspection_assigned": [("approve", 5, {}), ("assign", 4, insp)],
            "repair_needed": [("approve", 8, {}), ("assign", 7, insp),
                              ("repair_needed", 6, {"repair_eta": in_days(6), "notes": "Edge breaking up"})],
            "overdue": [("approve", 19, {}), ("assign", 18, insp),
                        ("repair_needed", 17, {"repair_eta": in_days(-4), "severity_band": "S3",
                                               "notes": "Deep pothole in the bus lane"})],
            "repaired": [("approve", 15, {}), ("assign", 14, insp), ("repair_needed", 13, {"repair_eta": in_days(-6)}),
                         ("repaired", 8, {"notes": "Hot-mix patch"})],
            "reopened": [("approve", 16, {}), ("assign", 15, insp), ("repair_needed", 14, {"repair_eta": in_days(-9)}),
                         ("repaired", 10, {"notes": "Cold patch"})],
            "no_repair_needed": [("approve", 9, {}), ("assign", 8, insp),
                                 ("no_repair_needed", 6, {"notes": "Surface wear only, no structural damage"})],
            "rejected": [("reject", 12, {"reason": "Shadow of a manhole cover, not damage"})],
        }
        for kind, steps in plan.items():
            for action, days, fields in steps:
                field_work = action in ("repair_needed", "no_repair_needed", "repaired")
                workflow.transition(conn, made[kind], action,
                                    "inspector@roadsense.local" if field_work else "reviewer@roadsense.local",
                                    "inspector" if field_work else "reviewer", at=ago(days),
                                    user_id=ids["inspector"] if field_work else ids["reviewer"], **fields)
        # a later trip sees the patched pothole again: RoadSense itself reopens the report
        _, lat, lon, _ = WORKFLOW_SPOTS[5]
        photo = settings.media_dir / "demo-wf-again.jpg"
        photo.write_bytes((settings.media_dir / "demo-wf-06.jpg").read_bytes())
        conn.execute(EVENT_SQL, ("demo-wf-again", "demo-halifax-2", 99, 0.8, lat + 3 / 111_320, lon, ago(1), str(photo),
                                 0.84, ago(1)))
        out = reports.create_or_merge(conn, "demo-wf-again", "high", "x", hrm, settings.dedupe_radius_m,
                                      actor="agent:triage", jurisdiction=hrm)
        assert out["action"] == "reopened", out["action"]
        service_standards.sweep(conn)
    (DATA / "demo_users.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nworkflow demo: a report in every status; staff sign-ins in {DATA / 'demo_users.txt'}")


if __name__ == "__main__":
    main()
