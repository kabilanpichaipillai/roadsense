"""Fixed, read-only query functions: one per intent, each built on the existing core module that owns the data.

No SQL is built from user text: filters from the validated QuerySpec are applied as bound parameters or in Python.
Scores, deadlines and forecasts are read from the services that compute them (exposure, service_standards,
forecast, condition, repair); nothing here invents or re-derives an official number.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..core import condition, exposure, forecast, repair, service_standards
from ..core.reports import CLASS_NAMES, OPEN_STATUSES
from .spec import QuerySpec

TZ = ZoneInfo("America/Halifax")
BAND_RANK = {"S1": 1, "S2": 2, "S3": 3, "S4": 4}
SEV_RANK = {"low": 1, "medium": 2, "high": 3}
MAX_TREND_CANDIDATES = 300  # deterioration is one query per report: cap the candidates and say so


@dataclass
class Result:
    intent: str
    title: str
    kind: str                       # observed | calculated | predicted | mixed
    columns: list[dict] = field(default_factory=list)   # {key, label, kind}
    rows: list[dict] = field(default_factory=list)
    report_ids: list[int] = field(default_factory=list)
    segment_ids: list[str] = field(default_factory=list)
    metrics: list[dict] = field(default_factory=list)   # {label, value, kind, note?}
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    total: int = 0                  # records that matched before the result limit
    available: bool = True          # False: the capability has no data at all
    method: str | None = None
    points: list[dict] = field(default_factory=list)    # map points, one per row record


def col(key: str, label: str, kind: str = "observed") -> dict:
    return {"key": key, "label": label, "kind": kind}


def metric(label: str, value, kind: str = "observed", note: str | None = None) -> dict:
    return {"label": label, "value": value, "kind": kind, **({"note": note} if note else {})}


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _local_day(v: str | None):
    d = _ts(v)
    return d.astimezone(TZ).date() if d else None


class Memo:
    """Per-request cache: each source is read once per question, never across questions (records change)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn, self._c = conn, {}

    def get(self, key: str, fn):
        if key not in self._c:
            self._c[key] = fn()
        return self._c[key]

    def risk(self) -> dict:
        return self.get("risk", lambda: exposure.listing(self.conn))

    def risk_by_id(self) -> dict[int, dict]:
        return self.get("risk_by_id", lambda: {x["report_id"]: x for x in self.risk()["reports"]})

    def standards(self) -> dict:
        return self.get("standards", lambda: service_standards.dashboard(self.conn))

    def standards_by_id(self) -> dict[int, dict]:
        return self.get("standards_by_id", lambda: {i["report_id"]: i for i in self.standards()["items"]})

    def forecast(self) -> dict:
        return self.get("forecast", lambda: forecast.latest(self.conn))

    def trend(self, report_id: int) -> dict:
        return self.get(f"trend:{report_id}", lambda: condition.deterioration(self.conn, report_id))


# ---------------- reports: the shared loader and filters ----------------
REPORT_SQL = """
SELECT r.id, r.trip_id, r.class_code, r.class_name, r.lat, r.lon, r.address, r.municipality, r.jurisdiction,
       r.road_name, r.road_class, r.severity, r.severity_band, r.priority, r.confirmations, r.status,
       r.verification_status, r.closed_at, r.reopened_at, r.reopen_reason, r.best_event_id, r.created_at, r.width_m,
       r.closed_reason, r.repair_eta, r.reopen_count, r.inspector_id,
       (SELECT MIN(e.captured_at) FROM events e WHERE e.report_id = r.id) AS first_seen,
       (SELECT MAX(e.captured_at) FROM events e WHERE e.report_id = r.id) AS last_seen,
       (SELECT COUNT(*) FROM events e WHERE e.report_id = r.id AND e.suppressed_by IS NULL) AS observations,
       (SELECT COUNT(DISTINCT e.trip_id) FROM events e WHERE e.report_id = r.id AND e.suppressed_by IS NULL) AS trips,
       k.neighbourhood AS risk_neighbourhood, c.neighbourhood AS clock_neighbourhood,
       COALESCE(c.reopened_count, 0) AS authority_reopened
FROM reports r LEFT JOIN report_risk k ON k.report_id = r.id LEFT JOIN service_clocks c ON c.report_id = r.id
"""


def load_reports(conn: sqlite3.Connection, ids: list[int] | None = None) -> list[dict]:
    if ids is not None:
        if not ids:
            return []
        rows = conn.execute(REPORT_SQL + f" WHERE r.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    else:
        rows = conn.execute(REPORT_SQL).fetchall()
    out = []
    for x in rows:
        x = dict(x)
        x["neighbourhood"] = x.pop("risk_neighbourhood") or x.pop("clock_neighbourhood", None)
        x.pop("clock_neighbourhood", None)
        road = x["road_name"] or (x["address"] or "").split(" near ")[0]
        # the offline geocoder's "near 44.64, -63.57" is a position, not a road name (and stays out of the AI context)
        x["road"] = road if road and not road.startswith("near ") else "Unnamed road"
        out.append(x)
    return out


def _matches_location(x: dict, loc: str) -> bool:
    loc = loc.lower()
    hay = " | ".join(str(x.get(k) or "") for k in ("road_name", "address", "municipality", "jurisdiction",
                                                   "neighbourhood", "road", "location")).lower()
    return loc in hay


def _in_dates(day, spec: QuerySpec) -> bool:
    if spec.date_from and (day is None or day < spec.date_from):
        return False
    if spec.date_to and (day is None or day > spec.date_to):
        return False
    return True


def filter_reports(rows: list[dict], spec: QuerySpec, res: Result, exclude_rejected: bool = True,
                   date_field: str = "first_seen") -> list[dict]:
    if spec.statuses:
        rows = [x for x in rows if x["status"] in spec.statuses]
    elif exclude_rejected:
        rows = [x for x in rows if not (x["status"] == "closed" and x.get("closed_reason") == "rejected")]
    if spec.classes:
        rows = [x for x in rows if x["class_code"] in spec.classes]
    if spec.bands:
        # a report scored before severity bands existed falls back to its low/medium/high estimate
        rows = [x for x in rows if (x.get("severity_band") in spec.bands) or (
            not x.get("severity_band") and x.get("severity") == "high" and set(spec.bands) & {"S3", "S4"})]
    if spec.severity:
        rows = [x for x in rows if x.get("severity") in spec.severity]
    if spec.verification:
        rows = [x for x in rows if x.get("verification_status") in spec.verification]
    if spec.date_from or spec.date_to:
        rows = [x for x in rows if _in_dates(_local_day(x.get(date_field) or x.get("created_at")), spec)]
    if spec.location:
        before = len(rows)
        rows = [x for x in rows if _matches_location(x, spec.location)]
        if before and not rows:
            res.warnings.append(f"No record matched the location “{spec.location}”. Locations are matched against the "
                                "road names, addresses, municipalities and neighbourhoods stored on reports.")
    return rows


def sort_reports(rows: list[dict], how: str | None) -> list[dict]:
    if how == "recent":
        return sorted(rows, key=lambda x: x.get("first_seen") or x["created_at"] or "", reverse=True)
    if how == "oldest":
        return sorted(rows, key=lambda x: x.get("first_seen") or x["created_at"] or "")
    if how == "confirmations":
        return sorted(rows, key=lambda x: (-(x["confirmations"] or 0), -(x["trips"] or 0), -x["priority"]))
    if how == "priority":
        return sorted(rows, key=lambda x: (-x["priority"], -x["id"]))
    # severity: band, then the low/medium/high estimate, then RoadSense's own priority
    return sorted(rows, key=lambda x: (-BAND_RANK.get(x.get("severity_band") or "", 0),
                                       -SEV_RANK.get(x.get("severity") or "", 0), -x["priority"], -x["id"]))


def evidence(x: dict) -> dict:
    return {"report_id": x["id"], "first_seen": x.get("first_seen"), "last_seen": x.get("last_seen"),
            "observations": x.get("observations"), "trips": x.get("trips"), "drivers": x.get("confirmations"),
            "photo_event_id": x.get("best_event_id"),
            "lat": round(x["lat"], 6) if x.get("lat") is not None else None,
            "lon": round(x["lon"], 6) if x.get("lon") is not None else None}


def report_row(x: dict) -> dict:
    return {"report_id": x["id"], "class_code": x["class_code"], "class_name": x["class_name"], "road": x["road"],
            "neighbourhood": x.get("neighbourhood"), "band": x.get("severity_band") or "–",
            "severity": x.get("severity"), "status": x["status"], "lifecycle": repair.lifecycle(x),
            "verification": x.get("verification_status"), "observations": x.get("observations"),
            "trips": x.get("trips"), "drivers": x.get("confirmations"), "first_seen": x.get("first_seen"),
            "priority": x.get("priority"), "lat": x.get("lat"), "lon": x.get("lon"), "evidence": evidence(x)}


def point(x: dict, label: str, **extra) -> dict | None:
    if x.get("lat") is None or x.get("lon") is None:
        return None
    return {"report_id": x.get("report_id") or x.get("id"), "segment_id": x.get("segment_id"), "lat": x["lat"],
            "lon": x["lon"], "label": label, "status": x.get("status"), "class_code": x.get("class_code"),
            "band": x.get("band") or x.get("severity_band"), **extra}


def finish(res: Result, rows: list[dict], limit: int, label=None) -> Result:
    res.total = len(rows)
    res.rows = rows[:limit]
    res.report_ids = [r["report_id"] for r in res.rows if r.get("report_id")]
    label = label or (lambda r: f"{r.get('class_name', 'Report')} #{r['report_id']}")
    res.points = [p for r in res.rows if (p := point(r, label(r)))]
    if res.total > len(res.rows):
        res.warnings.append(f"Showing the first {len(res.rows)} of {res.total} matching records.")
    return res


REPORT_COLUMNS = [col("report_id", "Report"), col("class_name", "Damage"), col("road", "Location"),
                  col("band", "Severity band", "calculated"), col("observations", "Observations"),
                  col("first_seen", "First detected"), col("lifecycle", "Status")]


# ---------------- intents ----------------
def defects(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("defects", "Road defects", "observed", columns=REPORT_COLUMNS, sources=["reports", "events"])
    rows = filter_reports(load_reports(m.conn, scope), spec, res)
    rows = sort_reports(rows, spec.sort or "severity")
    by_class: dict[str, int] = {}
    for x in rows:
        by_class[x["class_code"]] = by_class.get(x["class_code"], 0) + 1
    res.metrics = [metric("Matching reports", len(rows))] + [
        metric(CLASS_NAMES.get(c, c), n) for c, n in sorted(by_class.items(), key=lambda kv: -kv[1])]
    res.method = ("Severity uses the stored severity band (S1 minor to S4 urgent; pothole width or crack area) and "
                  "the detector's low/medium/high estimate. Depth is not measured.")
    if spec.date_from or spec.date_to:
        res.limitations.append("Dates are the first time a dashcam detected the defect (local Halifax time).")
    return finish(res, [report_row(x) for x in rows], spec.limit)


def defects_by_road(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("defects_by_road", "Defects by road", "observed", sources=["reports"],
                 columns=[col("road", "Road"), col("defects", "Defects"), col("potholes", "Potholes"),
                          col("worst_band", "Worst band", "calculated"), col("report_list", "Reports")])
    rows = filter_reports(load_reports(m.conn, scope), spec, res)
    groups: dict[str, list[dict]] = {}
    for x in rows:
        groups.setdefault(x["road"], []).append(x)
    out = []
    for road, items in groups.items():
        items = sort_reports(items, "severity")
        lat = sum(i["lat"] for i in items) / len(items)
        lon = sum(i["lon"] for i in items) / len(items)
        out.append({"road": road, "defects": len(items), "potholes": sum(i["class_code"] == "D40" for i in items),
                    "worst_band": max((i.get("severity_band") or "" for i in items), key=lambda b: BAND_RANK.get(b, 0))
                    or "–", "report_list": [i["id"] for i in items], "lat": lat, "lon": lon, "items": items})
    out.sort(key=lambda g: (-g["defects"], -g["potholes"], g["road"]))
    res.total = len(out)
    res.rows = out[:spec.limit]
    for g in res.rows:
        res.report_ids += g["report_list"]
        res.points += [p for i in g.pop("items") if (p := point(i, f"{i['class_name']} #{i['id']} · {g['road']}"))]
    for g in out[spec.limit:]:
        g.pop("items", None)
    res.metrics = [metric("Roads with matching defects", len(out)), metric("Matching reports", len(rows))]
    if any(g["road"] == "Unnamed road" for g in res.rows):
        res.limitations.append("“Unnamed road” groups reports whose road name could not be geocoded.")
    if res.total > len(res.rows):
        res.warnings.append(f"Showing the first {len(res.rows)} of {res.total} roads.")
    return res


def repeated_damage(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("repeated_damage", "Locations with repeated damage", "observed", sources=["reports", "events",
                                                                                           "service_clocks"],
                 columns=[col("report_id", "Report"), col("class_name", "Damage"), col("road", "Location"),
                          col("trips", "Trips that saw it"), col("drivers", "Independent drivers"),
                          col("observations", "Observations"), col("reopened", "Reopened"),
                          col("lifecycle", "Status")])
    rows = filter_reports(load_reports(m.conn, scope), spec, res)
    rows = [x for x in rows if (x["trips"] or 0) >= 2 or (x["confirmations"] or 0) >= 2 or
            x.get("verification_status") == "reopened" or x.get("authority_reopened")]
    out = []
    for x in sort_reports(rows, spec.sort or "confirmations"):
        r = report_row(x)
        r["reopened"] = "yes" if x.get("verification_status") == "reopened" or x.get("authority_reopened") else "no"
        out.append(r)
    res.metrics = [metric("Locations seen more than once or reopened", len(out))]
    res.method = "Repeated = seen on two or more trips, by two or more drivers, or reopened after a closure."
    return finish(res, out, spec.limit)


def deterioration(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("deterioration", "Deteriorating defects", "calculated", sources=["reports", "events"],
                 columns=[col("report_id", "Report"), col("class_name", "Damage"), col("road", "Location"),
                          col("band", "Severity band", "calculated"), col("passes", "Passes"),
                          col("first_size", "First size"), col("last_size", "Latest size"),
                          col("growth_per_week", "Growth per week", "calculated"), col("trend", "Trend", "calculated")])
    rows = sort_reports(filter_reports(load_reports(m.conn, scope), spec, res), "priority")
    if len(rows) > MAX_TREND_CANDIDATES:
        res.warnings.append(f"Trends were checked for the {MAX_TREND_CANDIDATES} highest-priority of {len(rows)} "
                            "matching reports.")
        rows = rows[:MAX_TREND_CANDIDATES]
    out, single = [], 0
    for x in rows:
        t = m.trend(x["id"])
        if t["passes"] < 2:
            single += 1
            continue
        if spec.trend == "worsening" and t["trend"] != "worsening":
            continue
        r = report_row(x)
        pts = t["points"]
        r.update(passes=t["passes"], first_size=f"{pts[0]['size']:.3g} {pts[0]['unit']}",
                 last_size=f"{pts[-1]['size']:.3g} {pts[-1]['unit']}", growth_per_week=t["growth_per_week"],
                 trend=t["trend"])
        r["evidence"]["passes"] = [{"at": p["at"], "trip_id": p["trip_id"], "size": p["size"], "unit": p["unit"]}
                                   for p in pts]
        out.append(r)
    out.sort(key=lambda r: -(r["growth_per_week"] or 0))
    res.metrics = [metric("Defects with a measured trend", len(out), "calculated"),
                   metric("Defects seen only once (trend unknown)", single, "observed")]
    res.method = ("Size is the measured width (m) when the camera geometry gave one, otherwise the detection's share "
                  "of the frame. Worsening = more than 15% larger at the latest pass than at the first.")
    if single:
        res.limitations.append(f"{single} matching defect(s) were seen only once, so their trend is unknown, not "
                               "stable.")
    return finish(res, out, spec.limit)


def road_condition(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("road_condition", "Road condition estimate", "calculated", sources=["reports (condition.segments)"],
                 columns=[col("road", "Road segment"), col("condition_index", "Condition index", "calculated"),
                          col("rating", "Rating", "calculated"), col("defects", "Open defects"),
                          col("potholes", "Potholes"), col("report_list", "Reports")])
    segs = condition.segments(m.conn)
    if scope is not None:
        segs = [s for s in segs if set(s["report_ids"]) & set(scope)]
    if spec.location:
        before = len(segs)
        segs = [s for s in segs if spec.location.lower() in s["road"].lower()]
        if before and not segs:
            res.warnings.append(f"No road segment matched “{spec.location}”.")
    out = [{"road": s["road"], "condition_index": s["condition_index"], "rating": s["rating"],
            "defects": s["defects"], "potholes": s["potholes"], "report_list": s["report_ids"],
            "lat": s["lat"], "lon": s["lon"]} for s in segs]
    res.total = len(out)
    res.rows = out[:spec.limit]
    for s in res.rows:
        res.report_ids += s["report_list"]
        if (p := point(s, f"{s['road']} · condition {s['condition_index']}")):
            res.points.append(p)
    res.metrics = [metric("Segments with open defects", len(out), "calculated")]
    res.method = "An estimate: 100 minus capped, density-scaled deducts per open defect, per road and 100 m bin."
    res.limitations.append("A condition estimate from detected defects, not a certified pavement survey.")
    return res


def repair_verification(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("repair_verification", "Independent repair verification", "observed",
                 sources=["reports", "repair_verifications"],
                 columns=[col("report_id", "Report"), col("class_name", "Damage"), col("road", "Location"),
                          col("closed_at", "Marked repaired"), col("verification", "Verification"),
                          col("latest_result", "Latest check"), col("latest_reason", "Why"),
                          col("reopened_at", "Reopened")])
    met = repair.metrics(m.conn)
    if not met["marked_repaired"]:
        res.available = False
        res.limitations.append("No report has been marked repaired yet, so there are no repairs to verify.")
        return res
    rows = [x for x in load_reports(m.conn, scope) if x.get("verification_status")]
    rows = filter_reports(rows, spec, res, exclude_rejected=False, date_field="closed_at")
    out = []
    for x in sort_reports(rows, "recent"):
        hist = repair.history(m.conn, x["id"])
        last = hist[-1] if hist else None
        r = report_row(x)
        r.update(closed_at=x.get("closed_at"), reopened_at=x.get("reopened_at"),
                 latest_result=last["result"] if last else "not yet re-observed",
                 latest_reason=(x.get("reopen_reason") if x["verification_status"] == "reopened" else None)
                 or (last["reason"] if last else "no trip has passed the spot since the closure"))
        r["evidence"]["checks"] = len(hist)
        r["evidence"]["latest_check_at"] = last["observed_at"] if last else None
        out.append(r)
    rate = met["verification_rate"]
    res.metrics = [metric("Marked repaired", met["marked_repaired"]),
                   metric("Verified", met["verified"]), metric("Reopened (repair not verified)", met["reopened"]),
                   metric("Awaiting verification", met["awaiting_verification"]),
                   metric("Not yet re-observed", met["not_yet_reobserved"]),
                   metric("Repair verification rate", f"{rate}%" if rate is not None else "not enough data",
                          "calculated", "verified ÷ (verified + reopened)")]
    res.method = ("A repair is verified after clean passes from 2 different trips; a confident re-detection reopens "
                  "it. The authority's own status is kept separately.")
    res.limitations.append("Awaiting verification means no trip has passed (or only inconclusive frames exist); it is "
                           "not a failed repair.")
    return finish(res, out, spec.limit)


def repair_history(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("repair_history", "Repair history", "observed", sources=["reports", "repair_verifications",
                                                                          "service_clocks"],
                 columns=[col("report_id", "Report"), col("class_name", "Damage"), col("road", "Location"),
                          col("closed_at", "Marked repaired"), col("verification", "Verification"),
                          col("checks", "Re-observations"), col("results", "Results"),
                          col("authority_reopened", "Reopened by authority")])
    if scope is None:
        res.available = False
        res.limitations.append("Name the reports (for example “repair history of #12”), or ask about a list first and "
                               "then ask for “their repair history”.")
        return res
    rows = filter_reports(load_reports(m.conn, scope), spec, res, exclude_rejected=False)
    out = []
    for x in rows:
        hist = repair.history(m.conn, x["id"])
        r = report_row(x)
        counts: dict[str, int] = {}
        for h in hist:
            counts[h["result"]] = counts.get(h["result"], 0) + 1
        r.update(closed_at=x.get("closed_at"), checks=len(hist),
                 results=", ".join(f"{k.replace('_', ' ')} ×{n}" for k, n in counts.items()) or "none",
                 authority_reopened=x.get("authority_reopened") or 0)
        r["evidence"]["history"] = [{k: h[k] for k in ("observed_at", "result", "reason", "distance_m",
                                                       "observed_class", "confidence")} for h in hist]
        out.append(r)
    order = {i: n for n, i in enumerate(scope)}
    out.sort(key=lambda r: order.get(r["report_id"], 0))
    if not any(r["checks"] or r["closed_at"] for r in out):
        res.limitations.append("None of these reports has been closed by an authority, so they have no repair history.")
    return finish(res, out, spec.limit)


def repair_performance(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("repair_performance", "Repair performance", "calculated", sources=["reports", "service_clocks",
                                                                                    "repair_verifications"])
    res.limitations.append("RoadSense does not record repair types, materials or methods, so durability by repair "
                           "type cannot be assessed and no method is judged better or worse.")
    rows = [x for x in load_reports(m.conn, scope) if x.get("verification_status") or x.get("closed_at")
            or x.get("authority_reopened")]
    rows = filter_reports(rows, spec, res, exclude_rejected=False, date_field="closed_at")
    if not rows:
        res.available = False
        res.limitations.append("No report has been closed by an authority yet.")
        return res
    if spec.group_by == "neighbourhood":
        res.columns = [col("neighbourhood", "Neighbourhood"), col("closed", "Closed"), col("verified", "Verified"),
                       col("reopened", "Reopened"), col("awaiting", "Awaiting verification"),
                       col("rate", "Verification rate", "calculated")]
        groups: dict[str, list[dict]] = {}
        for x in rows:
            groups.setdefault(x.get("neighbourhood") or "Unassigned", []).append(x)
        out = []
        for name, items in groups.items():
            v = sum(i["verification_status"] == "verified" for i in items)
            ro = sum(i["verification_status"] == "reopened" for i in items)
            out.append({"neighbourhood": name, "closed": len(items), "verified": v, "reopened": ro,
                        "awaiting": sum(i["verification_status"] == "awaiting" for i in items),
                        "rate": f"{round(100 * v / (v + ro), 1)}%" if v + ro else "not enough data",
                        "report_list": [i["id"] for i in items]})
        out.sort(key=lambda g: -g["closed"])
        res.total, res.rows = len(out), out[:spec.limit]
        res.report_ids = [i for g in res.rows for i in g["report_list"]]
        res.points = [p for x in rows if x["id"] in set(res.report_ids)
                      if (p := point(x, f"{x['class_name']} #{x['id']}"))]
        res.limitations.append("Few closed reports per neighbourhood make these rates unstable; compare counts, not "
                               "only percentages.")
        return res
    res.columns = [col("report_id", "Report"), col("class_name", "Damage"), col("road", "Location"),
                   col("closed_at", "Closed"), col("reopened_at", "Defect seen again"),
                   col("days_to_recurrence", "Days to recurrence", "calculated"),
                   col("authority_reopened", "Reopened by authority"), col("verification", "Verification")]
    out, gaps = [], []
    for x in rows:
        c, ro = _ts(x.get("closed_at")), _ts(x.get("reopened_at"))
        days = round((ro - c).total_seconds() / 86400, 1) if c and ro and ro >= c else None
        if days is not None:
            gaps.append(days)
        if x.get("verification_status") == "reopened" or x.get("authority_reopened"):
            r = report_row(x)
            r.update(closed_at=x.get("closed_at"), reopened_at=x.get("reopened_at"), days_to_recurrence=days,
                     authority_reopened=x.get("authority_reopened") or 0)
            out.append(r)
    out.sort(key=lambda r: (-(r["authority_reopened"] or 0), r["days_to_recurrence"] or 1e9))
    avg = round(sum(gaps) / len(gaps), 1) if gaps else None
    res.metrics = [metric("Closed by the authority", len(rows)),
                   metric("Repairs that failed (defect seen again or reopened)", len(out)),
                   metric("Average days from closure to recurrence", avg if avg is not None else "no recurrences",
                          "calculated", f"from {len(gaps)} recurrence(s)" if gaps else None)]
    if 0 < len(gaps) < 5:
        res.limitations.append(f"The average is based on only {len(gaps)} recurrence(s): treat it as anecdotal.")
    return finish(res, out, spec.limit)


def service_standard_rows(m: Memo, spec: QuerySpec, scope: list[int] | None, res: Result) -> list[dict]:
    items = m.standards()["items"]
    if scope is not None:
        allowed = set(scope)
        items = [i for i in items if i["report_id"] in allowed]
    if spec.service_status:
        items = [i for i in items if i["status"] in spec.service_status or i.get("provisional") in spec.service_status]
    if spec.classes:
        items = [i for i in items if i["class_code"] in spec.classes]
    if spec.bands:
        items = [i for i in items if i.get("priority") in spec.bands]
    if spec.date_from or spec.date_to:
        items = [i for i in items if _in_dates(_local_day(i["submitted_at"]), spec)]
    reports = {x["id"]: x for x in load_reports(m.conn, [i["report_id"] for i in items])}
    if spec.location:
        before = len(items)
        items = [i for i in items if i["report_id"] in reports and _matches_location(reports[i["report_id"]],
                                                                                       spec.location)]
        if before and not items:
            res.warnings.append(f"No filed report matched the location “{spec.location}”.")
    out = []
    for i in items:
        x = reports.get(i["report_id"])
        if not x:
            continue
        r = report_row(x)
        r.update(authority=i["authority"], service_status=i["status"], provisional=i.get("provisional"),
                 reason=i["reason"], submitted_at=i["submitted_at"], next_due=i["next_due"],
                 next_due_kind=i["next_due_kind"], days_left=i["days_left"], time_unit=i["time_unit"],
                 acknowledged_at=i["acknowledged_at"], completed_at=i["completed_at"],
                 rule_verification=(i.get("rule") or {}).get("verification"),
                 response_due=i["response_due"], completion_due=i["completion_due"])
        out.append(r)
    return out


def service_standards_intent(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("service_standards", "Service standards", "calculated", sources=["service_clocks",
                                                                                  "service_standards", "reports"],
                 columns=[col("report_id", "Report"), col("class_name", "Damage"), col("road", "Location"),
                          col("authority", "Authority"), col("service_status", "Service status", "calculated"),
                          col("next_due", "Next deadline", "calculated"), col("days_left", "Days left", "calculated"),
                          col("submitted_at", "Submitted")])
    d = m.standards()
    if not d["items"]:
        res.available = False
        res.limitations.append("No report has been filed with an authority yet, so no service-standard clock is "
                               "running.")
        return res
    out = service_standard_rows(m, spec, scope, res)
    count = {s: sum(r["service_status"] == s for r in out) for s in service_standards.STATUSES}
    unknown = count.get(service_standards.UNKNOWN, 0)
    res.metrics = [metric("Filed reports matched", len(out)),
                   metric("Overdue", count[service_standards.OVERDUE], "calculated"),
                   metric("Approaching deadline", count[service_standards.APPROACHING], "calculated"),
                   metric("Awaiting authority response", count[service_standards.AWAITING], "calculated"),
                   metric("Completed within target", count[service_standards.MET], "calculated"),
                   metric("Completed after target", count[service_standards.MISSED], "calculated"),
                   metric("Unable to determine", unknown, "calculated")]
    res.method = d["method"]
    res.limitations.append(d["disclaimer"])
    if unknown:
        res.limitations.append(f"{unknown} report(s) have no verified service standard (or none applies), so they are "
                               "not counted as on time or overdue. Deadlines come only from configured rules.")
    order = {s: n for n, s in enumerate(service_standards.STATUSES)}
    out.sort(key=lambda r: (order.get(r["service_status"], 99), r["next_due"] or "9999"))
    return finish(res, out, spec.limit)


def _risk_rows(m: Memo, spec: QuerySpec, scope: list[int] | None, res: Result) -> list[dict]:
    listing = m.risk()["reports"]
    if scope is not None:
        allowed = set(scope)
        listing = [x for x in listing if x["report_id"] in allowed]
    if spec.risk_category:
        listing = [x for x in listing if x["category"] in spec.risk_category]
    if spec.infrastructure:
        listing = [x for x in listing if set(spec.infrastructure) & set(x["infrastructure"])]
    reports = {x["id"]: x for x in load_reports(m.conn, [x["report_id"] for x in listing])}
    rows = filter_reports([reports[x["report_id"]] for x in listing if x["report_id"] in reports], spec, res)
    keep = {x["id"] for x in rows}
    out = []
    for k in listing:
        if k["report_id"] not in keep:
            continue
        x = reports[k["report_id"]]
        r = report_row(x)
        r.update(physical=k["physical_score"], exposure=k["exposure_score"], overall=k["overall_score"],
                 risk=k["category"], data_quality=k["data_quality"],
                 near=", ".join(exposure.FEATURE_LABELS.get(i, i) for i in k["infrastructure"]) or "–",
                 factors=", ".join(f["label"] for f in k["main_factors"]), neighbourhood=k["neighbourhood"])
        r["evidence"]["risk_factors"] = k["main_factors"]
        r["evidence"]["osm_status"] = k["osm_status"]
        out.append(r)
    return out


def risk(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("risk", "Exposure-weighted risk", "calculated", sources=["report_risk", "exposure_features",
                                                                          "reports"],
                 columns=[col("report_id", "Report"), col("class_name", "Damage"), col("road", "Location"),
                          col("physical", "Physical severity", "calculated"),
                          col("exposure", "Exposure (proxy)", "calculated"),
                          col("overall", "Overall risk", "calculated"), col("risk", "Risk level", "calculated"),
                          col("near", "Nearby infrastructure")])
    summary = m.risk()["summary"]
    if not summary["scored"]:
        res.available = False
        res.limitations.append("No exposure-weighted risk score has been calculated yet (the score_exposure job runs "
                               "a few minutes after reports are created).")
        return res
    out = _risk_rows(m, spec, scope, res)
    if spec.trend == "worsening":
        keep = []
        for r in out:
            t = m.trend(r["report_id"])
            if t["trend"] == "worsening":
                r["trend"] = "worsening"
                keep.append(r)
        res.columns.append(col("trend", "Trend", "calculated"))
        out = keep
    key = "exposure" if spec.sort == "exposure" else "overall"
    out.sort(key=lambda r: -(r[key] if r[key] is not None else -1))
    res.metrics = [metric("Scored reports matched", len(out), "calculated"),
                   metric("Critical", sum(r["risk"] == "critical" for r in out), "calculated"),
                   metric("High", sum(r["risk"] == "high" for r in out), "calculated")]
    if summary["unscored"]:
        res.warnings.append(f"{summary['unscored']} report(s) have no risk score yet and are not included.")
    if summary["osm_unavailable"]:
        res.limitations.append(f"{summary['osm_unavailable']} scored report(s) have no OpenStreetMap data: their "
                               "exposure is unknown and the score reflects physical severity only.")
    res.limitations.append(m.risk()["disclaimer"])
    res.method = "Overall = weighted physical severity and exposure from the existing RoadSense risk service."
    return finish(res, out, spec.limit)


def inspection_priorities(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("inspection_priorities", "Recommended inspection order", "mixed",
                 sources=["report_risk", "service_clocks", "events", "reports"],
                 columns=[col("rank", "Rank"), col("report_id", "Report"), col("class_name", "Damage"),
                          col("road", "Location"), col("overall", "Overall risk", "calculated"),
                          col("risk", "Risk level", "calculated"), col("service_status", "Service status", "calculated"),
                          col("trend", "Trend", "calculated"), col("why", "Why it is ranked here")])
    rows = [x for x in load_reports(m.conn, scope) if (x["status"] in OPEN_STATUSES and
                                                      x.get("verification_status") != "verified")
            or x.get("verification_status") == "reopened"]
    rows = filter_reports(rows, spec.model_copy(update={"statuses": []}), res)
    risk_by, std_by = m.risk_by_id(), m.standards_by_id()
    if spec.infrastructure:
        rows = [x for x in rows if set(spec.infrastructure) & set((risk_by.get(x["id"]) or {}).get("infrastructure",
                                                                                                    []))]
    rows = sort_reports(rows, "priority")
    if len(rows) > MAX_TREND_CANDIDATES:
        res.warnings.append(f"Only the {MAX_TREND_CANDIDATES} highest-priority of {len(rows)} open reports were "
                            "considered.")
        rows = rows[:MAX_TREND_CANDIDATES]
    out = []
    for x in rows:
        k, s, t = risk_by.get(x["id"]), std_by.get(x["id"]), m.trend(x["id"])
        r = report_row(x)
        why = []
        if k:
            factors = ", ".join(f["label"].lower() for f in k["main_factors"]) or "no single dominant factor"
            why.append(f"overall risk {k['overall_score']:.2f} ({k['category']}) from {factors}")
        else:
            why.append("no exposure-weighted risk score yet")
        if x.get("severity_band"):
            why.append(f"severity band {x['severity_band']}")
        if (x["trips"] or 0) >= 2 or (x["confirmations"] or 0) >= 2:
            why.append(f"seen on {x['trips']} trip(s) by {x['confirmations']} driver(s)")
        if s and s["status"] in (service_standards.OVERDUE, service_standards.APPROACHING):
            why.append(f"service standard {s['status'].replace('_', ' ')}: {s['reason']}")
        if t["trend"] == "worsening":
            why.append(f"getting worse ({t['growth_per_week']} per week)")
        if x.get("verification_status") == "reopened":
            why.append("reopened: the defect was seen again after the authority closed it")
        r.update(overall=k["overall_score"] if k else None, risk=k["category"] if k else "not scored",
                 service_status=s["status"] if s else "not filed", trend=t["trend"], why="; ".join(why),
                 factors=why)
        r["evidence"]["risk_factors"] = k["main_factors"] if k else []
        out.append(r)
    overdue = (service_standards.OVERDUE, service_standards.APPROACHING)
    out.sort(key=lambda r: (r["overall"] is None, -(r["overall"] or 0), r["service_status"] not in overdue,
                            r["trend"] != "worsening", -BAND_RANK.get(r["band"], 0), -(r["drivers"] or 0),
                            -(r["priority"] or 0)))
    for n, r in enumerate(out, 1):
        r["rank"] = n
    unscored = sum(r["overall"] is None for r in out)
    res.metrics = [metric("Open candidates", len(out)), metric("With a risk score", len(out) - unscored, "calculated"),
                   metric("Overdue or approaching a deadline", sum(r["service_status"] in overdue for r in out),
                          "calculated"),
                   metric("Getting worse", sum(r["trend"] == "worsening" for r in out), "calculated")]
    res.method = ("Ranked by the existing RoadSense exposure-weighted overall risk score; ties (and reports without a "
                  "score) are ordered by an overdue or approaching service standard, a worsening trend, the severity "
                  "band, independent drivers and the report's RoadSense priority. The assistant does not compute a "
                  "score of its own.")
    if unscored:
        res.warnings.append(f"{unscored} candidate(s) have no risk score yet, so the recommendation is incomplete for "
                            "them; they are ranked after the scored reports by deadline, trend, severity band, drivers "
                            "and priority.")
    res.limitations.append("A recommendation to support the inspection plan, not an official work order.")
    return finish(res, out, spec.limit)


def _segment_report_ids(conn: sqlite3.Connection, segment_ids: list[str]) -> dict[str, list[int]]:
    if not segment_ids:
        return {}
    out: dict[str, list[int]] = {}
    for s, r in conn.execute(
            f"SELECT se.segment_id, e.report_id FROM segment_events se JOIN events e ON e.id = se.event_id "
            f"WHERE se.segment_id IN ({','.join('?' * len(segment_ids))}) AND e.report_id IS NOT NULL",
            segment_ids):
        out.setdefault(s, [])
        if r not in out[s]:
            out[s].append(r)
    return out


def freeze_thaw(m: Memo, spec: QuerySpec, scope: list[int] | None, seg_scope: list[str] | None = None) -> Result:
    res = Result("freeze_thaw", "Freeze-thaw pothole risk (forecast)", "predicted",
                 sources=["risk_predictions", "freeze_thaw_indicators", "weather_daily (ECCC)"],
                 columns=[col("road", "Road segment"), col("risk", "Forecast risk", "predicted"),
                          col("score", "Score", "predicted"),
                          col("recent_cycles", "Freeze-thaw cycles, last 30 days"),
                          col("forecast_cycles", "Forecast freeze-thaw days", "predicted"),
                          col("damage", "Existing cracks"), col("data_quality", "Data quality", "calculated")])
    f = m.forecast()
    if not f["run"]:
        res.available = False
        res.limitations.append("No freeze-thaw forecast has been run yet, so there is no prediction to report.")
        return res
    segs = f["segments"]
    mapping = _segment_report_ids(m.conn, [s["segment_id"] for s in segs])
    if seg_scope is not None:
        segs = [s for s in segs if s["segment_id"] in set(seg_scope)]
    elif scope is not None:
        segs = [s for s in segs if set(mapping.get(s["segment_id"], [])) & set(scope)]
    if spec.forecast_category:
        segs = [s for s in segs if s["category"] in spec.forecast_category]
    if spec.classes:
        segs = [s for s in segs if set(spec.classes) & set((s["existing_damage"] or {}).get("classes", {}))]
    if spec.location:
        before = len(segs)
        segs = [s for s in segs if _matches_location({"road": s["road_name"], "municipality": s["municipality"],
                                                      "location": s["location"]}, spec.location)]
        if before and not segs:
            res.warnings.append(f"No forecast segment matched the location “{spec.location}”.")
    out = []
    for s in segs:
        ft = s["freeze_thaw"]
        out.append({"segment_id": s["segment_id"], "road": s["road_name"], "risk": s["category"],
                    "score": round(s["score"], 2), "recent_cycles": ft["recent_confirmed"]
                    if ft["recent_available"] else None,
                    "forecast_cycles": ft["forecast_potential"] if ft["forecast_available"] else None,
                    "damage": ", ".join(f"{CLASS_NAMES.get(c, c)} ×{n}" for c, n in
                                        (s["existing_damage"]["classes"] or {}).items()) or "–",
                    "data_quality": s["data_quality"], "insufficient_data": bool(s["insufficient_data"]),
                    "lat": s["lat"], "lon": s["lon"], "report_list": mapping.get(s["segment_id"], []),
                    "explanation": s["explanation"],
                    "evidence": {"segment_id": s["segment_id"], "period": f"{s['period_start']} to {s['period_end']}",
                                 "observations": s["existing_damage"]["observations"],
                                 "trips": s["existing_damage"]["trips"], "factors": s["main_factors"],
                                 "lat": s["lat"], "lon": s["lon"]}})
    sort = spec.sort if spec.sort in ("forecast_cycles", "recent_cycles") else None
    if sort:
        out.sort(key=lambda r: (r[sort] is None, -(r[sort] or 0), -r["score"]))
    else:
        out.sort(key=lambda r: -r["score"])
    res.total = len(out)
    res.rows = out[:spec.limit]
    res.segment_ids = [r["segment_id"] for r in res.rows]
    res.report_ids = [i for r in res.rows for i in r["report_list"]]
    res.points = [p for r in res.rows if (p := point(r, f"{r['road']} · {r['risk']} forecast risk"))]
    if res.total > len(res.rows):
        res.warnings.append(f"Showing the first {len(res.rows)} of {res.total} segments.")
    no_fc = sum(r["forecast_cycles"] is None for r in out)
    no_recent = sum(r["recent_cycles"] is None for r in out)
    if no_fc:
        res.warnings.append(f"The weather forecast was unavailable for {no_fc} segment(s): their forecast freeze-thaw "
                            "days are unknown, not zero.")
    if no_recent:
        res.warnings.append(f"Recent station weather was unavailable for {no_recent} segment(s): their past cycles "
                            "are unknown, not zero.")
    insufficient = sum(r["insufficient_data"] for r in out)
    if insufficient:
        res.limitations.append(f"{insufficient} segment(s) have insufficient data and are never rated High.")
    res.metrics = [metric("Segments forecast", len(out), "predicted"),
                   metric("High", sum(r["risk"] == "high" for r in out), "predicted"),
                   metric("Medium", sum(r["risk"] == "medium" for r in out), "predicted"),
                   metric("Forecast period", f"{f['run']['period_start']} to {f['run']['period_end']}", "predicted")]
    res.limitations.append(f["disclaimer"])
    res.method = "The RoadSense freeze-thaw forecast (core.forecast): a configurable heuristic over ECCC weather."
    return res


def report_detail(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("report_detail", "Report details", "mixed", sources=["reports", "events", "report_risk",
                                                                      "service_clocks", "repair_verifications"],
                 columns=REPORT_COLUMNS + [col("risk", "Risk level", "calculated"),
                                           col("service_status", "Service status", "calculated"),
                                           col("trend", "Trend", "calculated")])
    ids = scope or []
    if not ids:
        res.available = False
        res.limitations.append("Name a report number, for example “show report #12”.")
        return res
    found = {x["id"]: x for x in load_reports(m.conn, ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        res.warnings.append("Not in RoadSense: " + ", ".join(f"#{i}" for i in missing) + ".")
    risk_by, std_by = m.risk_by_id(), m.standards_by_id()
    out = []
    for i in ids:
        x = found.get(i)
        if not x:
            continue
        r = report_row(x)
        k, s, t = risk_by.get(i), std_by.get(i), m.trend(i)
        r.update(risk=k["category"] if k else "not scored", overall=k["overall_score"] if k else None,
                 service_status=s["status"] if s else "not filed", next_due=s["next_due"] if s else None,
                 trend=t["trend"], authority=x.get("jurisdiction"))
        r["evidence"]["risk_factors"] = k["main_factors"] if k else []
        out.append(r)
    return finish(res, out, spec.limit)


def overview(m: Memo, spec: QuerySpec, scope: list[int] | None) -> Result:
    res = Result("overview", "Overview", "mixed", sources=["reports", "report_risk", "service_clocks",
                                                           "repair_verifications"],
                 columns=[col("lifecycle", "Status"), col("reports", "Reports")])
    rows = filter_reports(load_reports(m.conn, scope), spec, res, exclude_rejected=False)
    by: dict[str, int] = {}
    for x in rows:
        by[repair.lifecycle(x)] = by.get(repair.lifecycle(x), 0) + 1
    res.rows = [{"lifecycle": k, "reports": v} for k, v in sorted(by.items(), key=lambda kv: -kv[1])]
    res.total = len(rows)
    rv = repair.metrics(m.conn)
    std = m.standards()["metrics"]
    rk = m.risk()["summary"]
    res.metrics = [metric("Reports", len(rows)), metric("Potholes", sum(x["class_code"] == "D40" for x in rows)),
                   metric("Awaiting review", sum(x["status"] == "pending_approval" for x in rows)),
                   metric("Overdue (service standard)", std.get("overdue", 0), "calculated"),
                   metric("Critical or high risk", rk["critical"] + rk["high"], "calculated"),
                   metric("Repairs verified / reopened", f"{rv['verified']} / {rv['reopened']}")]
    return res


INTENT_FUNCS = {
    "defects": defects, "defects_by_road": defects_by_road, "repeated_damage": repeated_damage,
    "deterioration": deterioration, "road_condition": road_condition, "repair_verification": repair_verification,
    "repair_history": repair_history, "repair_performance": repair_performance,
    "service_standards": service_standards_intent, "risk": risk, "inspection_priorities": inspection_priorities,
    "report_detail": report_detail, "overview": overview,
}


def scope_for(conn: sqlite3.Connection, spec: QuerySpec, previous: dict | None) -> tuple[list[int] | None,
                                                                                         list[str] | None, list[str]]:
    """The report ids (and forecast segment ids) a question is restricted to, from explicit ids or the previous
    answer. The previous answer's ids are re-read from the database: its rows are never reused."""
    notes: list[str] = []
    if spec.report_ids:
        return list(spec.report_ids), None, notes
    if not spec.use_previous:
        return None, None, notes
    if not previous:
        notes.append("There is no earlier answer in this conversation to narrow down, so all records were searched.")
        return None, None, notes
    ids = list(previous.get("result_ids") or [])
    segs = list(previous.get("segment_ids") or [])
    if spec.first_n:
        if segs:
            segs = segs[:spec.first_n]
            ids = [i for s, rs in _segment_report_ids(conn, segs).items() for i in rs]
        else:
            ids = ids[:spec.first_n]
    if not ids and not segs:
        notes.append("The previous answer had no records, so there is nothing to narrow down.")
    if segs and spec.intent != "freeze_thaw" and not ids:
        notes.append("The previous answer listed road segments with no linked reports.")
    return ids, (segs or None), notes


def retrieve(conn: sqlite3.Connection, spec: QuerySpec, previous: dict | None = None) -> Result:
    ids, segs, notes = scope_for(conn, spec, previous)
    m = Memo(conn)
    if spec.intent == "freeze_thaw":
        res = freeze_thaw(m, spec, ids, segs if spec.use_previous and segs else None)
    else:
        res = INTENT_FUNCS[spec.intent](m, spec, ids)
    res.warnings[:0] = notes
    return res


# ---------------- capabilities: what the data can answer right now ----------------
def capabilities(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    reports = one("SELECT COUNT(*) FROM reports WHERE NOT (status = 'closed' AND closed_reason IS 'rejected')")
    closed = one("SELECT COUNT(*) FROM reports WHERE verification_status IS NOT NULL")
    clocks = one("SELECT COUNT(*) FROM service_clocks")
    scored = one("SELECT COUNT(*) FROM report_risk")
    forecasts = one("SELECT COUNT(*) FROM forecast_runs WHERE kind = 'live'")
    multi = one("SELECT COUNT(*) FROM reports WHERE confirmations > 1")
    caps = {"defects": reports > 0, "deterioration": multi > 0 or reports > 0, "repair_verification": closed > 0,
            "service_standards": clocks > 0, "risk": scored > 0, "inspection_priorities": reports > 0,
            "freeze_thaw": forecasts > 0, "repair_performance": closed > 0}
    suggestions = [(caps["risk"] or caps["inspection_priorities"], "inspection_priorities",
                    "Which five road segments should our inspection team investigate first?"),
                   (caps["risk"], "risk", "Find the highest-risk road segments."),
                   (caps["service_standards"], "service_standards", "Show overdue road repair reports."),
                   (caps["repair_verification"], "repair_verification", "Which completed repairs have not been "
                                                                         "independently verified?"),
                   (caps["deterioration"], "deterioration", "Which defects are getting worse?"),
                   (caps["freeze_thaw"], "freeze_thaw", "Which roads may be affected by upcoming freeze-thaw "
                                                        "conditions?"),
                   (caps["repair_performance"], "repair_performance", "Compare repair performance across "
                                                                      "neighbourhoods."),
                   (caps["defects"], "defects_by_road", "Which roads have the highest number of potholes?"),
                   (caps["defects"], "defects", "Show me the most severe road defects detected this week.")]
    return {"available": caps, "counts": {"reports": reports, "closed_by_authority": closed, "filed": clocks,
                                          "risk_scored": scored, "forecast_runs": forecasts},
            "suggestions": [{"intent": i, "text": t} for ok, i, t in suggestions if ok],
            "unavailable": [k for k, v in caps.items() if not v]}


def as_json(v) -> str:
    return json.dumps(v, default=str)
