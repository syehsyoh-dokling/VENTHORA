# ruff: noqa: E501
"""Which language model reads email and evidence: chosen in Settings → AI model, never required.

Three modes, one switch:
* rules        no model; the deterministic readers decide alone (the default)
* self_hosted  any OpenAI-compatible server: Qwen on this laptop, a GPU box elsewhere, vLLM, Ollama, LM Studio
* provider     a hosted model with the user's own API key: OpenAI, Anthropic, Groq, Google Gemini, Mistral, OpenRouter

Whatever is chosen, the model only proposes; every value it returns must be quoted verbatim from the source or it is
dropped (intake/reader.py, evidence/reader.py). Sending email and PDF text to a hosted provider needs an explicit
acknowledgement, because that text then leaves this machine.

Secrets (API key, server token) are stored in this machine's settings database (data/, git-ignored, like .env) and
are never returned by the API: reads show only whether a key is set and its last four characters. Saved settings
win over MODEL_ENDPOINT / MODEL_NAME / MODEL_TOKEN in .env, which still apply when nothing was saved.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from .config import data_path, env
from .evidence.llm import AnthropicModel, ChatModel

PROVIDERS: dict[str, dict] = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "api": "openai"},
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "api": "anthropic",
                  "suggested": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "api": "openai"},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api": "openai"},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1", "api": "openai"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "api": "openai"},
}
Mode = Literal["rules", "self_hosted", "provider"]


def _url(v: str) -> str:
    v = (v or "").strip().rstrip("/")
    if v:
        u = urlparse(v)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("the server address must start with http:// or https://")
    return v


class SelfHosted(BaseModel):
    endpoint: str = Field(default="", max_length=300)
    model: str = Field(default="", max_length=200)
    token: str | None = Field(default=None, max_length=500, description="write-only; None keeps the stored token")
    clear_token: bool = False

    @field_validator("endpoint")
    @classmethod
    def check_endpoint(cls, v: str) -> str:
        return _url(v)


class Provider(BaseModel):
    name: Literal["openai", "anthropic", "groq", "gemini", "mistral", "openrouter"] = "openai"
    model: str = Field(default="", max_length=200)
    api_key: str | None = Field(default=None, max_length=500, description="write-only; None keeps the stored key")
    clear_api_key: bool = False


class AISettings(BaseModel):
    mode: Mode = "rules"
    self_hosted: SelfHosted = Field(default_factory=SelfHosted)
    provider: Provider = Field(default_factory=Provider)
    consent_external: bool = Field(default=False, description="acknowledged: email and PDF text is sent to the provider")
    timeout_s: float = Field(default=60.0, ge=5, le=300)


class AIStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_AI_DB", "data/venthora_ai.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1), record_json TEXT NOT NULL,"
                      " updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def raw(self) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT record_json, updated_at, updated_by FROM settings WHERE id = 1").fetchone()
        return {**json.loads(r["record_json"]), "_updated_at": r["updated_at"], "_updated_by": r["updated_by"]} if r else None

    def merged(self, s: AISettings) -> dict:
        """The settings with write-only secrets resolved: a new secret replaces, None keeps, clear_* removes."""
        old = self.raw() or {}
        rec = s.model_dump()
        for section, key, clear in (("self_hosted", "token", "clear_token"), ("provider", "api_key", "clear_api_key")):
            new = rec[section].pop(key)
            wipe = rec[section].pop(clear)
            kept = ((old.get(section) or {}).get(key) or "") if (section != "provider" or (old.get("provider") or {}).get("name") == rec["provider"]["name"]) else ""
            rec[section][key] = "" if wipe else (new.strip() if new else kept)
        return rec

    def save(self, s: AISettings, by: str) -> dict:
        rec = self.merged(s)
        check(rec)
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO settings VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET record_json = excluded.record_json,"
                      " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                      (json.dumps(rec), datetime.now(UTC).isoformat(timespec="seconds"), by))
        return public(self)


def check(rec: dict) -> None:
    """Refuse a configuration that cannot work or that would send data out without consent."""
    if rec["mode"] == "self_hosted" and not (rec["self_hosted"]["endpoint"] and rec["self_hosted"]["model"]):
        raise ValueError("a self-hosted model needs the server address and the model name")
    if rec["mode"] == "provider":
        p = rec["provider"]
        if not p["api_key"]:
            raise ValueError(f"enter the {PROVIDERS[p['name']]['label']} API key")
        if not p["model"]:
            raise ValueError("choose a model")
        if not rec.get("consent_external"):
            raise ValueError(f"confirm that email and PDF text may be sent to {PROVIDERS[p['name']]['label']}")


def _hint(secret: str) -> str | None:
    return f"…{secret[-4:]}" if secret and len(secret) >= 8 else ("set" if secret else None)


def public(store: AIStore) -> dict:
    """What the dashboard may see: never a secret."""
    raw = store.raw()
    if raw is None:  # nothing saved: the .env settings (if any) are what runs
        ep, model = env("MODEL_ENDPOINT"), env("MODEL_NAME")
        return {"source": "env" if ep and model else "default", "mode": "self_hosted" if ep and model else "rules",
                "self_hosted": {"endpoint": ep, "model": model, "token_set": bool(env("MODEL_TOKEN") or env("MODEL_API_KEY")),
                                "token_hint": None},
                "provider": {"name": "openai", "model": "", "key_set": False, "key_hint": None},
                "consent_external": False, "timeout_s": float(env("MODEL_TIMEOUT_SEC", "60")), "updated_at": None, "updated_by": None,
                "providers": PROVIDERS}
    sh, pv = raw["self_hosted"], raw["provider"]
    return {"source": "settings", "mode": raw["mode"],
            "self_hosted": {"endpoint": sh["endpoint"], "model": sh["model"], "token_set": bool(sh["token"]), "token_hint": _hint(sh["token"])},
            "provider": {"name": pv["name"], "model": pv["model"], "key_set": bool(pv["api_key"]), "key_hint": _hint(pv["api_key"])},
            "consent_external": raw.get("consent_external", False), "timeout_s": raw.get("timeout_s", 60.0),
            "updated_at": raw["_updated_at"], "updated_by": raw["_updated_by"], "providers": PROVIDERS}


def model_for(rec: dict | None):
    """The client for a resolved settings record (None = .env); its `configured` is False in rules mode."""
    if rec is None:
        return ChatModel()
    timeout = float(rec.get("timeout_s") or 60)
    if rec["mode"] == "self_hosted":
        sh = rec["self_hosted"]
        return ChatModel(endpoint=sh["endpoint"], model=sh["model"], token=sh["token"] or "", timeout_s=timeout)
    if rec["mode"] == "provider":
        p = rec["provider"]
        if PROVIDERS[p["name"]]["api"] == "anthropic":
            return AnthropicModel(api_key=p["api_key"], model=p["model"], timeout_s=timeout)
        return ChatModel(endpoint=PROVIDERS[p["name"]]["base_url"], model=p["model"], token=p["api_key"], timeout_s=timeout)
    return ChatModel(endpoint="", model="")


def active_model(store: AIStore | None = None):
    """The model every reader uses now: saved settings first, then .env, else none (rules only)."""
    store = store or AIStore()
    raw = store.raw()
    return model_for({k: v for k, v in raw.items() if not k.startswith("_")} if raw else None)


def describe(model) -> str:
    if not model.configured:
        return "rules only"
    if isinstance(model, AnthropicModel):
        return f"Anthropic · {model.model}"
    return f"{model.model} at {model.endpoint}"


async def list_models(model) -> list[str]:
    """Model ids the server or provider offers (OpenAI-compatible GET /models; Anthropic's models list)."""
    if isinstance(model, AnthropicModel):
        return await model.list_models()
    import httpx

    base = model.endpoint if model.endpoint.endswith("/v1") or "/openai" in model.endpoint or model.endpoint.endswith("/api/v1") else f"{model.endpoint}/v1"
    headers = {"Authorization": f"Bearer {model.token}"} if model.token else {}
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(f"{base}/models", headers=headers)
        res.raise_for_status()
        body = res.json()
    items = body.get("data", body.get("models", [])) if isinstance(body, dict) else body
    return sorted({str(m.get("id") or m.get("name")) for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))})
