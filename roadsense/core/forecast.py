"""Freeze-thaw pothole risk forecast per road segment: an interpretable HEURISTIC, not a trained model.

Cracks let water into the pavement; when it freezes and thaws, the crack opens and the surface breaks into a pothole.
For each road segment that has crack sightings (D00 longitudinal, D10 transverse, D20 alligator), this module scores
how likely a pothole is within the horizon (default 14 days) from:

    crack_severity        worst sighting (class, severity band, detector confidence) and repeat sightings
    recent_freeze_thaw    freeze-thaw cycles in the last N days (ECCC station records)
    forecast_freeze_thaw  cycles in the ECCC forecast for the horizon (about 6 of 14 days are forecast)
    moisture              wet days around the window
    road_class            OSM road class as a stand-in for traffic (no traffic counts exist)
    pothole_history       whether the segment has already gone from crack to pothole; only used once enough
                          segments show that history

Weights and thresholds come from forecast_rules.json and are recorded (as a hash) with every prediction. A factor
without data contributes nothing and lowers the data-quality score; a segment below the minimum data quality is never
rated High. Each prediction stores its factors, sources and a plain-language explanation.

Predictions are compared with what later trips actually see (evaluate_outcomes), and `backtest` re-runs the forecast
as of past dates using only the detections and weather observed before each date, so no future data leaks in.

Prediction runs are separate from the detection pipeline: they only read `events` and `reports`.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import httpx

from ..common.config import settings
from ..common.db import audit, now
from ..edge.gps import haversine_m
from . import freezethaw, weather
from .condition import _segment_key
from .reports import severity_band

RULES_FILE = Path(__file__).with_name("forecast_rules.json")
CRACKS, POTHOLE = ("D00", "D10", "D20"), "D40"
VALID_EVENT_STATUSES = ("verified", "processed")
CATEGORIES = ("low", "medium", "high")
FACTOR_LABELS = {"crack_severity": "Existing crack severity", "recent_freeze_thaw": "Recent freeze-thaw activity",
                 "forecast_freeze_thaw": "Forecast freeze-thaw activity", "moisture": "Precipitation / moisture",
                 "road_class": "Road classification", "pothole_history": "Crack-to-pothole history"}
DISCLAIMER = ("Heuristic risk estimate from configurable weights. It is not a trained or validated prediction; "
              "compare it with the evaluation figures before relying on it.")


# ---------------- configuration ----------------
@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _strip(d):
    return {k: _strip(v) for k, v in d.items() if not k.startswith("_")} if isinstance(d, dict) else d


def rules() -> dict:
    r = _strip(_load(settings.forecast_rules_file or str(RULES_FILE)))
    c = r["categories"]
    if not 0 <= c["medium"] <= c["high"] <= 1:
        raise ValueError("forecast categories need 0 <= medium <= high <= 1")
    if any(w < 0 for w in r["weights"].values()) or not sum(r["weights"].values()):
        raise ValueError("forecast weights must be non-negative and not all zero")
    return r


def config_hash(r: dict | None = None, horizon_days: int | None = None) -> str:
    blob = json.dumps({"rules": r or rules(), "horizon": horizon_days or settings.forecast_horizon_days}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------- input validation ----------------
def valid_gps(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0)


def valid_confidence(c) -> bool:
    try:
        c = float(c)
    except (TypeError, ValueError):
        return False
    return math.isfinite(c) and 0 <= c <= 1


def _ts(v) -> datetime:
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------------- observations and segments ----------------
def load_events(conn: sqlite3.Connection, as_of: datetime | None = None) -> tuple[list[dict], Counter]:
    """Confirmed damage sightings (cracks and potholes) known at `as_of`, with road context from their report.
    Rejected and suppressed sightings are left out (a person or the verifier said they are not damage)."""
    sql = ("SELECT e.id, e.trip_id, e.class_code, e.confidence, e.box_area_ratio, e.width_m, e.lat, e.lon, "
           "e.captured_at, e.image_path, e.report_id, e.road_name, e.road_class, e.jolt_g, r.road_name AS r_road, "
           "r.road_class AS r_class, r.address, r.municipality, r.severity_band AS r_band FROM events e "
           "LEFT JOIN reports r ON r.id = e.report_id WHERE e.status IN (?, ?) AND e.suppressed_by IS NULL "
           "AND e.class_code IN (?, ?, ?, ?)")
    args: list = [*VALID_EVENT_STATUSES, *CRACKS, POTHOLE]
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY e.captured_at", args)]
    out, skipped = [], Counter()
    for e in rows:
        try:
            at = _ts(e["captured_at"])
        except (TypeError, ValueError):
            skipped["invalid_timestamp"] += 1
            continue
        if as_of is not None and at > as_of:
            continue  # not known yet at the forecast time
        if not valid_gps(e["lat"], e["lon"]):
            skipped["invalid_gps"] += 1
            continue
        if not valid_confidence(e["confidence"]):
            skipped["invalid_confidence"] += 1
            continue
        e["road_name"] = e["road_name"] or e.pop("r_road", None)
        e["road_class"] = e["road_class"] or e.pop("r_class", None)
        e["at"] = at
        out.append(e)
    return out, skipped


def build_segments(events: list[dict], tolerance_m: float | None = None) -> list[dict]:
    """Group sightings into road segments: road name + 100 m bin (the same key as the Road condition page), then merge
    neighbouring bins of the same road where sightings are within the tolerance (one crack must not straddle two)."""
    tol = settings.segment_tolerance_m if tolerance_m is None else tolerance_m
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in events:
        groups[_segment_key(e)].append(e)
    keys = list(groups)
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    by_road = defaultdict(list)
    for k in keys:
        by_road[k[0]].append(k)
    for road_keys in by_road.values():
        road_keys.sort(key=lambda k: k[1])
        for a, b in zip(road_keys, road_keys[1:]):
            if b[1] - a[1] == 1 and any(haversine_m(x["lat"], x["lon"], y["lat"], y["lon"]) <= tol
                                        for x in groups[a] for y in groups[b]):
                parent[find(b)] = find(a)
    merged: dict[tuple, list[dict]] = defaultdict(list)
    for k in keys:
        merged[find(k)].extend(groups[k])
    out = []
    for (road, first_bin), items in merged.items():
        items.sort(key=lambda e: e["at"])
        out.append({"id": "seg-" + hashlib.sha1(f"{road}|{first_bin}".encode()).hexdigest()[:12], "road_name": road,
                    "road_class": _most_common(items, "road_class"), "municipality": _most_common(items, "municipality"),
                    "location": _most_common(items, "address"),
                    "lat": sum(i["lat"] for i in items) / len(items), "lon": sum(i["lon"] for i in items) / len(items),
                    "first_seen": items[0]["captured_at"], "last_seen": items[-1]["captured_at"], "events": items})
    return out


def _most_common(items: list[dict], field: str):
    """The most frequent known value; None (explicitly unknown) when no sighting has one."""
    return (Counter(i[field] for i in items if i.get(field)).most_common(1) or [(None, 0)])[0][0]


def store_segments(conn: sqlite3.Connection, segments: list[dict]) -> None:
    ts = now()
    for s in segments:
        conn.execute(
            "INSERT INTO road_segments (id, road_name, road_class, municipality, location, lat, lon, first_seen, "
            "last_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "road_class = excluded.road_class, municipality = excluded.municipality, location = excluded.location, "
            "lat = excluded.lat, lon = excluded.lon, first_seen = excluded.first_seen, last_seen = excluded.last_seen, "
            "updated_at = excluded.updated_at",
            (s["id"], s["road_name"], s["road_class"], s["municipality"], s["location"], s["lat"], s["lon"],
             s["first_seen"], s["last_seen"], ts))
        for e in s["events"]:
            conn.execute("INSERT OR REPLACE INTO segment_events (segment_id, event_id) VALUES (?, ?)", (s["id"], e["id"]))


# ---------------- deterioration history ----------------
def deterioration(seg_events: list[dict]) -> dict:
    """How the damage on one segment changed over passes: first crack, later sightings, class changes, first pothole
    and the days from the first crack to the first pothole. Built only from real sightings."""
    items = sorted(seg_events, key=lambda e: e["at"])
    cracks = [e for e in items if e["class_code"] in CRACKS]
    first_crack = cracks[0] if cracks else None
    potholes = [e for e in items if e["class_code"] == POTHOLE and (not first_crack or e["at"] >= first_crack["at"])]
    changes, last = [], None
    for e in items:
        if e["class_code"] != last:
            changes.append({"at": e["captured_at"], "class_code": e["class_code"], "trip_id": e["trip_id"]})
            last = e["class_code"]
    days = None
    if first_crack and potholes:
        days = round((potholes[0]["at"] - first_crack["at"]).total_seconds() / 86400, 1)
    trips = len({e["trip_id"] for e in items})
    return {"observations": len(items), "trips": trips,
            "first_crack_at": first_crack["captured_at"] if first_crack else None,
            "first_pothole_at": potholes[0]["captured_at"] if potholes else None,
            "days_crack_to_pothole": days, "class_changes": changes,
            "timeline": [{"event_id": e["id"], "at": e["captured_at"], "class_code": e["class_code"],
                          "confidence": e["confidence"], "trip_id": e["trip_id"], "report_id": e["report_id"]}
                         for e in items],
            "sufficient": trips >= 2}


def history_summary(segments: list[dict], r: dict | None = None) -> dict:
    r = r or rules()
    hist = [deterioration(s["events"]) for s in segments]
    pairs = [h["days_crack_to_pothole"] for h in hist if h["days_crack_to_pothole"] is not None]
    repeat = sum(h["trips"] >= 2 for h in hist)
    need = r["history"]["min_pairs"]
    return {"segments": len(hist), "segments_seen_on_2_or_more_trips": repeat, "crack_to_pothole_pairs": len(pairs),
            "median_days_crack_to_pothole": sorted(pairs)[len(pairs) // 2] if pairs else None,
            "sufficient": len(pairs) >= need, "min_pairs": need,
            "message": None if len(pairs) >= need else
            f"Not enough history yet: {len(pairs)} segment(s) have gone from a crack to a pothole on later trips; "
            f"{need} are needed before deterioration trends or the history factor are used."}


# ---------------- factors ----------------
def _factor(name, value, raw, coverage=1.0, available=True, applicable=True, weight=0.0) -> dict:
    value = None if not available else round(max(0.0, min(1.0, value)), 3)
    return {"factor": name, "label": FACTOR_LABELS[name], "value": value, "raw": raw, "coverage": round(coverage, 3),
            "available": available, "applicable": applicable, "weight": weight}


def crack_severity_factor(seg_events: list[dict], r: dict) -> dict:
    cfg = r["crack_severity"]
    cracks = [e for e in seg_events if e["class_code"] in CRACKS]
    if not cracks:
        return _factor("crack_severity", 0, {"observations": 0}, 0, available=False)
    scored = []
    for e in cracks:
        band = e.get("r_band") or severity_band(e["class_code"], e.get("width_m"), e.get("box_area_ratio") or 0,
                                                e.get("jolt_g"))
        s = cfg["class_weight"].get(e["class_code"], 0.5) * cfg["band_multiplier"].get(band, 0.8) * (
            0.5 + 0.5 * float(e["confidence"]))
        scored.append((s, e, band))
    worst, e, band = max(scored, key=lambda x: x[0])
    trips = len({c["trip_id"] for c in cracks})
    value = worst * (1 + cfg["repeat_boost"] * (min(trips, cfg["max_trips"]) - 1))
    return _factor("crack_severity", value, {
        "worst_class": e["class_code"], "worst_band": band, "observations": len(cracks), "trips": trips,
        "mean_confidence": round(sum(float(c["confidence"]) for c in cracks) / len(cracks), 3),
        "classes": dict(Counter(c["class_code"] for c in cracks))})


def freeze_thaw_factor(name: str, summary: dict | None, full_cycles: float, potential_weight: float) -> dict:
    if not summary or summary["coverage"] == 0:
        return _factor(name, 0, {"reason": "no weather data for this window", **(summary and {
            "days": summary["days"], "missing_days": summary["missing_days"]} or {})}, 0, available=False)
    cycles = summary["confirmed_cycles"] + potential_weight * summary["potential_cycles"]
    return _factor(name, cycles / full_cycles, {k: summary[k] for k in (
        "start", "end", "days", "confirmed_cycles", "potential_cycles", "missing_days", "moist_cycles", "sources")},
        summary["coverage"])


def moisture_factor(summaries: list[dict | None], full_wet_days: float) -> dict:
    known = [s for s in summaries if s]
    days = sum(s["days"] for s in known) or 1
    precip_known = sum(s["precip_known_days"] for s in known)
    if not precip_known:
        return _factor("moisture", 0, {"reason": "no precipitation data"}, 0, available=False)
    wet = sum(s["wet_days"] for s in known)
    return _factor("moisture", wet / full_wet_days, {"wet_days": wet, "days_with_precipitation_data": precip_known,
                                                     "moist_cycles": sum(s["moist_cycles"] for s in known)},
                   precip_known / days)


def road_class_factor(road_class: str | None, r: dict) -> dict:
    table = r["road_class"]
    if not road_class or road_class not in table:
        return _factor("road_class", 0, {"road_class": road_class, "reason": "road class unknown" if not road_class
                                         else f"no exposure value for class '{road_class}'"}, 0, available=False)
    return _factor("road_class", table[road_class], {"road_class": road_class, "traffic_counts": "not available"})


def pothole_history_factor(hist: dict, history_ok: bool) -> dict:
    if not history_ok:
        return _factor("pothole_history", 0, {"reason": "not enough crack-to-pothole history across segments"}, 0,
                       available=False, applicable=False)
    seen = hist["first_pothole_at"] is not None
    return _factor("pothole_history", 1.0 if seen else 0.0, {"first_pothole_at": hist["first_pothole_at"],
                                                              "days_crack_to_pothole": hist["days_crack_to_pothole"]})


def score(factors: list[dict], r: dict) -> dict:
    """Weighted mean over the applicable factors. Missing data scores 0 and lowers data quality."""
    weights = r["weights"]
    applicable = [f for f in factors if f["applicable"] and weights.get(f["factor"], 0) > 0]
    total = sum(weights[f["factor"]] for f in applicable) or 1.0
    for f in factors:
        w = weights.get(f["factor"], 0) / total if f in applicable else 0.0
        f["weight"] = round(w, 4)
        f["contribution"] = round(w * (f["value"] or 0.0), 4) if f["available"] else 0.0
    value = round(sum(f["contribution"] for f in applicable), 4)
    quality = round(sum(f["weight"] * (f["coverage"] if f["available"] else 0.0) for f in applicable), 3)
    return {"score": value, "data_quality": quality}


def categorise(value: float, quality: float, r: dict) -> tuple[str, bool]:
    c = r["categories"]
    category = "high" if value >= c["high"] else "medium" if value >= c["medium"] else "low"
    insufficient = quality < c["min_data_quality"]
    if insufficient and category == "high":
        category = "medium"  # never alarm on incomplete inputs
    return category, insufficient


def explain(seg: dict, factors: list[dict], category: str, insufficient: bool, quality: float) -> str:
    f = {x["factor"]: x for x in factors}
    parts = []
    cs = f["crack_severity"]["raw"]
    if cs.get("observations"):
        name = {"D20": "alligator crack", "D10": "transverse crack", "D00": "longitudinal crack"}[cs["worst_class"]]
        parts.append(f"worst damage is an {name} (band {cs['worst_band']})" if name[0] == "a"
                     else f"worst damage is a {name} (band {cs['worst_band']})")
        parts.append(f"{cs['observations']} crack sighting(s) on {cs['trips']} trip(s)")
    for key, word in (("recent_freeze_thaw", "in the last"), ("forecast_freeze_thaw", "forecast over the next")):
        x = f[key]
        if x["available"]:
            raw = x["raw"]
            n = raw["confirmed_cycles"] if key == "recent_freeze_thaw" else raw["potential_cycles"]
            extra = f", {raw['potential_cycles']} more possible" if key == "recent_freeze_thaw" and raw[
                "potential_cycles"] else ""
            parts.append(f"{n} freeze-thaw day(s) {word} {raw['days']} days{extra}"
                         + (f" ({raw['missing_days']} day(s) without data)" if raw["missing_days"] else ""))
        else:
            parts.append(f"no weather data for the {'past' if key == 'recent_freeze_thaw' else 'forecast'} window")
    m = f["moisture"]
    parts.append(f"precipitation on {m['raw']['wet_days']} day(s)" if m["available"] else "no precipitation data")
    rc = f["road_class"]
    parts.append(f"road class {rc['raw']['road_class']}" if rc["available"] else "road class unknown")
    if f["pothole_history"]["applicable"] and f["pothole_history"]["value"]:
        parts.append("this segment has already developed a pothole")
    text = f"{category.capitalize()} risk: " + "; ".join(parts) + "."
    if insufficient:
        text += f" Data quality {quality:.0%} is below the minimum, so this segment is not rated High."
    return text


# ---------------- forecasting run ----------------
def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def run_forecast(conn: sqlite3.Connection, as_of: datetime | None = None, kind: str = "live", force: bool = False,
                 client: httpx.Client | None = None, actor: str = "service", horizon_days: int | None = None) -> dict:
    """Score every road segment that has crack sightings. Skips the work when nothing changed since the last run of
    the same kind and day (same detections, weather and rules), unless forced."""
    r = rules()
    horizon = horizon_days or settings.forecast_horizon_days
    as_of = as_of or datetime.now(timezone.utc)
    start = as_of.date()
    end = start + timedelta(days=horizon - 1)
    ft = r["freeze_thaw"]
    hist_start, hist_end = start - timedelta(days=ft["history_days"]), start - timedelta(days=1)
    chash = config_hash(r, horizon)

    events, skipped = load_events(conn, as_of)
    all_segments = build_segments(events)
    segments = [s for s in all_segments if any(e["class_code"] in CRACKS for e in s["events"])]
    if kind == "live":
        store_segments(conn, all_segments)
    else:  # a backtest still needs the segment rows its predictions point at
        store_segments(conn, segments)
    hist_info = history_summary(all_segments, r)

    obs_by_cell, fc_by_cell, weather_errors = {}, {}, []
    for s in segments:
        cell = weather.cell_for(s["lat"], s["lon"])
        if cell not in obs_by_cell:
            obs_by_cell[cell] = weather.observed(conn, s["lat"], s["lon"], hist_start, hist_end, client)
            if kind == "backtest":  # no archived forecasts: using later observations would leak the future
                fc_by_cell[cell] = {"available": False, "records": [], "error": "no archived forecast for a past date"}
            else:
                fc_by_cell[cell] = weather.forecast(conn, s["lat"], s["lon"], client)
            weather_errors += [x["error"] for x in (obs_by_cell[cell], fc_by_cell[cell])
                               if x.get("error") and not (kind == "backtest" and x is fc_by_cell[cell])]

    def keep(rows, lo, hi):
        return [x for x in rows if lo.isoformat() <= str(x["date"])[:10] <= hi.isoformat()]

    inputs = {"events": [(e["id"], e["captured_at"], e["class_code"]) for s in segments for e in s["events"]],
              "weather": {c: [(x["date"], x.get("t_min"), x.get("t_max"), x.get("precip_mm"), x.get("precip_expected"))
                              for x in keep(o["records"], hist_start, hist_end)
                              + keep(fc_by_cell[c]["records"], start, end)] for c, o in obs_by_cell.items()},
              "config": chash, "start": start.isoformat(), "kind": kind}
    ihash = _hash(inputs)
    if not force:
        last = conn.execute("SELECT * FROM forecast_runs WHERE kind = ? AND period_start = ? AND config_hash = ? "
                            "ORDER BY id DESC LIMIT 1", (kind, start.isoformat(), chash)).fetchone()
        if last and last["inputs_hash"] == ihash:
            return {"skipped": True, "reason": "no new detections or weather since the last run", "run_id": last["id"],
                    "segments": last["segments"]}

    statuses = [o["available"] for o in obs_by_cell.values()] + [
        f["available"] for f in fc_by_cell.values() if kind != "backtest"]
    weather_status = "none" if not statuses else "ok" if all(statuses) else "unavailable" if not any(
        statuses) else "partial"
    cur = conn.execute(
        "INSERT INTO forecast_runs (kind, model_kind, as_of, period_start, period_end, horizon_days, config_hash, "
        "inputs_hash, segments, weather_status, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, r.get("model_kind", "heuristic"), as_of.isoformat(timespec="seconds"), start.isoformat(),
         end.isoformat(), horizon, chash, ihash, len(segments), weather_status, actor, now()))
    run_id = cur.lastrowid
    ts, counts = now(), Counter()
    mo = r["moisture"]
    for s in segments:
        cell = weather.cell_for(s["lat"], s["lon"])
        obs, fc = obs_by_cell[cell], fc_by_cell[cell]
        args = dict(freeze_c=ft["freeze_c"], margin_c=ft["margin_c"], precip_min_mm=mo["precip_min_mm"],
                    chance_min=mo["chance_min"], moisture_window_days=mo["window_days"])
        past = freezethaw.summarise(obs["records"], hist_start, hist_end, **args)
        ahead = freezethaw.summarise(fc["records"], start, end, **args)
        hist = deterioration(s["events"])
        factors = [crack_severity_factor(s["events"], r),
                   freeze_thaw_factor("recent_freeze_thaw", past, ft["history_full_cycles"], ft["potential_weight"]),
                   freeze_thaw_factor("forecast_freeze_thaw", ahead, ft["forecast_full_cycles"], ft["potential_weight"]),
                   moisture_factor([past, ahead], mo["full_wet_days"]),
                   road_class_factor(s["road_class"], r),
                   pothole_history_factor(hist, hist_info["sufficient"])]
        sc = score(factors, r)
        category, insufficient = categorise(sc["score"], sc["data_quality"], r)
        counts[category] += 1
        sources = {"detections": f"RoadSense detections ({len(s['events'])} sighting(s))",
                   "weather_observed": obs.get("source") or (obs.get("error") or "unavailable"),
                   "weather_forecast": fc.get("source") or (fc.get("error") or "unavailable"),
                   "road_class": "OpenStreetMap (Nominatim)" if s["road_class"] else "unknown"}
        conn.execute(
            "INSERT INTO risk_predictions (run_id, segment_id, run_kind, score, category, data_quality, "
            "insufficient_data, explanation, data_sources, period_start, period_end, horizon_days, config_hash, "
            "predicted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(segment_id, period_start, horizon_days, config_hash, run_kind) DO UPDATE SET "
            "run_id = excluded.run_id, score = excluded.score, category = excluded.category, "
            "data_quality = excluded.data_quality, insufficient_data = excluded.insufficient_data, "
            "explanation = excluded.explanation, data_sources = excluded.data_sources, "
            "predicted_at = excluded.predicted_at",
            (run_id, s["id"], kind, sc["score"], category, sc["data_quality"], int(insufficient),
             explain(s, factors, category, insufficient, sc["data_quality"]), json.dumps(sources),
             start.isoformat(), end.isoformat(), horizon, chash, as_of.isoformat(timespec="seconds")))
        pid = conn.execute("SELECT id FROM risk_predictions WHERE segment_id = ? AND period_start = ? AND "
                           "horizon_days = ? AND config_hash = ? AND run_kind = ?",
                           (s["id"], start.isoformat(), horizon, chash, kind)).fetchone()["id"]
        conn.execute("DELETE FROM prediction_factors WHERE prediction_id = ?", (pid,))
        for f in factors:
            conn.execute("INSERT INTO prediction_factors (prediction_id, factor, raw, value, weight, contribution, "
                         "coverage, available) VALUES (?,?,?,?,?,?,?,?)",
                         (pid, f["factor"], json.dumps(f["raw"], default=str), f["value"], f["weight"],
                          f.get("contribution", 0.0), f["coverage"], int(f["available"])))
        for span, summ, src in (("history", past, obs.get("source")), ("forecast", ahead, fc.get("source"))):
            conn.execute(
                "INSERT OR REPLACE INTO freeze_thaw_indicators (run_id, segment_id, span, window_start, window_end, "
                "days, confirmed_cycles, potential_cycles, missing_days, wet_days, moist_cycles, source, computed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, s["id"], span, summ["start"], summ["end"], summ["days"], summ["confirmed_cycles"],
                 summ["potential_cycles"], summ["missing_days"], summ["wet_days"], summ["moist_cycles"], src, ts))
    result = {"skipped": False, "run_id": run_id, "kind": kind, "segments": len(segments), "by_category": dict(counts),
              "period_start": start.isoformat(), "period_end": end.isoformat(), "horizon_days": horizon,
              "weather_status": weather_status, "weather_errors": sorted(set(weather_errors))[:5],
              "skipped_inputs": dict(skipped)}
    audit(conn, actor, "forecast_run", f"forecast:{run_id}", {k: v for k, v in result.items() if k != "skipped"})
    return result


# ---------------- outcomes and evaluation ----------------
def evaluate_outcomes(conn: sqlite3.Connection, now_dt: datetime | None = None) -> dict:
    """Compare predictions with later sightings. A new pothole (D40) near the segment within the forecast period
    confirms it; after the period ends, a segment that later trips saw without a pothole is 'no pothole observed',
    and one no trip passed is 'not observed' (neither a hit nor a miss). A segment that already had a pothole when
    the prediction was made is set aside."""
    r = rules()
    radius = r["history"]["match_radius_m"]
    now_dt = now_dt or datetime.now(timezone.utc)
    pending = [dict(x) for x in conn.execute(
        "SELECT p.id, p.predicted_at, p.period_end, s.lat, s.lon, s.road_name FROM risk_predictions p "
        "JOIN road_segments s ON s.id = p.segment_id LEFT JOIN prediction_outcomes o ON o.prediction_id = p.id "
        "WHERE o.prediction_id IS NULL")]
    if not pending:
        return {"evaluated": 0}
    events, _ = load_events(conn)
    done = Counter()
    for p in pending:
        t0 = _ts(p["predicted_at"])
        t1 = datetime.combine(date.fromisoformat(p["period_end"]), datetime.max.time(), timezone.utc)
        near = [e for e in events if haversine_m(p["lat"], p["lon"], e["lat"], e["lon"]) <= radius
                and (not e.get("road_name") or e["road_name"] == p["road_name"] or p["road_name"] == "Unnamed road")]
        before = [e for e in near if e["class_code"] == POTHOLE and e["at"] <= t0]
        during = [e for e in near if t0 < e["at"] <= t1]
        new_pothole = next((e for e in during if e["class_code"] == POTHOLE), None)
        if before:
            outcome, ev = "pothole_already_present", before[0]
        elif new_pothole:
            outcome, ev = "pothole_confirmed", new_pothole
        elif now_dt > t1:
            outcome, ev = ("no_pothole_observed", during[-1]) if during else ("not_observed", None)
        else:
            continue  # still inside the forecast period
        conn.execute("INSERT OR REPLACE INTO prediction_outcomes (prediction_id, outcome, evidence_event_id, "
                     "observed_at, evaluated_at) VALUES (?,?,?,?,?)",
                     (p["id"], outcome, ev["id"] if ev else None, ev["captured_at"] if ev else None, now()))
        done[outcome] += 1
    return {"evaluated": sum(done.values()), **done}


def evaluation_summary(conn: sqlite3.Connection, kind: str | None = None) -> dict:
    r = rules()
    sql = ("SELECT p.run_id, p.run_kind, p.category, o.outcome, f.created_at FROM risk_predictions p "
           "JOIN forecast_runs f ON f.id = p.run_id LEFT JOIN prediction_outcomes o ON o.prediction_id = p.id")
    rows = [dict(x) for x in conn.execute(sql + (" WHERE p.run_kind = ?" if kind else ""), (kind,) if kind else ())]

    def tally(items):
        high = [x for x in items if x["category"] == "high"]
        observed = [x for x in items if x["outcome"] in ("pothole_confirmed", "no_pothole_observed")]
        hits = sum(x["outcome"] == "pothole_confirmed" for x in high)
        false_alarms = sum(x["outcome"] == "no_pothole_observed" for x in high)
        return {"predictions": len(items), "high_predictions": len(high),
                "confirmed_potholes": sum(x["outcome"] == "pothole_confirmed" for x in items),
                "high_confirmed": hits, "false_alarms": false_alarms,
                "missed": sum(x["outcome"] == "pothole_confirmed" and x["category"] != "high" for x in items),
                "not_observed": sum(x["outcome"] == "not_observed" for x in items),
                "pending": sum(x["outcome"] is None for x in items),
                "already_present": sum(x["outcome"] == "pothole_already_present" for x in items),
                "with_outcome": len(observed),
                "high_precision": round(hits / (hits + false_alarms), 3) if hits + false_alarms else None}

    total = tally(rows)
    need = r["history"]["min_outcomes"]
    by_run = defaultdict(list)
    for x in rows:
        by_run[(x["run_id"], x["run_kind"], x["created_at"])].append(x)
    validated = total["with_outcome"] >= need
    return {**total, "validated": validated, "min_outcomes": need,
            "message": None if validated else
            f"Not evaluated yet: {total['with_outcome']} prediction(s) have an observed outcome; {need} are needed "
            "before any accuracy figure means something. Missed potholes only count where a later trip passed.",
            "runs": [{"run_id": k[0], "kind": k[1], "created_at": k[2], **tally(v)}
                     for k, v in sorted(by_run.items(), reverse=True)][:30]}


def backtest(conn: sqlite3.Connection, as_of_dates: list[date], client: httpx.Client | None = None,
             actor: str = "backtest") -> dict:
    """Re-run the forecast as of past dates with only what was known then (detections up to that moment, weather
    observed before that day, no forecast), then score those predictions against what trips saw afterwards."""
    runs = []
    for d in as_of_dates:
        as_of = datetime.combine(d, datetime.min.time(), timezone.utc)
        runs.append(run_forecast(conn, as_of=as_of, kind="backtest", force=True, client=client, actor=actor))
    evaluate_outcomes(conn)
    return {"runs": runs, "evaluation": evaluation_summary(conn, kind="backtest")}


# ---------------- reads for the API ----------------
def _factors(conn, pid: int) -> list[dict]:
    out = []
    for f in conn.execute("SELECT * FROM prediction_factors WHERE prediction_id = ?", (pid,)):
        d = dict(f)
        d["raw"] = json.loads(d["raw"]) if d["raw"] else {}
        d["label"] = FACTOR_LABELS.get(d["factor"], d["factor"])
        out.append(d)
    return sorted(out, key=lambda x: -x["contribution"])


def latest(conn: sqlite3.Connection, kind: str = "live") -> dict:
    run = conn.execute("SELECT * FROM forecast_runs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    r = rules()
    base = {"model_kind": r.get("model_kind", "heuristic"), "disclaimer": DISCLAIMER,
            "weights": r["weights"], "thresholds": {k: v for k, v in r["categories"].items()},
            "horizon_days": settings.forecast_horizon_days}
    if not run:
        return {**base, "run": None, "segments": [], "summary": {"total": 0, "high": 0, "medium": 0, "low": 0}}
    run = dict(run)
    # the predictions of the latest run, plus same-period ones it did not need to recompute
    rows = [dict(x) for x in conn.execute(
        "SELECT p.*, s.road_name, s.road_class, s.municipality, s.location, s.lat, s.lon, s.first_seen, s.last_seen, o.outcome "
        "FROM risk_predictions p JOIN road_segments s ON s.id = p.segment_id "
        "LEFT JOIN prediction_outcomes o ON o.prediction_id = p.id "
        "WHERE p.run_kind = ? AND p.period_start = ? AND p.config_hash = ? ORDER BY p.score DESC",
        (kind, run["period_start"], run["config_hash"]))]
    segs = []
    for x in rows:
        factors = _factors(conn, x["id"])
        f = {y["factor"]: y for y in factors}
        cs = (f.get("crack_severity") or {}).get("raw") or {}
        ft_past = (f.get("recent_freeze_thaw") or {}).get("raw") or {}
        ft_ahead = (f.get("forecast_freeze_thaw") or {}).get("raw") or {}
        x["data_sources"] = json.loads(x["data_sources"])
        x["existing_damage"] = {"classes": cs.get("classes", {}), "worst_class": cs.get("worst_class"),
                                "worst_band": cs.get("worst_band"), "observations": cs.get("observations", 0),
                                "trips": cs.get("trips", 0)}
        x["freeze_thaw"] = {"recent_confirmed": ft_past.get("confirmed_cycles"),
                            "recent_potential": ft_past.get("potential_cycles"),
                            "forecast_potential": ft_ahead.get("potential_cycles"),
                            "recent_available": (f.get("recent_freeze_thaw") or {}).get("available", 0),
                            "forecast_available": (f.get("forecast_freeze_thaw") or {}).get("available", 0)}
        x["main_factors"] = [{"factor": y["factor"], "label": y["label"], "contribution": y["contribution"]}
                             for y in factors if y["contribution"] > 0][:3]
        segs.append(x)
    summary = {"total": len(segs), **{c: sum(s["category"] == c for s in segs) for c in CATEGORIES},
               "insufficient_data": sum(bool(s["insufficient_data"]) for s in segs)}
    return {**base, "run": run, "segments": segs, "summary": summary, "horizon_days": run["horizon_days"]}


def segment_detail(conn: sqlite3.Connection, segment_id: str) -> dict | None:
    seg = conn.execute("SELECT * FROM road_segments WHERE id = ?", (segment_id,)).fetchone()
    if not seg:
        return None
    seg = dict(seg)
    ids = [x["event_id"] for x in conn.execute("SELECT event_id FROM segment_events WHERE segment_id = ?",
                                                (segment_id,))]
    events = [e for e in load_events(conn)[0] if e["id"] in set(ids)]
    preds = [dict(x) for x in conn.execute(
        "SELECT p.*, o.outcome, o.evidence_event_id, o.observed_at FROM risk_predictions p "
        "LEFT JOIN prediction_outcomes o ON o.prediction_id = p.id WHERE p.segment_id = ? "
        "ORDER BY p.predicted_at DESC LIMIT 30", (segment_id,))]
    latest_pred = next((p for p in preds if p["run_kind"] == "live"), None)
    indicators, weather_days = [], []
    if latest_pred:
        latest_pred["data_sources"] = json.loads(latest_pred["data_sources"])
        latest_pred["factors"] = _factors(conn, latest_pred["id"])
        indicators = [dict(x) for x in conn.execute(
            "SELECT * FROM freeze_thaw_indicators WHERE run_id = ? AND segment_id = ?",
            (latest_pred["run_id"], segment_id))]
        r = rules()
        cell = weather.cell_for(seg["lat"], seg["lon"])
        lo = (date.fromisoformat(latest_pred["period_start"]) - timedelta(days=r["freeze_thaw"]["history_days"]))
        rows = [dict(x) for x in conn.execute(
            "SELECT date, kind, t_min, t_max, precip_mm, snow_cm, precip_chance, precip_expected, source "
            "FROM weather_daily WHERE cell = ? AND date >= ? AND date <= ? ORDER BY date, kind",
            (cell, lo.isoformat(), latest_pred["period_end"]))]
        ft = r["freeze_thaw"]
        for w in rows:
            w["status"] = freezethaw.classify_day(w, ft["freeze_c"], ft["margin_c"])
        weather_days = rows
    for e in events:
        e.pop("at", None)
    return {"segment": seg, "events": events, "deterioration": deterioration(
        [{**e, "at": _ts(e["captured_at"])} for e in events]) if events else None,
            "prediction": latest_pred, "indicators": indicators, "weather": weather_days,
            "history": [{k: p[k] for k in ("id", "run_id", "run_kind", "category", "score", "data_quality",
                                           "period_start", "period_end", "predicted_at", "outcome")} for p in preds],
            "disclaimer": DISCLAIMER}


def history(conn: sqlite3.Connection) -> dict:
    events, _ = load_events(conn)
    segs = build_segments(events)
    summary = history_summary(segs)
    detail = []
    for s in segs:
        h = deterioration(s["events"])
        if h["sufficient"]:
            detail.append({"segment_id": s["id"], "road_name": s["road_name"],
                           **{k: h[k] for k in ("observations", "trips", "first_crack_at", "first_pothole_at",
                                                "days_crack_to_pothole", "class_changes")}})
    return {**summary, "segments_detail": detail[:200]}
