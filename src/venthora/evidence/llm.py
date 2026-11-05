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


# The Generic Prompting System: set by the app at start (prompt_system.frame with the organisation), it puts the
# organisation's system prompt in front of every call. None (a script, a test): the messages go as written.
FRAMER = None


def framed(messages: list[dict]) -> list[dict]:
    if FRAMER is None:
        return [{k: v for k, v in m.items() if k != "frame"} for m in messages]
    return FRAMER(messages)


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
        body = {"model": self.model, "messages": framed(messages), "temperature": 0, "max_tokens": max_tokens}
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


class BedrockModel:
    """A model on Amazon Bedrock through the Converse API, authenticated with a Bedrock API key (sent as a bearer
    token; no AWS SDK needed). The base URL names the region: https://bedrock-runtime.<region>.amazonaws.com."""

    def __init__(self, api_key: str, model: str, endpoint: str, timeout_s: float = 60.0, client: httpx.AsyncClient | None = None):
        self.api_key, self.model, self.endpoint = api_key, model, endpoint.rstrip("/")
        self.timeout_s, self._client = timeout_s, client

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model and self.endpoint)

    async def complete(self, messages: list[dict], max_tokens: int = 300) -> str:
        if not self.configured:
            raise LLMUnavailable("Bedrock API key, region or model not set")
        from urllib.parse import quote

        messages = framed(messages)
        body = {"messages": [{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages if m["role"] != "system"],
                "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0}}
        system = [{"text": m["content"]} for m in messages if m["role"] == "system"]
        if system:
            body["system"] = system
        url = f"{self.endpoint}/model/{quote(self.model, safe='')}/converse"
        headers = {"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"}
        try:
            if self._client is not None:
                res = await self._client.post(url, json=body, headers=headers, timeout=self.timeout_s)
            else:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    res = await client.post(url, json=body, headers=headers)
            if res.status_code in (401, 403):
                raise LLMUnavailable("Bedrock refused the API key (or the model is not enabled for this account and region)")
            res.raise_for_status()
            parts = res.json()["output"]["message"]["content"]
            return "".join(p.get("text", "") for p in parts)
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc


class AnthropicModel:
    """Claude through the official Anthropic SDK (optional dependency: pip install "anthropic>=1,<2").
    Same surface as ChatModel: `configured` and `complete(messages)`, so every reader works with either."""

    def __init__(self, api_key: str, model: str, timeout_s: float = 60.0, workspace_id: str = ""):
        self.api_key, self.model, self.timeout_s, self.workspace_id = api_key, model, timeout_s, workspace_id.strip()

    def _client(self):
        import anthropic

        # A key that is not scoped to a workspace must name one on every request (anthropic-workspace-id).
        headers = {"anthropic-workspace-id": self.workspace_id} if self.workspace_id else None
        return anthropic.AsyncAnthropic(api_key=self.api_key, timeout=self.timeout_s, max_retries=1, default_headers=headers)

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    async def complete(self, messages: list[dict], max_tokens: int = 300) -> str:
        if not self.configured:
            raise LLMUnavailable("Anthropic API key or model not set")
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable('the Anthropic SDK is not installed: pip install "anthropic>=1,<2"') from exc
        messages = framed(messages)
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        client = self._client()
        try:
            # Thinking counts against max_tokens, so the reader's short answers get room; effort low suits a short
            # extraction. fallbacks="default": a declined request is re-run on a fallback model in the same call.
            kwargs = {"system": system} if system else {}
            res = await client.beta.messages.create(
                model=self.model, max_tokens=max(4000, max_tokens), messages=turns, output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable("Anthropic rejected the API key") from exc
        except anthropic.NotFoundError as exc:
            raise LLMUnavailable(f"Anthropic does not know the model {self.model!r}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable("Anthropic rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            if "not scoped to a workspace" in str(exc.message):
                raise LLMUnavailable("this Anthropic API key belongs to no workspace: enter the workspace ID (Claude "
                                     "Console → Settings → Workspaces, it starts with wrkspc_), or create the key inside a "
                                     "workspace") from exc
            raise LLMUnavailable(f"Anthropic HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"cannot reach Anthropic: {type(exc).__name__}") from exc
        finally:
            await client.close()
        if res.stop_reason == "refusal":
            raise LLMUnavailable("the model declined the request")
        return "".join(b.text for b in res.content if b.type == "text")

    async def web_search(self, prompt: str, max_uses: int = 5) -> str:
        """One question answered with Anthropic's server-side web search (the procurement agent's vendor search).
        A long search may pause (stop_reason pause_turn); the turn is resumed up to three times."""
        if not self.configured:
            raise LLMUnavailable("Anthropic API key or model not set")
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable('the Anthropic SDK is not installed: pip install "anthropic>=1,<2"') from exc
        headers = {"anthropic-workspace-id": self.workspace_id} if self.workspace_id else None
        client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=max(self.timeout_s, 240), max_retries=1,
                                          default_headers=headers)
        messages: list[dict] = [{"role": "user", "content": prompt}]
        tool = {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}
        try:
            for _ in range(4):
                res = await client.messages.create(model=self.model, max_tokens=8000, messages=messages, tools=[tool])
                if res.stop_reason != "pause_turn":
                    break
                messages = [messages[0], {"role": "assistant", "content": res.content}]
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"Anthropic HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"cannot reach Anthropic: {type(exc).__name__}") from exc
        finally:
            await client.close()
        if res.stop_reason == "refusal":
            raise LLMUnavailable("the model declined the request")
        return "".join(b.text for b in res.content if b.type == "text")

    async def list_models(self) -> list[str]:
        client = self._client()
        try:
            return [m.id async for m in client.models.list()]
        finally:
            await client.close()


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
