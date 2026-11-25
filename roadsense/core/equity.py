"""Repair Equity Audit: do repairs take longer in some neighbourhoods than others?

Records come from data RoadSense already holds, without copying it:

    city_sr     the city's own service requests (external_requests, source 'city_sr'), synced read-only from its
                ArcGIS open-data layer by core.external. Timestamps the layer may carry are read from the stored raw
                record: acknowledged / inspected / work started, when present. Many layers only have created and
                closed dates; nothing is assumed about the others.
    roadsense   RoadSense reports delivered to an authority: submitted (audit log) to closed by the authority
                (reports.closed_at). A report the city also holds (linked city request) is counted once, as the city's.

Three durations are kept apart and computed only when both timestamps exist:
    response time    reported -> acknowledged
    inspection time  reported -> inspected
    repair time      reported -> completed (closed)

Neighbourhood comes from evidence only: a neighbourhood/community field on the city record, else the suburb in the
cached OpenStreetMap reverse geocode of the position. Anything else is "Unassigned"; names are never guessed.

A neighbourhood is flagged as a POSSIBLE difference in service delivery only when its median repair time is clearly
longer than the rest of the city's (ratio and a Mann-Whitney U test, both groups with enough completed repairs).
The flag lists the factors that could explain it (damage mix, road classes, volume, missing timestamps); it is never
presented as proof of unfair treatment. Dashboards show aggregates; record lists carry no driver or trip data.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

from ..common.config import settings
from ..common.db import now
from .exposure import rules, valid_gps

UNASSIGNED = "Unassigned"
NEIGHBOURHOOD_FIELDS = ("neighbourhood", "neighborhood", "neighbourhood_name", "neighborhood_name", "community",
                        "community_name", "comm_name", "suburb")
ACK_FIELDS = ("acknowledgeddate", "dateacknowledged", "acknowledged_date", "date_acknowledged", "dispatchdate",
              "dispatched_date", "datedispatched")
INSPECT_FIELDS = ("inspectiondate", "inspecteddate", "date_inspected", "inspected_date", "inspection_date")
START_FIELDS = ("workstartdate", "actualstartdate", "work_start_date", "datestarted", "start_date")
COMPLETE_FIELDS = ("closeddate", "closed_date", "datetimeclosed", "completed_date", "datecompleted",
                   "actualfinishdate")
DURATIONS = {"response": ("reported_at", "acknowledged_at"), "inspection": ("reported_at", "inspected_at"),
             "repair": ("reported_at", "completed_at")}


# ---------------- parsing ----------------
def parse_ts(v) -> datetime | None:
    """ISO strings, 'YYYY-MM-DD HH:MM:SS', or epoch milliseconds (ArcGIS). None when unreadable."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit() and len(v.strip()) >= 10):
        try:
            return datetime.fromtimestamp(float(v) / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _field(attrs: dict, names) -> tuple[str | None, object]:
    lower = {str(k).lower(): (k, v) for k, v in (attrs or {}).items()}
    for n in names:
        if n in lower and lower[n][1] not in (None, ""):
            return lower[n][0], lower[n][1]
    return None, None


def damage_category(text: str | None, class_code: str | None = None) -> str:
    if class_code:
        return {"D40": "Pothole", "D20": "Alligator crack", "D10": "Transverse crack",
                "D00": "Longitudinal crack"}.get(class_code, class_code)
    t = (text or "").lower()
    if "pot" in t and "hole" in t:
        return "Pothole"
    if "crack" in t:
        return "Crack"
    return "Other road surface"


# ---------------- neighbourhoods ----------------
def _cached_place(conn: sqlite3.Connection, lat, lon) -> dict | None:
    if not valid_gps(lat, lon):
        return None
    hit = conn.execute("SELECT value FROM geocache WHERE key = ?", (f"v3:{float(lat):.4f},{float(lon):.4f}",)).fetchone()
    return json.loads(hit["value"]) if hit else None


def neighbourhood_for(conn: sqlite3.Connection, lat, lon, raw: dict | None = None) -> tuple[str | None, str]:
    """(name, evidence). A field on the city record wins; else the OSM suburb of the cached reverse geocode. No
    network call: positions not geocoded yet stay unassigned until resolve_missing looks them up."""
    key, value = _field(raw or {}, NEIGHBOURHOOD_FIELDS)
    if value:
        return str(value).strip().title(), f"city record field '{key}'"
    place = _cached_place(conn, lat, lon)
    if place and place.get("suburb") and place.get("source") == "openstreetmap_nominatim":
        return str(place["suburb"]).strip(), "OpenStreetMap suburb (Nominatim reverse geocode)"
    return None, "no geographic evidence" if valid_gps(lat, lon) else "invalid or missing GPS position"


def assign(conn: sqlite3.Connection, kind: str, record_id: str, lat, lon, raw: dict | None = None) -> tuple[str, str]:
    name, source = neighbourhood_for(conn, lat, lon, raw)
    prev = conn.execute("SELECT neighbourhood, source FROM neighbourhood_assignments WHERE record_kind = ? AND "
                        "record_id = ?", (kind, str(record_id))).fetchone()
    if prev is None or (prev["neighbourhood"], prev["source"]) != (name, source):
        conn.execute("INSERT OR REPLACE INTO neighbourhood_assignments (record_kind, record_id, neighbourhood, source, "
                     "assigned_at) VALUES (?,?,?,?,?)", (kind, str(record_id), name, source, now()))
    return name or UNASSIGNED, source


def resolve_missing(conn: sqlite3.Connection, limit: int = 10) -> dict:
    """Reverse-geocode a few unassigned positions (Nominatim, 1 request a second, cached) so they can be assigned on
    the next read. Offline mode and the geocoder's circuit breaker are respected."""
    from . import geo

    rows = conn.execute("SELECT a.record_kind, a.record_id FROM neighbourhood_assignments a WHERE a.neighbourhood IS "
                        "NULL AND a.source = 'no geographic evidence' LIMIT ?", (limit,)).fetchall()
    looked = 0
    for a in rows:
        table, rid = ("external_requests", a["record_id"]) if a["record_kind"] == "city_sr" else ("reports",
                                                                                                    a["record_id"])
        pos = conn.execute(f"SELECT lat, lon FROM {table} WHERE id = ?", (rid,)).fetchone()
        if pos and valid_gps(pos["lat"], pos["lon"]) and _cached_place(conn, pos["lat"], pos["lon"]) is None:
            geo.reverse_geocode(conn, pos["lat"], pos["lon"])
            looked += 1
    return {"looked_up": looked}


# ---------------- records ----------------
def _days(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 86400, 2)


def records(conn: sqlite3.Connection) -> tuple[list[dict], dict]:
    """Normalised repair records from city service requests and delivered RoadSense reports, plus data-quality counts."""
    quality = Counter()
    out, seen, linked_reports = [], {}, set()
    for x in conn.execute("SELECT * FROM external_requests WHERE source = 'city_sr' ORDER BY synced_at"):
        x = dict(x)
        try:
            raw = json.loads(x.get("raw") or "{}")
        except ValueError:
            raw = {}
        reported = parse_ts(x.get("reported_at"))
        completed = parse_ts(x.get("closed_at")) or parse_ts(_field(raw, COMPLETE_FIELDS)[1])
        # the same request republished under another object id: same place, same time, same kind
        key = (round(x["lat"], 5), round(x["lon"], 5), reported.isoformat() if reported else x["id"], x.get("kind"))
        if key in seen:
            quality["duplicates_removed"] += 1
            continue
        seen[key] = x["id"]
        if x.get("report_id"):
            linked_reports.add(x["report_id"])
        hood, hood_src = assign(conn, "city_sr", x["id"], x["lat"], x["lon"], raw)
        place = _cached_place(conn, x["lat"], x["lon"]) or {}
        out.append({
            "record_kind": "city_sr", "id": x["id"], "source": "City service request",
            "authority": settings.city_sr_authority, "category": damage_category(x.get("kind") or x.get("description")),
            "road_class": place.get("road_class") or "unknown", "neighbourhood": hood, "neighbourhood_source": hood_src,
            "status": "completed" if x.get("status") == "closed" else "unresolved",
            "reported_at": reported, "acknowledged_at": parse_ts(_field(raw, ACK_FIELDS)[1]),
            "inspected_at": parse_ts(_field(raw, INSPECT_FIELDS)[1]),
            "work_started_at": parse_ts(_field(raw, START_FIELDS)[1]), "completed_at": completed,
            "severity": None, "description": (x.get("description") or "")[:200]})
    submitted = {}
    for a in conn.execute("SELECT subject, MIN(ts) AS ts FROM audit WHERE action = 'status_submitted' GROUP BY subject"):
        submitted[a["subject"]] = parse_ts(a["ts"])
    for r in conn.execute("SELECT * FROM reports WHERE status IN ('submitted', 'fixed') ORDER BY id"):
        r = dict(r)
        if r["id"] in linked_reports:
            quality["linked_to_city_request"] += 1  # the city's record of the same request already counts
            continue
        hood, hood_src = assign(conn, "roadsense", str(r["id"]), r["lat"], r["lon"])
        out.append({
            "record_kind": "roadsense", "id": f"report:{r['id']}", "source": "RoadSense report",
            "authority": r.get("jurisdiction") or r.get("municipality") or "unknown",
            "category": damage_category(None, r["class_code"]), "road_class": r.get("road_class") or "unknown",
            "neighbourhood": hood, "neighbourhood_source": hood_src,
            "status": "completed" if r["status"] == "fixed" else "unresolved",
            "reported_at": submitted.get(f"report:{r['id']}"), "acknowledged_at": None, "inspected_at": None,
            "work_started_at": None, "completed_at": parse_ts(r.get("closed_at")),
            "severity": r.get("severity_band"), "description": None})
    for rec in out:
        for name, (a, b) in DURATIONS.items():
            d = _days(rec[a], rec[b])
            if d is not None and d < 0:
                quality["inconsistent_timestamps"] += 1  # completed before reported: kept out of every average
                d = None
            rec[f"{name}_days"] = d
        if rec["status"] == "completed" and rec["completed_at"] is None:
            quality["completed_without_timestamp"] += 1
        if rec["reported_at"] is None:
            quality["missing_reported_timestamp"] += 1
        if rec["neighbourhood"] == UNASSIGNED:
            quality["unassigned_neighbourhood"] += 1
    quality["acknowledged_timestamps"] = sum(r["acknowledged_at"] is not None for r in out)
    quality["inspection_timestamps"] = sum(r["inspected_at"] is not None for r in out)
    return out, dict(quality)


def filter_records(recs: list[dict], start: date | None = None, end: date | None = None, category: str | None = None,
                   road_class: str | None = None, authority: str | None = None, neighbourhood: str | None = None,
                   source: str | None = None) -> list[dict]:
    out = []
    for r in recs:
        if (start or end) and r["reported_at"] is None:
            continue  # without a report date it cannot be placed in a date range
        if start and r["reported_at"].date() < start:
            continue
        if end and r["reported_at"].date() > end:
            continue
        if category and r["category"] != category:
            continue
        if road_class and r["road_class"] != road_class:
            continue
        if authority and r["authority"] != authority:
            continue
        if neighbourhood and r["neighbourhood"] != neighbourhood:
            continue
        if source and r["record_kind"] != source:
            continue
        out.append(r)
    return out


# ---------------- statistics ----------------
def stats(values: list[float]) -> dict:
    v = sorted(x for x in values if x is not None)
    if not v:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {"n": len(v), "mean": round(statistics.fmean(v), 2), "median": round(statistics.median(v), 2),
            "min": v[0], "max": v[-1]}


def mann_whitney_p(a: list[float], b: list[float]) -> float | None:
    """Two-sided p-value of the Mann-Whitney U test (normal approximation with tie and continuity correction)."""
    n1, n2 = len(a), len(b)
    if not n1 or not n2:
        return None
    values = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    ranks, ties, i = [0.0] * len(values), 0.0, 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[j + 1][0] == values[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r1 = sum(rank for rank, (_, g) in zip(ranks, values) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))) if n > 1 else 0
    if sigma == 0:
        return 1.0
    z = (abs(u1 - n1 * n2 / 2) - 0.5) / sigma
    return round(max(0.0, min(1.0, 2 * (1 - 0.5 * (1 + math.erf(max(z, 0) / math.sqrt(2)))))), 4)


def compare(group: list[float], rest: list[float], cfg: dict) -> dict:
    """Is the group's repair time longer than the rest? Only says so with enough data on both sides."""
    g, o = stats(group), stats(rest)
    need = cfg["min_sample"]
    if g["n"] < need or o["n"] < need:
        return {"status": "insufficient_data", "n": g["n"], "n_rest": o["n"], "min_sample": need,
                "message": f"Too few completed repairs to compare ({g['n']} here, {o['n']} elsewhere; {need} needed "
                           "on each side)."}
    ratio = round(g["median"] / o["median"], 2) if o["median"] else None
    p = mann_whitney_p(group, rest)
    longer = ratio is not None and ratio >= cfg["min_ratio"] and p is not None and p < cfg["alpha"]
    return {"status": "possible_difference" if longer else "no_clear_difference", "n": g["n"], "n_rest": o["n"],
            "median": g["median"], "median_rest": o["median"], "ratio": ratio, "p_value": p}


def _context(group: list[dict], everyone: list[dict]) -> dict:
    """Factors that could explain a difference, next to the city-wide mix."""
    def share(items, pred):
        return round(sum(map(pred, items)) / len(items), 2) if items else None

    return {"requests": len(group),
            "pothole_share": share(group, lambda r: r["category"] == "Pothole"),
            "pothole_share_city": share(everyone, lambda r: r["category"] == "Pothole"),
            "road_classes": dict(Counter(r["road_class"] for r in group).most_common(4)),
            "missing_timestamp_share": share(group, lambda r: r["reported_at"] is None or (
                r["status"] == "completed" and r["completed_at"] is None)),
            "severe_share": share([r for r in group if r["severity"]], lambda r: r["severity"] in ("S3", "S4"))}


def summary(conn: sqlite3.Connection, **filters) -> dict:
    cfg = rules()["equity"]
    all_recs, quality = records(conn)
    recs = filter_records(all_recs, **filters)
    durations = lambda items, kind="repair": [r[f"{kind}_days"] for r in items if r[f"{kind}_days"] is not None]
    by_hood: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_hood[r["neighbourhood"]].append(r)
    rows = []
    for name, items in by_hood.items():
        rest = [r for r in recs if r["neighbourhood"] != name]
        rep = stats(durations(items))
        row = {"neighbourhood": name, "requests": len(items),
               "completed": sum(r["status"] == "completed" for r in items),
               "unresolved": sum(r["status"] == "unresolved" for r in items),
               "repair": rep, "response": stats(durations(items, "response")),
               "inspection": stats(durations(items, "inspection")),
               "insufficient_data": rep["n"] < cfg["min_sample"], "context": _context(items, recs)}
        row["comparison"] = compare(durations(items), durations(rest), cfg) if name != UNASSIGNED else {
            "status": "not_compared", "message": "Records without a neighbourhood are not compared."}
        rows.append(row)
    rows.sort(key=lambda x: (x["neighbourhood"] == UNASSIGNED, -(x["repair"]["median"] or -1)))
    # like with like: the same comparison within each damage category and road class with enough data
    strata = []
    for (cat, rc), items in sorted(_group(recs, lambda r: (r["category"], r["road_class"])).items()):
        hoods = _group(items, lambda r: r["neighbourhood"])
        cells = []
        for name, group in sorted(hoods.items()):
            if name == UNASSIGNED:
                continue
            rest = [r for r in items if r["neighbourhood"] != name]
            cells.append({"neighbourhood": name, **compare(durations(group), durations(rest), cfg)})
        if cells:
            strata.append({"category": cat, "road_class": rc, "records": len(items),
                           "completed": len(durations(items)), "neighbourhoods": cells})
    for row in rows:
        same = [c for s in strata for c in s["neighbourhoods"] if c["neighbourhood"] == row["neighbourhood"]]
        row["holds_within_similar_damage"] = (
            None if not any(c["status"] != "insufficient_data" for c in same)
            else any(c["status"] == "possible_difference" for c in same))
    overall = stats(durations(recs))
    sufficient = [x for x in rows if not x["insufficient_data"] and x["neighbourhood"] != UNASSIGNED]
    flagged = [x["neighbourhood"] for x in rows if x["comparison"].get("status") == "possible_difference"]
    return {
        "overall": {"requests": len(recs), "completed": sum(r["status"] == "completed" for r in recs),
                    "unresolved": sum(r["status"] == "unresolved" for r in recs), "repair": overall,
                    "response": stats(durations(recs, "response")), "inspection": stats(durations(recs, "inspection"))},
        "longest": [{"neighbourhood": x["neighbourhood"], "median": x["repair"]["median"], "n": x["repair"]["n"]}
                    for x in sorted(sufficient, key=lambda x: -(x["repair"]["median"] or 0))[:3]],
        "neighbourhoods": rows, "similar_damage": strata, "flagged": flagged,
        "data_quality": quality, "sources": sorted({r["source"] for r in recs}),
        "options": _options(all_recs), "rules": cfg,
        "city_layer_configured": bool(settings.city_sr_layer_url),
        "caveat": ("A longer median repair time is a possible difference in service delivery to look into, not proof "
                   "of unfair treatment: damage severity, repair complexity, road type, crew availability, request "
                   "volume and missing or inconsistent timestamps can all explain it.")}


def _group(items, key) -> dict:
    out = defaultdict(list)
    for r in items:
        out[key(r)].append(r)
    return out


def _options(recs: list[dict]) -> dict:
    return {k: sorted({str(r[f]) for r in recs}) for k, f in (
        ("neighbourhoods", "neighbourhood"), ("categories", "category"), ("road_classes", "road_class"),
        ("authorities", "authority"))}


def neighbourhood_records(conn: sqlite3.Connection, neighbourhood: str, limit: int = 300, **filters) -> list[dict]:
    """The records behind one neighbourhood's figures. No driver, trip or reviewer data."""
    recs, _ = records(conn)
    out = []
    for r in filter_records(recs, neighbourhood=neighbourhood, **filters)[:limit]:
        out.append({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()
                    if k in ("id", "source", "authority", "category", "road_class", "neighbourhood_source", "status",
                             "reported_at", "acknowledged_at", "inspected_at", "completed_at", "response_days",
                             "inspection_days", "repair_days", "severity", "description")})
    return out
