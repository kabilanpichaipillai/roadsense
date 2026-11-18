"""The trip workflow: plain code runs every step, and a model is asked only where judgement adds something.

    vision    verify each detection (re-check the close-up when the rule asks)  -> model judges borderline ones
    severity  rule (estimated width, box size, jolt)
    location  rank the road authorities from the map                          -> model picks when rules can't
    triage    create a report or merge into an open one of the same damage
    drafting  the description the authority will receive                      -> model writes it
    summary   counted from the database

Model calls are batched (a few items per call) and get evidence as data, never tools: each step calls its own MCP
tools through an allow-list, and checks the model's typed answer before acting on it. An answer about an id it was not
asked about, or a choice outside the candidates, is dropped and the rule decides. If the model becomes unavailable,
the rule decides the rest. Filing (after approval) and follow-up have nothing to judge, so they are plain code. The
deterministic fallback still runs after every trip and finishes anything left undone; its count is the fallback rate.
"""
from __future__ import annotations

import json
import logging
import time

from pydantic import BaseModel

from ..common.db import audit, session
from . import fallback
from .contracts import DraftResult, LocationResult, VisionResult, validate
from .llm import LLM, LLMUnavailable
from .mcp_client import Toolbox
from .stages import DRAFT_PROMPT, LOCATION_PROMPT, VISION_PROMPT, use

log = logging.getLogger("roadsense.agents")

BATCH = 8                 # items per model call: few calls on free-tier limits, small enough to answer reliably
BORDER_VERIFIER = 0.65    # a verifier confidence below this is weak support
BORDER_FRAMES = 2         # seen in this many frames or fewer
BORDER_IOU = 0.30         # the two models' boxes barely overlap


def _audit(actor: str, action: str, subject: str | None, detail) -> None:
    with session() as conn:
        audit(conn, actor, action, subject, detail)


class Judge:
    """Asks the model for typed decisions in batches. Returns None for anything it could not decide, so the rule
    applies; after the model becomes unavailable it stops asking for the rest of the run."""

    def __init__(self, use_llm: bool):
        self.llm: LLM | None = None
        self.interrupted = False
        if use_llm:
            try:
                self.llm = LLM()
            except LLMUnavailable as e:
                _audit("agent:orchestrator", "llm_unavailable", None, str(e))

    @property
    def active(self) -> bool:
        return self.llm is not None and not self.interrupted

    async def decide(self, actor: str, system: str, items: list[dict], output: type[BaseModel], key: str,
                     title: str) -> dict:
        """Ask about `items` (each with an id under `key`) in batches; return {id: answer item} for valid answers."""
        out = {}
        for i in range(0, len(items), BATCH):
            if not self.active:
                break
            batch = items[i:i + BATCH]
            asked = {str(x[key]) for x in batch}
            parsed = await self._ask(actor, system, json.dumps(batch, default=str), output)
            if parsed is None:
                continue
            answers = [a for a in next(iter(parsed.model_dump().values())) if str(a[key]) in asked]
            out.update({str(a[key]): a for a in answers})
            _audit(actor, "decided", None, {"title": title, "model": self.llm.last_model, "asked": len(batch),
                                            "items": [{"id": a[key], **{k: v for k, v in a.items() if k != key}}
                                                      for a in answers]})
        return out

    async def _ask(self, actor: str, system: str, evidence: str, output: type[BaseModel]):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": evidence}]
        try:
            for attempt in range(2):
                msg = await self.llm.chat(messages)
                parsed, error = validate(output, msg.content or "")
                if parsed is not None:
                    return parsed
                _audit(actor, "invalid_output", None, error)
                if attempt == 0:
                    messages += [{"role": "assistant", "content": msg.content or ""},
                                 {"role": "user", "content": f"Your answer did not match the required JSON schema: "
                                                             f"{error}. Reply with only the corrected JSON object."}]
        except LLMUnavailable as e:
            self.interrupted = True
            _audit(actor, "llm_unavailable", None, str(e))
            log.warning("LLM unavailable, the rules decide the rest: %s", e)
        return None


def _borderline(event: dict, check: dict, rechecked: bool) -> bool:
    if rechecked:
        return True
    conf, iou = check.get("verifier_confidence"), check.get("iou")
    return ((conf is not None and conf < BORDER_VERIFIER) or event["frames_seen"] <= BORDER_FRAMES
            or (iou is not None and iou < BORDER_IOU))


async def _vision(toolbox: Toolbox, judge: Judge, events: list[dict]) -> list[dict]:
    """Verify every event and record a verdict. Returns the events that are real damage (confirmed or disputed)."""
    suggested, borderline = {}, []
    for e in events:
        check = await use(toolbox, "vision", "detector_verify", {"event_id": e["id"]})
        verdict, reason, rechecked = check.get("suggested_verdict"), check.get("reason", ""), False
        if verdict == "recheck":
            check = await use(toolbox, "vision", "detector_recheck", {"event_id": e["id"]})
            verdict, reason, rechecked = check.get("suggested_verdict", "rejected"), check.get("reason", ""), True
        suggested[e["id"]] = (verdict or "rejected", reason)
        if verdict == "confirmed" and _borderline(e, check, rechecked):
            borderline.append({"event_id": e["id"], "edge_class": e["class_code"], "edge_confidence": e["confidence"],
                               "frames_seen": e["frames_seen"], "box_area_ratio": e["box_area_ratio"],
                               "verifier_confidence": check.get("verifier_confidence"), "iou": check.get("iou"),
                               "close_up_recheck": rechecked, "close_up_confidence": check.get("confidence"),
                               "rule_reason": reason})

    judged = await judge.decide("agent:vision", VISION_PROMPT, borderline, VisionResult, "event_id",
                                "Judged borderline detections") if borderline else {}
    real = []
    for e in events:
        verdict, reason = suggested[e["id"]]
        rationale = f"rule: {reason}"
        a = judged.get(e["id"])
        if a and a["verdict"] in ("confirmed", "rejected"):  # the model may only keep or drop a borderline detection
            verdict, rationale = a["verdict"], a["rationale"]
        result = await use(toolbox, "vision", "detector_record_verdict",
                           {"event_id": e["id"], "verdict": verdict, "rationale": rationale})
        if result.get("error"):  # e.g. a guard refused it: record the rule's verdict instead
            verdict, rationale = suggested[e["id"]][0], f"rule: {suggested[e['id']][1]}"
            await use(toolbox, "vision", "detector_record_verdict",
                      {"event_id": e["id"], "verdict": verdict, "rationale": rationale})
        if verdict in ("confirmed", "disputed"):
            real.append(e)
    return real


async def _location(toolbox: Toolbox, judge: Judge, events: list[dict]) -> dict:
    """The responsible authority for each event: {event_id: (authority, reason)}."""
    chosen, unsure, places = {}, [], {}
    for e in events:
        place = await use(toolbox, "location", "geo_find_jurisdiction", {"lat": e["lat"], "lon": e["lon"]})
        cands = place.get("candidates") or [{"authority": place.get("municipality"), "confidence": "low",
                                             "evidence": "no candidates"}]
        places[e["id"]] = cands
        top = cands[0]
        chosen[e["id"]] = (top["authority"] or "unknown", f"rule: top candidate ({top['evidence']})")
        if len(cands) > 1 and top.get("confidence") != "high":
            unsure.append({"event_id": e["id"], "road": place.get("road"), "road_ref": place.get("road_ref"),
                           "road_class": place.get("road_class"), "municipality": place.get("municipality"),
                           "candidates": [{k: c.get(k) for k in ("authority", "confidence", "evidence")}
                                          for c in cands]})
    judged = await judge.decide("agent:location", LOCATION_PROMPT, unsure, LocationResult, "event_id",
                                "Chose the authority where the rules were unsure") if unsure else {}
    for eid, a in judged.items():
        if a["jurisdiction"] in {c["authority"] for c in places[eid]}:  # only one of the candidates
            chosen[eid] = (a["jurisdiction"], a["rationale"])
    return chosen


async def _drafts(toolbox: Toolbox, judge: Judge, report_ids: list[int]) -> int:
    """Descriptions for the new reports. Without a model the authority gets the standard template."""
    if not report_ids or not judge.active:
        return 0
    facts = []
    for rid in report_ids:
        r = await use(toolbox, "drafting", "reports_get", {"report_id": rid})
        facts.append({"report_id": rid, **{k: r.get(k) for k in ("class_name", "severity", "width_m", "confirmations",
                                                                    "address", "class_disputed", "jurisdiction")}})
    drafts = await judge.decide("agent:filing", DRAFT_PROMPT, facts, DraftResult, "report_id",
                                "Drafted descriptions for the authority")
    saved = 0
    for a in drafts.values():
        result = await use(toolbox, "drafting", "reports_save_draft",
                           {"report_id": a["report_id"], "description": a["description"]})
        saved += bool(result.get("ok"))
    return saved


async def process_trip(toolbox: Toolbox, trip_id: str, use_llm: bool = True) -> dict:
    """Verify, locate, triage and draft every new event of a trip. Returns a summary and run metrics."""
    started = time.perf_counter()
    with session() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT id, class_code, class_name, confidence, frames_seen, box_area_ratio, lat, lon, width_m "
            "FROM events WHERE trip_id = ? AND status = 'received'", (trip_id,))]
    if not events:
        return {"trip_id": trip_id, "summary": "No new events.", "mode": "none"}

    judge = Judge(use_llm)
    _audit("agent:orchestrator", "task", f"trip:{trip_id}", f"{len(events)} new detection(s)")
    real = await _vision(toolbox, judge, events)
    authority = await _location(toolbox, judge, real)
    created = []
    for e in real:
        sev = await use(toolbox, "vision", "detector_severity", {"event_id": e["id"]})
        jurisdiction, reason = authority[e["id"]]
        result = await use(toolbox, "triage", "reports_create_or_merge", {
            "event_id": e["id"], "severity": sev.get("severity", "medium"), "jurisdiction": jurisdiction,
            "rationale": reason})
        if result.get("action") == "created":
            created.append(result["report_id"])
    drafted = await _drafts(toolbox, judge, created)

    repaired = await fallback.complete_trip(toolbox, trip_id)  # safety net: finish anything left undone
    with session() as conn:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM events WHERE trip_id = ? GROUP BY status",
                                   (trip_id,)).fetchall())
    if judge.llm is None:
        mode, stats = "deterministic", {}
    else:
        mode, stats = ("agents (interrupted)" if judge.interrupted else "agents"), judge.llm.stats()
    result = {"trip_id": trip_id, "summary": fallback.trip_summary(trip_id), "mode": mode, "events": counts,
              "drafted": drafted, "fallback_events": repaired, "fallback_rate": round(repaired / len(events), 2),
              "seconds": round(time.perf_counter() - started, 1), "llm": stats}
    _audit("agent:orchestrator", "trip_done", f"trip:{trip_id}", result)
    return result


async def file_report(toolbox: Toolbox, report_id: int, use_llm: bool = True) -> dict:
    """Send an approved report. Nothing to judge: the MCP tool re-checks approval and the payload hash."""
    return await fallback.file_report(toolbox, report_id)


async def follow_up(toolbox: Toolbox, use_llm: bool = True) -> dict:
    """Ask the authorities about submitted reports and tell drivers about repairs. Nothing to judge."""
    return await fallback.follow_up(toolbox)
