"""OpenAI-compatible chat client, configured the same way as the author's Theorem/Verity/Horizon
agents: MODEL_ENDPOINT, MODEL_NAME and MODEL_TOKEN (or MODEL_API_KEY).

Works with a local server (scripts/local_llm_server.py, vLLM, Ollama's OpenAI API) or a hosted
one. The model never receives credentials or the ability to act; it only returns text.
"""
from __future__ import annotations

import json
import re

import httpx

from ..config import env


class LLMUnavailable(RuntimeError):
    pass


class ChatModel:
    def __init__(self, endpoint: str | None = None, model: str | None = None, token: str | None = None,
                 timeout_s: float | None = None, client: httpx.AsyncClient | None = None):
        self.endpoint = (endpoint if endpoint is not None else env("MODEL_ENDPOINT")).rstrip("/")
        self.model = model if model is not None else env("MODEL_NAME")
        self.token = token if token is not None else (env("MODEL_TOKEN") or env("MODEL_API_KEY"))
        self.timeout_s = timeout_s or float(env("MODEL_TIMEOUT_SEC", "60"))
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.model)

    @property
    def url(self) -> str:
        if self.endpoint.endswith("/chat/completions"):
            return self.endpoint
        return f"{self.endpoint}/chat/completions" if self.endpoint.endswith("/v1") else f"{self.endpoint}/v1/chat/completions"

    async def complete(self, messages: list[dict], max_tokens: int = 300) -> str:
        if not self.configured:
            raise LLMUnavailable("MODEL_ENDPOINT / MODEL_NAME not set")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        body = {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        try:
            if self._client is not None:
                res = await self._client.post(self.url, json=body, headers=headers, timeout=self.timeout_s)
            else:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    res = await client.post(self.url, json=body, headers=headers)
            res.raise_for_status()
            return res.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc


def first_json_object(text: str) -> dict:
    """The first {...} block in a model reply (models wrap JSON in prose or code fences)."""
    for match in re.finditer(r"\{", text):
        depth = 0
        for i in range(match.start(), len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    value = json.loads(text[match.start():i + 1])
                except ValueError:
                    break
                if isinstance(value, dict):
                    return value
                break
    raise ValueError("no JSON object in model reply")
