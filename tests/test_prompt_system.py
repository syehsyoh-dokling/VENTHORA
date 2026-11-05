# ruff: noqa: E501
"""The Generic Prompting System: the FCO's prompt and notes, compiled with the rules in force, sent as the system
prompt of every model call (only the connection test goes bare)."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

ADMIN = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _model_seeing(sent: list):
    from venthora.evidence.llm import ChatModel

    def handler(req: httpx.Request):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"pay": []}'}}]})
    return ChatModel(endpoint="http://model.test/v1", model="any-model", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_the_default_prompt_and_the_rules_in_force(client):
    d = client.get("/v1/prompt-system").json()
    assert d["saved"] is False and d["prompt"] == d["default_prompt"] and "never move money" in d["prompt"]
    assert list(d["sections"]) == ["Strict rules (do and don't)", "What VENTHORA may do on its own (Delegation of Authority to VENTHORA)",
                                   "Approvers and the approval matrix", "Conversion policy", "Payment order", "Set in other menus"]
    strict = d["sections"]["Strict rules (do and don't)"]
    assert "DON'T: pay the same invoice, or the same instalment, twice" in strict
    assert any(x.startswith("DO: Call a person when an unpaid invoice is overdue by more than 30 days") for x in strict)
    assert any("paid from USD and converted inside the transfer" in x for x in d["sections"]["Conversion policy"])
    assert any("Payment → Budget allocation" in x for x in d["sections"]["Set in other menus"])
    assert len(d["when"]) == 6 and d["compiled"].startswith(d["prompt"])


def test_saving_the_prompt_and_the_notes(client):
    assert client.put("/v1/prompt-system?role=auditor&by=Ines", json={"prompt": "x" * 50}).status_code == 403
    assert client.put(f"/v1/prompt-system?{ADMIN}", json={"prompt": "too short"}).status_code == 422
    body = {"prompt": "You help Acme's treasury. Pay suppliers on time, never early, and keep the USD reserve.",
            "notes": "The budget line logistics also covers courier costs."}
    d = client.put(f"/v1/prompt-system?{ADMIN}", json=body).json()
    assert d["saved"] and d["prompt"] == body["prompt"] and d["updated_by"].startswith("Rosa Lindqvist")
    assert d["compiled"].startswith(body["prompt"]) and "## Notes from the FCO" in d["compiled"] and "courier costs" in d["compiled"]


def test_every_model_call_carries_the_system_prompt_and_the_test_goes_bare(client):
    from conftest import scenario
    from venthora import prompt_system
    from venthora.domain.models import TreasuryState
    from venthora.engines.llm_planner import llm_proposal

    sent: list = []
    model = _model_seeing(sent)
    ids, source = asyncio.run(llm_proposal(TreasuryState.model_validate(scenario("kit01_initial")), model))
    assert ids == [] and source == "LLM any-model"
    system = sent[-1]["messages"][0]
    assert system["role"] == "system" and system["content"].startswith(prompt_system.DEFAULT_PROMPT)
    assert "## Conversion policy" in system["content"] and system["content"].rstrip().endswith("You are a helpful finance assistant.")
    assert [m["role"] for m in sent[-1]["messages"]] == ["system", "user"]  # one system message, then the task's data
    before = prompt_system.SENT["count"]
    asyncio.run(model.complete([{"role": "system", "content": "Reply with the single word OK.", "frame": False}, {"role": "user", "content": "ping"}]))
    assert sent[-1]["messages"][0] == {"role": "system", "content": "Reply with the single word OK."}  # bare, and no 'frame' key sent
    assert prompt_system.SENT["count"] == before


def test_the_console_step_is_the_generic_prompting_system():
    from venthora.live import DEFAULT_APPROVERS, DEFAULT_MATRIX, TITLES

    assert TITLES["goal"] == "Generic Prompting System"
    assert [a["name"] for a in DEFAULT_APPROVERS] == ["Dana Finch", "Omar Reyes"] and DEFAULT_MATRIX[1]["approvals_required"] == 2
