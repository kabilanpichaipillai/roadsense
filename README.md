# RoadSense

**AI agents that find road damage from a dashcam and get it to the road authority that can fix it.**

Cognizant BrAInstorm Challenge 2026 · Agentic AI for Good · tracks: Public Services, Sustainability

Halifax logged 3,203 pothole reports between January 1 and March 19, 2026, and says the count includes the same
pothole reported several times and potholes on provincial roads it does not maintain
([CTV Atlantic](https://www.ctvnews.ca/atlantic/nova-scotia/article/more-potholes-in-halifax-this-year-compared-to-2024-municipality-says/)).
RoadSense attacks exactly that: a dashcam finds the damage, and a team of AI
agents confirms it, works out **who** is responsible, merges repeat sightings into one report, and files it once the
driver approves.

A dashcam video and its GPS track go through a YOLOv8m detector on the edge device (a laptop GPU). Only
privacy-blurred frames of damage leave the vehicle. LLM agents, connected to their tools through the **Model Context
Protocol (MCP)**, decide what each detection is, who maintains that road, whether it is already reported, and what
the authority is told; the driver approves the exact payload; a follow-up agent tells the driver when it is fixed.

## Architecture

```mermaid
flowchart LR
  subgraph EDGE["Level 1 · Edge (laptop GPU, offline-capable)"]
    V[Dashcam video + GPX/CSV] --> D[YOLOv8m @ 5 fps]
    D --> T[BoT-SORT + camera-motion compensation]
    T --> B[best frame: confidence × sharpness]
    B --> R[blur everything but the damage]
    R --> G[flat-road geometry: defect position + width]
    G --> O[(outbox)]
  end
  O -- token, validated --> API
  subgraph AGENTS["Level 2 · Agents (FastAPI + OpenRouter + 4 MCP servers)"]
    WEB[RoadSense web app] -- server-side token --> API
    API[Agent API] --> WF[Trip workflow: code runs every step]
    WF --> VIS[Vision step] --> MD[[detector MCP: RT-DETR-L verify, close-up re-check, severity, verdict]]
    VIS -. borderline detections .-> AI{{AI judge: typed answer, checked}}
    WF --> LOC[Location step] --> MG[[geo MCP: road class + route number, jurisdiction candidates]]
    LOC -. unsure jurisdiction .-> AI
    WF --> TRI[Triage step: rules] --> MR[[reports MCP: dedupe / merge, drafts, notifications]]
    WF --> DRA[Drafting step] -. description .-> AI
    APP{{Driver approves exact payload}} --> FIL2[Filing: plain code] --> MC[[municipality MCP: Open311 / referral, status]]
    FU[Follow-up: plain code] --> MC
  end
  MC --> HRM[Halifax 311 · mock Open311]
  MC --> NS[NS Public Works · referral package]
```

### How the AI is used

A code workflow runs every step of a trip; the model is asked only where judgement adds something, in batches of up
to 8 items, and never calls tools itself. Each step uses its own allow-listed MCP tools (enforced in code), hands the
model the evidence as data, and checks its typed answer: an answer about an id it was not asked about, or a choice
outside the candidates, is dropped and the rule decides. If the model becomes unavailable, the rules finish the trip.

| Step | Rule | Where the AI judges |
|---|---|---|
| Vision | verifier support confirms; a class disagreement is "disputed"; no support is a false alarm | confirmations on weak evidence (verifier confidence < 0.65, ≤ 2 frames, box overlap < 0.3, or close-up re-check only): keep or reject |
| Location | top-ranked authority from road class and route number | numbered routes where the candidates tie |
| Triage | merge within 15 m or create a report | never |
| Drafting | standard template | writes the description the authority reads |
| Filing, follow-up | plain code after approval / on a schedule | never |

### Decisions the system makes (and the trace shows why)

| Decision | Evidence it acts on | Guard in code |
|---|---|---|
| **Real damage, false alarm, or disputed?** | RT-DETR-L verification on the redacted frame, close-up re-check for confident unconfirmed detections | Can't confirm without verifier evidence; "disputed" only when the two models disagree on class. YOLOv8m's label is kept and the driver is asked. |
| **Who maintains this road?** | OpenStreetMap road class and route number: 100-series highways → Nova Scotia Public Works; numbered routes through the city are ambiguous and the agent weighs them; local streets → HRM | Only authorities in the directory have a delivery channel |
| **New report or another sighting of a known one?** | Open reports of the same class within 15 m of the projected position | Merging is idempotent; confirmations raise priority |
| **What the authority is told** | Report facts: class, width band, severity, confirmations, address | Text is sanitised, and locked by hash once the driver approves |
| **Is it fixed?** | Authority status feed | Notifies each driver once |
| **Is it really fixed?** | Later trips past a closed report: the defect detected again, or clean frames of the spot | The authority's status is kept; RoadSense reopens on a confident re-detection and verifies only after clean passes from 2 different trips. Blurred, dark, out-of-view or weak evidence is inconclusive |

The fallback that completes any skipped step is a circuit breaker, not the engine: its use is counted and reported
per trip as the fallback rate.

### Independent repair verification

An authority closing a request does not prove the pothole was filled. When the follow-up feed (or the city's own
request layer) closes a report, RoadSense keeps that status as "Closed" and marks the report **awaiting
verification**. Later trips re-observe it (`roadsense/core/repair.py`):

- **Seen again**: a verified detection of a compatible class within `ROADSENSE_REPAIR_MATCH_RADIUS_M` (15 m), on the
  same road, captured after the closure. At or above `ROADSENSE_REPAIR_REDETECT_CONFIDENCE` (0.5) the repair is
  **not verified** and the report is **Reopened**, with the reason and the new photo. Below it, the result is
  **inconclusive**. The sighting is attached to the closed report instead of opening a duplicate.
- **Not seen**: the edge downloads a watchlist of closed reports and keeps one privacy-blurred frame of each spot it
  passes (`roadsense/edge/reobserve.py`). A frame counts as a clean pass only when it is sharp, lit, shows the spot
  4-25 m ahead within 35° of the heading, and that trip detected nothing of that class there. The repair is
  **verified** after `ROADSENSE_REPAIR_MIN_CLEAN_PASSES` (2) clean passes from different trips. One miss is not proof.

Every attempt is stored once in an append-only history (`repair_verifications`) with a SHA-256 evidence hash and
audit events. The report page shows the original and latest follow-up photos side by side. The overview shows the
**Repair Verification Rate** = verified ÷ (verified + reopened) × 100. Closed reports nobody has driven past yet, and
those with only inconclusive frames, are counted separately and never as failed repairs.

## Measured results

All on held-out RDD2022 data (roads no model saw in training), reproducible with the scripts named.

**Detectors** (training repo, test split, thresholds tuned on validation)

| Model | Role | F1 | mAP@0.5 | Pothole AP | Latency (GTX 1650 Ti) |
|---|---|---:|---:|---:|---:|
| YOLOv8m | edge detector | 0.590 | 0.599 | 0.509 | 36 ms |
| RT-DETR-L | cloud verifier | 0.614 | 0.613 | 0.540 | 71 ms |

**Vision verification** (`scripts/eval_verifier.py`: rule tuned on 350 validation images, reported once on 350 test
images, 568 labelled defects, `results/verifier_eval.json`)

| | Edge only | After verification |
|---|---:|---:|
| Precision (detection overlaps a labelled defect) | 0.714 | **0.744** |
| False alarms | 146 | **115 (−21%)** |
| True damage kept | 364 | 335 (92%) |

12% of kept detections get a class dispute between the two models, which goes to the driver instead of being
silently overwritten. The gain is modest because both models learned from the same data and share blind spots, and
part of the "false alarms" are unlabelled real damage. Multi-driver confirmation is the stronger filter.

**Agent decisions** (`scripts/eval_agents.py`: 42 scenarios from labelled test images through the real MCP servers,
deterministic mode, `results/agent_eval.json`)

| Metric | Result |
|---|---:|
| Jurisdiction (Highway 102 → province, local streets → HRM) | 100% (30/30) |
| Duplicate sightings merged instead of re-filed | 100% (6/6) |
| Real damage turned into reports | 100% (24/24) |
| Unlabelled detections rejected | 17% (2/12) |
| Sent to an authority without approval / tool outside allow-list | 0 / 0 |
| Processing time | 9 s for 36 events |

**With the AI judge** (`scripts/eval_agents.py --llm --singles 10`: 17 scenarios, free
`nvidia/nemotron-3-ultra-550b-a55b`, `results/agent_eval_llm.json`). The table below is the earlier design, where free-
running agents ran every step. The workflow design on the same scenarios: report precision 0.714 → 0.769, unlabelled
detections rejected 1 of 5 → 2 of 5, real damage, jurisdiction and merging unchanged at 100%, 36 → 5 model calls,
134k → 8k tokens, 307 s → 70 s for the two trips. A small sample: treat the precision gain as a direction, not a
measurement.

| Metric | Result |
|---|---:|
| Jurisdiction / duplicate merging / real damage reported | 100% / 100% / 100% |
| Unlabelled detections rejected | 1 of 5 |
| Fallback rate (steps the agents left for the circuit breaker) | **0%** |
| Sent without approval / tool outside allow-list | 0 / 0 |
| LLM calls per trip | 19 (15 events), 17 (2 events) |
| Tokens per trip | 102k (15 events), 32k (2 events) |
| Time per trip | 214 s, 93 s |
| Answers that failed the output schema and self-corrected | 2 |

The agents cite their evidence in every decision ("RT-DETR-L confirms D00 at confidence 0.90 with IoU 0.89, class
agrees"), and routed Highway 102 with "a 100-series provincial highway (NS 102), maintained by Nova Scotia Public
Works". One weakness to watch: the orchestrator's driver summary miscounted severities once (said 6 medium, listed 7);
the numbers in the reports themselves come from the database, not from the summary.

Free-tier budget: a 15-event trip uses about 19 of the 50 free requests a day (1,000 a day after a one-time $10
credit purchase). `scripts/smoke_llm.py` showed both Nemotron models returning correct tool calls; Gemma's free
endpoint was rate-limited upstream, so it is last in the fallback chain.

**Edge** (`clips/us_drive.mp4`, 60 test stills with simulated forward motion, 142 labelled defects): YOLOv8m with
BoT-SORT analysed 5 frames/s of video at 17.8 FPS and produced 84 events (ByteTrack: 66 events at 35.4 FPS). A road
crop that skips the top 35% of the frame lowered recall from 0.64 to 0.53 on the test split, so it is off by default.

## What is real and what is mocked

| Real | Mocked or simulated |
|---|---|
| YOLOv8m and RT-DETR-L trained on RDD2022; verification, tracking, redaction, geometry | Test videos are RDD2022 stills with simulated motion; a recorded Halifax drive is next |
| OpenStreetMap reverse geocoding (Nominatim), road class and route numbers | Offline fixture for two Halifax roads when there is no network |
| Road snapping: each defect is moved to the nearest drivable road (OSRM, within 25 m) | The public OSRM demo server is for development; run your own for a pilot |
| The simulated drive follows real street geometry (OSRM route over OpenStreetMap, `roadsense/edge/halifax_route.json`) | |
| MCP servers, agent loop, OpenRouter client, approvals, audit log | **HRM has no public Open311 API**: reports go to a local mock GeoReport v2 server by default |
| Delivery adapters for Open311, Cityworks REST and ArcGIS feature services, with retries and updates | Cityworks and ArcGIS adapters are tested against the vendors' API shapes, not against HRM's instance |
| Read-only sync of the city's own service requests from any ArcGIS layer (HRM publishes Cityworks SRs on ArcGIS Hub) | Off until `ROADSENSE_CITY_SR_LAYER_URL` is set |
| Sign-in, roles, OIDC single sign-on (Entra ID), signed evidence links, server-side redaction check, retention | Nova Scotia Public Works gets a referral package (JSON, emailed when SMTP is set), not an API submission |
| | Camera height, pitch and field of view are defaults until calibrated per vehicle |

## Setup

Python 3.11 and an NVIDIA GPU (CPU works, slower).

```bash
python -m venv .venv
.venv\Scripts\activate                      # Linux/macOS: source .venv/bin/activate
pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cu118
pip install -r requirements.txt
copy .env.example .env                       # Linux/macOS: cp .env.example .env
```

In `.env`:
- `OPENROUTER_API_KEY`: free key from openrouter.ai. The free tier allows 50 requests a day, or 1,000 a day after a
  one-time $10 credit purchase; a trip takes roughly 8-15 requests. Without a key the deterministic pipeline runs.
- `ROADSENSE_LLM_MODELS`: free tool-calling models in fallback order. Check them with `python scripts/smoke_llm.py`.
- `ROADSENSE_LLM_MODE`: `cache` (default) stores every response, so reruns cost nothing; `replay` serves a recorded
  run without a key (keyless demo).
- `ROADSENSE_WEIGHTS_DIR`: folder with `yolov8m_rdd2022.pt` and `rtdetr_l_rdd2022.pt` from the training repo.
- `ROADSENSE_CAMERA_*`: dashcam height, pitch and field of view, used to place and size detections.
- Change `ROADSENSE_API_TOKEN` and `ROADSENSE_OPEN311_API_KEY`.

## Run

```bash
python scripts/run_all.py        # mock 311 :8311, agent API :8000 (/docs), web app :7860
```

or `docker compose up --build` (CPU image; not yet tested here).

### The web app (http://127.0.0.1:7860)

A SaaS-style single-page app (FastAPI + vanilla JS, light and dark themes, no build step), designed from the
research synthesis in [docs/research-synthesis.md](docs/research-synthesis.md):

| Page | What it is for |
|---|---|
| **Overview** | The review queue and reports by status, the pipeline as a road (detections → confirmed → unique reports → sent, with duplicates merged and false alarms dropped between stops), what the pipeline saved the city (duplicate 311 contacts avoided, reports sent straight to the province, staff time and intake cost at configurable rates), signals worth a look, a map, grouped agent decisions and recent trips |
| **Trips** | Upload a dashcam video (+ GPX/CSV, or a simulated Halifax route); live progress through edge → upload → agents; detections gallery; "Process now" for uploaded-but-unprocessed trips |
| **Review** | The approval inbox: photo, flags (class disputed, N drivers), the agents' decision timeline, and the exact payload locked by SHA-256. Sending always needs a click |
| **Reports** | All reports with status filters and a detail page |
| **Map** | Leaflet map (vendored) coloured by status, sized by independent drivers |
| **Road condition** | Segment-level condition estimate (0-100, PCI-style deducts per 100 m) from open defects, worst first |
| **Exposure risk** | Each report's physical severity, exposure (cycling, pedestrian, bus and road context from OpenStreetMap) and overall risk kept as separate values, Low/Medium/High/Critical, on a map and in a table filterable by risk, damage, neighbourhood, road class and nearby infrastructure. The report page shows every factor and feature |
| **Repair equity** | Repair times by neighbourhood from the city's own service requests and RoadSense reports: median and average, response/inspection/repair kept apart, like-with-like comparisons, possible differences flagged with their sample sizes and the factors that could explain them |
| **Agent activity** | Every plan, verdict and tool call, filterable by agent and trip |
| **Authorities** | Halifax 311 (Open311) requests, NS Public Works referrals, Follow-up agent, driver notifications |
| **Trust & system** | Production checklist, guardrails, privacy and retention, delivery channels, jobs and scheduler, MCP servers |
| **Users** (admin) | Add people, set roles (admin, reviewer, driver, viewer), disable accounts |

Everyone signs in (password or single sign-on). On first start the admin password is written to
`data/initial_admin_password.txt` unless `ROADSENSE_ADMIN_PASSWORD` is set. The browser never sees the API token:
the web server holds it, checks the user's role, and exposes a fixed set of calls. Production setup:
[docs/deployment.md](docs/deployment.md); security model: [SECURITY.md](SECURITY.md).

## Exposure-weighted risk and the Repair Equity Audit

**Exposure-weighted risk.** The same pothole is more dangerous in a bike lane or at a crossing than on a quiet street.
For every report RoadSense keeps three separate values (the detector's confidence is never changed):

| Value | Weight in overall | From |
|---|---:|---|
| Physical severity | 50% | The existing severity band (width for potholes, box area for cracks) × damage class, + repeat drivers. Depth is not measured |
| Cyclist exposure | 20% | Bike lane tagged on the same road (full weight) or a cycle lane/path nearby (reduced: may not share the road) |
| Pedestrian exposure | 15% | Pedestrian crossing, school, footway/sidewalk nearby |
| Public transport exposure | 10% | Bus stop nearby |
| Road context | 5% | OSM road class from the existing geocoding |

- Nearby features come from OpenStreetMap through the **Overpass API** (Nominatim's reverse lookup returns only the
  road), cached in `geocache` and refreshed after `ROADSENSE_OSM_CACHE_DAYS`. Each OSM element counts once, in one
  category, and only the most relevant feature of a category scores. Relevance falls linearly with distance within
  the configured limits (`roadsense/core/exposure_rules.json`, documented in the file).
- **Exposure is a proxy, not measured traffic.** A school or bus stop nearby is a reason to expect people, not a count
  of them. The weights are illustrative starting values, not validated safety measurements.
- Low / Medium (≥ 0.35) / High (≥ 0.55) / **Critical** (≥ 0.75 *and* the severity model's urgent band S4, so exposure
  alone never makes a minor defect critical). Without OSM data the exposure is "unknown" and data quality drops.
- The `score_exposure` job scores new or changed reports every `ROADSENSE_EXPOSURE_MINUTES`; a score is recomputed
  only when the report, its OSM data or the rules change.

**Repair Equity Audit.** Uses the city's own service requests (the existing read-only ArcGIS/Cityworks sync,
`ROADSENSE_CITY_SR_LAYER_URL`) and RoadSense reports sent to an authority (submitted → closed), a report the city
also holds counted once.

- Response (→ acknowledged), inspection and repair (→ completed) times are separate and computed only when both dates
  exist; average, median, min and max are shown, with the number of records behind each.
- Neighbourhood comes from a neighbourhood/community field on the city record or the cached OpenStreetMap suburb;
  anything else is **Unassigned**, never guessed.
- A neighbourhood is flagged as a **possible difference in service delivery** only when its median repair time is
  ≥ 1.25× the rest of the city's, a Mann-Whitney U test gives p < 0.05, and both sides have ≥ 10 completed repairs.
  The same comparison is repeated within each damage category and road class. A flag lists what could explain it
  (damage mix, road classes, volume, missing dates); it is not a finding of unfair treatment.
- Dashboards show aggregates; the record list behind a neighbourhood has no driver, trip or reviewer data.

## Keyless demo

```bash
python scripts/demo.py            # replays a real recorded agent run on the fixed demo trip: no API key needed
python scripts/demo.py --record   # re-record it live (needs OPENROUTER_API_KEY, ~17 free-tier requests)
```

The demo trip (`demo/trip/`) covers every decision: real damage on Halifax streets, Highway 102 routed to Nova
Scotia Public Works, an ambiguous numbered route, a disputed class, and a second driver's sighting that merges. Each run
starts from a fresh `data/demo` folder with offline geocoding, so the agents see the same tool results as when the run
was recorded. Tool results the agents see are kept stable through a field allow-list; if a replay reports
INCOMPLETE after an upgrade, re-record with `--record`. Browse the result with `ROADSENSE_DATA_DIR=data/demo ROADSENSE_OFFLINE_GEO=1 python scripts/run_all.py`.

## Evidence

```bash
python scripts/check.py                                   # ruff + 146 tests + pip-audit (no known vulnerabilities)
python scripts/eval_verifier.py --split <RDD>/val --tune  # choose the verification rule on validation data
python scripts/eval_verifier.py --split <RDD>/test        # report it once on test
python scripts/eval_agents.py [--llm]                     # 42 decision scenarios end to end
python scripts/smoke_llm.py                               # does each free model really call tools?
```

Business case (costs, savings, pilot proposal): [docs/business-case.md](docs/business-case.md). Finale pitch kit
(run sheet, demo path, pre-flight checklist, likely questions): [docs/pitch.md](docs/pitch.md).

Security and privacy: see [SECURITY.md](SECURITY.md) (threat model, OWASP Agentic Top 10 mapping, PIPEDA/PIIDPA notes).

## Project layout

```
roadsense/
  common/       config, detectors, SQLite schema (+ migrations, append-only audit), edge→API schema
  edge/         gps, camera geometry, tracker (BoT-SORT config), redact, process_video, outbox, simulate_gps, make_clip
  core/         verdict + severity + dedupe rules, verification, geocoding + jurisdiction, authority directory + payloads,
                exposure-weighted risk (OSM Overpass), repair equity audit
  mcp_servers/  detector, geo, reports, municipality (FastMCP, stdio, annotated tools)
  agents/       OpenRouter client (fallback chain, rate limit, cache/replay), MCP toolbox, typed contracts,
                workflow steps + allow-lists (stages), trip workflow (orchestrator), deterministic safety net
  services/     agent API (FastAPI), mock Open311 server
  web/          web app: backend-for-frontend (FastAPI) + single-page app in static/ (vanilla JS, vendored Leaflet)
scripts/        run_all, check, eval_verifier, eval_agents, smoke_llm, list_free_models
```

## Roadmap

Done in the production-readiness pass: sign-in and roles, durable jobs and a scheduler, delivery retries and updates,
Cityworks/ArcGIS/email adapters, city-request sync, signed media, server-side redaction check, retention, data
residency enforcement, cross-class dedupe and rejection suppression, severity bands and repair targets, segment
condition estimate, deterioration trends, third-party feed ingest, ground-truth label export, CI, pinned versions.

Next:
- Recorded Halifax drive with a phone GPS log; calibrate the camera; tape-measure potholes to validate widths;
  fine-tune on the exported Halifax labels (`scripts/export_labels.py`)
- Validate the Cityworks/ArcGIS field mapping with HRM; a Canadian no-retention LLM endpoint; a PIA
- In-vehicle capture (phone app or fleet kit on Jetson/Hailo) with automatic GPS sync
- Postgres + PostGIS, object storage, MCP over Streamable HTTP for horizontal scaling
- French UI and a WCAG 2.1 AA audit; winter (snow-cover) gating

## Team

Rahul Raj · kabilanpichaipillai
