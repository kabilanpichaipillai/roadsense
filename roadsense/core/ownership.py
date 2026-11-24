"""Who owns the road, from Nova Scotia's open data.

    municipality_at   which of the 49 NS municipalities contains a point (bundled boundaries, works offline)
    road_owner        who owns the nearest road segment: the Province (TIR), a municipality, or someone else, from the
                      Nova Scotia Road Network (NSRN), queried live and cached

Both datasets are published under the Open Government Licence - Nova Scotia. The boundaries file is built by
scripts/build_ns_boundaries.py.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

import httpx

from ..common.config import settings
from .geo import BREAK_S, NETWORK_TIMEOUT_S, _seg_distance_m

BOUNDARIES = Path(__file__).with_name("ns_municipalities.json")
NSRN_URL = "https://data.novascotia.ca/resource/484g-adjn.json"
SEARCH_M = 25   # a defect is on the road: farther than this, the nearest segment is someone else's road
TIE_M = 5       # two owners this close together (an intersection): let the Location step weigh them
_down_until = 0.0


@lru_cache(maxsize=1)
def municipalities() -> list[dict]:
    return json.loads(BOUNDARIES.read_text(encoding="utf-8"))["municipalities"]


def _in_polygon(x: float, y: float, rings: list) -> bool:
    """Even-odd ray casting over the outer ring and its holes."""
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def municipality_at(lat: float, lon: float) -> dict | None:
    """{name, type, county} of the municipality containing the point, or None (outside Nova Scotia, or in water)."""
    for m in municipalities():
        x0, y0, x1, y1 = m["bbox"]
        if x0 <= lon <= x1 and y0 <= lat <= y1 and any(_in_polygon(lon, lat, p) for p in m["polygons"]):
            return {"name": m["name"], "type": m["type"], "county": m["county"]}
    return None


def _segment(row: dict, lat: float, lon: float) -> dict:
    lines = (row.get("the_geom") or {}).get("coordinates") or []
    d = min((_seg_distance_m((lat, lon), (a[1], a[0]), (b[1], b[0]))
             for line in lines for a, b in zip(line, line[1:])), default=math.inf)
    return {"owner": row.get("owner"), "owner_desc": row.get("owner_desc"), "street": row.get("street"),
            "distance_m": round(d, 1)}


def road_owner(conn: sqlite3.Connection, lat: float, lon: float) -> dict | None:
    """Owner of the nearest NSRN road segment within SEARCH_M: {owner, owner_desc, street, distance_m, tie}.
    owner is None when no owned road is that close (tracks and driveways have no owner). tie is the nearest segment
    of a different owner when it is within TIE_M of the first, else None. Returns None when NSRN can't be asked
    (offline mode, or a recent failure), so the caller falls back to the road-class rules."""
    global _down_until
    if settings.offline_geo or time.monotonic() < _down_until:
        return None
    key = f"nsrn:v1:{lat:.4f},{lon:.4f}"
    hit = conn.execute("SELECT value FROM geocache WHERE key = ?", (key,)).fetchone()
    if hit:
        return json.loads(hit["value"])
    dlat, dlon = SEARCH_M / 111_320, SEARCH_M / (111_320 * math.cos(math.radians(lat)))
    box = (f"POLYGON(({lon - dlon} {lat - dlat},{lon + dlon} {lat - dlat},{lon + dlon} {lat + dlat},"
           f"{lon - dlon} {lat + dlat},{lon - dlon} {lat - dlat}))")
    try:
        r = httpx.get(NSRN_URL, timeout=NETWORK_TIMEOUT_S, headers={"User-Agent": settings.nominatim_user_agent},
                      params={"$select": "the_geom,street,owner,owner_desc", "$limit": 50,
                              "$where": f"intersects(the_geom,'{box}') AND owner IS NOT NULL"})
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):  # Socrata reports query errors as an object, sometimes with HTTP 200
            raise ValueError(f"NSRN error: {rows}")
    except (httpx.HTTPError, ValueError):
        _down_until = time.monotonic() + BREAK_S
        return None
    segs = sorted((s for s in (_segment(row, lat, lon) for row in rows) if s["distance_m"] <= SEARCH_M),
                  key=lambda s: s["distance_m"])
    if segs:
        best = segs[0]
        other = next((s for s in segs if s["owner"] != best["owner"]), None)
        result = {**best, "tie": other if other and other["distance_m"] - best["distance_m"] <= TIE_M else None}
    else:
        result = {"owner": None, "owner_desc": None, "street": None, "distance_m": None, "tie": None}
    conn.execute("INSERT OR REPLACE INTO geocache (key, value) VALUES (?, ?)", (key, json.dumps(result)))
    conn.commit()
    return result
