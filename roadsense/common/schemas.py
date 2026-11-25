"""Edge → agent API contract. Kept small and validated, since it crosses a trust boundary."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from .detectors import CLASSES

CODES = {code for code, _, _ in CLASSES.values()}


class DamageEvent(BaseModel):
    """One piece of road damage seen by the edge device (a track over several frames, best frame kept)."""

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{6,64}$")
    trip_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,64}$")
    track_id: int | None = None
    class_code: str
    class_name: str = Field(max_length=40)
    confidence: float = Field(ge=0, le=1)
    frames_seen: int = Field(ge=1, le=10_000)
    box: list[int] = Field(min_length=4, max_length=4)
    box_area_ratio: float = Field(ge=0, le=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    heading: float | None = Field(default=None, ge=0, lt=360)
    captured_at: datetime
    # lat/lon are the defect's estimated ground position; car_lat/car_lon where the car was
    car_lat: float | None = Field(default=None, ge=-90, le=90)
    car_lon: float | None = Field(default=None, ge=-180, le=180)
    distance_m: float | None = Field(default=None, ge=0, le=100)
    width_m: float | None = Field(default=None, ge=0, le=20)
    position_uncertainty_m: float | None = Field(default=None, ge=0, le=200)
    # peak vertical acceleration (g) from the device IMU around the sighting: corroborates pothole depth
    jolt_g: float | None = Field(default=None, ge=0, le=10)

    @field_validator("class_code")
    @classmethod
    def known_class(cls, v: str) -> str:
        if v not in CODES:
            raise ValueError(f"unknown damage class {v!r}")
        return v


class RoadPass(BaseModel):
    """One redacted frame the edge kept where it drove past a closed report on the watchlist (repair verification).
    Only sent for watched spots, never as a track. lat/lon/heading are the car's at that frame."""

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{6,80}$")
    trip_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,64}$")
    report_id: int | None = Field(default=None, ge=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    heading: float | None = Field(default=None, ge=0, lt=360)
    captured_at: datetime
    # the road region where the defect should be (kept sharp; everything else is blurred)
    box: list[int] = Field(min_length=4, max_length=4)
    sharpness: float = Field(ge=0, le=1e6)
    brightness: float = Field(ge=0, le=255)
