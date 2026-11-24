"""Build roadsense/core/ns_municipalities.json from Nova Scotia's open Municipality Boundaries dataset.

The source GeoJSON is ~72 MB (full coastline detail); Douglas-Peucker at ~40 m brings it to ~1 MB, which is accurate
enough to say which municipality a pothole is in. Run again when the province publishes new boundaries:

    python scripts/build_ns_boundaries.py
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx

SOURCE = "https://data.novascotia.ca/api/geospatial/7bqh-hssn?method=export&format=GeoJSON"
OUT = Path(__file__).resolve().parents[1] / "roadsense" / "core" / "ns_municipalities.json"
TOLERANCE = 0.0005  # degrees, ~40 m


def simplify(points: list, tol: float) -> list:
    """Iterative Douglas-Peucker (rings have tens of thousands of points: no recursion)."""
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy or 1e-18
        best, idx = 0.0, None
        for i in range(a + 1, b):
            px, py = points[i]
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
            d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if idx is not None and best > tol * tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def build(geojson: dict) -> dict:
    out = []
    for f in geojson["features"]:
        props, polygons = f["properties"], []
        for poly in f["geometry"]["coordinates"]:
            rings = [[[round(x, 5), round(y, 5)] for x, y in simplify(ring, TOLERANCE)] for ring in poly]
            rings = [r for r in rings if len(r) >= 4]
            if rings:
                polygons.append(rings)
        xs = [x for poly in polygons for x, _ in poly[0]]
        ys = [y for poly in polygons for _, y in poly[0]]
        out.append({"name": props["fullname"], "type": props["featdesc"], "county": props["county"],
                    "bbox": [min(xs), min(ys), max(xs), max(ys)], "polygons": polygons})
    out.sort(key=lambda m: m["name"])
    return {"source": "Nova Scotia Municipality Boundaries (data.novascotia.ca/datasets/7bqh-hssn)",
            "licence": "Open Government Licence - Nova Scotia", "built": dt.date.today().isoformat(),
            "tolerance_deg": TOLERANCE, "municipalities": out}


if __name__ == "__main__":
    r = httpx.get(SOURCE, timeout=300, follow_redirects=True)
    r.raise_for_status()
    data = build(r.json())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"{len(data['municipalities'])} municipalities, {OUT.stat().st_size // 1024} KB -> {OUT}")
