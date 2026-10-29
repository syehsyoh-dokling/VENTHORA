# ruff: noqa: E501
"""Settings → AI model: rules only, a self-hosted model at any address, or a provider with the user's own key.
Keys are write-only, a hosted provider needs consent, and every reader follows the saved choice."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from venthora import ai_settings
from venthora.evidence.llm import AnthropicModel, ChatModel, LLMUnavailable

KEY = "sk-test-0123456789abcdef"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_default_and_env_fallback(client, monkeypatch):
    s = client.get("/v1/settings/ai").json()
    assert (s["source"], s["mode"], s["active"]) == ("default", "rules", "rules only")
    monkeypatch.setenv("MODEL_ENDPOINT", "http://127.0.0.1:8003")
    monkeypatch.setenv("MODEL_NAME", "Qwen/Qwen2.5-0.5B-Instruct")
    s = client.get("/v1/settings/ai").json()
    assert (s["source"], s["mode"]) == ("env", "self_hosted") and "Qwen" in s["active"]


def test_provider_needs_key_model_and_consent_and_never_shows_the_key(client):
    body = {"mode": "provider", "provider": {"name": "anthropic", "model": "claude-opus-5-5", "api_key": KEY}}
    r = client.put("/v1/settings/ai", json=body)
    assert r.status_code == 422 and "may be sent to Anthropic" in r.json()["detail"]
    assert client.put("/v1/settings/ai", json={"mode": "provider", "consent_external": True,
                                                "provider": {"name": "anthropic", "model": "claude-opus-5-5"}}).status_code == 422
    assert client.put("/v1/settings/ai?role=auditor", json=body | {"consent_external": True}).status_code == 403
    saved = client.put("/v1/settings/ai", json=body | {"consent_external": True})
    assert saved.status_code == 200 and saved.json()["provider"]["key_hint"] == "…cdef"
    for page in (saved.text, client.get("/v1/settings/ai").text, json.dumps(client.get("/api/audit?limit=50").json())):
        assert KEY not in page
    # blank key keeps the saved one; another provider does not inherit it
    kept = client.put("/v1/settings/ai", json={"mode": "provider", "consent_external": True,
                                               "provider": {"name": "anthropic", "model": "claude-sonnet-5-5", "api_key": None}})
    assert kept.status_code == 200 and kept.json()["provider"]["key_set"] and kept.json()["active"] == "Anthropic · claude-sonnet-5-5"
    other = client.put("/v1/settings/ai", json={"mode": "provider", "consent_external": True,
                                                "provider": {"name": "groq", "model": "qwen/qwen3-32b"}})
    assert other.status_code == 422 and "Groq API key" in other.json()["detail"]


def test_active_model_follows_the_saved_choice(tmp_path):
    store = ai_settings.AIStore(str(tmp_path / "ai.db"))
    assert not ai_settings.active_model(store).configured
    store.save(ai_settings.AISettings(mode="self_hosted", self_hosted={"endpoint": "http://gpu-box:8000/v1/", "model": "Qwen/Qwen3-8B",
                                                                       "token": "tok-123456789"}), "test")
    m = ai_settings.active_model(store)
    assert isinstance(m, ChatModel) and m.url == "http://gpu-box:8000/v1/chat/completions" and m.token == "tok-123456789"
    store.save(ai_settings.AISettings(mode="provider", consent_external=True,
                                      provider={"name": "groq", "model": "qwen/qwen3-32b", "api_key": KEY}), "test")
    m = ai_settings.active_model(store)
    assert isinstance(m, ChatModel) and m.url == "https://api.groq.com/openai/v1/chat/completions" and m.model == "qwen/qwen3-32b"
    store.save(ai_settings.AISettings(mode="provider", consent_external=True,
                                      provider={"name": "anthropic", "model": "claude-opus-5-5", "api_key": KEY}), "test")
    assert isinstance(ai_settings.active_model(store), AnthropicModel)
    store.save(ai_settings.AISettings(mode="rules"), "test")
    assert not ai_settings.active_model(store).configured
    with pytest.raises(ValueError, match="http"):
        ai_settings.AISettings(mode="self_hosted", self_hosted={"endpoint": "file:///etc/passwd", "model": "x"})


class _FakeAnthropic:
    """Stands in for anthropic.AsyncAnthropic: records the request, answers like the Messages API."""
    calls = []
    stop = "end_turn"

    def __init__(self, **kw):
        self.kw = kw
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        _FakeAnthropic.calls.append(kw)
        return SimpleNamespace(stop_reason=_FakeAnthropic.stop, content=[SimpleNamespace(type="thinking", thinking=""),
                                                                          SimpleNamespace(type="text", text="OK")])

    async def close(self):
        pass


def test_anthropic_adapter_request_shape(monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    m = AnthropicModel(api_key=KEY, model="claude-opus-5-5")
    out = asyncio.run(m.complete([{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}], max_tokens=20))
    req = _FakeAnthropic.calls[-1]
    assert out == "OK" and req["system"] == "Be brief." and req["messages"] == [{"role": "user", "content": "Hi"}]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    assert req["max_tokens"] >= 4000 and "temperature" not in req
    _FakeAnthropic.stop = "refusal"
    with pytest.raises(LLMUnavailable, match="declined"):
        asyncio.run(m.complete([{"role": "user", "content": "Hi"}]))
    _FakeAnthropic.stop = "end_turn"


def test_connection_test_and_model_list(client, monkeypatch):
    async def ok(self, messages, max_tokens=300):
        return "OK"

    async def models(model):
        return ["Qwen/Qwen2.5-0.5B-Instruct", "Qwen/Qwen3-8B"]
    monkeypatch.setattr(ChatModel, "complete", ok)
    monkeypatch.setattr(ai_settings, "list_models", models)
    draft = {"mode": "self_hosted", "self_hosted": {"endpoint": "http://127.0.0.1:8003/v1", "model": "Qwen/Qwen3-8B"}}
    r = client.post("/v1/settings/ai/test", json=draft).json()
    assert r["ok"] and r["reply"] == "OK" and "Qwen/Qwen3-8B" in r["model"]
    assert client.post("/v1/settings/ai/models", json=draft).json()["models"][1] == "Qwen/Qwen3-8B"
    assert client.post("/v1/settings/ai/test", json={"mode": "rules"}).json() == {"ok": False, "error": "rules only: no model to test"}


def test_intake_reads_with_the_saved_model(client, monkeypatch):
    async def reply(self, messages, max_tokens=300):
        return json.dumps({"invoice_no": {"value": "Q4-ALPHA/77", "quote": "reference is Q4-ALPHA/77"}})
    monkeypatch.setattr(ChatModel, "complete", reply)
    assert client.put("/v1/settings/ai", json={"mode": "self_hosted", "self_hosted": {
        "endpoint": "http://127.0.0.1:8003/v1", "model": "Qwen/Qwen2.5-0.5B-Instruct"}}).status_code == 200
    raw = (b"From: Lumen <lumen.design.studio.billing@gmail.com>\r\nTo: ap@acme.example\r\nSubject: October work\r\n"
           b"Message-ID: <ai-settings-1@test>\r\nDate: Fri, 24 Oct 2026 09:00:00 +0000\r\n\r\n"
           b"Hello, our reference is Q4-ALPHA/77. Amount due: USD 1,250.00, due 30 Nov 2026.\r\n")
    r = client.post("/v1/intake/emails?dry_run=true", content=raw, headers={"content-type": "message/rfc822"}).json()
    assert r["reader"] == "model+rules" and r["invoice_no"] == "Q4-ALPHA/77"
