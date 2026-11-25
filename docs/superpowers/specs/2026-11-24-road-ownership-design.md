# Road ownership from provincial open data — design

Date: 2026-11-24 · Status: draft for approval

## Why

Today the authority is guessed from the OSM road class: 100-series → Province, numbered route → ambiguous (LLM picks),
anything else → the municipality OSM names. Outside Halifax that is often wrong: rural local roads are mostly
provincial (Duke St, Chester is owned by the Province), and urban numbered routes are often municipal (Bedford Hwy is
HRM's). The goal is that a pothole anywhere on a Halifax → Queens drive is routed to the office that actually owns the
road.

## Sources (both free, no key, Open Government Licence – Nova Scotia)

| Source | Gives | Access |
|---|---|---|
| Nova Scotia Road Network (NSRN), `data.novascotia.ca` dataset `484g-adjn` | `owner` per road segment: `TIR` (provincial), `M` (municipal), `P` private, `F` federal, `C`/`O`/`U` other; plus street, road class, route no. | Live Socrata query: `intersects(the_geom, <box>)` |
| Municipality Boundaries, dataset `7bqh-hssn` | All 49 municipalities (25 towns, 11 districts, 9 counties, 4 regional), non-overlapping | Bundled file, simplified to ~40 m (≈1 MB) |

No open dataset lists municipal contacts; those stay hand-curated.

## Approaches considered

- **Hybrid (chosen):** road owner queried live from NSRN with a SQLite cache; boundaries bundled. Smallest change,
  follows the province's yearly updates, municipality lookup works offline.
- Local snapshot of NSRN + boundaries in SQLite: fully offline, but a tens-of-MB download and a refresh job.
- Live for both: simplest, but no network means back to guessing.

## Design

**New `core/ownership.py`**
- `road_owner(conn, lat, lon)`: query NSRN for segments within a 25 m box (owner not null, so tracks and driveways
  are skipped), pick the nearest by point-to-segment distance (reuses `geo._seg_distance_m`). Returns
  `{owner, street, distance_m, runner_up}` or `None`. Cached in `geocache` under `nsrn:v1:{lat:.4f},{lon:.4f}`
  (misses cached too). Same circuit breaker and timeout as Nominatim; skipped when `offline_geo` is on.
- `municipality_at(lat, lon)`: point-in-polygon over the bundled boundaries (bbox prefilter, ray casting, pure Python,
  no new dependency). Returns the official full name, e.g. "Region of Queens Municipality".

**Data file** `roadsense/core/data/ns_municipalities.json`, built by `scripts/build_ns_boundaries.py` (downloads the
GeoJSON, Douglas-Peucker to ~0.0005°, keeps name / type / county). The file records source, licence and build date.

**`geo.jurisdiction_candidates`** becomes ownership-first:
- municipality comes from the bundled boundaries, falling back to Nominatim's field.
- `TIR` → Nova Scotia Public Works, **high**, evidence "NSRN: <street> owned by the Province".
- `M` → the municipality at the point, **high**, evidence "NSRN: <street> owned by <municipality>".
- Two owned segments within 5 m of each other with different owners (an intersection) → both candidates at **medium**,
  so the existing Location step asks the model, which may only pick one of them.
- `P`/`F`/`C`/`O`/`U` → a single low-confidence candidate naming the owner ("Private road", "Federal road"…), with no
  delivery channel, so the reviewer sees why it can't be filed.
- NSRN unreachable or nothing within 25 m → today's road-class rules (unchanged), with the boundary municipality.

`geo_find_jurisdiction` output gains `owner_source` (`nsrn` | `rules`) so the audit log shows which one decided.

**Directory** `municipalities.json`: add all 49 municipalities by their official names (generated from the boundary
file), channel `referral`, `contact: null`. My call: a referral with no contact still writes the package, but its
notice says "no contact on file: forward manually" rather than "send it to None". Real contacts are filled in by hand
over time; HRM keeps Open311 and its aliases.

## Tests

Offline and deterministic: NSRN responses mocked with recorded rows (Bedford Hwy → M, Duke St Chester → TIR, Highway
103 → TIR, a private road, an intersection tie, a network error → fallback). Boundary lookups use real points (Halifax,
Chester, Bridgewater, Liverpool, Toronto → none). Existing `test_jurisdiction_routing` keeps passing through the
rules fallback in offline mode.

## Non-goals

- Finding contacts for the 47 new municipalities.
- Blocking approval of reports that have no delivery channel (worth doing next).
- Regions outside Nova Scotia: the ownership lookup only runs when the point is inside an NS boundary.

## Privacy

Coordinates go to `data.novascotia.ca`, the same kind of lookup that already goes to Nominatim; no images or user data.
