"""Who owns the road, from Nova Scotia's open data.

    municipality_at   which of the 49 NS municipalities contains a point (bundled boundaries, works offline)
    road_owner        who owns the nearest road segment: the Province (TIR), a municipality, or someone else, from the
                      Nova Scotia Road Network (NSRN), queried live and cached

Both datasets are published under the Open Government Licence - Nova Scotia. The boundaries file is built by
scripts/build_ns_boundaries.py.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

BOUNDARIES = Path(__file__).with_name("ns_municipalities.json")


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
