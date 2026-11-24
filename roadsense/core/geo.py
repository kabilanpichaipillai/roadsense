"""Address, road and jurisdiction lookup: OpenStreetMap Nominatim with a SQLite cache and an offline Halifax fixture."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time

import httpx

from ..common.config import settings

# Halifax Regional Municipality, rough bounding box (lat_min, lat_max, lon_min, lon_max)
OFFLINE_REGIONS = [("Halifax Regional Municipality", "Nova Scotia", (44.40, 45.25, -64.20, -62.50))]
# Offline road fixture so routing works without network: (name, class, ref, path, buffer m). Each path is a polyline
# of OSM geometry (via OSRM), south to north. Bedford Highway curves around Bedford Basin, so a straight line between
# its ends would cross the water.
OFFLINE_ROADS = [
    ("Highway 102", "motorway", "NS 102", [(44.66842, -63.67697), (44.69157, -63.68247)], 80),
    ("Bedford Highway", "primary", "NS 2", [
        (44.66249, -63.62983), (44.66351, -63.63282), (44.66549, -63.63579), (44.66735, -63.63805),
        (44.66937, -63.64037), (44.67144, -63.6427), (44.67341, -63.64497), (44.67511, -63.64745),
        (44.67749, -63.64932), (44.67952, -63.65158), (44.68017, -63.65517), (44.68135, -63.65884),
        (44.68333, -63.66099), (44.68603, -63.65944), (44.69068, -63.65989), (44.69405, -63.66091),
        (44.69819, -63.66102)], 40),
]


def point_on_road(index: int, t: float) -> tuple[float, float]:
    """The point a fraction t (0..1) of the way along an offline road, measured along its path."""
    path = OFFLINE_ROADS[index][3]
    legs = [math.dist(a, b) for a, b in zip(path, path[1:])]
    goal = max(0.0, min(1.0, t)) * sum(legs)
    for i, leg in enumerate(legs):
        if goal <= leg or i == len(legs) - 1:
            (a, b), f = (path[i], path[i + 1]), min(1.0, goal / leg) if leg else 0.0
            return round(a[0] + f * (b[0] - a[0]), 6), round(a[1] + f * (b[1] - a[1]), 6)
        goal -= leg
    return path[-1]
PROVINCIAL = "Nova Scotia Public Works"
_lock = threading.Lock()
_last_call = 0.0
_down_until = 0.0  # circuit breaker: after a failure, use the offline fixture for a while instead of waiting again
NETWORK_TIMEOUT_S, BREAK_S = 5, 120


def _seg_distance_m(p, a, b) -> float:
    """Distance from point p to segment ab in metres (local flat approximation)."""
    k = 111_320.0
    c = math.cos(math.radians(p[0]))
    ax, ay, bx, by, px, py = a[1] * k * c, a[0] * k, b[1] * k * c, b[0] * k, p[1] * k * c, p[0] * k
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _offline(lat: float, lon: float) -> dict:
    out = {"address": f"near {lat:.5f}, {lon:.5f}", "municipality": None, "province": None, "country": None,
           "road": None, "road_class": None, "road_ref": None, "source": "offline_fixture"}
    for name, province, (a, b, c, d) in OFFLINE_REGIONS:
        if a <= lat <= b and c <= lon <= d:
            out.update(municipality=name, province=province, country="Canada")
    for name, cls, ref, path, buf in OFFLINE_ROADS:
        if min(_seg_distance_m((lat, lon), a, b) for a, b in zip(path, path[1:])) <= buf:
            out.update(road=name, road_class=cls, road_ref=ref, address=f"{name} near {lat:.5f}, {lon:.5f}")
    return out


def _municipality(addr: dict) -> str | None:
    # The local government that maintains the road: OSM's "municipality" (e.g. Halifax Regional Municipality, which
    # contains the communities Halifax, Dartmouth, Bedford...), else the city/town itself
    return (addr.get("municipality") or addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("county") or None)


def reverse_geocode(conn: sqlite3.Connection, lat: float, lon: float) -> dict:
    global _last_call, _down_until
    key = f"v3:{lat:.4f},{lon:.4f}"  # ~10 m cells: one lookup serves every sighting of the same spot
    hit = conn.execute("SELECT value FROM geocache WHERE key = ?", (key,)).fetchone()
    if hit:
        return {**json.loads(hit["value"]), "cached": True}
    if settings.offline_geo or time.monotonic() < _down_until:
        return _offline(lat, lon)
    try:
        with _lock:  # Nominatim usage policy: at most 1 request per second, identify the application
            wait = 1.05 - (time.monotonic() - _last_call)
            if wait > 0:
                time.sleep(wait)
            # zoom 17 = streets, so the matched object is the road itself, with its class and route number
            r = httpx.get(f"{settings.nominatim_url}/reverse", timeout=NETWORK_TIMEOUT_S,
                          params={"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1, "namedetails": 1,
                                  "zoom": 17},
                          headers={"User-Agent": settings.nominatim_user_agent})
            _last_call = time.monotonic()
        r.raise_for_status()
        data = r.json()
        addr = data.get("address", {})
        is_road = data.get("category") == "highway"
        road = addr.get("road") or (data.get("name") if is_road else None)
        result = {"address": road or data.get("display_name", key),
                  "suburb": addr.get("suburb") or addr.get("neighbourhood"),
                  "municipality": _municipality(addr), "province": addr.get("state"), "country": addr.get("country"),
                  "road": road, "road_class": data.get("type") if is_road else None,
                  "road_ref": (data.get("namedetails") or {}).get("ref") if is_road else None,
                  "source": "openstreetmap_nominatim"}
    except (httpx.HTTPError, ValueError):
        _down_until = time.monotonic() + BREAK_S
        return _offline(lat, lon)
    conn.execute("INSERT OR REPLACE INTO geocache (key, value) VALUES (?, ?)", (key, json.dumps(result)))
    conn.commit()
    return result


_snap_down_until = 0.0


def snap_to_road(conn: sqlite3.Connection, lat: float, lon: float) -> dict:
    """Nearest point on the drivable road network (OSRM "nearest"). GPS drifts 5-10 m and the camera projection adds
    its own error, so a defect can land beside the road; potholes are on it. Snaps only within settings.max_snap_m:
    a point farther away is more likely a parking lot or private road than a GPS error, and is left as measured.
    Returns {lat, lon, snapped, distance_m, road}."""
    global _snap_down_until
    out = {"lat": lat, "lon": lon, "snapped": False, "distance_m": None, "road": None}
    if settings.offline_geo or time.monotonic() < _snap_down_until:
        return out
    key = f"snap:{lat:.5f},{lon:.5f}"
    hit = conn.execute("SELECT value FROM geocache WHERE key = ?", (key,)).fetchone()
    if hit:
        return json.loads(hit["value"])
    try:
        r = httpx.get(f"{settings.osrm_url}/nearest/v1/driving/{lon},{lat}", params={"number": 1},
                      timeout=NETWORK_TIMEOUT_S, headers={"User-Agent": settings.nominatim_user_agent})
        r.raise_for_status()
        w = r.json()["waypoints"][0]
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        _snap_down_until = time.monotonic() + BREAK_S
        return out
    if w["distance"] <= settings.max_snap_m:
        out = {"lat": round(w["location"][1], 6), "lon": round(w["location"][0], 6), "snapped": True,
               "distance_m": round(w["distance"], 1), "road": w.get("name") or None}
    else:
        out.update(distance_m=round(w["distance"], 1), road=w.get("name") or None)
    conn.execute("INSERT OR REPLACE INTO geocache (key, value) VALUES (?, ?)", (key, json.dumps(out)))
    conn.commit()
    return out


NON_PUBLIC = {"P": "Private road", "F": "Federal road"}


def _owned_by(code: str, desc: str | None, street: str | None, muni: str | None, provincial: str) -> dict:
    road = street or "road"
    if code == "TIR":
        return {"authority": provincial, "confidence": "high", "evidence": f"NSRN: {road} is owned by the Province"}
    if code == "M":
        if muni:
            return {"authority": muni, "confidence": "high", "evidence": f"NSRN: {road} is municipally owned, in {muni}"}
        return {"authority": None, "confidence": "medium",
                "evidence": f"NSRN: {road} is municipally owned, but the point is outside known boundaries"}
    name = NON_PUBLIC.get(code, f"Other owner ({desc or code})")
    return {"authority": name, "confidence": "low",
            "evidence": f"NSRN: {road} is owned by {desc or code}; no public road authority to file with"}


def jurisdiction_candidates(place: dict, owner: dict | None = None) -> list[dict]:
    """Who maintains this road, best first, with confidence and the evidence for each.

    With a road owner from the Nova Scotia Road Network (core.ownership.road_owner) the owner decides: the Province
    (TIR), the municipality the point is in, or a non-public owner. Two owners at an intersection are both offered at
    medium confidence for the Location step to weigh.

    Without one, the road class decides: 100-series highways and motorways are provincial. Other numbered provincial
    routes (NS 2, Trunk 7, Route 333...) run through municipalities and ownership varies by section, so they are
    returned as ambiguous for the agent to weigh; local streets belong to the municipality."""
    from .open311 import rules

    r = rules()
    provincial = r.get("provincial_authority", PROVINCIAL)
    if owner and owner.get("owner"):
        muni = place.get("municipality")
        best = _owned_by(owner["owner"], owner.get("owner_desc"), owner.get("street"), muni, provincial)
        tie = owner.get("tie")
        if tie:
            other = _owned_by(tie["owner"], tie.get("owner_desc"), tie.get("street"), muni, provincial)
            return [{**c, "confidence": "medium", "source": "nsrn"} for c in (best, other)]
        out = [{**best, "source": "nsrn"}]
        if owner["owner"] == "TIR":
            out.append({"authority": muni, "confidence": "low", "evidence": "municipality surrounding the road",
                        "source": "nsrn"})
        return out
    return [{**c, "source": "rules"} for c in _rule_candidates(place, r, provincial)]


def _rule_candidates(place: dict, r: dict, provincial: str) -> list[dict]:
    lo, hi = r.get("provincial_route_range", [100, 199])
    muni, cls, ref = place.get("municipality"), place.get("road_class"), place.get("road_ref") or ""
    numbers = [int(n) for n in re.findall(r"\d+", ref)]
    if cls in r.get("provincial_road_classes", ["motorway", "motorway_link"]) or any(lo <= n <= hi for n in numbers):
        return [{"authority": provincial, "confidence": "high",
                 "evidence": f"{place.get('road') or 'road'} is a {lo}-series highway ({ref or cls})"},
                {"authority": muni, "confidence": "low", "evidence": "municipality surrounding the highway"}]
    if numbers or cls in r.get("ambiguous_road_classes", ["trunk"]):
        return [{"authority": muni, "confidence": "medium",
                 "evidence": f"numbered route {ref or cls} inside {muni}; many urban sections are municipally "
                             "maintained"},
                {"authority": provincial, "confidence": "medium",
                 "evidence": f"{ref or cls} is a numbered provincial route; rural sections are provincially maintained"}]
    return [{"authority": muni, "confidence": "high" if muni else "low",
             "evidence": f"local road ({cls or 'unknown class'}) in {muni or 'unknown municipality'}"}]
