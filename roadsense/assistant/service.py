"""The query handler: validate, understand, retrieve, answer, store. Read-only by construction: nothing here
writes to a RoadSense record; the only writes are the person's own conversation and an audit line.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from ..common.config import settings
from ..common.db import audit, now, session
from . import context, response, retrieval, store
from .spec import NOT_ENOUGH, QuerySpec
from .understanding import MAX_QUESTION, clean_question, understand

log = logging.getLogger("roadsense.assistant")
_hits: dict[str, deque] = defaultdict(deque)


class AssistantError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def rate_limit(owner: str) -> None:
    q, t = _hits[owner], time.monotonic()
    while q and t - q[0] > 60:
        q.popleft()
    if len(q) >= settings.assistant_rate_per_min:
        raise AssistantError(429, "too many questions in a minute; wait a moment and ask again")
    q.append(t)


_CONFIGURED = object()


def configured_llm():
    """A factory for the assistant's model client, or None when the LLM is off (rules and code answer everything)."""
    if not settings.llm_enabled:
        return None
    from ..agents.llm import LLM

    return lambda: LLM(models=settings.assistant_models, max_calls=2)


def _table(res: retrieval.Result) -> dict:
    keys = [c["key"] for c in res.columns]
    rows = []
    for r in res.rows:
        out = {k: r.get(k) for k in keys}
        for k in ("report_id", "segment_id", "lat", "lon", "report_list", "evidence", "factors", "status",
                  "class_code", "band", "severity", "explanation", "provisional", "reason"):
            if k in r and k not in out:
                out[k] = r[k]
        rows.append(out)
    return {"columns": res.columns, "rows": rows}


async def ask(question: str, owner: str, role: str, conversation_id: str | None = None,
              llm_factory=_CONFIGURED) -> dict:
    if not settings.assistant_enabled:
        raise AssistantError(503, "the Road Inspection Assistant is turned off (ROADSENSE_ASSISTANT=0)")
    q = clean_question(question)
    if not q:
        raise AssistantError(422, "ask a question")
    if len(q) > MAX_QUESTION:
        raise AssistantError(422, f"questions are limited to {MAX_QUESTION} characters")
    if conversation_id is not None and not store.valid_id(conversation_id):
        raise AssistantError(404, "unknown conversation")
    rate_limit(owner)

    with session() as conn:
        conv = store.get(conn, conversation_id, owner) if conversation_id else None
        if conversation_id and conv is None:  # someone else's, or deleted: indistinguishable on purpose
            raise AssistantError(404, "unknown conversation")
        if conv and store.count_messages(conn, conv["id"]) >= store.MAX_MESSAGES:
            raise AssistantError(409, "this conversation is full; start a new one")
        previous = store.last_answer(conn, conv["id"]) if conv else None

    factory = configured_llm() if llm_factory is _CONFIGURED else llm_factory
    spec, how, warnings = await understand(
        q, previous, factory if settings.assistant_understanding != "rules" else None)
    retrieved_at = now()
    res: retrieval.Result | None = None
    if spec.intent not in ("unknown", "unsupported_action"):
        with session() as conn:
            res = retrieval.retrieve(conn, spec, previous)
    answer = response.compose(spec, res, retrieved_at)

    narrative, withheld = None, None
    grounded = res is not None and res.available and bool(res.rows)
    if grounded and factory is not None and settings.assistant_narrative:
        ctx = context.build(q, spec, res, retrieved_at)
        try:
            narrative, withheld = await response.narrate(ctx, factory())
        except Exception as e:  # the client could not even be built (no key, residency): answer without it
            log.info("assistant: no model client (%s)", type(e).__name__)
            narrative, withheld = None, "the AI service is unavailable"

        if withheld:
            warnings.append(f"The AI summary was not shown because {withheld}. The answer above comes straight from "
                            "the records.")
    if res:
        warnings += res.warnings
    insufficient = (res is None and spec.intent != "unsupported_action") or (res is not None and not res.available)

    with session() as conn:
        conv = conv or store.create(conn, owner, q)
        store.add(conn, conv["id"], "user", q)
        mid = store.add(conn, conv["id"], "assistant", answer, intent=spec.intent,
                        spec=spec.model_dump(mode="json"), result_ids=res.report_ids if res else [],
                        segment_ids=res.segment_ids if res else [], warnings=warnings, understanding=how,
                        retrieved_at=retrieved_at)
        # what was asked is not logged (it may name people or places); what was done is
        audit(conn, f"user:{owner}", "assistant_query", f"assistant:{conv['id']}",
              {"intent": spec.intent, "understanding": how, "results": len(res.rows) if res else 0,
               "matched": res.total if res else 0, "narrative": bool(narrative), "role": role,
               "follow_up": spec.use_previous})
    return {
        "conversation_id": conv["id"], "message_id": mid, "question": q, "intent": spec.intent,
        "interpretation": spec.describe(), "understanding": how, "spec": spec.model_dump(mode="json"),
        "answer": {"text": answer, "kind": res.kind if res else None, "insufficient": insufficient},
        "narrative": {"text": narrative, "label": "AI interpretation of the records above",
                      "model": "configured LLM"} if narrative else None,
        "title": res.title if res else None,
        "metrics": res.metrics if res else [],
        "table": _table(res) if res and res.rows else None,
        "report_refs": res.report_ids if res else [],
        "segment_refs": res.segment_ids if res else [],
        "map": {"points": res.points} if res and res.points else None,
        "evidence": [r["evidence"] for r in res.rows if r.get("evidence")] if res else [],
        "warnings": list(dict.fromkeys(warnings)),
        "limitations": res.limitations if res else ([] if spec.intent == "unsupported_action" else [NOT_ENOUGH]),
        "method": res.method if res else None,
        "sources": res.sources if res else [],
        "matched": res.total if res else 0,
        "retrieved_at": retrieved_at,
        "read_only": True,
    }


def interpret_only(question: str) -> QuerySpec:  # used by tests and the docs examples
    from .understanding import parse_rules

    return parse_rules(question)[0]
