# ruff: noqa: E501
"""Platform Setup -> Airwallex MCP: Airwallex's hosted MCP servers (guide: Get running, step 2).

* docs (https://mcp.sandbox.airwallex.com/docs): no account needed. VENTHORA connects, lists its tools and can ask
  it a question (search_public_docs), e.g. the exact fields of a payload before a build.
* developer (https://mcp.sandbox.airwallex.com/developer): needs the sandbox login (an OAuth sign-in in an MCP client
  such as Claude Code). VENTHORA itself makes every Airwallex call over REST behind one typed function per endpoint,
  which the guide allows ("keep the equivalent REST call behind one function"), so it only reports this server's state.

Streamable HTTP: JSON-RPC over POST; the answer may come as JSON or as a server-sent event.
"""
from __future__ import annotations

import json

import httpx

SERVERS = {"docs": "https://mcp.sandbox.airwallex.com/docs", "developer": "https://mcp.sandbox.airwallex.com/developer"}
PROTOCOL = "2025-06-18"
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


class MCPError(RuntimeError):
    pass


def _parse(res: httpx.Response) -> dict:
    text = res.text
    if "text/event-stream" in (res.headers.get("content-type") or ""):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise MCPError("an empty event stream")
    return json.loads(text) if text.strip() else {}


class Session:
    def __init__(self, url: str, client: httpx.AsyncClient | None = None):
        self.url, self.client, self.sid, self.n = url, client, None, 0

    async def _post(self, body: dict) -> httpx.Response:
        h = HEADERS | ({"mcp-session-id": self.sid} if self.sid else {})
        if self.client is not None:
            return await self.client.post(self.url, json=body, headers=h, timeout=30)
        async with httpx.AsyncClient(timeout=30) as c:
            return await c.post(self.url, json=body, headers=h)

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        res = await self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        if res.status_code in (401, 403):
            raise MCPError("sign-in required")
        res.raise_for_status()
        self.sid = res.headers.get("mcp-session-id") or self.sid
        out = _parse(res)
        if out.get("error"):
            raise MCPError(str(out["error"].get("message") or out["error"]))
        return out.get("result") or {}

    async def open(self) -> dict:
        info = await self.call("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "venthora", "version": "1"}})
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return info


async def status(client: httpx.AsyncClient | None = None) -> dict:
    out = {}
    for name, url in SERVERS.items():
        s = Session(url, client)
        try:
            info = await s.open()
            tools = (await s.call("tools/list")).get("tools", [])
            out[name] = {"url": url, "connected": True, "server": (info.get("serverInfo") or {}).get("name"), "protocol": info.get("protocolVersion"),
                         "tools": [{"name": t["name"], "title": t.get("title") or t["name"], "description": (t.get("description") or "")[:240]} for t in tools]}
        except MCPError as exc:
            out[name] = {"url": url, "connected": False, "needs_sign_in": "sign-in" in str(exc), "detail": str(exc)}
        except (httpx.HTTPError, ValueError) as exc:
            out[name] = {"url": url, "connected": False, "needs_sign_in": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}
    return out


async def ask_docs(question: str, client: httpx.AsyncClient | None = None) -> str:
    """The docs server's answer to a question (search_public_docs), as text."""
    s = Session(SERVERS["docs"], client)
    await s.open()
    res = await s.call("tools/call", {"name": "search_public_docs", "arguments": {"question": question}})
    parts = [c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"]
    if res.get("isError"):
        raise MCPError(" ".join(parts) or "the docs server refused the question")
    return "\n".join(parts).strip()
