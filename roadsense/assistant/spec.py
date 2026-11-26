"""The validated query specification: the only thing understanding (rules or LLM) may hand to retrieval.

Every field is an enum, a bounded number, a date or a short location string, so a model's answer cannot carry SQL,
code or instructions into the data layer. Unknown fields are rejected (extra="forbid").
"""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core import exposure, service_standards

Intent = Literal[
    "defects",                # list reports (filters, sort, limit)
    "defects_by_road",        # count reports per road
    "repeated_damage",        # spots seen on several trips / by several drivers, or reopened
    "deterioration",          # defects growing between passes (condition.deterioration)
    "road_condition",         # segment condition estimate (condition.segments)
    "repair_verification",    # independent repair verification (repair.*)
    "repair_history",         # verification history of given reports
    "repair_performance",     # reopenings, time to recurrence; repair types are not recorded
    "service_standards",      # overdue / approaching / awaiting / met (service_standards.dashboard)
    "risk",                   # exposure-weighted risk (exposure.listing)
    "inspection_priorities",  # ranked recommendation from the existing scores
    "freeze_thaw",            # freeze-thaw pothole forecast (forecast.latest)
    "report_detail",          # one or more reports by id
    "overview",               # counts by status
    "unsupported_action",     # a request to change something: the assistant is read-only
    "unknown",                # could not tell what was asked
]
INTENTS: tuple[str, ...] = Intent.__args__  # type: ignore[attr-defined]
READ_ONLY_REFUSAL = ("The Road Inspection Assistant is read-only: it cannot submit, approve, reject, close, reopen, "
                     "edit or delete reports, repair verifications or service-standard deadlines. Use the matching "
                     "RoadSense page, where each change needs your role and an explicit confirmation.")
NOT_ENOUGH = "I could not find sufficient information in the available RoadSense records to answer this question reliably."

ClassCode = Literal["D00", "D10", "D20", "D40"]
Severity = Literal["low", "medium", "high"]
Band = Literal["S1", "S2", "S3", "S4"]
ReportStatus = Literal["pending_approval", "approved", "rejected", "submitted", "fixed", "delivery_failed"]
Verification = Literal["awaiting", "verified", "reopened"]
RiskCategory = Literal["critical", "high", "medium", "low"]
ForecastCategory = Literal["high", "medium", "low"]
Sort = Literal["severity", "priority", "recent", "oldest", "confirmations", "risk", "exposure", "growth", "count",
               "condition", "forecast_cycles", "recent_cycles"]
ServiceStatus = Literal[tuple(service_standards.STATUSES)]  # type: ignore[valid-type]
Infrastructure = Literal[tuple(exposure.FEATURE_LABELS)]  # type: ignore[valid-type]

MAX_LIMIT = 50
_LOCATION = re.compile(r"^[\w .,'&()/-]{1,80}$", re.UNICODE)


class QuerySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    intent: Intent = "unknown"
    classes: list[ClassCode] = Field(default_factory=list, max_length=4)
    severity: list[Severity] = Field(default_factory=list, max_length=3)
    bands: list[Band] = Field(default_factory=list, max_length=4)
    statuses: list[ReportStatus] = Field(default_factory=list, max_length=6)
    verification: list[Verification] = Field(default_factory=list, max_length=3)
    service_status: list[ServiceStatus] = Field(default_factory=list, max_length=7)
    risk_category: list[RiskCategory] = Field(default_factory=list, max_length=4)
    forecast_category: list[ForecastCategory] = Field(default_factory=list, max_length=3)
    infrastructure: list[Infrastructure] = Field(default_factory=list, max_length=5)
    location: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    date_label: str | None = Field(default=None, max_length=60)
    sort: Sort | None = None
    limit: int = Field(default=10, ge=1, le=MAX_LIMIT)
    report_ids: list[int] = Field(default_factory=list, max_length=MAX_LIMIT)
    use_previous: bool = False
    first_n: int | None = Field(default=None, ge=1, le=MAX_LIMIT)
    trend: Literal["worsening"] | None = None
    group_by: Literal["neighbourhood", "road"] | None = None

    @field_validator("location")
    @classmethod
    def _location(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = " ".join(v.split())
        if not v:
            return None
        if not _LOCATION.match(v):
            raise ValueError("location may only contain letters, digits, spaces and simple punctuation")
        return v

    @field_validator("report_ids")
    @classmethod
    def _ids(cls, v: list[int]) -> list[int]:
        if any(i <= 0 or i > 10_000_000 for i in v):
            raise ValueError("report ids must be positive integers")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self

    def describe(self) -> list[str]:
        """The interpretation shown to the user, one short phrase per filter."""
        from ..core.reports import CLASS_NAMES

        out = [INTENT_LABELS.get(self.intent, self.intent)]
        if self.intent in ("unsupported_action", "unknown"):
            return out
        if self.use_previous:
            out.append(f"the first {self.first_n} previous results" if self.first_n else "the previous results")
        if self.report_ids:
            out.append("reports " + ", ".join(f"#{i}" for i in self.report_ids[:10]))
        if self.classes:
            out.append(", ".join(CLASS_NAMES.get(c, c) for c in self.classes))
        if self.bands:
            out.append("severity band " + "/".join(self.bands))
        if self.severity:
            out.append("severity " + "/".join(self.severity))
        if self.statuses:
            out.append("status " + "/".join(s.replace("_", " ") for s in self.statuses))
        if self.verification:
            out.append("verification " + "/".join(self.verification))
        if self.service_status:
            out.append("service status " + "/".join(s.replace("_", " ") for s in self.service_status))
        if self.risk_category:
            out.append("risk " + "/".join(self.risk_category))
        if self.forecast_category:
            out.append("forecast " + "/".join(self.forecast_category))
        if self.infrastructure:
            out.append("near " + " or ".join(exposure.FEATURE_LABELS[i].lower() for i in self.infrastructure))
        if self.trend:
            out.append("getting worse")
        if self.location:
            out.append(f"location “{self.location}”")
        if self.date_label or self.date_from or self.date_to:
            out.append(self.date_label or f"{self.date_from or '…'} to {self.date_to or 'now'}")
        if self.group_by:
            out.append(f"grouped by {self.group_by}")
        if self.sort:
            out.append(f"sorted by {SORT_LABELS.get(self.sort, self.sort)}")
        out.append(f"up to {self.first_n or self.limit} results")
        return out


INTENT_LABELS = {
    "defects": "Road defects", "defects_by_road": "Defect counts by road", "repeated_damage": "Repeated damage",
    "deterioration": "Deteriorating defects", "road_condition": "Road condition estimate",
    "repair_verification": "Independent repair verification", "repair_history": "Repair history",
    "repair_performance": "Repair performance", "service_standards": "Service standards",
    "risk": "Exposure-weighted risk", "inspection_priorities": "Inspection priorities",
    "freeze_thaw": "Freeze-thaw pothole forecast", "report_detail": "Report details", "overview": "Overview",
    "unsupported_action": "Change request (not supported)", "unknown": "Not understood",
}
SORT_LABELS = {"severity": "severity", "priority": "RoadSense priority", "recent": "most recent",
               "oldest": "oldest first", "confirmations": "independent drivers", "risk": "overall risk score",
               "exposure": "exposure score", "growth": "growth per week", "count": "number of defects",
               "condition": "worst condition", "forecast_cycles": "forecast freeze-thaw cycles",
               "recent_cycles": "recent freeze-thaw cycles"}


def json_schema_for_prompt() -> dict:
    return QuerySpec.model_json_schema()
