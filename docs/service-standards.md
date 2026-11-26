# Service-standard tracking and the duty-of-care ledger

RoadSense compares each approved report against the municipality's published service standard (or, for a report
referred to another authority, against that authority's). It also keeps a tamper-evident history of what happened to
the report.

This is a **compliance-monitoring and evidence-management tool**. Meeting or missing a published standard does not, on
its own, establish or remove legal liability. Liability depends on notice, the standard that applied, the facts and
the law, and those are matters for the authority's own staff and counsel.

## What happens to a report

1. A reviewer approves the report (`core.workflow.transition(..., "approve")`) and **the service clock starts** in the
   same transaction. The municipal workflow then moves it (see the table below). A report referred to another
   authority (`ROADSENSE_FORWARD_MODE=on`, a road the municipality does not own) is the exception: its clock starts
   when the referral is accepted (`service_standards.start_clock_safely`), and a tracking error never rolls back a
   referral that already happened.
2. The clock records the **responsible authority**, using the same directory and aliases as delivery, so "Dartmouth"
   resolves to Halifax Regional Municipality.
3. It looks up the **applicable rule** (see below) and computes the **response** and **completion** deadlines in the
   authority's timezone.
4. The rule is **copied into the clock**: `rule_snapshot`, a `rule_hash` of the timing fields, and the holidays that
   fall inside the window. A database trigger refuses any later change to `submitted_at`, the deadlines or the rule
   reference. Editing a rule later never moves an existing report's deadlines.
5. For referred reports, follow-up (scheduled hourly, or the *Run follow-up* button) polls the other authority. It turns status changes into
   lifecycle events and runs `sweep()`, which writes a ledger entry when a deadline becomes close or passes. Repaired
   reports are re-polled for `ROADSENSE_REOPEN_WATCH_DAYS` (30 by default), so a reopened request is noticed.
6. Reviewers can record what an authority said outside its API (phone, email, referral replies, site visits) from
   the report's timeline. This is the only route for referral-only authorities such as NS Public Works.

### The municipal workflow drives the clock

| Workflow step (`core.workflow`) | Clock event |
| --- | --- |
| approve | clock starts (`report_submitted`) |
| assign inspector | `acknowledged` |
| inspection recorded (repair needed / no repair needed) | `inspection_started` |
| no repair needed | `report_closed` (not counted as a repair) |
| marked repaired | `repair_completed` |
| reopen (by a person, or a confident re-detection) | `report_reopened` |
| repair independently verified | `report_closed` |

Each step is also a `workflow_<action>` entry in the same hash chain. Recording an event by hand on the report's
timeline only moves the clock; a report's own status changes only through the workflow.

The authority's own status (`authority_status`, `authority_notes`) is stored exactly as reported. It is kept separate
from RoadSense's **lifecycle stage** (submitted → acknowledged → inspection → repair started → repaired → closed, plus
reopened) and from the **tracking status** below.

### Mapping authority statuses to events

The keywords live in `roadsense/core/service_standards.json` (`status_keywords`):

| Authority says | Ledger event(s) |
| --- | --- |
| `closed` | `repair_completed` + `report_closed` |
| `closed` with "duplicate", "not found", "no action"… | `report_closed` only (not counted as a repair) |
| open, notes contain "acknowledg", "assigned"… | `acknowledged` |
| open, notes contain "inspect", "assess"… | `inspection_started` |
| open, notes contain "in progress", "work order", "crew"… | `repair_started` |
| open after a closure | `report_reopened` (the closure entries stay) |
| anything else that changed | `authority_status_changed` |

An automatic receipt such as "Received" is **not** counted as an acknowledgement. Reaching any later step implies
acknowledgement and sets `acknowledged_at`. Repeated polls with no change add nothing.

## Service-standard rules

These rules are RoadSense's **only** deadline source. The "Repair target" on a report page is the completion target
of the rule that applies to it: frozen from its service clock once filed, otherwise the rule that would apply if it
were filed today, marked *unverified* until an admin verifies or approves the rule. RoadSense does not send a repair
deadline to the authority.

Severity is one scale too: the **severity band** (S1 minor to S4 urgent, from estimated width or area, capped at S2
when the detector is unsure). The low/medium/high wording in reports and in the authority's description is only the
band's name (S1 low, S2 medium, S3–S4 high), so the words and the band that selects the rule never disagree.

The rules live in the `service_standards` table and are managed by admins on **Service standards**. The values are not
hardcoded in code. Each rule has an authority and a jurisdiction label. It can optionally be narrowed by road category
(OSM road class), damage category (D00/D10/D20/D40) and priority (severity band S1–S4). It also has a response target
and/or a completion target, a unit (`business_days` or `calendar_days`), a timezone, effective start and end dates,
the official source URL, a verification status and a last-reviewed date.

**Selection.** A rule applies when it is in force on the local submission date and each narrowing field is either
empty (meaning any) or equal to the report's value. The most specific rule wins. Verification only breaks ties: an
unverified specific rule still applies, and the report is then flagged. It is never silently measured against a looser
verified rule.

**Verification.**
- `unverified`: deadlines are calculated and shown as *provisional*, but the report counts as **unable to determine**.
- `verified`: checked against the official publication. A source URL is required.
- `approved`: adopted by an admin, for example an internal policy with no public source.

When an admin verifies or approves a rule without changing its timing, open reports filed under that exact rule (same
`rule_hash`) get `rule_verified_at` and a `standard_verified` ledger entry. Their deadlines stay unchanged.

**Seed values.** On first use the database is seeded from `service_standards.json`:
- HRM: 30 business days by default, with 10 days to first response; 7 business days for S3/S4 potholes.
- NS Public Works: 120 business days by default; 7 business days for S3/S4 potholes.

These come from RoadSense's research notes, not from a checked official publication, so **every seeded rule is
unverified**. The unit for provincial roads and the mapping from severity band to "priority" are assumptions to confirm
with each authority. Rules cannot be deleted (trigger). Retire a rule by setting `effective_to`.

## Deadline calculation (`roadsense/core/business_days.py`)

- Day 0 is the **local** calendar day of submission, in the rule's timezone (default `America/Halifax`).
- *N business days* ends at 23:59:59 local time on the N-th business day after day 0. Weekends (Sat/Sun) and the
  configured holidays are skipped. A report filed on a Saturday starts counting on Monday.
- *N calendar days* ends at 23:59:59 local time, N days after day 0.
- Results are stored in UTC. Naive timestamps are refused. Daylight-saving changes cannot shift a deadline.
- The warning window (`warn_days`, 2 by default) starts at the end of the day N units before each deadline.
- Holidays are configured as **observed** dates, either for all authorities or for one. The seed is an illustrative
  Nova Scotia calendar for 2026–2027 that needs confirming. Adding or removing a holiday affects only reports filed
  afterwards.

## Tracking statuses

| Status | Rule |
| --- | --- |
| Overdue | Open, and the completion deadline has passed; or the authority has not acknowledged and the response deadline has passed. Acknowledgement does not stop the completion clock. |
| Approaching deadline | Open, not overdue, inside the warning window of the next deadline. |
| Awaiting authority response | Open, nothing close or passed, and not yet acknowledged. |
| Within service standard | Open, acknowledged, nothing close or passed. A late acknowledgement is still shown ("response was late"). |
| Completed within target | Repaired (or closed) on or before the completion deadline. |
| Completed after target | Repaired (or closed) after the completion deadline. |
| Unable to determine | No applicable rule, or the rule is unverified, or the report was closed and the rule has no completion target. |

A reopened report keeps its original deadlines and is measured against them again.

## Duty-of-care ledger (`roadsense/core/ledger.py`)

The ledger is not a second audit system. Its entries are rows of the existing append-only `audit` table, with action
`ledger_<event>` and subject `report:<id>`, plus two new columns, `prev_hash` and `hash`. The detail JSON holds:
- the report ID and authority
- the event type and when it happened
- the previous and new lifecycle stage
- the tracking status before and after
- the source (`authority:open311`, `manual`, `roadsense`, `backfill`…) and who was responsible
- evidence references: best evidence frame, the authority's request ID, the submitted payload's SHA-256
- the rule ID, its hash and verification, and the deadlines
- the authority's status and notes, and an optional note

**Integrity.** The hash reuses the existing payload hashing (`open311.payload_hash`: SHA-256 over sorted-key JSON)
over `ts, actor, action, subject, detail, prev_hash`. Each report's entries form a hash chain that starts from
`"0" * 64`. The existing triggers refuse `UPDATE` and `DELETE` on `audit`, and a unique index on
`(subject, prev_hash)` refuses a forked chain. `ledger.verify()` recomputes every hash and link. It also compares the
newest entry with the chain head kept on the service clock, so it catches entries deleted from the end as well.
Reviewers see the result on each report's timeline. `GET /service-standards/integrity` checks everything.

**What SHA-256 does not do.** Someone with write access to the SQLite file can drop the triggers and rewrite a whole
chain consistently. Hashing makes tampering detectable, not impossible. For evidence that must hold up:
- run the services under a dedicated account that is the only one with write access to `data/roadsense.db` (mode 0600)
- keep backups that the application cannot overwrite
- periodically copy the chain heads (`/service-standards/integrity`) somewhere RoadSense cannot write, such as a
  records system, an email to the clerk or a WORM bucket
- on PostgreSQL, grant the application role only `INSERT`/`SELECT` on `audit`

## Access

| Action | Who |
| --- | --- |
| Dashboard and rules list | any signed-in role. No photos or car positions; addresses only. |
| Report timeline, ledger, integrity check, recording authority events | reviewers and admins. These name staff. |
| Create or edit rules and holidays | admins. Every change is audited with before/after values. |

## API (agent service; the web app forwards it under `/api/service-standards…`)

| Method | Path | |
| --- | --- | --- |
| GET | `/service-standards/dashboard?authority=&neighbourhood=&damage=&priority=&status=&date_from=&date_to=` | metrics + items |
| GET | `/service-standards/rules` | rules, holidays and allowed values |
| POST / PATCH | `/service-standards/rules[/{id}]` | admin |
| POST / DELETE | `/service-standards/holidays[/{day}]` | admin |
| GET | `/service-standards/reports/{id}` | clock, rule snapshot, ledger, integrity (reviewer) |
| POST | `/service-standards/reports/{id}/events` | `{"event", "at"?, "note"?}` (reviewer) |
| GET | `/service-standards/integrity[?report_id=]` | reviewer |

## Demo

In the mock 311 inbox (`python -m roadsense.services.mock_open311`, http://127.0.0.1:8311), each request now has
*Acknowledge*, *Inspect*, *In progress*, *Mark fixed* and *Reopen* buttons. Click one, then *Run follow-up* in the app,
and the change appears on **Service standards**.

## Known limitations

- The seeded standards and holidays are unverified. An admin must confirm them, with the official source, before the
  dashboard counts any report against them.
- Response and repair averages are in calendar hours and in deadline-style day counts. They are not adjusted for a
  report's time spent reopened.
- Open311 GeoReport v2 only exposes open/closed plus free-text notes, so the finer lifecycle steps depend on the
  keyword mapping, and an authority's wording may need new keywords. Referral-only authorities depend on reviewers
  recording events by hand.
- Neighbourhood comes from the geocoder cache (OpenStreetMap `suburb`). It is empty in offline mode.
- Event times from the authority are the times RoadSense observed the change, unless a reviewer records the actual
  time. Polling is hourly by default.
- Reports filed before this feature are backfilled by the first sweep from their `status_submitted` audit time, using
  the rule in force now; that is noted in their ledger entry.
