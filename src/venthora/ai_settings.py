# ruff: noqa: E501
"""Which language model reads email and evidence: chosen in Settings → AI model, never required.

Three modes, one switch:
* rules        no model; the deterministic readers decide alone (the default)
* self_hosted  any OpenAI-compatible server: Qwen on this laptop, a GPU box elsewhere, vLLM, Ollama, LM Studio
* provider     a hosted model with the user's own API key (Anthropic, OpenAI, Google Gemini, Mistral, Groq, DeepSeek, xAI,
               Together, Fireworks, Cerebras, NVIDIA NIM, Qwen / DashScope, Moonshot, Cohere, Perplexity, OpenRouter,
               Hugging Face, or any other OpenAI-compatible service). Keys are kept per provider (Settings → AI API keys),
               so switching provider never asks for a key again.

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
from .evidence.llm import AnthropicModel, BedrockModel, ChatModel

PROVIDERS: dict[str, dict] = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "api": "openai"},
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "api": "anthropic"},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "api": "openai"},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api": "openai"},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1", "api": "openai"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "api": "openai"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "api": "openai"},
    "xai": {"label": "xAI (Grok)", "base_url": "https://api.x.ai/v1", "api": "openai"},
    "together": {"label": "Together AI", "base_url": "https://api.together.xyz/v1", "api": "openai"},
    "fireworks": {"label": "Fireworks AI", "base_url": "https://api.fireworks.ai/inference/v1", "api": "openai"},
    "cerebras": {"label": "Cerebras", "base_url": "https://api.cerebras.ai/v1", "api": "openai"},
    "nvidia": {"label": "NVIDIA NIM", "base_url": "https://integrate.api.nvidia.com/v1", "api": "openai"},
    "dashscope": {"label": "Qwen (Alibaba DashScope)", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "api": "openai"},
    "moonshot": {"label": "Moonshot (Kimi)", "base_url": "https://api.moonshot.ai/v1", "api": "openai"},
    "cohere": {"label": "Cohere", "base_url": "https://api.cohere.ai/compatibility/v1", "api": "openai"},
    "perplexity": {"label": "Perplexity", "base_url": "https://api.perplexity.ai", "api": "openai"},
    "huggingface": {"label": "Hugging Face Inference", "base_url": "https://router.huggingface.co/v1", "api": "openai"},
    "bedrock": {"label": "Amazon Bedrock (AWS)", "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com", "api": "bedrock", "regional": True},
    "custom": {"label": "Other OpenAI-compatible service", "base_url": "", "api": "openai", "custom": True},
}
# The usual model ids per provider, for the dropdown; the first is the default. List models asks the provider for its
# current list, and Other takes any id, so a model released after this list is still one choice away.
MODELS: dict[str, list[str]] = {
    "openai": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o4-mini"],
    "anthropic": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-fable-5-1"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3-32b"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    "mistral": ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"],
    "openrouter": ["openrouter/auto"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "xai": ["grok-3", "grok-3-mini"],
    "together": ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"],
    "fireworks": ["accounts/fireworks/models/llama-v3p3-70b-instruct"],
    "cerebras": ["llama-3.3-70b", "llama3.1-8b"],
    "nvidia": ["meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct"],
    "dashscope": ["qwen-max", "qwen-plus", "qwen-turbo"],
    "moonshot": ["kimi-k2-0711-preview", "moonshot-v1-32k", "moonshot-v1-8k"],
    "cohere": ["command-a-03-2025", "command-r-plus", "command-r"],
    "perplexity": ["sonar", "sonar-pro"],
    "huggingface": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct"],
    "bedrock": ["us.anthropic.claude-sonnet-4-5-20250929-v1:0", "amazon.nova-pro-v1:0", "amazon.nova-lite-v1:0", "us.meta.llama3-3-70b-instruct-v1:0"],
    "custom": [],
}
for _name, _models in MODELS.items():
    PROVIDERS[_name] |= {"suggested": _models, "default": _models[0] if _models else ""}
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
    name: str = "openai"
    model: str = Field(default="", max_length=200)
    api_key: str | None = Field(default=None, max_length=500, description="write-only; None keeps the stored key")
    clear_api_key: bool = False
    base_url: str = Field(default="", max_length=300, description="only for an other OpenAI-compatible service")
    workspace_id: str = Field(default="", max_length=80, description="Anthropic only: for a key that is not scoped to a workspace")

    @field_validator("name")
    @classmethod
    def known(cls, v: str) -> str:
        if v not in PROVIDERS:
            raise ValueError(f"unknown provider {v!r}; one of {', '.join(PROVIDERS)}")
        return v

    @field_validator("base_url")
    @classmethod
    def check_base(cls, v: str) -> str:
        return _url(v)


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
        """The settings with write-only secrets resolved: a new secret replaces, None keeps (from the key vault for a
        provider), clear_* removes. The vault keeps one key per provider."""
        old = self.raw() or {}
        rec = s.model_dump()
        keys = dict(old.get("keys") or {})
        op = old.get("provider") or {}
        if op.get("api_key") and op.get("name") and op["name"] not in keys:  # a record from before the vault
            keys[op["name"]] = {"api_key": op["api_key"], "model": op.get("model", ""), "base_url": op.get("base_url", "")}
        sh = rec["self_hosted"]
        new, wipe = sh.pop("token"), sh.pop("clear_token")
        sh["token"] = "" if wipe else (new.strip() if new else ((old.get("self_hosted") or {}).get("token") or ""))
        pv = rec["provider"]
        new, wipe = pv.pop("api_key"), pv.pop("clear_api_key")
        slot = dict(keys.get(pv["name"]) or {})
        if wipe:
            keys.pop(pv["name"], None)
            pv["api_key"] = ""
        else:
            pv["api_key"] = new.strip() if new else slot.get("api_key", "")
            if pv["api_key"]:
                keys[pv["name"]] = {"api_key": pv["api_key"], "model": pv["model"] or slot.get("model", ""),
                                    "base_url": pv["base_url"] or slot.get("base_url", ""),
                                    "workspace_id": pv.get("workspace_id") or slot.get("workspace_id", "")}
        pv["base_url"] = pv["base_url"] or slot.get("base_url", "")
        pv["workspace_id"] = pv.get("workspace_id") or slot.get("workspace_id", "")
        rec["keys"] = keys
        return rec

    def _write(self, rec: dict, by: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO settings VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET record_json = excluded.record_json,"
                      " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                      (json.dumps(rec), datetime.now(UTC).isoformat(timespec="seconds"), by))

    def current(self) -> dict:
        """The saved record without its bookkeeping, or the defaults."""
        raw = self.raw()
        if raw is None:
            return AISettings().model_dump() | {"keys": {}}
        return {k: v for k, v in raw.items() if not k.startswith("_")}

    def save_key(self, name: str, api_key: str | None, model: str, base_url: str, by: str, workspace_id: str = "") -> dict:
        """Keep a provider's key in the vault without switching to it (a new key replaces, None keeps the old one)."""
        rec = self.current()
        keys = dict(rec.get("keys") or {})
        old = keys.get(name) or {}
        key = (api_key or "").strip() or old.get("api_key", "")
        if not key:
            raise ValueError(f"paste the {PROVIDERS[name]['label']} API key")
        if PROVIDERS[name].get("custom") and not (base_url or old.get("base_url")):
            raise ValueError("an other OpenAI-compatible service needs its base URL")
        keys[name] = {"api_key": key, "model": model or old.get("model", ""), "base_url": _url(base_url) or old.get("base_url", ""),
                      "workspace_id": workspace_id.strip() or old.get("workspace_id", "")}
        rec["keys"] = keys
        if rec.get("provider", {}).get("name") == name:  # the active provider follows its new key
            rec["provider"] = rec["provider"] | {"api_key": key, "model": keys[name]["model"] or rec["provider"].get("model", ""),
                                                 "base_url": keys[name]["base_url"], "workspace_id": keys[name]["workspace_id"]}
        self._write(rec, by)
        return public(self)

    def delete_key(self, name: str, by: str) -> dict:
        rec = self.current()
        if rec.get("mode") == "provider" and rec.get("provider", {}).get("name") == name:
            raise ValueError("this provider is in use; switch the AI model to another one first")
        rec["keys"] = {k: v for k, v in (rec.get("keys") or {}).items() if k != name}
        if rec.get("provider", {}).get("name") == name:
            rec["provider"] = rec["provider"] | {"api_key": ""}
        self._write(rec, by)
        return public(self)

    def save(self, s: AISettings, by: str) -> dict:
        rec = self.merged(s)
        check(rec)
        self._write(rec, by)
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
        if PROVIDERS[p["name"]].get("custom") and not p.get("base_url"):
            raise ValueError("an other OpenAI-compatible service needs its base URL")
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
                "provider": {"name": "openai", "model": "", "key_set": False, "key_hint": None, "base_url": ""},
                "consent_external": False, "timeout_s": float(env("MODEL_TIMEOUT_SEC", "60")), "updated_at": None, "updated_by": None,
                "providers": PROVIDERS, "keys": {}}
    sh, pv = raw["self_hosted"], raw["provider"]
    return {"source": "settings", "mode": raw["mode"],
            "self_hosted": {"endpoint": sh["endpoint"], "model": sh["model"], "token_set": bool(sh["token"]), "token_hint": _hint(sh["token"])},
            "provider": {"name": pv["name"], "model": pv["model"], "key_set": bool(pv["api_key"]), "key_hint": _hint(pv["api_key"]),
                         "base_url": pv.get("base_url", ""), "workspace_id": pv.get("workspace_id", "")},
            "consent_external": raw.get("consent_external", False), "timeout_s": raw.get("timeout_s", 60.0),
            "updated_at": raw["_updated_at"], "updated_by": raw["_updated_by"], "providers": PROVIDERS,
            "keys": {n: {"key_set": bool(k.get("api_key")), "key_hint": _hint(k.get("api_key", "")), "model": k.get("model", ""),
                         "base_url": k.get("base_url", ""), "workspace_id": k.get("workspace_id", "")} for n, k in (raw.get("keys") or {}).items()}}


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
            return AnthropicModel(api_key=p["api_key"], model=p["model"], timeout_s=timeout, workspace_id=p.get("workspace_id", ""))
        if PROVIDERS[p["name"]]["api"] == "bedrock":
            return BedrockModel(api_key=p["api_key"], model=p["model"], endpoint=p.get("base_url") or PROVIDERS["bedrock"]["base_url"], timeout_s=timeout)
        return ChatModel(endpoint=p.get("base_url") or PROVIDERS[p["name"]]["base_url"], model=p["model"], token=p["api_key"], timeout_s=timeout)
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
    if isinstance(model, BedrockModel):
        return f"Amazon Bedrock · {model.model} ({model.endpoint.split('.')[1] if '.' in model.endpoint else model.endpoint})"
    return f"{model.model} at {model.endpoint}"


async def list_models(model) -> list[str]:
    """Model ids the server or provider offers (OpenAI-compatible GET /models; Anthropic's models list)."""
    if isinstance(model, AnthropicModel):
        return await model.list_models()
    if isinstance(model, BedrockModel):  # a Bedrock API key reaches the runtime only: offer the usual ids, Other takes any
        return list(PROVIDERS["bedrock"]["suggested"])
    import httpx

    base = model.endpoint if model.endpoint.endswith("/v1") or "/openai" in model.endpoint or model.endpoint.endswith("/api/v1") else f"{model.endpoint}/v1"
    headers = {"Authorization": f"Bearer {model.token}"} if model.token else {}
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(f"{base}/models", headers=headers)
        res.raise_for_status()
        body = res.json()
    items = body.get("data", body.get("models", [])) if isinstance(body, dict) else body
    return sorted({str(m.get("id") or m.get("name")) for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))})
