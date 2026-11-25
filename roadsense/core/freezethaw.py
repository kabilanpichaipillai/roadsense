"""Freeze-thaw indicators from daily weather records (pure functions, no I/O).

A freeze-thaw cycle is water in a crack freezing (expanding) and thawing again. From daily station data the best we
can say is that the air crossed the freezing point within a day: the minimum was at or below the freeze threshold and
the maximum at or above the thaw threshold. That is an indicator of the conditions, not proof that the pavement
froze, and a crossing on its own does not mean the road was damaged.

Each day is classified as
    confirmed   observed (station record) minimum and maximum, and the air crossed the threshold
    potential   a forecast says it will cross, or only one observed bound is known and it does not rule a crossing out
    none        the known values rule a crossing out (e.g. the maximum stayed below freezing)
    missing     no usable temperature for the day
A record is a dict: {date, kind: 'observed'|'forecast', t_min, t_max, t_mean, precip_mm, snow_cm, precip_chance,
precip_expected}; any value may be None.
"""
from __future__ import annotations

from datetime import date, timedelta

CONFIRMED, POTENTIAL, NONE, MISSING = "confirmed", "potential", "none", "missing"


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN is missing


def classify_day(rec: dict | None, freeze_c: float = 0.0, margin_c: float = 0.0) -> str:
    """One day's freeze-thaw status. A margin requires the air to go `margin_c` below and above the threshold."""
    if not rec:
        return MISSING
    lo, hi = _num(rec.get("t_min")), _num(rec.get("t_max"))
    froze = lo is not None and lo <= freeze_c - margin_c
    thawed = hi is not None and hi >= freeze_c + margin_c
    if lo is None and hi is None:
        mean = _num(rec.get("t_mean"))
        if mean is None:
            return MISSING
        # a mean near zero cannot rule a crossing out; a mean far from it nearly always does
        return POTENTIAL if abs(mean - freeze_c) <= 3 + margin_c else NONE
    if lo is not None and hi is not None:
        if froze and thawed:
            return CONFIRMED if rec.get("kind", "observed") == "observed" else POTENTIAL
        return NONE
    # exactly one bound is known
    if (lo is not None and lo > freeze_c + margin_c) or (hi is not None and hi < freeze_c - margin_c):
        return NONE  # never froze, or never thawed
    return POTENTIAL


def is_wet(rec: dict | None, precip_min_mm: float = 0.2, chance_min: float = 50) -> bool | None:
    """True/False when the record says whether there was (or will be) precipitation; None when it doesn't say."""
    if not rec:
        return None
    precip, snow, chance = _num(rec.get("precip_mm")), _num(rec.get("snow_cm")), _num(rec.get("precip_chance"))
    expected = rec.get("precip_expected")
    known = [v for v in (precip, snow, chance) if v is not None] or ([expected] if expected is not None else [])
    if not known:
        return None
    return bool((precip is not None and precip >= precip_min_mm) or (snow is not None and snow > 0)
                or (chance is not None and chance >= chance_min) or bool(expected))


def summarise(records: list[dict], start: date, end: date, freeze_c: float = 0.0, margin_c: float = 0.0,
              precip_min_mm: float = 0.2, chance_min: float = 50, moisture_window_days: int = 1) -> dict:
    """Indicators over the days start..end inclusive. Days without a record count as missing; nothing is filled in."""
    by_day = {}
    for r in records:
        d = r.get("date")
        d = date.fromisoformat(str(d)[:10]) if d is not None else None
        if d is not None and start <= d <= end:
            by_day[d] = r
    days, statuses, wet = [], {}, {}
    d = start
    while d <= end:
        days.append(d)
        statuses[d] = classify_day(by_day.get(d), freeze_c, margin_c)
        wet[d] = is_wet(by_day.get(d), precip_min_mm, chance_min)
        d += timedelta(days=1)
    cycles = [d for d in days if statuses[d] in (CONFIRMED, POTENTIAL)]
    near = lambda d: any(wet.get(d + timedelta(days=k)) for k in range(-moisture_window_days, moisture_window_days + 1))
    sources = sorted({r.get("source") for r in by_day.values() if r.get("source")})
    return {
        "start": start.isoformat(), "end": end.isoformat(), "days": len(days),
        "confirmed_cycles": sum(statuses[d] == CONFIRMED for d in days),
        "potential_cycles": sum(statuses[d] == POTENTIAL for d in days),
        "missing_days": sum(statuses[d] == MISSING for d in days),
        "wet_days": sum(bool(wet[d]) for d in days),
        "precip_known_days": sum(wet[d] is not None for d in days),
        "moist_cycles": sum(near(d) for d in cycles),
        "coverage": round(sum(statuses[d] != MISSING for d in days) / len(days), 3) if days else 0.0,
        "sources": sources,
        "daily": [{"date": d.isoformat(), "status": statuses[d], "wet": wet[d],
                   "t_min": _num((by_day.get(d) or {}).get("t_min")), "t_max": _num((by_day.get(d) or {}).get("t_max")),
                   "kind": (by_day.get(d) or {}).get("kind")} for d in days],
    }
