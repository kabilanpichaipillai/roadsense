"""Tiny in-process metrics in Prometheus text format (no extra dependency), plus a request-logging middleware.

    GET /metrics   counters, latency summaries and gauges read from the database at scrape time
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict

log = logging.getLogger("roadsense.http")


def _labels(labels: dict) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in sorted(labels.items())) + "}"


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.counters: dict[tuple, float] = defaultdict(float)
        self.sums: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0])

    def inc(self, name: str, value: float = 1, **labels) -> None:
        with self.lock:
            self.counters[(name, _labels(labels))] += value

    def observe(self, name: str, seconds: float, **labels) -> None:
        with self.lock:
            s = self.sums[(name, _labels(labels))]
            s[0] += seconds
            s[1] += 1

    def render(self, gauges: dict[str, list[tuple[dict, float]]] | None = None) -> str:
        lines = []
        with self.lock:
            for name in sorted({n for n, _ in self.counters}):
                lines.append(f"# TYPE {name} counter")
                lines += [f"{n}{lab} {v:g}" for (n, lab), v in sorted(self.counters.items()) if n == name]
            for name in sorted({n for n, _ in self.sums}):
                lines.append(f"# TYPE {name} summary")
                for (n, lab), (total, count) in sorted(self.sums.items()):
                    if n == name:
                        lines += [f"{n}_sum{lab} {total:.4f}", f"{n}_count{lab} {count}"]
        for name, samples in (gauges or {}).items():
            lines.append(f"# TYPE {name} gauge")
            lines += [f"{name}{_labels(lab)} {v:g}" for lab, v in samples]
        return "\n".join(lines) + "\n"


metrics = Metrics()


def workflow_gauges(conn) -> dict[str, list[tuple[dict, float]]]:
    """The municipal workflow for /metrics: reports by status, overdue repairs, reopens, and the median days from
    approval to the repair being marked done (over reports repaired at least once)."""
    from statistics import median

    from ..core import workflow
    from ..core.business_days import parse_ts

    by = dict(conn.execute("SELECT status, COUNT(*) FROM reports GROUP BY status").fetchall())
    due = [dict(r) for r in conn.execute("SELECT status, repair_eta FROM reports WHERE status IN ('repair_needed', "
                                         "'reopened')")]
    days = [(parse_ts(r[1]) - parse_ts(r[0])).total_seconds() / 86400 for r in conn.execute(
        "SELECT approved_at, repaired_at FROM reports WHERE approved_at IS NOT NULL AND repaired_at IS NOT NULL")]
    return {
        "roadsense_reports": [({"status": s}, by.get(s, 0)) for s in workflow.STATUSES],
        "roadsense_reports_overdue": [({}, sum(workflow.overdue(r) for r in due))],
        "roadsense_report_reopens": [({}, conn.execute("SELECT COALESCE(SUM(reopen_count), 0) FROM reports")
                                      .fetchone()[0])],
        "roadsense_approve_to_repaired_days_median": [({}, round(median(days), 2) if days else 0)],
        "roadsense_reports_repaired_measured": [({}, len(days))],
    }


def install(app, service: str) -> None:
    """Request id, structured access log and request metrics for a FastAPI app."""

    @app.middleware("http")
    async def observe(request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            elapsed = time.monotonic() - started
            metrics.inc("roadsense_http_requests_total", service=service, method=request.method, path=path,
                        status=status)
            metrics.observe("roadsense_http_request_seconds", elapsed, service=service, path=path)
            if not path.startswith(("/static", "/metrics", "/health")):
                log.info(json.dumps({"svc": service, "rid": rid, "method": request.method, "path": path,
                                     "status": status, "ms": round(elapsed * 1000, 1)}))
