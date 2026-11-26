"""The compact context an external model may see: an allow-list of fields from the retrieved rows.

Never included: coordinates, addresses, photos, trip or event ids, user names or emails, agent- or authority-written
free text (descriptions, notes), tokens or settings. Road names, damage classes, statuses and scores only.
"""
from __future__ import annotations

from ..common.config import settings
from .retrieval import Result
from .spec import QuerySpec

ROW_FIELDS = ("rank", "report_id", "segment_id", "road", "class_name", "band", "lifecycle", "verification",
              "observations", "trips", "drivers", "neighbourhood", "overall", "risk", "physical", "exposure", "near",
              "factors", "service_status", "days_left", "time_unit", "trend", "growth_per_week", "passes",
              "recent_cycles", "forecast_cycles", "score", "defects", "potholes", "condition_index", "rating",
              "days_to_recurrence", "authority_reopened", "checks", "results", "latest_result", "closed", "verified",
              "reopened", "awaiting", "rate", "report_list", "worst_band", "reports", "authority", "overdue")
DATE_FIELDS = ("first_seen", "next_due", "closed_at", "reopened_at", "submitted_at", "repair_eta")


def _clean(v):
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, list):
        return v[:20]
    if isinstance(v, str):
        return v[:160]
    return v


def build(question: str, spec: QuerySpec, res: Result, retrieved_at: str) -> dict:
    rows = []
    for r in res.rows[:settings.assistant_context_rows]:
        out = {k: _clean(r[k]) for k in ROW_FIELDS if k in r and r[k] not in (None, "", [])}
        for k in DATE_FIELDS:
            if r.get(k):
                out[k] = str(r[k])[:10]  # the date is enough, and keeps times out of the prompt
        rows.append(out)
    return {
        "question": question,
        "interpretation": spec.describe(),
        "retrieved_at": retrieved_at[:16],
        "data_kind": res.kind,
        "matched_records": res.total,
        "records_shown": len(res.rows),
        "metrics": [{"label": m["label"], "value": m["value"], "kind": m["kind"]} for m in res.metrics],
        "records": rows,
        "warnings": res.warnings[:6],
        "limitations": res.limitations[:6],
        "method": res.method,
    }


def allowed_ids(ctx: dict) -> set[int]:
    ids: set[int] = set()
    for r in ctx["records"]:
        if isinstance(r.get("report_id"), int):
            ids.add(r["report_id"])
        ids.update(i for i in r.get("report_list") or [] if isinstance(i, int))
    return ids
