"""Exposure-weighted risk: the same pothole is more dangerous in a bike lane or at a crossing than on a quiet street.

For each report RoadSense keeps three separate values, and never changes the detector's confidence:

    physical_score   0..1 from the existing severity model (severity band from width or box area, damage class,
                     repeat sightings). Depth is not measured from video.
    exposure_score   0..1 from OpenStreetMap infrastructure near the defect: cycling, pedestrian, public transport
                     and road context. A PROXY: a school or bus stop nearby is a reason to expect people, not a count
                     of them. There is no traffic-volume data.
    overall_score    weighted combination (core/exposure_rules.json), then Low / Medium / High / Critical.

Nearby features come from the Overpass API (Nominatim's reverse lookup returns only the road itself; the road class
still comes from the existing geocoding). Each OSM element is counted once, in one category, and only the most
relevant feature of a category scores, so ten footways do not add up to ten times the exposure. A feature is
"on this road" only when OSM ties it to the same named road (a bike lane tagged on it); anything else is "nearby",
with lower relevance, because the closest path is not necessarily connected to the damaged carriageway.

Results are cached: OSM answers in `geocache` (refreshed after ROADSENSE_OSM_CACHE_DAYS) and each report's score in
`report_risk` with a hash of its inputs, so a score is recomputed only when the report, its OSM data or the rules
change. When OSM is unavailable the exposure factors are marked missing (data quality drops), never guessed.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import httpx

from ..common.config import settings
from ..common.db import audit, now
from .geo import BREAK_S, _seg_distance_m

RULES_FILE = Path(__file__).with_name("exposure_rules.json")
CATEGORIES = ("low", "medium", "high", "critical")
FEATURE_LABELS = {"cycling": "Cycling infrastructure", "crossing": "Pedestrian crossing", "bus_stop": "Bus stop",
                  "school": "School", "footway": "Pedestrian path / sidewalk"}
FACTOR_LABELS = {"physical": "Physical damage severity", "cyclist": "Cyclist exposure",
                 "pedestrian": "Pedestrian exposure", "transit": "Public transport exposure",
                 "road_context": "Road context"}
DISCLAIMER = ("Exposure is estimated from nearby OpenStreetMap infrastructure, not measured traffic, and the weights "
              "are illustrative starting values, not validated safety measurements.")
CYCLE_LANE_VALUES = {"lane", "track", "shared_lane", "share_busway", "opposite_lane", "opposite_track", "separate"}
FOOT_HIGHWAYS = {"footway", "pedestrian", "path", "steps", "living_street"}
NOT_ROADS = FOOT_HIGHWAYS | {"cycleway", "bridleway", "corridor", "elevator", "platform", "proposed", "construction"}
KEEP_TAGS = ("highway", "name", "cycleway", "cycleway:left", "cycleway:right", "cycleway:both", "sidewalk",
             "sidewalk:left", "sidewalk:right", "sidewalk:both", "crossing", "public_transport", "bus", "amenity",
             "bicycle", "foot")
OSM_TIMEOUT_S = 25
_lock = threading.Lock()
_last_call = 0.0
_down_until = 0.0


def reset() -> None:
    global _down_until
    _down_until = 0.0


# ---------------- configuration ----------------
@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _strip(d):
    return {k: _strip(v) for k, v in d.items() if not k.startswith("_")} if isinstance(d, dict) else d


def rules(override: dict | None = None) -> dict:
    r = override or _strip(_load(settings.exposure_rules_file or str(RULES_FILE)))
    c = r["categories"]
    if not 0 <= c["medium"] <= c["high"] <= c["critical"] <= 1:
        raise ValueError("risk categories need 0 <= medium <= high <= critical <= 1")
    if any(w < 0 for w in r["weights"].values()) or not sum(r["weights"].values()):
        raise ValueError("risk weights must be non-negative and not all zero")
    return r


def rules_hash(r: dict | None = None) -> str:
    return hashlib.sha256(json.dumps(r or rules(), sort_keys=True).encode()).hexdigest()[:16]


def valid_gps(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0)


# ---------------- OpenStreetMap (Overpass) ----------------
def overpass_query(lat: float, lon: float, d: dict) -> str:
    road = max(d["cycle_lane_on_road"], d["cycleway"], d["footway"])
    p = f"{lat:.6f},{lon:.6f}"
    return (f'[out:json][timeout:{OSM_TIMEOUT_S}];('
            f'way(around:{road},{p})["highway"];'
            f'node(around:{max(d["crossing"], d["bus_stop"])},{p})["highway"~"^(crossing|bus_stop)$"];'
            f'node(around:{d["crossing"]},{p})["crossing"];'
            f'node(around:{d["bus_stop"]},{p})["public_transport"="platform"];'
            f'nwr(around:{d["school"]},{p})["amenity"~"^(school|kindergarten|college)$"];'
            ');out tags geom;')


def _compact(el: dict) -> dict:
    out = {"type": el.get("type"), "id": el.get("id"),
           "tags": {k: v for k, v in (el.get("tags") or {}).items() if k in KEEP_TAGS}}
    if el.get("type") == "node":
        out["points"] = [[el.get("lat"), el.get("lon")]]
    elif el.get("geometry"):
        out["points"] = [[g["lat"], g["lon"]] for g in el["geometry"] if g]
    elif el.get("members"):
        out["points"] = [[g["lat"], g["lon"]] for m in el["members"] for g in (m.get("geometry") or []) if g]
    elif el.get("bounds"):
        b = el["bounds"]
        out["points"] = [[(b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2]]
    else:
        out["points"] = []
    out["points"] = [p for p in out["points"] if None not in p]
    return out


def _age_days(ts: str | None) -> float:
    if not ts:
        return float("inf")
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 86400


def fetch_osm(conn: sqlite3.Connection, lat: float, lon: float, client: httpx.Client | None = None,
              r: dict | None = None) -> dict:
    """OSM elements around a point: {available, elements, source, fetched_at, stale, error}. Cached in geocache."""
    global _last_call, _down_until
    r = r or rules()
    key = f"osm:v1:{lat:.4f},{lon:.4f}:{rules_hash({'d': r['distances_m']})}"
    hit = conn.execute("SELECT value FROM geocache WHERE key = ?", (key,)).fetchone()
    cached = json.loads(hit["value"]) if hit else None
    if cached and _age_days(cached.get("fetched_at")) < settings.osm_cache_days:
        return {**cached, "available": True, "stale": False, "cached": True, "error": None}

    def fallback(error: str) -> dict:
        if cached:  # old OSM data is better than none, but it is flagged as stale
            return {**cached, "available": True, "stale": True, "cached": True, "error": error}
        return {"available": False, "elements": [], "source": None, "fetched_at": None, "stale": False,
                "cached": False, "error": error}

    if client is None and (settings.offline_geo or time.monotonic() < _down_until):
        return fallback("OpenStreetMap not contacted (offline mode or recent failure)")
    http = client or httpx.Client(timeout=OSM_TIMEOUT_S + 35,  # public instances queue busy requests
                                  headers={"User-Agent": settings.nominatim_user_agent})
    query = {"data": overpass_query(lat, lon, r["distances_m"])}
    # each configured instance in turn (ROADSENSE_OVERPASS_URL may list mirrors); busy public servers return 429/5xx
    urls = [u.strip() for u in settings.overpass_url.split(",") if u.strip()]
    elements, error = None, None
    for url in urls * 2 if len(urls) == 1 else urls:  # a single instance gets one retry
        try:
            with _lock:  # the public Overpass instance asks for at most about one request a second
                wait = 1.0 - (time.monotonic() - _last_call)
                if wait > 0 and client is None:
                    time.sleep(wait)
                resp = http.post(url, data=query)
                _last_call = time.monotonic()
            resp.raise_for_status()
            elements = resp.json().get("elements")
            if not isinstance(elements, list):
                raise ValueError("Overpass response has no element list")
            break
        except httpx.HTTPStatusError as e:
            error = e
            if e.response.status_code < 500 and e.response.status_code != 429:
                break  # a bad request will not get better on another try
        except (httpx.HTTPError, ValueError) as e:
            error = e
    if elements is None:
        _down_until = time.monotonic() + BREAK_S
        return fallback(f"OpenStreetMap (Overpass) unavailable: {type(error).__name__}: {error}"[:300])
    value = {"elements": [_compact(e) for e in elements], "source": "OpenStreetMap (Overpass API)",
             "fetched_at": now()}
    conn.execute("INSERT OR REPLACE INTO geocache (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    return {**value, "available": True, "stale": False, "cached": False, "error": None}


# ---------------- features ----------------
def _distance(el: dict, lat: float, lon: float) -> float:
    pts = el.get("points") or []
    if not pts:
        return float("inf")
    if len(pts) == 1:
        return _seg_distance_m((lat, lon), tuple(pts[0]), tuple(pts[0]))
    return min(_seg_distance_m((lat, lon), tuple(a), tuple(b)) for a, b in zip(pts, pts[1:]))


def _same_road(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def _has_cycle_lane(tags: dict) -> bool:
    return any(str(tags.get(k, "")).lower() in CYCLE_LANE_VALUES
               for k in ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both"))


def _has_sidewalk(tags: dict) -> bool:
    if str(tags.get("sidewalk", "")).lower() in ("both", "left", "right", "yes", "separate"):
        return True
    return any(str(tags.get(k, "")).lower() in ("yes", "separate") for k in ("sidewalk:left", "sidewalk:right",
                                                                              "sidewalk:both"))


def classify(el: dict, d: float, road_name: str | None, r: dict) -> dict | None:
    """The one category an OSM element counts in (or None), with how it relates to the damaged road. Priority:
    cycling > crossing > bus stop > school > footway, so an element never counts twice."""
    tags, dist, rel = el.get("tags") or {}, r["distances_m"], r["relevance"]
    hw, name = tags.get("highway"), tags.get("name")

    def feature(category, relation, base, limit):
        if d > limit:
            return None
        return {"category": category, "relation": relation, "relevance": round(base * (1 - d / limit), 3),
                "osm_type": el["type"], "osm_id": el["id"], "name": name, "distance_m": round(d, 1),
                "tags": tags}

    if el["type"] == "way" and hw and hw not in NOT_ROADS and _has_cycle_lane(tags):
        if _same_road(name, road_name):
            return feature("cycling", "bike lane on this road", rel["cycle_lane_on_road"], dist["cycle_lane_on_road"])
        return feature("cycling", "bike lane on a nearby road", rel["cycleway"], dist["cycleway"])
    if el["type"] == "way" and (hw == "cycleway" or (hw == "path" and tags.get("bicycle") == "designated")):
        return feature("cycling", "separate cycle path nearby (may not share this road)", rel["cycleway"],
                       dist["cycleway"])
    if el["type"] == "node" and (hw == "crossing" or "crossing" in tags):
        return feature("crossing", "pedestrian crossing nearby", rel["crossing"], dist["crossing"])
    if el["type"] == "node" and (hw == "bus_stop" or (tags.get("public_transport") == "platform"
                                                      and tags.get("bus") in (None, "yes"))):
        return feature("bus_stop", "bus stop nearby", rel["bus_stop"], dist["bus_stop"])
    if tags.get("amenity") in ("school", "kindergarten", "college"):
        return feature("school", "school nearby (a reason to expect pedestrians, not a count of them)",
                       rel["school"], dist["school"])
    if el["type"] == "way" and hw in FOOT_HIGHWAYS:
        return feature("footway", "footway or path nearby", rel["footway"], dist["footway"])
    if el["type"] == "way" and hw and hw not in NOT_ROADS and _has_sidewalk(tags) and _same_road(name, road_name):
        return feature("footway", "sidewalk on this road", rel["footway"], dist["footway"])
    return None


def features(elements: list[dict], lat: float, lon: float, road_name: str | None, r: dict) -> list[dict]:
    """Relevant OSM features around a defect, each element once. Two tagged copies of the same real-world thing (a
    bus-stop node and its platform, both named) within 20 m are merged."""
    seen, out = set(), []
    for el in elements:
        if not el.get("type") or el.get("id") is None or (el["type"], el["id"]) in seen:
            continue
        seen.add((el["type"], el["id"]))
        f = classify(el, _distance(el, lat, lon), road_name, r)
        if f:
            out.append(f)
    out.sort(key=lambda f: -f["relevance"])
    merged: list[dict] = []
    for f in out:
        twin = next((m for m in merged if m["category"] == f["category"] and f["name"] and m["name"] == f["name"]
                     and abs(m["distance_m"] - f["distance_m"]) <= 20), None)
        if twin is None:
            merged.append(f)
    return merged


# ---------------- scoring ----------------
def band_for(report: dict) -> str:
    band = report.get("severity_band")
    if band in ("S1", "S2", "S3", "S4"):
        return band
    return {"low": "S1", "medium": "S2", "high": "S3"}.get(report.get("severity"), "S2")


def physical_factor(report: dict, r: dict) -> dict:
    p = r["physical"]
    band = band_for(report)
    base = p["band"][band] * p["class_factor"].get(report.get("class_code"), 0.5)
    confirmations = max(int(report.get("confirmations") or 1), 1)
    boost = p["confirmation_boost"] * (min(confirmations, p["max_confirmations"]) - 1)
    width = report.get("width_m")
    raw = {"class_code": report.get("class_code"), "severity_band": band, "severity": report.get("severity"),
           "width_cm": round(width * 100) if width else None, "depth": "not measured",
           "confirmations": confirmations, "basis": "estimated width" if width and report.get("class_code") == "D40"
           else "box area (no width estimate)" if report.get("class_code") == "D40" else "crack class and box area"}
    return {"factor": "physical", "value": round(min(1.0, base + boost), 3), "raw": raw, "available": True,
            "coverage": 1.0 if width or report.get("class_code") != "D40" else 0.8}


def exposure_factors(feats: list[dict], osm_ok: bool, road_class: str | None, r: dict) -> list[dict]:
    def best(cats):
        hits = [f for f in feats if f["category"] in cats]
        top = max(hits, key=lambda f: f["relevance"], default=None)
        return (top["relevance"] if top else 0.0), top, len(hits)

    out = []
    for name, cats in (("cyclist", ("cycling",)), ("pedestrian", ("crossing", "school", "footway")),
                       ("transit", ("bus_stop",))):
        if not osm_ok:
            out.append({"factor": name, "value": None, "available": False, "coverage": 0.0,
                        "raw": {"reason": "no OpenStreetMap data for this location"}})
            continue
        value, top, n = best(cats)
        raw = {"features": n, "proxy": True}
        if top:
            raw.update(top_feature=FEATURE_LABELS[top["category"]], relation=top["relation"],
                       distance_m=top["distance_m"], name=top["name"])
        out.append({"factor": name, "value": round(value, 3), "available": True, "coverage": 1.0, "raw": raw})
    table = r["road_context"]
    if road_class and road_class in table:
        out.append({"factor": "road_context", "value": table[road_class], "available": True, "coverage": 1.0,
                    "raw": {"road_class": road_class, "traffic_counts": "not available"}})
    else:
        out.append({"factor": "road_context", "value": None, "available": False, "coverage": 0.0,
                    "raw": {"road_class": road_class, "reason": "road class unknown" if not road_class
                            else f"unrecognised road class '{road_class}'"}})
    return out


def combine(factors: list[dict], band: str, r: dict) -> dict:
    """Weighted sum (missing factors score 0 and lower data quality) and the category."""
    w = r["weights"]
    total = sum(w.values())
    for f in factors:
        f["weight"] = round(w[f["factor"]] / total, 4)
        f["contribution"] = round(f["weight"] * (f["value"] or 0.0), 4) if f["available"] else 0.0
        f["label"] = FACTOR_LABELS[f["factor"]]
    overall = round(sum(f["contribution"] for f in factors), 4)
    exp = [f for f in factors if f["factor"] != "physical"]
    exp_w = sum(f["weight"] for f in exp)
    infra_known = all(f["available"] for f in exp if f["factor"] != "road_context")
    # without the infrastructure (OSM) part, road class alone is not an exposure estimate: report it as unknown
    exposure = round(sum(f["contribution"] for f in exp) / exp_w, 4) if infra_known and exp_w else None
    quality = round(sum(f["weight"] * f["coverage"] for f in factors if f["available"]), 3)
    c = r["categories"]
    category = "high" if overall >= c["high"] else "medium" if overall >= c["medium"] else "low"
    if overall >= c["critical"] and band == c["critical_band"]:
        category = "critical"
    return {"overall": overall, "exposure": exposure, "data_quality": quality, "category": category}


def explain(report: dict, factors: list[dict], res: dict, feats: list[dict], osm: dict) -> str:
    f = {x["factor"]: x for x in factors}
    ph = f["physical"]["raw"]
    what = {"D40": "pothole", "D20": "alligator crack", "D10": "transverse crack", "D00": "longitudinal crack"}.get(
        ph["class_code"], "defect")
    size = f" about {ph['width_cm']} cm wide" if ph.get("width_cm") else ""
    parts = [f"{what}{size}, severity band {ph['severity_band']}"
             + (f", seen by {ph['confirmations']} drivers" if ph["confirmations"] > 1 else "")]
    if not osm.get("available"):
        parts.append("nearby infrastructure unknown (no OpenStreetMap data), so the score reflects physical "
                     "severity only")
    else:
        near = []
        for cat in sorted({x["category"] for x in feats}, key=lambda c: -max(x["relevance"] for x in feats
                                                                             if x["category"] == c)):
            same = sorted((x for x in feats if x["category"] == cat), key=lambda x: -x["relevance"])
            top = same[0]
            count = f"{len(same)} mapped, " if len(same) > 1 else ""
            near.append(f"{FEATURE_LABELS[cat].lower()} ({count}nearest {top['distance_m']:.0f} m: {top['relation']})")
        parts.append("near " + "; ".join(near) if near else "no cycling, pedestrian or bus infrastructure mapped nearby")
        if osm.get("stale"):
            parts.append("OpenStreetMap data is older than the refresh period")
    rc = f["road_context"]
    parts.append(f"road class {rc['raw']['road_class']}" if rc["available"] else "road class unknown")
    return f"{res['category'].capitalize()} risk: " + "; ".join(parts) + ". Exposure is a proxy, not measured traffic."


# ---------------- neighbourhood ----------------
NEIGHBOURHOOD_SOURCE = "openstreetmap_nominatim"


def neighbourhood_for(conn: sqlite3.Connection, lat, lon) -> str | None:
    """The OSM suburb of the cached reverse geocode of the position, or None. No network call."""
    if not valid_gps(lat, lon):
        return None
    hit = conn.execute("SELECT value FROM geocache WHERE key = ?", (f"v3:{float(lat):.4f},{float(lon):.4f}",)).fetchone()
    place = json.loads(hit["value"]) if hit else None
    if place and place.get("suburb") and place.get("source") == NEIGHBOURHOOD_SOURCE:
        return str(place["suburb"]).strip()
    return None


# ---------------- per report ----------------
def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def compute(conn: sqlite3.Connection, report: dict, client: httpx.Client | None = None, force: bool = False,
            r: dict | None = None) -> dict:
    """Score one report and store it. Returns the stored row; reuses it when nothing it depends on changed."""
    r = r or rules()
    rhash = rules_hash(r)
    gps_ok = valid_gps(report.get("lat"), report.get("lon"))
    if gps_ok:
        osm = fetch_osm(conn, float(report["lat"]), float(report["lon"]), client, r)
        osm_status = "ok" if osm["available"] and not osm.get("stale") else "stale" if osm["available"] else \
            "unavailable"
    else:
        osm = {"available": False, "elements": [], "error": "invalid GPS coordinates"}
        osm_status = "invalid_gps"
    inputs = {k: report.get(k) for k in ("lat", "lon", "class_code", "severity", "severity_band", "width_m",
                                         "confirmations", "road_class", "road_name")}
    ihash = _hash({"report": inputs, "osm": osm.get("fetched_at"), "osm_status": osm_status, "rules": rhash})
    existing = conn.execute("SELECT * FROM report_risk WHERE report_id = ?", (report["id"],)).fetchone()
    if existing and existing["inputs_hash"] == ihash and not force:
        return {**dict(existing), "recomputed": False}
    feats = features(osm.get("elements") or [], float(report["lat"]), float(report["lon"]), report.get("road_name"),
                     r) if gps_ok and osm["available"] else []
    factors = [physical_factor(report, r)] + exposure_factors(feats, gps_ok and osm["available"],
                                                              report.get("road_class"), r)
    band = band_for(report)
    res = combine(factors, band, r)
    text = explain(report, factors, res, feats, osm if gps_ok else {"available": False})
    if not gps_ok:
        text = text.replace("nearby infrastructure unknown (no OpenStreetMap data)",
                            "nearby infrastructure unknown (invalid GPS position)")
    hood = neighbourhood_for(conn, report.get("lat"), report.get("lon")) if gps_ok else None
    conn.execute("DELETE FROM exposure_features WHERE report_id = ?", (report["id"],))
    for f in feats:
        conn.execute("INSERT INTO exposure_features (report_id, osm_type, osm_id, category, name, distance_m, relation,"
                     " relevance, tags) VALUES (?,?,?,?,?,?,?,?,?)",
                     (report["id"], f["osm_type"], f["osm_id"], f["category"], f["name"], f["distance_m"],
                      f["relation"], f["relevance"], json.dumps(f["tags"])))
    conn.execute(
        "INSERT INTO report_risk (report_id, physical_score, exposure_score, overall_score, category, data_quality, "
        "factors, explanation, osm_status, osm_source, osm_fetched_at, neighbourhood, rules_hash, inputs_hash, "
        "computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(report_id) DO UPDATE SET "
        "physical_score = excluded.physical_score, exposure_score = excluded.exposure_score, "
        "overall_score = excluded.overall_score, category = excluded.category, data_quality = excluded.data_quality, "
        "factors = excluded.factors, explanation = excluded.explanation, osm_status = excluded.osm_status, "
        "osm_source = excluded.osm_source, osm_fetched_at = excluded.osm_fetched_at, "
        "neighbourhood = excluded.neighbourhood, rules_hash = excluded.rules_hash, "
        "inputs_hash = excluded.inputs_hash, computed_at = excluded.computed_at",
        (report["id"], factors[0]["value"], res["exposure"], res["overall"], res["category"], res["data_quality"],
         json.dumps(factors, default=str), text, osm_status, osm.get("source"), osm.get("fetched_at"), hood, rhash,
         ihash, now()))
    return {**dict(conn.execute("SELECT * FROM report_risk WHERE report_id = ?", (report["id"],)).fetchone()),
            "recomputed": True}


def pending(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Reports without a current score: never scored, changed since, or scored with other rules."""
    rhash = rules_hash()
    sql = ("SELECT r.* FROM reports r LEFT JOIN report_risk k ON k.report_id = r.id WHERE NOT (r.status = 'closed' AND "
           "r.closed_reason IS 'rejected') AND "
           "(k.report_id IS NULL OR k.rules_hash != ? OR r.updated_at > k.computed_at OR k.osm_status IN "
           "('unavailable', 'stale')) ORDER BY (k.report_id IS NOT NULL), r.priority DESC, r.id")
    rows = [dict(x) for x in conn.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), (rhash,))]
    return rows


def run(conn: sqlite3.Connection, client: httpx.Client | None = None, limit: int | None = None,
        actor: str = "service") -> dict:
    todo = pending(conn, limit or settings.exposure_batch)
    done = {"scored": 0, "unchanged": 0, "osm_unavailable": 0}
    for rep in todo:
        res = compute(conn, rep, client)
        done["scored" if res["recomputed"] else "unchanged"] += 1
        done["osm_unavailable"] += res["osm_status"] in ("unavailable", "invalid_gps")
    if done["scored"]:
        audit(conn, actor, "exposure_scored", None, done)
    return done


# ---------------- reads for the API ----------------
def _row(conn, x: dict) -> dict:
    x = dict(x)
    x["factors"] = json.loads(x["factors"])
    x["features"] = [{**dict(f), "tags": json.loads(f["tags"] or "{}"), "label": FEATURE_LABELS[f["category"]]}
                     for f in conn.execute("SELECT * FROM exposure_features WHERE report_id = ? ORDER BY relevance "
                                           "DESC", (x["report_id"],))]
    return x


def detail(conn: sqlite3.Connection, report_id: int) -> dict | None:
    x = conn.execute("SELECT * FROM report_risk WHERE report_id = ?", (report_id,)).fetchone()
    return {**_row(conn, x), "disclaimer": DISCLAIMER} if x else None


def listing(conn: sqlite3.Connection, category: str | None = None, damage: str | None = None,
            neighbourhood: str | None = None, road_class: str | None = None, infrastructure: str | None = None) -> dict:
    rows = [dict(x) for x in conn.execute(
        "SELECT k.*, r.class_code, r.class_name, r.status, r.address, r.lat, r.lon, r.road_name, r.road_class, "
        "r.severity, r.severity_band, r.width_m, r.confirmations, r.jurisdiction, r.verification_status "
        "FROM report_risk k JOIN reports r ON r.id = k.report_id "
        "WHERE NOT (r.status = 'closed' AND r.closed_reason IS 'rejected') "
        "ORDER BY k.overall_score DESC")]
    feats: dict[int, set] = {}
    for f in conn.execute("SELECT report_id, category FROM exposure_features"):
        feats.setdefault(f["report_id"], set()).add(f["category"])
    for x in rows:
        x["infrastructure"] = sorted(feats.get(x["report_id"], ()))
        x["neighbourhood"] = x["neighbourhood"] or "Unassigned"
        x["road_class"] = x["road_class"] or "unknown"
        factors = json.loads(x.pop("factors"))
        x["main_factors"] = [{"factor": f["factor"], "label": f["label"], "contribution": f["contribution"]}
                             for f in sorted(factors, key=lambda f: -f["contribution"]) if f["contribution"] > 0][:3]
    all_rows = rows
    if category:
        rows = [x for x in rows if x["category"] == category]
    if damage:
        rows = [x for x in rows if x["class_code"] == damage]
    if neighbourhood:
        rows = [x for x in rows if x["neighbourhood"] == neighbourhood]
    if road_class:
        rows = [x for x in rows if x["road_class"] == road_class]
    if infrastructure:
        rows = [x for x in rows if infrastructure in x["infrastructure"]]
    near = lambda cat: sum(cat in x["infrastructure"] for x in all_rows)
    total = conn.execute("SELECT COUNT(*) FROM reports WHERE NOT (status = 'closed' AND "
                         "closed_reason IS 'rejected')").fetchone()[0]
    summary = {"defects": total, "scored": len(all_rows), **{c: sum(x["category"] == c for x in all_rows)
                                                              for c in CATEGORIES},
               "near_cycling": near("cycling"), "near_crossing": near("crossing"), "near_school": near("school"),
               "near_bus_stop": near("bus_stop"), "near_footway": near("footway"),
               "osm_unavailable": sum(x["osm_status"] in ("unavailable", "invalid_gps") for x in all_rows),
               "osm_stale": sum(x["osm_status"] == "stale" for x in all_rows),
               "unscored": max(total - len(all_rows), 0)}
    options = {"neighbourhoods": sorted({x["neighbourhood"] for x in all_rows}),
               "road_classes": sorted({x["road_class"] for x in all_rows}),
               "damage": sorted({x["class_code"] for x in all_rows}), "infrastructure": list(FEATURE_LABELS)}
    r = rules()
    return {"summary": summary, "reports": rows, "options": options, "weights": r["weights"],
            "thresholds": r["categories"], "disclaimer": DISCLAIMER, "feature_labels": FEATURE_LABELS}
