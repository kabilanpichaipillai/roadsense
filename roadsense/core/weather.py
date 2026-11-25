"""Daily weather from Environment and Climate Change Canada (MSC GeoMet OGC API, api.weather.gc.ca), cached in SQLite.

    observed   `climate-daily`: station records of daily min/max/mean temperature, precipitation and snowfall. The
               current day is published empty and filled in about a day later; any day the station did not report
               stays empty (None). Values are never interpolated or invented.
    forecast   `citypageweather-realtime`: the public city forecast. It covers about six days in day/night periods
               ("Thursday" high, "Thursday night" low). A night's low is the next calendar day's minimum. Days beyond
               the forecast are simply absent, so a 14-day horizon is partly unknown, and that shows in data quality.

Weather is looked up per 0.1-degree cell (about 11 km): one fetch serves every road segment in a neighbourhood.
Every stored value carries its source, station and fetch time. When ECCC is unreachable, the last stored values are
used if there are any, the failure is recorded, and the caller is told the data is stale or unavailable.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from ..common.config import settings
from ..common.db import now
from ..edge.gps import haversine_m

NETWORK_TIMEOUT_S, BREAK_S = 10, 300
CLIMATE_DAILY, CITY_PAGE = "climate-daily", "citypageweather-realtime"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_down_until = 0.0  # circuit breaker: after a failure, don't wait on ECCC again for BREAK_S


def reset() -> None:
    global _down_until
    _down_until = 0.0


def cell_for(lat: float, lon: float) -> str:
    return f"{round(lat, 1):.1f},{round(lon, 1):.1f}"


def _client(client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(timeout=NETWORK_TIMEOUT_S, headers={"User-Agent": settings.nominatim_user_agent})


def _get_features(client: httpx.Client | None, collection: str, params: dict) -> list:
    """GET a collection's items; one retry for a dropped connection or a 5xx, so a blip does not trip the breaker."""
    http, url = _client(client), f"{settings.eccc_api_url.rstrip('/')}/collections/{collection}/items"
    for attempt in (1, 2):
        try:
            r = http.get(url, params=params)
            r.raise_for_status()
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            retry = isinstance(e, httpx.TransportError) or e.response.status_code >= 500
            if attempt == 2 or not retry:
                raise
    features = r.json().get("features")
    if not isinstance(features, list):
        raise ValueError("response has no feature list")
    return features


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _stored(conn: sqlite3.Connection, cell: str, kind: str, start: date | None = None, end: date | None = None) -> list[dict]:
    sql, args = "SELECT * FROM weather_daily WHERE cell = ? AND kind = ?", [cell, kind]
    if start:
        sql, args = sql + " AND date >= ?", [*args, start.isoformat()]
    if end:
        sql, args = sql + " AND date <= ?", [*args, end.isoformat()]
    return [dict(r) for r in conn.execute(sql + " ORDER BY date", args)]


def _fetch_state(conn: sqlite3.Connection, cell: str, kind: str) -> dict | None:
    r = conn.execute("SELECT * FROM weather_fetches WHERE cell = ? AND kind = ?", (cell, kind)).fetchone()
    return dict(r) if r else None


def _record_fetch(conn: sqlite3.Connection, cell: str, kind: str, ok: bool, start=None, end=None, error=None) -> None:
    prev = _fetch_state(conn, cell, kind) or {}
    if not ok:  # keep the range of the last good fetch, so the cache still knows what it holds
        start, end = prev.get("range_start"), prev.get("range_end")
    conn.execute("INSERT OR REPLACE INTO weather_fetches (cell, kind, range_start, range_end, ok, error, fetched_at) "
                 "VALUES (?,?,?,?,?,?,?)", (cell, kind, str(start) if start else None, str(end) if end else None,
                                            int(ok), (error or "")[:300] or None, now()))


def _age_hours(ts: str | None) -> float:
    if not ts:
        return float("inf")
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600


def _fresh_observed(state: dict | None, start: date, end: date, today: date) -> bool:
    """A cached range is reusable if it covers the request and is recent, or if every requested day is old enough
    (a week) that ECCC will not change it any more."""
    if not state or not state.get("range_start"):
        return False
    covers = state["range_start"] <= start.isoformat() and state["range_end"] >= end.isoformat()
    if not covers:
        return False
    fetched = datetime.fromisoformat(state["fetched_at"]).date()
    return _age_hours(state["fetched_at"]) < settings.weather_cache_hours or end <= fetched - timedelta(days=7)


def _result(records, available, station=None, error=None, cached=False, source=None) -> dict:
    return {"available": available, "records": records, "station": station, "error": error, "cached": cached,
            "source": source}


def observed(conn: sqlite3.Connection, lat: float, lon: float, start: date, end: date,
             client: httpx.Client | None = None, today: date | None = None) -> dict:
    """Daily station records for start..end from the ECCC station nearest the point with the most complete data."""
    global _down_until
    cell, today = cell_for(lat, lon), today or datetime.now(timezone.utc).date()
    state = _fetch_state(conn, cell, "observed")
    if state and state["ok"] and _fresh_observed(state, start, end, today):
        rows = _stored(conn, cell, "observed", start, end)
        return _result(rows, bool(rows), rows[0]["station_name"] if rows else None, cached=True,
                       source=rows[0]["source"] if rows else None)
    if client is None and (settings.offline_weather or time.monotonic() < _down_until):
        rows = _stored(conn, cell, "observed", start, end)
        return _result(rows, bool(rows), error="ECCC not contacted (offline mode or recent failure)", cached=True,
                       source=rows[0]["source"] if rows else None)
    clat, clon = (float(v) for v in cell.split(","))
    params = {"f": "json", "limit": 5000, "datetime": f"{start.isoformat()}/{end.isoformat()}",
              "bbox": f"{clon - 0.25:.3f},{clat - 0.25:.3f},{clon + 0.25:.3f},{clat + 0.25:.3f}"}
    try:
        features = _get_features(client, CLIMATE_DAILY, params)
    except (httpx.HTTPError, ValueError) as e:
        _down_until = time.monotonic() + BREAK_S
        _record_fetch(conn, cell, "observed", False, error=f"{type(e).__name__}: {e}")
        rows = _stored(conn, cell, "observed", start, end)
        return _result(rows, bool(rows), error=f"ECCC climate-daily unavailable: {e}"[:300], cached=True,
                       source=rows[0]["source"] if rows else None)
    stations: dict[str, dict] = {}
    for f in features:
        p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or [None, None]
        sid = p.get("CLIMATE_IDENTIFIER")
        day = str(p.get("LOCAL_DATE") or "")[:10]
        if not sid or not day:
            continue
        s = stations.setdefault(sid, {"name": p.get("STATION_NAME"), "days": {}, "complete": 0,
                                      "distance": haversine_m(lat, lon, g[1], g[0]) if None not in g[:2] else 1e9})
        rec = {"t_min": _num(p.get("MIN_TEMPERATURE")), "t_max": _num(p.get("MAX_TEMPERATURE")),
               "t_mean": _num(p.get("MEAN_TEMPERATURE")), "precip_mm": _num(p.get("TOTAL_PRECIPITATION")),
               "snow_cm": _num(p.get("TOTAL_SNOW"))}
        s["days"][day] = rec
        s["complete"] += rec["t_min"] is not None and rec["t_max"] is not None
    usable = [(sid, s) for sid, s in stations.items() if s["complete"]]
    if not usable:
        _record_fetch(conn, cell, "observed", True, start, end)
        return _result([], False, error="no ECCC station near this point reported temperatures for these dates")
    # most complete record first; between equally complete stations, the nearest
    sid, s = max(usable, key=lambda kv: (kv[1]["complete"], -kv[1]["distance"]))
    source, ts = f"ECCC climate-daily ({s['name']}, {sid})", now()
    for day, rec in s["days"].items():
        conn.execute(
            "INSERT OR REPLACE INTO weather_daily (cell, date, kind, station_id, station_name, t_min, t_max, t_mean, "
            "precip_mm, snow_cm, precip_chance, precip_expected, source, issued_at, fetched_at) "
            "VALUES (?,?,'observed',?,?,?,?,?,?,?,NULL,NULL,?,NULL,?)",
            (cell, day, sid, s["name"], rec["t_min"], rec["t_max"], rec["t_mean"], rec["precip_mm"], rec["snow_cm"],
             source, ts))
    _record_fetch(conn, cell, "observed", True, start, end)
    rows = _stored(conn, cell, "observed", start, end)
    return _result(rows, True, s["name"], source=source)


# ---------------- forecast ----------------
def _en(v):
    return v.get("en") if isinstance(v, dict) else v


def parse_city_forecast(feature: dict) -> list[dict]:
    """citypageweather-realtime feature → one record per calendar day: {date, t_min, t_max, precip_expected,
    precip_chance, issued_at}. Dates are derived from the issue time and the weekday names of the periods."""
    props = feature.get("properties") or {}
    group = props.get("forecastGroup") or {}
    periods = group.get("forecasts") or []
    issued = str(_en(group.get("timestamp")) or _en(props.get("lastUpdated")) or "")  # bilingual {"en", "fr"}
    try:
        base = datetime.fromisoformat(issued.replace("Z", "+00:00")).date()
    except ValueError:
        return []

    def weekday(period: dict) -> tuple[int | None, bool]:
        name = str(_en((period.get("period") or {}).get("value")) or
                   _en((period.get("period") or {}).get("textForecastName")) or "").lower()
        first = name.split(" ")[0] if name else ""
        return (WEEKDAYS.index(first) if first in WEEKDAYS else None), "night" in name

    first_wd = next((w for w, _ in map(weekday, periods) if w is not None), None)
    if first_wd is None:
        return []
    # the issue time is UTC, which runs ahead of Canadian local time: step back to the local day of the first period
    while base.weekday() != first_wd:
        base -= timedelta(days=1)
    days: dict[date, dict] = {}
    for p in periods:
        wd, night = weekday(p)
        if wd is None:
            continue
        d = base + timedelta(days=(wd - base.weekday()) % 7)
        temps = (p.get("temperatures") or {}).get("temperature") or []
        for t in temps if isinstance(temps, list) else [temps]:
            cls, value = str(_en(t.get("class")) or "").lower(), _num(_en(t.get("value")))
            if value is None:
                continue
            if cls == "high":
                days.setdefault(d, {})["t_max"] = value
            elif cls == "low":  # a night's low is reached before dawn: the next calendar day's minimum
                days.setdefault(d + timedelta(days=1) if night else d, {})["t_min"] = value
        target = d + timedelta(days=1) if night else d
        rec = days.setdefault(target, {})
        precip = (p.get("precipitation") or {}).get("precipPeriods") or []
        rec["precip_expected"] = bool(rec.get("precip_expected")) or bool(precip)
        m = re.search(r"(\d{1,3})\s*percent chance", str(_en(p.get("cloudPrecip")) or ""), re.I)
        if m:
            rec["precip_chance"] = max(rec.get("precip_chance") or 0, float(m.group(1)))
    return [{"date": d.isoformat(), "t_min": v.get("t_min"), "t_max": v.get("t_max"), "precip_expected":
             v.get("precip_expected"), "precip_chance": v.get("precip_chance"), "issued_at": issued}
            for d, v in sorted(days.items())]


def forecast(conn: sqlite3.Connection, lat: float, lon: float, client: httpx.Client | None = None) -> dict:
    """The current ECCC city forecast nearest the point, as daily records (about six days)."""
    global _down_until
    cell = cell_for(lat, lon)
    state = _fetch_state(conn, cell, "forecast")
    if state and state["ok"] and _age_hours(state["fetched_at"]) < settings.weather_cache_hours:
        rows = _stored(conn, cell, "forecast")
        return _result(rows, bool(rows), rows[0]["station_name"] if rows else None, cached=True,
                       source=rows[0]["source"] if rows else None)
    if client is None and (settings.offline_weather or time.monotonic() < _down_until):
        rows = _stored(conn, cell, "forecast")
        return _result(rows, bool(rows), error="ECCC not contacted (offline mode or recent failure)", cached=True,
                       source=rows[0]["source"] if rows else None)
    clat, clon = (float(v) for v in cell.split(","))
    params = {"f": "json", "limit": 20,
              "bbox": f"{clon - 0.5:.3f},{clat - 0.5:.3f},{clon + 0.5:.3f},{clat + 0.5:.3f}"}
    try:
        features = _get_features(client, CITY_PAGE, params)
    except (httpx.HTTPError, ValueError) as e:
        _down_until = time.monotonic() + BREAK_S
        _record_fetch(conn, cell, "forecast", False, error=f"{type(e).__name__}: {e}")
        rows = _stored(conn, cell, "forecast")
        return _result(rows, bool(rows), error=f"ECCC city forecast unavailable: {e}"[:300], cached=True,
                       source=rows[0]["source"] if rows else None)

    def dist(f):
        c = (f.get("geometry") or {}).get("coordinates") or []
        return haversine_m(lat, lon, c[1], c[0]) if len(c) >= 2 else 1e12

    for f in sorted(features, key=dist):
        days = parse_city_forecast(f)
        if not days:
            continue
        name = _en((f.get("properties") or {}).get("name")) or f.get("id")
        source, ts = f"ECCC city forecast ({name})", now()
        conn.execute("DELETE FROM weather_daily WHERE cell = ? AND kind = 'forecast'", (cell,))  # a new issue replaces
        for d in days:
            conn.execute(
                "INSERT INTO weather_daily (cell, date, kind, station_id, station_name, t_min, t_max, t_mean, precip_mm,"
                " snow_cm, precip_chance, precip_expected, source, issued_at, fetched_at) "
                "VALUES (?,?,'forecast',?,?,?,?,NULL,NULL,NULL,?,?,?,?,?)",
                (cell, d["date"], str(f.get("id") or ""), name, d["t_min"], d["t_max"], d["precip_chance"],
                 None if d["precip_expected"] is None else int(d["precip_expected"]), source, d["issued_at"], ts))
        _record_fetch(conn, cell, "forecast", True, days[0]["date"], days[-1]["date"])
        return _result(_stored(conn, cell, "forecast"), True, name, source=source)
    _record_fetch(conn, cell, "forecast", True)
    return _result([], False, error="no ECCC city forecast near this point")
