# Finale pitch kit (Halifax, November 28, 1-3 PM)

Format: **5 minutes including a live demo**, then 1-2 questions. Judged on business impact, UX, technical excellence,
presentation quality and implementation readiness. Everything below is sized to that.

## Run sheet (5:00)

| Time | Who | What is on screen | What to say (the point, not a script to read) |
|---|---|---|---|
| 0:00-0:35 | Speaker 1 | One photo of a Halifax pothole, the number **3,203** | 3,203 pothole reports in 11 weeks this year. HRM itself says the count includes the same pothole reported many times and potholes on roads the city does not even own. Residents have to know whether to call 311 or 511. |
| 0:35-1:05 | Speaker 1 | Architecture in one line: dashcam → edge model → 4 AI agent steps → driver approves → right authority | RoadSense turns any dashcam into a road inspector. It does not stop at a map: it finds the damage, confirms it, works out who owns the road, merges repeats, and files one report once a human approves. |
| 1:05-3:35 | Speaker 2 | **Live demo** (below) | Narrate what the agents decided and *why*, not the clicks. |
| 3:35-4:15 | Speaker 1 | The measured-results table | Numbers on held-out data: false alarms down 21% after verification, 100% correct authority routing and duplicate merging on 42 scenarios, nothing ever sent without approval. Be candid: report precision is about 0.74, which is why a human approves every report. |
| 4:15-4:50 | Speaker 1 | Pilot slide from [business-case.md](business-case.md) | 8-week spring pilot with 5-10 HRM fleet vehicles; read-only Cityworks integration first; what we need from HRM. |
| 4:50-5:00 | Speaker 1 | Logo + URL | One sentence: "Find it once, send it to the right desk, prove it was fixed." |

## Demo path (2:30)

Signed in as a reviewer on the Overview page, with the demo trips already processed.

1. **Overview** (20 s). The road strip: 6 detections → confirmed → unique reports. Point at **What this saved the
   city**: the duplicate that was merged and the highway pothole that went straight to the province.
2. **Review** (60 s). Open the Highway 102 report. Show the blurred photo (faces and plates never leave the car), the
   agent timeline with its cited evidence ("RT-DETR-L confirms … at confidence 0.83"), and the routing decision to
   Nova Scotia Public Works. Open the disputed report: the two models disagree on the damage class and the system
   asks the human instead of guessing.
3. **Approve** (30 s). Approve one report. Point out the SHA-256 lock: the payload the city receives is exactly
   what the reviewer saw; an agent cannot change it afterwards.
4. **Authorities** (20 s). The request is now in Halifax 311 (mock Open311) and the provincial referral is filed.
5. **Map / Road condition** (20 s, if time allows). Segment condition scores, worst first: what a roads manager
   plans with.

If anything fails live, switch to the recorded backup video at the same step and keep talking. Do not debug on stage.

## Pre-flight checklist

Do these the day before and again on the morning of November 28.

- [ ] **Re-record the keyless demo** on a machine with free memory: `python scripts/demo.py --record` (needs
      `OPENROUTER_API_KEY`, about 17 free requests). Then `python scripts/demo.py` must print
      `replay: complete`. The recording from November 15 predates the November 17 agent workflow, so today the
      replay falls back to the rule engine and the AI reasoning does not show. Commit `demo/llm_cache.json` and let
      CD redeploy, so the hosted demo shows the agents too.
- [ ] **Free memory on the demo laptop.** With Chrome, other IDE sessions and four MCP servers open, a 16 GB laptop
      ran out of commit memory during testing and the verifier failed. Close everything else, or raise the Windows
      page file (System → Advanced → Performance → Virtual memory) to at least 16 GB.
- [ ] Start the stack 10 minutes early: `python scripts/run_all.py` with `ROADSENSE_DATA_DIR=data/demo` and
      `ROADSENSE_OFFLINE_GEO=1` (no venue Wi-Fi needed). Sign in, click through every page once.
- [ ] Keep the hosted VM open in a second tab as a fallback.
- [ ] Record a 2:30 backup video of the demo path above.
- [ ] Phone hotspot ready; laptop on power; display mirroring tested; notifications off.
- [ ] Put the second team member's name in the README and on the title slide.

## Likely questions

**"Isn't the AI just a wrapper around rules?"**
Deliberately, partly. Code runs every step so the system cannot go off the rails; the model is asked only where
judgement adds something: borderline detections, roads whose owner is ambiguous, and the text a crew reads. Its
answers are typed and checked, and it can only choose among candidates the tools produced. That cut model calls from
36 to 5 per demo run and raised report precision from 0.71 to 0.77 on the same scenarios. It is also why the rule
engine can finish a trip if the model is down.

**"How accurate is it really?"**
On held-out RDD2022 images: 0.74 precision after verification, false alarms down 21%, 92% of true damage kept.
Not yet measured on Halifax roads: the first pilot deliverable is labelled Halifax footage, and the export tool for it
already exists (`scripts/export_labels.py`). Multi-driver confirmation is the stronger filter, and a human approves
every report.

**"Halifax doesn't have an Open311 API. How would this actually reach the city?"**
Correct, HRM runs Cityworks. RoadSense already has Cityworks REST and ArcGIS feature-service adapters, and can read
HRM's published Cityworks service requests from ArcGIS Hub today without write access, to dedupe against what the
city already knows. The pilot starts read-only.

**"Privacy?"**
Everything outside the damage box is blurred on the device, so faces and plates never leave the car; the server
re-checks redaction; raw video is not kept; positions of the car expire after 30 days. The design maps to PIPEDA
and Nova Scotia's PIIDPA, and a residency mode refuses LLM endpoints outside Canada. See [SECURITY.md](../SECURITY.md).

**"What does it cost and who pays?"**
The model is asked about 5 times per trip, so AI cost stays at cents per trip. The buyer is the road authority's
operations budget; the intake savings alone are $7k-14k per spring in Halifax (scenario in
[business-case.md](business-case.md)), and the larger value is earlier repair and a defensible inspection record.

**"What about winter, snow, night?"**
Not handled yet: snow-cover gating is on the roadmap. Night footage lowers recall, which multi-pass confirmation
from fleet vehicles offsets.

**"Why not Waze or Samsara data?"**
Those tell you something is wrong somewhere. RoadSense's value is the layer after detection: verification, the right
authority, deduplication and a human-approved ticket. It can also ingest third-party feeds as another source.
