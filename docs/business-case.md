# RoadSense business case

The numbers a judge or a city manager will ask for, with sources and with what is still an assumption.

## The problem, in Halifax's own numbers

- **3,203 pothole reports from January 1 to March 19, 2026.** About half were Priority 1 (at least 25 cm wide and
  8 cm deep). HRM says the count includes the same pothole reported several times and potholes on provincial roads
  it does not maintain. ([CTV Atlantic](https://www.ctvnews.ca/atlantic/nova-scotia/article/more-potholes-in-halifax-this-year-compared-to-2024-municipality-says/),
  [HRM: Potholes and Sinkholes](https://www.halifax.ca/transportation/streets-sidewalks/paving-repair/report-a-pothole))
- **Two front doors.** City streets go to 311; provincial roads and 100-series highways go to 511. A resident has to
  know which road is whose. ([HRM](https://www.halifax.ca/transportation/streets-sidewalks/paving-repair/report-a-pothole))
- **Repair targets** are about 7 days for a priority pothole and 30 days for others; drivers' damage typically runs
  $300 to $1,000 per incident. ([CTV Atlantic](https://atlantic.ctvnews.ca/we-ve-had-thousands-pothole-related-damages-on-the-rise-as-temperatures-grow-warmer-in-halifax-1.6831435))
- **Most damage claims fail.** Nova Scotia claims for pothole compensation mostly hit a dead end, so the cost lands
  on residents. ([CBC](https://amp.cbc.ca/lite/story/1.7148599))
- **Who finds potholes.** In Barrie, Ontario, about 90% of potholes are found by roads operators and 7% by citizens.
  ([Esri Canada](https://resources.esri.ca/cityworks/integrating-arcgis-and-cityworks-for-a-complete-pothole-management-solution))
  Halifax's split is unknown. If it is similar, the bigger market is the city's own patrol, not citizen reports.

## What one 311 contact costs

| Figure | Value | Source |
|---|---|---|
| Cost per 311 call | $11 to $16 | Toronto's director of customer experience ([Global News](https://globalnews.ca/news/10238190/toronto-311-call-cost-budget)) |
| Average handle time | 293 s (Toronto, 2023); 353 s (Calgary, 2023) | Toronto 311 report; Calgary service plan |

RoadSense uses the low end ($11, 4.9 min) as its default. Both are settings (`ROADSENSE_COST_PER_CONTACT`,
`ROADSENSE_MINUTES_PER_CONTACT`) so HRM can enter its own figures.

## What RoadSense saves, and how it is counted

The Overview page shows **What this saved the city**, computed from the database
([roadsense/core/impact.py](../roadsense/core/impact.py)). Only work that never reached a person is priced:

| Counted | Why it is a saving |
|---|---|
| Duplicate sightings merged, and re-sightings of rejected reports suppressed | Each would have been a separate 311 contact for the same pothole |
| Provincial-road reports sent straight to NS Public Works | HRM did not receive and redirect them |
| False alarms filtered *(shown, not priced)* | No person would have filed them, so pricing them would overstate the saving |

### Scaling it to Halifax (scenario, not a measurement)

HRM has not published what share of the 3,203 reports were duplicates or provincial. The table shows the intake
saving for a range of shares, over the same 11 weeks, at $11 and 4.9 minutes per contact:

| Share that was duplicate or provincial | Contacts avoided | Intake cost avoided | Staff time |
|---:|---:|---:|---:|
| 20% | 641 | $7,050 | 52 h |
| 30% | 961 | $10,570 | 78 h |
| 40% | 1,281 | $14,090 | 105 h |

Intake savings alone are modest. The case rests on three bigger effects that RoadSense makes measurable but that we
have not yet measured in Halifax:

1. **Earlier detection.** A fleet vehicle that drives the same route every day sees a pothole before a resident
   reports it, which shortens the time to the 7-day Priority 1 target and the window for vehicle damage.
2. **Evidence a crew can act on.** Every report carries a privacy-blurred photo, an estimated width, a severity band,
   the number of independent drivers, and the exact road. Crews spend less time finding and sizing the defect.
3. **A defensible inspection record.** Each sighting, decision and repair is in an append-only audit log, which is
   what a road authority needs when a damage claim is disputed.

## What it costs to run

| Item | Today (MVP) | Pilot |
|---|---|---|
| Edge detection | Laptop GPU (GTX 1650 Ti), YOLOv8m at 17.8 FPS | Phone or Jetson-class kit per vehicle |
| AI judgement | 5 model calls and about 8k tokens per trip (free OpenRouter tier) | A Canadian-hosted endpoint; cents per trip at paid rates |
| Hosting | One Google Cloud VM with HTTPS | Postgres/PostGIS and object storage in a Canadian region |

The model is asked only where judgement adds something (borderline detections, ambiguous roads, the description a
crew reads), so the cost per trip stays flat as the fleet grows.

## Pilot proposal

- **Who:** HRM Roads Operations, with 5 to 10 vehicles that repeat routes (transit, solid waste, parking enforcement).
- **How long:** one spring thaw, about 8 weeks.
- **Integration:** read-only first. RoadSense reads HRM's open Cityworks service requests from ArcGIS Hub to dedupe
  against what the city already knows, and sends reports to a reviewer, not straight into Cityworks.
- **Success measures:** precision of approved reports, share of potholes found before a 311 report, days from first
  sighting to repair, and duplicate contacts avoided.
- **What we need from HRM:** a test Cityworks or ArcGIS endpoint, a reviewer for two hours a week, and a privacy
  impact assessment.
