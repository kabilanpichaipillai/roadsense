import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from roadsense.agents import orchestrator, stages
from roadsense.agents.contracts import VisionResult
from roadsense.agents.llm import LLMUnavailable
from roadsense.services import agent_api
from roadsense.services.mock_open311 import create_app

AUTH = {"Authorization": "Bearer test-token"}
EVENT = {"id": "trip1-3-abc123", "trip_id": "trip1", "track_id": 3, "class_code": "D40", "class_name": "Pothole",
         "confidence": 0.71, "frames_seen": 4, "box": [10, 20, 110, 90], "box_area_ratio": 0.02, "lat": 44.643,
         "lon": -63.58, "heading": 90.0, "captured_at": "2026-11-02T12:00:00Z"}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100


def test_open311_mock_contract(tmp_path):
    c = TestClient(create_app(tmp_path / "311.db"))
    form = {"service_code": "POTHOLE", "lat": 44.6, "long": -63.5, "description": "Pothole"}
    assert c.post("/requests.json", data={**form, "api_key": "wrong"}).status_code == 403
    rid = c.post("/requests.json", data={**form, "api_key": "test-key"}).json()[0]["service_request_id"]
    assert c.get(f"/requests/{rid}.json").json()[0]["status"] == "open"
    c.post(f"/admin/requests/{rid}/close", data={"notes": "Patched"})
    assert c.get(f"/requests/{rid}.json").json()[0]["status"] == "closed"


def test_event_upload_requires_token_and_valid_payload(db):
    c = TestClient(agent_api.app)  # no lifespan: the agent worker is not started
    files = {"image": ("e.jpg", JPEG, "image/jpeg")}
    assert c.post("/events", data={"event": json.dumps(EVENT)}, files=files).status_code == 401
    bad = {**EVENT, "class_code": "D99"}
    assert c.post("/events", data={"event": json.dumps(bad)}, files=files, headers=AUTH).status_code == 422
    not_jpeg = {"image": ("e.jpg", b"GIF89a", "image/jpeg")}
    assert c.post("/events", data={"event": json.dumps(EVENT)}, files=not_jpeg, headers=AUTH).status_code == 415
    assert c.post("/events", data={"event": json.dumps(EVENT)}, files=files, headers=AUTH).json() == {
        "accepted": EVENT["id"]}
    assert c.get("/trips/trip1/events").status_code == 401  # every read needs the service token
    assert c.get("/trips/trip1/events", headers=AUTH).json()[0]["status"] == "received"


class ScriptedLLM:
    """A model that gives the scripted answers in order, or fails when the script says so."""

    last_model = "stub"

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    async def chat(self, messages, tools=None):
        self.asked.append(json.loads(messages[1]["content"]))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(content=answer, tool_calls=None)

    def stats(self):
        return {"calls": len(self.asked)}


def judge_with(llm):
    judge = orchestrator.Judge(use_llm=False)
    judge.llm = llm
    return judge


def test_workflow_steps_only_reach_their_own_tools(db):
    calls = []

    class Box:
        async def call(self, name, args):
            calls.append(name)
            return {"ok": True}

    assert asyncio.run(stages.use(Box(), "vision", "detector_verify", {})) == {"ok": True}
    for step, tool in (("drafting", "city_submit_request"), ("vision", "reports_create_or_merge"),
                       ("follow_up", "city_submit_request")):
        with pytest.raises(stages.ToolNotAllowed):
            asyncio.run(stages.use(Box(), step, tool, {}))
    assert calls == ["detector_verify"]  # refused tools never reached a server


def test_judge_ignores_answers_it_did_not_ask_for(db):
    llm = ScriptedLLM(json.dumps({"events": [
        {"event_id": "e1", "verdict": "rejected", "rationale": "verifier 0.52, one frame"},
        {"event_id": "e9", "verdict": "confirmed", "rationale": "an id it was never asked about"}]}))
    out = asyncio.run(judge_with(llm).decide("agent:vision", "system", [{"event_id": "e1"}], VisionResult,
                                             "event_id", "test"))
    assert set(out) == {"e1"} and out["e1"]["verdict"] == "rejected"


def test_judge_gets_one_correction_then_the_rule_decides(db):
    llm = ScriptedLLM("not json", "still not json")
    judge = judge_with(llm)
    out = asyncio.run(judge.decide("agent:vision", "system", [{"event_id": "e1"}], VisionResult, "event_id", "t"))
    assert out == {} and len(llm.asked) == 2 and judge.active  # a bad answer does not end the run


def test_unavailable_model_hands_the_rest_to_the_rules(db):
    llm = ScriptedLLM(LLMUnavailable("rate limit"))
    judge = judge_with(llm)
    items = [{"event_id": f"e{i}"} for i in range(orchestrator.BATCH + 3)]  # two batches
    out = asyncio.run(judge.decide("agent:vision", "system", items, VisionResult, "event_id", "t"))
    assert out == {} and judge.interrupted and len(llm.asked) == 1  # the second batch was never sent


def test_location_keeps_the_rule_when_the_model_picks_a_non_candidate(db):
    class Box:
        async def call(self, name, args):
            return {"road": "Bedford Highway", "road_ref": "NS 2", "municipality": "HRM", "candidates": [
                {"authority": "Halifax Regional Municipality", "confidence": "medium", "evidence": "urban section"},
                {"authority": "Nova Scotia Public Works", "confidence": "medium", "evidence": "numbered route"}]}

    events = [{"id": "a", "lat": 44.68, "lon": -63.65}, {"id": "b", "lat": 44.69, "lon": -63.66}]
    llm = ScriptedLLM(json.dumps({"events": [
        {"event_id": "a", "jurisdiction": "Nova Scotia Public Works", "confidence": "medium", "rationale": "rural"},
        {"event_id": "b", "jurisdiction": "City of Toronto", "confidence": "high", "rationale": "made up"}]}))
    chosen = asyncio.run(orchestrator._location(Box(), judge_with(llm), events))
    assert chosen["a"] == ("Nova Scotia Public Works", "rural")
    assert chosen["b"][0] == "Halifax Regional Municipality" and chosen["b"][1].startswith("rule:")
    assert {c["authority"] for c in llm.asked[0][0]["candidates"]} == {"Halifax Regional Municipality",
                                                                       "Nova Scotia Public Works"}


def test_borderline_rule():
    strong = {"frames_seen": 6}
    assert not orchestrator._borderline(strong, {"verifier_confidence": 0.9, "iou": 0.7}, rechecked=False)
    assert orchestrator._borderline(strong, {"verifier_confidence": 0.55, "iou": 0.7}, rechecked=False)
    assert orchestrator._borderline({"frames_seen": 1}, {"verifier_confidence": 0.9, "iou": 0.7}, rechecked=False)
    assert orchestrator._borderline(strong, {"verifier_confidence": 0.9, "iou": 0.1}, rechecked=False)
    assert orchestrator._borderline(strong, {"confidence": 0.8}, rechecked=True)
