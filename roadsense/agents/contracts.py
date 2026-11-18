"""Typed answers the model must give at each judgement step. Validated on every call; one self-correction, then the
rule decides. An answer about an id the model was not asked about, or with a choice it was not offered, is dropped."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Severity = Literal["low", "medium", "high"]


class VisionItem(BaseModel):
    event_id: str
    verdict: Literal["confirmed", "disputed", "rejected"]
    severity: Severity | None = None
    rationale: str = Field(max_length=400)


class VisionResult(BaseModel):
    events: list[VisionItem]


class LocationItem(BaseModel):
    event_id: str
    jurisdiction: str = Field(max_length=80)
    confidence: Literal["high", "medium", "low"]
    rationale: str = Field(max_length=400)


class LocationResult(BaseModel):
    events: list[LocationItem]


class DraftItem(BaseModel):
    report_id: int
    description: str = Field(min_length=30, max_length=600)


class DraftResult(BaseModel):
    reports: list[DraftItem]


def extract_json(text: str) -> dict:
    """The JSON object in a model answer (tolerates ```json fences and surrounding prose)."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    return json.loads(text[start:end + 1])


def validate(model: type[BaseModel], text: str) -> tuple[BaseModel | None, str | None]:
    try:
        return model.model_validate(extract_json(text)), None
    except (ValueError, ValidationError) as e:
        return None, str(e)[:600]
