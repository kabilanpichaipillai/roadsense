# Road Ownership from NS Open Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route each defect to the authority that actually owns the road, using NSRN road ownership (live, cached) and bundled NS municipal boundaries.

**Architecture:** A new `roadsense/core/ownership.py` answers two questions: who owns the nearest road (NSRN Socrata query, cached in `geocache`, circuit breaker), and which municipality contains the point (point-in-polygon over a bundled, simplified JSON). `geo.jurisdiction_candidates` uses the owner when it has one and the existing road-class rules when it doesn't. `geo_find_jurisdiction` wires them together.

**Tech Stack:** Python 3.10+, httpx, sqlite3, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-24-road-ownership-design.md`

## Global Constraints

- No new dependencies (pure-Python geometry).
- NSRN endpoint: `https://data.novascotia.ca/resource/484g-adjn.json`, filter `intersects(the_geom,'POLYGON(...)') AND owner IS NOT NULL`, `$limit=50`.
- Search box half-size 25 m; intersection tie when the two nearest owned segments differ in owner and their distances differ by ≤ 5 m.
- Cache key `nsrn:v1:{lat:.4f},{lon:.4f}`; store misses as `null` owner too.
- Same `NETWORK_TIMEOUT_S`/`BREAK_S` as `geo.py`; skipped entirely when `settings.offline_geo`.
- Owner codes: `TIR` → `geo.PROVINCIAL`; `M` → municipality at point; `P` "Private road", `F` "Federal road", `C`/`O`/`U` "Other owner (<owner_desc>)".
- Boundaries file `roadsense/core/data/ns_municipalities.json`, Douglas-Peucker tolerance 0.0005°, coords rounded to 5 dp, top-level `{"source","licence","built","municipalities":[{"name","type","county","bbox","polygons"}]}`.
- Commit messages contain no Claude/Claude Code mention and no Co-Authored-By trailer (user's CLAUDE.md).

## Review Focus

- Point exactly on a municipal boundary or in water near the coast → `municipality_at` returns None, routing falls back to Nominatim's municipality (test in Task 1).
- NSRN returns HTTP 200 with an error object (Socrata `{"error": true}`) instead of a list → treated as unavailable, not as "no road" (test in Task 2).
- Cached miss must not be re-queried every sighting, but a network failure must not be cached (test in Task 2).
- `M`-owned road where the point is outside every boundary → candidate falls back to Nominatim municipality, confidence medium (test in Task 3).
- Referral to an authority with `contact: null` → notice reads "no contact on file: forward manually", never "None" (test in Task 4).

---

### Task 1: Bundled municipal boundaries + `municipality_at`

**Files:**
- Create: `scripts/build_ns_boundaries.py`, `roadsense/core/data/ns_municipalities.json`, `roadsense/core/ownership.py`
- Test: `tests/test_ownership.py`

**Interfaces:**
- Produces: `ownership.municipality_at(lat: float, lon: float) -> dict | None` returning `{"name", "type", "county"}`; `ownership.municipalities() -> list[dict]` (cached load).

- [ ] **Step 1: Write failing tests** `test_municipality_at_route_points`: (44.6455,-63.5735)→"Halifax Regional Municipality"; (44.5410,-64.2390)→"Municipality of the District of Chester"; (44.3780,-64.5190)→"Town of Bridgewater"; (44.0390,-64.7160)→"Region of Queens Municipality"; (43.65,-79.38)→None. `test_boundaries_file_has_49` : `len(municipalities()) == 49`.
- [ ] **Step 2: Run** `pytest tests/test_ownership.py -v` → FAIL (module missing).
- [ ] **Step 3: Write `scripts/build_ns_boundaries.py`**: download `https://data.novascotia.ca/api/geospatial/7bqh-hssn?method=export&format=GeoJSON`, iterative Douglas-Peucker per ring, drop rings < 4 points, compute bbox per municipality, write the file. Run it; check the file is under 1.5 MB.
- [ ] **Step 4: Implement `municipality_at`**: bbox prefilter, even-odd ray casting over every ring of every polygon (holes handled by even-odd across all rings of a polygon).
- [ ] **Step 5: Run tests** → PASS.
- [ ] **Step 6: Commit** `feat: bundle NS municipal boundaries and point lookup`.

### Task 2: NSRN road owner lookup

**Files:** Modify `roadsense/core/ownership.py`; Test `tests/test_ownership.py`

**Interfaces:**
- Consumes: `geo._seg_distance_m(p, a, b)`, `geo.NETWORK_TIMEOUT_S`, `geo.BREAK_S`.
- Produces: `ownership.road_owner(conn, lat, lon) -> dict | None`. `None` = unavailable (offline, breaker open, error). Otherwise `{"owner": str|None, "owner_desc", "street", "distance_m", "tie": {"owner","owner_desc","street","distance_m"} | None}` with `owner None` when no owned road within 25 m.

- [ ] **Step 1: Write failing tests** (monkeypatch `settings.offline_geo=False` and `ownership.httpx.get` with recorded rows):
  - `test_owner_picks_nearest_segment`: two rows (Duke St TIR at ~10 m, other M at ~30 m) → owner "TIR", street "Duke St".
  - `test_owner_tie_at_intersection`: TIR at 3 m and M at 6 m → `tie["owner"] == "M"`.
  - `test_owner_miss_is_cached`: empty list → owner None; second call makes no HTTP request.
  - `test_owner_error_not_cached`: `httpx.ConnectError` then `{"error": True}` payload → both return None, nothing written to geocache, breaker set.
  - `test_owner_offline_returns_none`: default test settings → None, no HTTP.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `road_owner`** with module-level `_down_until` breaker; select `the_geom,street,owner,owner_desc`; distance = min `_seg_distance_m` over every leg of every line (coordinates are `[lon, lat]`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: look up road owner from NSRN with cache and breaker`.

### Task 3: Ownership-first jurisdiction candidates

**Files:** Modify `roadsense/core/geo.py:150-174`, `roadsense/mcp_servers/geo_server.py:28-42`; Test `tests/test_ownership.py`, keep `tests/test_decisions.py::test_jurisdiction_routing` green.

**Interfaces:**
- Consumes: `road_owner`, `municipality_at`.
- Produces: `geo.jurisdiction_candidates(place: dict, owner: dict | None = None) -> list[dict]` (same item shape as today, plus `"source": "nsrn" | "rules"`). `geo_find_jurisdiction` output gains `"owner_source"` and uses the boundary municipality when found.

- [ ] **Step 1: Write failing tests** (pure, pass `owner` dicts directly):
  - TIR → one high candidate, authority `PROVINCIAL`, evidence contains "NSRN"; plus municipality low.
  - M with `place["municipality"]="Town of Bridgewater"` → single high candidate "Town of Bridgewater".
  - M with municipality None → medium, authority None evidence "outside known boundaries".
  - Tie TIR/M → two medium candidates, both authorities present.
  - P → one low candidate "Private road".
  - `owner={"owner": None, ...}` and `owner=None` → identical to today's rules output, `source == "rules"`.
  - MCP-level: `find_jurisdiction(44.0390, -64.7160)` offline → `municipality == "Region of Queens Municipality"`, `owner_source == "rules"`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the owner branch at the top of `jurisdiction_candidates`; in `find_jurisdiction` set `place["municipality"] = (municipality_at(lat, lon) or {}).get("name") or place["municipality"]` before ranking, and pass `road_owner(conn, lat, lon)`.
- [ ] **Step 4: Run** `pytest tests/test_ownership.py tests/test_decisions.py tests/test_mcp.py -v` → PASS.
- [ ] **Step 5: Commit** `feat: route by NSRN road owner and NS boundaries`.

### Task 4: Directory entries and referral notice

**Files:** Modify `roadsense/core/municipalities.json`, `roadsense/core/delivery.py:198`; Test `tests/test_ownership.py`

**Interfaces:**
- Consumes: `ownership.municipalities()` names (generate entries once from the file; HRM and NS Public Works entries unchanged).

- [ ] **Step 1: Write failing tests**: every name in `municipalities()` resolves via `open311.service_info` with channel `referral` (HRM stays `open311`); `_referral_submit` with `contact None` and no SMTP → notice contains "no contact on file: forward manually".
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Add 48 entries** (`{"channel": "referral", "contact": null, "notes": "Added from NS Municipality Boundaries; contact not yet verified."}`), add the notice branch.
- [ ] **Step 4: Run full suite** `pytest -q` and `ruff check roadsense scripts tests` → all pass.
- [ ] **Step 5: Commit** `feat: add all NS municipalities to the directory`.
