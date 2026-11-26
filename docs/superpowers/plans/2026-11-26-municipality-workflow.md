# Municipal Repair Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RoadSense runs the municipality's whole repair workflow (review → inspection → repair → reopen) with one
state machine, and sends nothing outside by default.

**Architecture:** `roadsense/core/workflow.py` owns statuses, the transition table and the only `reports.status`
writer, `transition()`, which also writes the hash-chained ledger entry and drives the service-standards clock. The
agent API exposes one route per action; the web app forwards them and renders buttons from `allowed_actions`.

**Spec:** `docs/superpowers/specs/2026-11-26-municipality-workflow-design.md`

## Global constraints

- Branch `feat/municipal-workflow` off `Dev`; one commit per task; full `pytest` and `python scripts/check.py` before
  each commit.
- No new dependencies.
- Commit messages and the PR do not mention Claude or Claude Code.
- `grep -rn "SET status\|status = ?" roadsense` may only hit `core/workflow.py` (events/jobs tables excepted).

## Every status literal today (grep `pending_approval|submitted|delivery_failed|fixed|approved|rejected`)

| File | What changes |
|---|---|
| common/db.py | `REPORT_STATUSES` → from workflow; new columns; `connect()` runs `workflow.migrate` |
| core/reports.py | `OPEN_STATUSES`, `_recently_rejected`, merge `needs_update`, `set_status` removed |
| core/repair.py | `lifecycle`, `_eligible`, watchlist, `check_redetection` reopen via transition |
| core/condition.py | `OPEN` |
| core/exposure.py | `status != 'rejected'` (×3) |
| core/external.py | `close_reports_fixed_by_city` no longer closes |
| core/service_standards.py | sweep backfill query/actions |
| core/delivery.py | docstring only |
| services/agent_api.py | approve/reject/resend, list filter, manual events, stats |
| services/jobs.py | retry/push queries |
| mcp_servers/municipality_server.py | `DELIVERABLE`, `deliver`, `request_status` |
| mcp_servers/reports_server.py | `REPORT_STATUSES`, `save_draft` (pending_approval stays) |
| agents/fallback.py | trip summary, file_report, follow_up |
| agents/orchestrator.py | docstrings |
| assistant/spec.py, understanding.py, retrieval.py, context.py | status words and columns |
| web/static/app.js | STATUS, describe(), MAP_COLORS, URGENCY, tabs, overview, detail, review |
| scripts/eval_agents.py | `status_submitted` audit action |
| tests (assistant, decisions, mcp, ownership, production, repair, service_standards) | new statuses |
| docs/service-standards.md, docs/assistant.md, README.md, docs/pitch.md | wording |

---

### Task 1: workflow module, columns, migration

**Files:** Create `roadsense/core/workflow.py`, `tests/test_workflow.py`; modify `roadsense/common/db.py`.

- [ ] Tests first: every legal transition (table-driven); every illegal (status × action not in table → 409);
  role checks (driver/viewer refused; inspector only when assigned; agent:* refused; system only for refer/reopen);
  required fields (`repair_eta` required and must be a date not before today; reopen reason; assign needs an active
  inspect-capable user); reopen increments `reopen_count` and clears ETA; verified report cannot be reopened; each
  transition writes one `ledger_workflow_*` row and `ledger.verify` is ok; `overdue()`; `allowed_actions()`.
- [ ] Migration tests: one row per old status → new status/closed_reason; second `connect()` changes nothing (no new
  audit rows).
- [ ] Implement; run `pytest tests/test_workflow.py`; full suite (old code still uses `set_status`, fine for now).
- [ ] Commit `workflow: one state machine for the municipal repair workflow`.

### Task 2: route every status write through `transition()`

**Files:** reports.py, repair.py (closure only), condition.py, exposure.py, agent_api.py approve/reject,
municipality_server.py, external.py, jobs.py, fallback.py, reports_server.py, assistant spec/understanding/retrieval,
service_standards.sweep; tests updated.

- [ ] Delete `reports.set_status`; replace callers. Update every duplicated status list from `workflow`.
- [ ] Tests updated to the new statuses; add a grep test: no `UPDATE reports SET status` outside workflow.py.
- [ ] Full suite; commit `route every report status change through the workflow`.

### Task 3: reopen path and service clock

**Files:** repair.py, service_standards.py, workflow.py; tests in test_repair.py, test_service_standards.py.

- [ ] Re-detection on a repaired report → `reopen` (status reopened, count 1, ledger chain ok).
- [ ] Clock: approve starts it; assign → acknowledged; inspection → inspection; repaired → repair_completed;
  reopen → report_reopened; verified → report_closed. Test the stage sequence and compliance.
- [ ] Commit `reopen repaired reports from re-detections and drive the service clock`.

### Task 4: roles, API routes, photo upload

**Files:** web/auth.py, web/app.py, services/agent_api.py, core/media.py; tests in test_web.py, test_production.py.

- [ ] `inspector` role and `inspect` permission; SSO accepts it; `/api/users?role=inspector` for reviewers.
- [ ] Routes: approve, reject, assign, inspection (multipart), repaired (multipart), reopen, PATCH eta, list filters
  (`status`, `inspector_id=me`, `overdue`), each returning the report with `allowed_actions`.
- [ ] Photos: JPEG/PNG magic bytes, ≤ 5 MB, re-encoded, redacted, served at `/api/media/photo/<id>.jpg`.
- [ ] Tests: role matrix over HTTP, 409 on illegal move, photo type/size refusal, inspector sees only own actions.
- [ ] Commit `inspector role, workflow API and inspection/repair photos`.

### Task 5: forwarding setting

**Files:** config.py, ownership.py, agent_api.py, municipality_server.py, jobs.py, fallback.py, external.py.

- [ ] Default off: approve queues nothing and `httpx` is never called (test patches `delivery.submit` to fail).
- [ ] On: foreign road → delivered → closed/referred, clock started; own road → not delivered.
- [ ] Commit `make forwarding to other road authorities optional (off by default)`.

### Task 6: UI

**Files:** web/static/app.js, app.css, index.html.

- [ ] Status vocabulary, map colours/legend, overview order + "Overdue repairs" signal, list overdue badge.
- [ ] Report page progress strip, action forms from `allowed_actions`, photos in the timeline.
- [ ] "My inspections" page (nav entry for inspect-capable users).
- [ ] Keep J/K, FAB clearance, both themes; check phone width.
- [ ] Commit `municipal workflow UI`.

### Task 7: assistant, metrics, demo data, docs

- [ ] Assistant status words; `tests/test_assistant.py` green.
- [ ] Metrics: by status, overdue, reopens, median approve→repaired days.
- [ ] Demo: inspector user and a report in every status (one overdue, one reopened) through `transition(at=...)`.
- [ ] Docs: README, service-standards, pitch, assistant.
- [ ] End-to-end test: review → assign → inspection (ETA) → repaired → re-detection reopen → repair needed →
  repaired → verified, `/service-standards/integrity` ok.
- [ ] Commit `assistant, metrics, demo data and docs for the municipal workflow`.

### Task 8: verify in the browser, open the PR against Dev

- [ ] `python scripts/demo.py`; click through as reviewer and inspector; light/dark; desktop/phone.
- [ ] PR against `Dev`.
