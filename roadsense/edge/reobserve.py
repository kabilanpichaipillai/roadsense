"""Re-observation on the edge: keep one frame of each closed report the car drives past (repair verification).

The agent API publishes a watchlist (report id, position, class) of reports the authority closed. While a trip is
processed, every analysed frame is checked against it: when a watched spot is ahead of the car (inside the camera's
field of view, within viewing distance), the road region where the defect was is projected into the frame and scored
for sharpness and brightness. The best frame per spot is kept, blurred everywhere except that road region, and
uploaded as a "pass". Nothing is kept for the rest of the drive, so no GPS track leaves the vehicle.

Whether the defect is still there is decided on the server (core.repair), together with the trip's detections.
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from ..common.schemas import RoadPass
from .camera import Camera
from .gps import bearing_deg, haversine_m
from .redact import redact

MIN_VIEW_M, MAX_VIEW_M = 4.0, 25.0
PATCH_M = 1.5  # width of road kept sharp around the expected defect position


def project(camera: Camera, forward_m: float, lateral_m: float, width: int, height: int) -> tuple[int, int] | None:
    """Pixel (u, v) of a ground point forward/lateral of the camera (inverse of Camera.ground)."""
    if forward_m <= 0:
        return None
    fx = (width / 2) / math.tan(math.radians(camera.hfov_deg) / 2)
    angle = math.atan(camera.height_m / forward_m) - math.radians(camera.pitch_deg)
    u, v = width / 2 + lateral_m * fx / forward_m, height / 2 + fx * math.tan(angle)
    return (int(u), int(v)) if 0 <= u < width and 0 <= v < height else None


def frame_quality(frame: np.ndarray, box: list[int]) -> tuple[float, float]:
    """(sharpness as Laplacian variance, mean brightness) of the road region."""
    x1, y1, x2, y2 = box
    gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    if gray.size == 0:
        return 0.0, 0.0
    return float(cv2.Laplacian(gray, cv2.CV_64F).var()), float(gray.mean())


class PassCollector:
    def __init__(self, watch: list[dict], camera: Camera | None = None, min_view_m: float = MIN_VIEW_M,
                 max_view_m: float = MAX_VIEW_M):
        self.watch = [w for w in watch or [] if w.get("lat") is not None and w.get("lon") is not None]
        self.camera = camera or Camera()
        self.min_view_m, self.max_view_m = min_view_m, max_view_m
        self.best: dict[int, dict] = {}

    def update(self, frame: np.ndarray, lat: float, lon: float, heading: float, when: datetime) -> None:
        h, w = frame.shape[:2]
        for spot in self.watch:
            d = haversine_m(lat, lon, spot["lat"], spot["lon"])
            if not self.min_view_m <= d <= self.max_view_m:
                continue
            off = math.radians((bearing_deg(lat, lon, spot["lat"], spot["lon"]) - heading + 180) % 360 - 180)
            if abs(off) > math.radians(self.camera.hfov_deg) / 2:
                continue
            forward, lateral = d * math.cos(off), d * math.sin(off)
            centre = project(self.camera, forward, lateral, w, h)
            if centre is None:
                continue
            fx = (w / 2) / math.tan(math.radians(self.camera.hfov_deg) / 2)
            half = max(int(PATCH_M / 2 * fx / forward), 12)
            box = [max(centre[0] - half, 0), max(centre[1] - half, 0), min(centre[0] + half, w), min(centre[1] + half, h)]
            sharp, bright = frame_quality(frame, box)
            best = self.best.get(spot["report_id"])
            if best is None or sharp > best["sharpness"]:
                self.best[spot["report_id"]] = {"frame": frame.copy(), "box": box, "sharpness": sharp,
                                                "brightness": bright, "lat": lat, "lon": lon,
                                                "heading": heading % 360, "when": when}

    def flush(self, trip_id: str, outbox: Path) -> list[RoadPass]:
        out = []
        for report_id, b in self.best.items():
            p = RoadPass(id=f"{trip_id}-r{report_id}", trip_id=trip_id, report_id=report_id, lat=b["lat"],
                         lon=b["lon"], heading=b["heading"], captured_at=b["when"], box=b["box"],
                         sharpness=round(b["sharpness"], 1), brightness=round(b["brightness"], 1))
            cv2.imwrite(str(outbox / f"pass-{p.id}.jpg"), redact(b["frame"], [b["box"]]),
                        [cv2.IMWRITE_JPEG_QUALITY, 90])
            (outbox / f"pass-{p.id}.json").write_text(p.model_dump_json(indent=2), encoding="utf-8")
            out.append(p)
        self.best.clear()
        return out
