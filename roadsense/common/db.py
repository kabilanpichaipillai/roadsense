"""SQLite store shared by the agent API and the MCP servers (separate processes, so WAL mode)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    track_id INTEGER,
    class_code TEXT NOT NULL,
    class_name TEXT NOT NULL,
    confidence REAL NOT NULL,
    frames_seen INTEGER NOT NULL,
    box_area_ratio REAL NOT NULL,
    box TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    heading REAL,
    captured_at TEXT NOT NULL,
    image_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    verified_confidence REAL,
    report_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id TEXT NOT NULL,
    class_code TEXT NOT NULL,
    class_name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    address TEXT,
    municipality TEXT,
    severity TEXT NOT NULL,
    priority REAL NOT NULL,
    confirmations INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    description TEXT,
    best_event_id TEXT,
    service_request_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    subject TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    report_id INTEGER,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geocache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    role TEXT NOT NULL,
    password_hash TEXT,
    oidc_subject TEXT UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT,
    result TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    channel TEXT,
    ok INTEGER NOT NULL,
    detail TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_requests (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT,
    status TEXT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    description TEXT,
    reported_at TEXT,
    closed_at TEXT,
    report_id INTEGER,
    raw TEXT,
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS external_pos ON external_requests (lat, lon);
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    report_id INTEGER,
    label TEXT NOT NULL,
    class_code TEXT,
    actor TEXT NOT NULL,
    ts TEXT NOT NULL
);
-- ---- freeze-thaw pothole forecast ----
CREATE INDEX IF NOT EXISTS events_class_time ON events (class_code, captured_at);
CREATE TABLE IF NOT EXISTS road_segments (
    id TEXT PRIMARY KEY,
    road_name TEXT NOT NULL,
    road_class TEXT,
    municipality TEXT,
    location TEXT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segment_events (
    segment_id TEXT NOT NULL REFERENCES road_segments (id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS segment_events_segment ON segment_events (segment_id);
CREATE TABLE IF NOT EXISTS weather_daily (
    cell TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    station_id TEXT,
    station_name TEXT,
    t_min REAL,
    t_max REAL,
    t_mean REAL,
    precip_mm REAL,
    snow_cm REAL,
    precip_chance REAL,
    precip_expected INTEGER,
    source TEXT NOT NULL,
    issued_at TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (cell, date, kind)
);
CREATE TABLE IF NOT EXISTS weather_fetches (
    cell TEXT NOT NULL,
    kind TEXT NOT NULL,
    range_start TEXT,
    range_end TEXT,
    ok INTEGER NOT NULL,
    error TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (cell, kind)
);
CREATE TABLE IF NOT EXISTS forecast_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'live',
    model_kind TEXT NOT NULL DEFAULT 'heuristic',
    as_of TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    segments INTEGER NOT NULL DEFAULT 0,
    weather_status TEXT,
    actor TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS forecast_runs_kind ON forecast_runs (kind, created_at);
CREATE TABLE IF NOT EXISTS freeze_thaw_indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES forecast_runs (id) ON DELETE CASCADE,
    segment_id TEXT NOT NULL REFERENCES road_segments (id) ON DELETE CASCADE,
    span TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    days INTEGER NOT NULL,
    confirmed_cycles INTEGER NOT NULL,
    potential_cycles INTEGER NOT NULL,
    missing_days INTEGER NOT NULL,
    wet_days INTEGER NOT NULL,
    moist_cycles INTEGER NOT NULL,
    source TEXT,
    computed_at TEXT NOT NULL,
    UNIQUE (run_id, segment_id, span)
);
CREATE TABLE IF NOT EXISTS risk_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES forecast_runs (id) ON DELETE CASCADE,
    segment_id TEXT NOT NULL REFERENCES road_segments (id) ON DELETE CASCADE,
    run_kind TEXT NOT NULL DEFAULT 'live',
    score REAL NOT NULL,
    category TEXT NOT NULL,
    data_quality REAL NOT NULL,
    insufficient_data INTEGER NOT NULL DEFAULT 0,
    explanation TEXT NOT NULL,
    data_sources TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    predicted_at TEXT NOT NULL,
    UNIQUE (segment_id, period_start, horizon_days, config_hash, run_kind)
);
CREATE INDEX IF NOT EXISTS risk_predictions_run ON risk_predictions (run_id, category);
CREATE TABLE IF NOT EXISTS prediction_factors (
    prediction_id INTEGER NOT NULL REFERENCES risk_predictions (id) ON DELETE CASCADE,
    factor TEXT NOT NULL,
    raw TEXT,
    value REAL,
    weight REAL NOT NULL,
    contribution REAL NOT NULL,
    coverage REAL NOT NULL,
    available INTEGER NOT NULL,
    PRIMARY KEY (prediction_id, factor)
);
CREATE TABLE IF NOT EXISTS prediction_outcomes (
    prediction_id INTEGER PRIMARY KEY REFERENCES risk_predictions (id) ON DELETE CASCADE,
    outcome TEXT NOT NULL,
    evidence_event_id TEXT,
    observed_at TEXT,
    evaluated_at TEXT NOT NULL
);
-- frames the edge kept where it passed a closed report: evidence that the defect is (or is not) still there.
-- One redacted frame per watched spot, never a track; lat/lon is the car position, cleared by the retention policy.
CREATE TABLE IF NOT EXISTS road_passes (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    report_id INTEGER REFERENCES reports(id),
    lat REAL,
    lon REAL,
    heading REAL,
    captured_at TEXT NOT NULL,
    image_path TEXT NOT NULL,
    box TEXT,
    sharpness REAL,
    brightness REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS road_passes_trip ON road_passes (trip_id);
-- independent repair verification history: one row per (report, new observation), append-only like the audit log
CREATE TABLE IF NOT EXISTS repair_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    original_event_id TEXT REFERENCES events(id),
    observation_kind TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    trip_id TEXT,
    observed_at TEXT,
    verified_at TEXT NOT NULL,
    distance_m REAL,
    original_class TEXT,
    observed_class TEXT,
    confidence REAL,
    quality TEXT,
    clean INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL CHECK (result IN ('verified', 'not_verified', 'inconclusive')),
    reason TEXT NOT NULL,
    original_image TEXT,
    followup_image TEXT,
    evidence_hash TEXT NOT NULL,
    UNIQUE (report_id, observation_id)
);
CREATE INDEX IF NOT EXISTS repair_verifications_report ON repair_verifications (report_id, id);
CREATE TRIGGER IF NOT EXISTS repair_verifications_no_update BEFORE UPDATE ON repair_verifications
BEGIN SELECT RAISE(ABORT, 'verification history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS repair_verifications_no_delete BEFORE DELETE ON repair_verifications
BEGIN SELECT RAISE(ABORT, 'verification history is append-only'); END;
-- ---- exposure-weighted risk (per report) and repair equity ----
-- OSM features near a report, one row per OSM element (an element is counted once, in one category)
CREATE TABLE IF NOT EXISTS exposure_features (
    report_id INTEGER NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
    osm_type TEXT NOT NULL,
    osm_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    name TEXT,
    distance_m REAL NOT NULL,
    relation TEXT NOT NULL,
    relevance REAL NOT NULL,
    tags TEXT,
    PRIMARY KEY (report_id, osm_type, osm_id)
);
CREATE INDEX IF NOT EXISTS exposure_features_category ON exposure_features (category, report_id);
-- the latest risk calculation per report: physical severity, exposure and overall kept as separate values
CREATE TABLE IF NOT EXISTS report_risk (
    report_id INTEGER PRIMARY KEY REFERENCES reports (id) ON DELETE CASCADE,
    physical_score REAL NOT NULL,
    exposure_score REAL,
    overall_score REAL NOT NULL,
    category TEXT NOT NULL,
    data_quality REAL NOT NULL,
    factors TEXT NOT NULL,
    explanation TEXT NOT NULL,
    osm_status TEXT NOT NULL,
    osm_source TEXT,
    osm_fetched_at TEXT,
    neighbourhood TEXT,
    rules_hash TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS report_risk_category ON report_risk (category, overall_score);
-- which neighbourhood a repair record (a city service request or a RoadSense report) belongs to, and on what evidence
CREATE TABLE IF NOT EXISTS neighbourhood_assignments (
    record_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    neighbourhood TEXT,
    source TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (record_kind, record_id)
);
CREATE INDEX IF NOT EXISTS neighbourhood_assignments_name ON neighbourhood_assignments (neighbourhood);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
-- published service standards (core.service_standards): configuration, edited by admins, never deleted
CREATE TABLE IF NOT EXISTS service_standards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    authority TEXT NOT NULL,
    jurisdiction TEXT,
    road_category TEXT,
    damage_category TEXT,
    priority TEXT,
    response_days INTEGER,
    completion_days INTEGER,
    time_unit TEXT NOT NULL DEFAULT 'business_days',
    timezone TEXT NOT NULL DEFAULT 'America/Halifax',
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    source_url TEXT,
    verification TEXT NOT NULL DEFAULT 'unverified',
    last_reviewed TEXT,
    approved_by TEXT,
    notes TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS service_standards_no_delete BEFORE DELETE ON service_standards
BEGIN SELECT RAISE(ABORT, 'service standards are retired with effective_to, never deleted'); END;
CREATE TABLE IF NOT EXISTS holidays (
    day TEXT NOT NULL,
    authority TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    PRIMARY KEY (day, authority)
);
-- one service clock per report filed with an authority: the rule snapshot and deadlines are frozen at submission
CREATE TABLE IF NOT EXISTS service_clocks (
    report_id INTEGER PRIMARY KEY,
    authority TEXT,
    rule_id INTEGER,
    rule_snapshot TEXT,
    rule_hash TEXT,
    timezone TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    response_due TEXT,
    response_warn_at TEXT,
    completion_due TEXT,
    completion_warn_at TEXT,
    stage TEXT NOT NULL DEFAULT 'submitted',
    acknowledged_at TEXT,
    inspection_at TEXT,
    repair_started_at TEXT,
    completed_at TEXT,
    closed_at TEXT,
    reopened_count INTEGER NOT NULL DEFAULT 0,
    authority_status TEXT,
    authority_notes TEXT,
    neighbourhood TEXT,
    note TEXT,
    rule_verified_at TEXT,
    last_flag TEXT,
    ledger_head TEXT,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS service_clocks_no_delete BEFORE DELETE ON service_clocks
BEGIN SELECT RAISE(ABORT, 'service clocks are evidence and cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS service_clocks_frozen BEFORE UPDATE ON service_clocks
WHEN OLD.submitted_at IS NOT NEW.submitted_at OR OLD.response_due IS NOT NEW.response_due
  OR OLD.completion_due IS NOT NEW.completion_due OR OLD.rule_id IS NOT NEW.rule_id
  OR OLD.rule_snapshot IS NOT NEW.rule_snapshot OR OLD.rule_hash IS NOT NEW.rule_hash
BEGIN SELECT RAISE(ABORT, 'a report''s service standard and deadlines are frozen at submission'); END;
"""

# columns added after the first schema; applied to existing databases on connect
MIGRATIONS = {
    "events": {"car_lat": "REAL", "car_lon": "REAL", "distance_m": "REAL", "width_m": "REAL",
               "position_uncertainty_m": "REAL", "verifier_class": "TEXT", "recheck_confidence": "REAL",
               "class_disputed": "INTEGER NOT NULL DEFAULT 0", "rationale": "TEXT", "snap_m": "REAL",
               "jolt_g": "REAL", "server_redacted": "INTEGER NOT NULL DEFAULT 0", "suppressed_by": "INTEGER",
               "road_class": "TEXT", "road_name": "TEXT"},
    "reports": {"jurisdiction": "TEXT", "channel": "TEXT", "width_m": "REAL",
                "class_disputed": "INTEGER NOT NULL DEFAULT 0", "rationale": "TEXT", "draft_description": "TEXT",
                "approved_payload_hash": "TEXT", "submitted_payload_hash": "TEXT",
                # the exact payload the reviewer approved: delivery sends this snapshot, never a rebuilt one
                "approved_payload": "TEXT", "approved_by": "TEXT", "approved_at": "TEXT", "rejected_by": "TEXT",
                "delivery_attempts": "INTEGER NOT NULL DEFAULT 0", "last_error": "TEXT", "next_attempt_at": "TEXT",
                "external_ref": "TEXT", "corroborations": "INTEGER NOT NULL DEFAULT 0",
                "severity_band": "TEXT", "repair_target_days": "INTEGER", "road_class": "TEXT", "road_name": "TEXT",
                "pending_update": "INTEGER NOT NULL DEFAULT 0",
                # independent repair verification, kept apart from the authority's status (status = 'fixed')
                "closed_at": "TEXT", "verification_status": "TEXT", "verification_updated_at": "TEXT",
                "reopened_at": "TEXT", "reopen_reason": "TEXT", "reopen_event_id": "TEXT"},
    # duty-of-care ledger rows (core.ledger) are audit rows with a SHA-256 hash chained per report
    "audit": {"prev_hash": "TEXT", "hash": "TEXT"},
}
# indexes on migrated columns: created after the columns exist
POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS audit_subject ON audit (subject, id);
CREATE UNIQUE INDEX IF NOT EXISTS audit_chain ON audit (subject, prev_hash) WHERE hash IS NOT NULL;
"""

REPORT_STATUSES = ("pending_approval", "approved", "rejected", "submitted", "fixed", "delivery_failed")
# RoadSense's own repair check after the authority closes a report (reports.verification_status)
VERIFICATION_STATUSES = ("awaiting", "verified", "reopened")
EVENT_STATUSES = ("received", "checked", "verified", "rejected", "processed")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.executescript(POST_MIGRATION)
    conn.execute("CREATE INDEX IF NOT EXISTS reports_verification ON reports (status, verification_status)")
    return conn


@contextmanager
def session(path: Path | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def audit(conn: sqlite3.Connection, actor: str, action: str, subject: str | None = None, detail=None) -> None:
    text = detail if isinstance(detail, str) or detail is None else json.dumps(detail, default=str)
    conn.execute("INSERT INTO audit (ts, actor, action, subject, detail) VALUES (?, ?, ?, ?, ?)",
                 (now(), actor, action, subject, text[:4000] if text else text))
