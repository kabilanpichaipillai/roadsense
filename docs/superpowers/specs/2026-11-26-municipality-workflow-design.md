# Municipal repair workflow — design

Date: 2026-11-26 · Status: approved brief, refinements marked **(changed)**

## Why

RoadSense stops at "Sent": a reviewer approves, the report goes to the road authority over Open311, and RoadSense
only watches for the authority to close it. A municipality running RoadSense as its own system needs the rest of the
job inside the product: inspection, a repair decision with a date, the repair, and a reopen when the repair fails.
Nothing should leave the building by default.

## The workflow

```
pending_approval ──reject──────────────────────────────▶ closed (rejected)
      │ approve
      ▼
awaiting_inspection ──assign──▶ inspection_assigned ──no_repair_needed──▶ closed (no_repair_needed)
      │ refer (forwarding on,             │ repair_needed (+ repair_eta)
      ▼  road not ours, system only)      ▼
closed (referred)                    repair_needed ──repaired──▶ repaired ──reopen──▶ reopened
                                          ▲  └─reschedule (eta)    │                     │ repair_needed (+ new eta)
                                          └──────────────────────────────────────────────┘
                                           repaired + verification_status = verified  =  "Repaired (verified)"
```

| Stored | Label |
|---|---|
| `pending_approval` | Awaiting review |
| `awaiting_inspection` | Awaiting inspection |
| `inspection_assigned` | Inspection assigned |
| `repair_needed` | Repair needed |
| `repaired` | Repaired (or "Repaired (verified)" when `verification_status = 'verified'`) |
| `reopened` | Reopened |
| `closed` | Closed (Rejected / No repair needed / Referred, from `closed_reason`) |

## One module: `roadsense/core/workflow.py`

- `STATUSES`, `LABELS`, `OPEN` (everything not `repaired`/`closed`), `CLOSED_REASONS`.
- `ACTIONS`: `{action: (from_statuses, to_status, roles, required_fields)}`.

| Action | From | To | Who | Needs |
|---|---|---|---|---|
| `approve` | pending_approval | awaiting_inspection | admin, reviewer | payload snapshot + hash (from the API) |
| `reject` | pending_approval | closed / rejected | admin, reviewer | `reason?` |
| `assign` | awaiting_inspection, inspection_assigned | inspection_assigned | admin, reviewer | `inspector_id` (active user with the `inspect` permission) |
| `repair_needed` | inspection_assigned, reopened | repair_needed | admin, reviewer, inspector* | `repair_eta` (ISO date, not before today); `severity_band?` (S1–S4), `notes?`, `photo?` |
| `no_repair_needed` | inspection_assigned | closed / no_repair_needed | admin, reviewer, inspector* | `notes?`, `photo?` |
| `repaired` | repair_needed | repaired | admin, reviewer, inspector* | `notes?`, `photo?` |
| `reopen` | repaired (not verified) | reopened | admin, reviewer, inspector*, system | `reason`; `event_id?` |
| `reschedule` | repair_needed | repair_needed | admin, reviewer, inspector* | `repair_eta` |
| `refer` | awaiting_inspection | closed / referred | system | `service_request_id`, `channel` |

\* an inspector only on reports assigned to them (`inspector_id`). "system" is plain RoadSense code
(`verify:repair`, the delivery path); an actor named `agent:*` is always refused.

- `transition(conn, report_id, action, actor, role, user_id=None, at=None, **fields) -> dict` is the only writer of
  `reports.status`. It checks the table, the role, the assignee and the fields; writes the columns; writes one
  hash-chained ledger entry (`core.ledger`, event `workflow_<action>`, with from/to, who, and the fields); feeds the
  service-standards clock; and returns the updated row. Illegal moves raise `WorkflowError` (`status` 409, or 403 for
  a role/assignee problem, 422 for a missing/invalid field). `at` back-dates a transition (demo data, tests only).
- `allowed_actions(report, role, user_id)` lists what this person may do next; every API response carries it.
- `overdue(report, today=None)`: `status in (repair_needed, reopened)` and `repair_eta < today` (a plain date, Halifax
  local day). Computed, never stored. Reopen clears the ETA, so a reopened report is not overdue until its new date
  passes; until then it shows "needs a new repair date".
- `migrate(conn)`: the old-status migration (below), run from `db.connect()`.

## Schema (via `MIGRATIONS`)

New report columns: `inspector_id` (users.id), `inspection_assigned_at`, `inspected_at`, `inspection_notes`,
`inspection_photo`, `repair_eta`, `repaired_at`, `repaired_by`, `repair_photo`, `repair_notes`, `closed_reason`,
`reopen_count INTEGER NOT NULL DEFAULT 0`. Existing `closed_at`, `reopened_at`, `reopen_reason`, `reopen_event_id`,
`approved_by/at`, `rejected_by` are reused. History lives in the audit table/ledger, not in more columns.

## Migrating old rows

Idempotent, inside `connect()`, one audit row per migrated report (`status_migrated`, `{from, to}`), so a second run
finds nothing to do:

| Old | New |
|---|---|
| approved, submitted, delivery_failed | awaiting_inspection |
| fixed (verification awaiting/verified/none) | repaired (closed_at, verification_status kept; repaired_at = closed_at) |
| fixed + verification reopened | reopened (reopen_count 1) |
| rejected | closed, closed_reason rejected |

`service_request_id` / `external_ref` stay as history.

## Repair verification (`core/repair.py`)

- Watches `status = 'repaired' AND verification_status = 'awaiting'` (was `fixed`).
- A confident re-detection calls `transition(..., "reopen", actor="verify:repair", role="system", reason, event_id)`.
  Reopen increments `reopen_count`, clears `repair_eta`, sets `verification_status = 'reopened'`.
- `repaired` resets any earlier verification cycle (`reset_for_reopen`) and starts a fresh `awaiting` one with
  `closed_at = repaired_at`, so only passes after the latest repair count.
- `verified` stays final: `reopen` is refused on a verified report; later damage there is a new report.
- `lifecycle()` returns the workflow label.

## Service-standards clock **(changed: when it starts)**

The clock measures the municipality now, so it starts at **approve** (not at delivery):

| Transition | Clock event |
|---|---|
| approve | `start_clock` (stage submitted) |
| assign | `acknowledged` |
| repair_needed / no_repair_needed | `inspection_started` (inspection) |
| no_repair_needed | + `report_closed` |
| repaired | `repair_completed` |
| reopen | `report_reopened` |
| verified (repair.py) | `report_closed` |

Referred reports are the exception: their clock starts when the referral is sent (the other authority's clock), as
today. The manual events endpoint stays for referred reports and history, but **no longer changes `reports.status`**.

## Forwarding (`ROADSENSE_FORWARD_MODE`, default `off`) **(changed: needs a home municipality)**

There is no notion of "our" municipality in the code yet, so a new setting `ROADSENSE_MUNICIPALITY` (default
"Halifax Regional Municipality") names it. `ownership.owned_here(jurisdiction)` compares canonical authority names.

- off: approve never queues delivery; `deliver()` refuses; Resend and delivery-failure UI are hidden.
- on: after approve, a report whose `jurisdiction` is not ours is queued for delivery. It waits in
  `awaiting_inspection` (so a failed referral is never lost: staff can resend or inspect it themselves); a successful
  send transitions `refer` → closed/referred and starts its clock. Payload-hash guardrails in `municipality_server.py`
  are unchanged.
- `external.sync_city_requests` keeps the city requests for dedupe but no longer closes reports; follow-up only
  updates the clocks of referred reports.

## Roles

`inspector` added to `ROLES`. `PERMS["review"]` stays admin+reviewer; new `PERMS["inspect"]` = admin+reviewer+inspector.
SSO role claims accept `inspector`. The web app forwards `X-RoadSense-User-Id` (new) with the user and role, so the
agent API can check the assignee. `GET /api/users?role=inspector` is open to reviewers (the list stays admin-only).

## API (web → agent API)

`POST /approve` (payload-hash check kept) · `POST /reject {reason?}` · `POST /assign {inspector_id}` ·
`POST /inspection` multipart `{outcome, repair_eta?, severity_band?, notes?, photo?}` · `POST /repaired` multipart
`{notes?, photo?}` · `POST /reopen {reason}` · `PATCH /eta {repair_eta}` · `GET /reports?status=&inspector_id=me|<id>&overdue=true`
· `GET /api/users?role=inspector`. Each returns the report with `allowed_actions`, `overdue`, `status_label`.
409 on an illegal move.

## Photos

`POST` multipart, JPEG/PNG only (magic bytes), ≤ 5 MB. Decoded with OpenCV and re-encoded as JPEG (drops EXIF incl.
GPS), faces/plates blurred with `privacy.server_redact` (the same server-side pass event photos get), stored as
`media/insp-<id>.jpg` / `media/repair-<id>.jpg`, referenced as `inspection:<id>` / `repair:<id>`, served by the
signed-in route `/api/media/photo/<id>.jpg`.

## UI

New `STATUS` map, map colours, legend, overview order. Report page: a progress strip (Review → Inspection → Repair →
Repaired) with dates, who and the ETA; actions rendered from `allowed_actions` (approve/reject; assign picker; record
inspection form with a date picker, band, notes, photo; mark repaired; reopen with reason; reschedule). The timeline
shows every `workflow_*` ledger entry with who/when and its photo. "My inspections" page (`inspector_id=me`, by
priority). Overdue badge on rows, markers and the overview, plus an "Overdue repairs" count linking to the list. No
"Sent" wording while forwarding is off.

## Agents

Agents draft and recommend only. MCP tools gain nothing that moves a report; `reports_list` uses the new statuses;
`city_submit_request` refuses unless forwarding is on and the report is a referral candidate.

## Metrics

`/metrics` (agent API): `roadsense_reports{status}`, `roadsense_reports_overdue`, `roadsense_report_reopens_total`,
`roadsense_approve_to_repaired_days_median`.

## Non-goals

- Work orders, crews, materials or costs; a crew is whoever marks the repair done.
- Notifying inspectors by email.
- Business-day ETAs (the ETA is a calendar date).

## Other refinements **(changed)**

- Inspection outcomes are two table actions (`repair_needed`, `no_repair_needed`); the API's `outcome` picks one.
- Severity correction is the band (S1–S4); the low/medium/high level and priority follow from it, as everywhere else.
- A new sighting near a report closed as rejected **or no repair needed** is suppressed for
  `ROADSENSE_REJECTED_SUPPRESS_DAYS`, like rejected ones today.
- Every reopen (automatic or manual) sets `verification_status = 'reopened'`, so the repair verification rate counts
  failed repairs either way.
