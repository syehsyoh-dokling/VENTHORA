# ruff: noqa: E501
"""T:0 (the AI finance team / books): reached through its MCP server with the organisation's API key as a Bearer token.

Developer Console → T:0 sets it up: the key (T0_API_KEY, in .env, never shown), the MCP URL (T0_MCP_URL). The status
check is read only: it lists the tools T:0 offers and the entities the key can act for. Nothing is written to T:0
from here (T:0 also offers writes, such as journal entries; those stay for later and behind the FCO's choice).
"""
from __future__ import annotations

import contextlib
import json

import httpx

from .config import env

DEFAULT_URL = "https://mcp.scalar.com/mcp/f5272265-5f85-4ec5-b4eb-a44e4f05d957"


def _parse(r: httpx.Response):
    t = r.text
    if "data:" in t:
        t = "\n".join(x[5:].strip() for x in t.splitlines() if x.startswith("data:"))
    try:
        return json.loads(t.splitlines()[-1]) if t.strip() else None
    except ValueError:
        return None


async def _session(c: httpx.AsyncClient, url: str, key: str) -> dict:
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    r = await c.post(url, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "venthora", "version": "1"}}})
    r.raise_for_status()
    if r.headers.get("mcp-session-id"):
        h["mcp-session-id"] = r.headers["mcp-session-id"]
    await c.post(url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return {"headers": h, "server": ((_parse(r) or {}).get("result") or {}).get("serverInfo")}


async def call(c: httpx.AsyncClient, url: str, s: dict, name: str, args: dict | None = None):
    r = await c.post(url, headers=s["headers"], json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
    res = ((_parse(r) or {}).get("result") or {})
    text = next((x.get("text") for x in res.get("content", []) if x.get("type") == "text"), "")
    try:
        outer = json.loads(text)
    except ValueError:
        return None
    body = outer.get("body", outer) if isinstance(outer, dict) else outer
    if isinstance(body, str):
        with contextlib.suppress(ValueError):
            body = json.loads(body)
    return {"status": outer.get("status") if isinstance(outer, dict) else None, "body": body}


async def status() -> dict:
    key, url = env("T0_API_KEY"), env("T0_MCP_URL") or DEFAULT_URL
    out = {"key_set": bool(key), "key_name": env("T0_KEY_NAME") or None, "key_hint": f"…{key[-4:]}" if len(key) >= 8 else None, "mcp_url": url,
           "connected": False, "server": None, "tools": [], "entities": None, "error": None}
    if not key:
        return out | {"error": "T0_API_KEY is not set on this server"}
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            s = await _session(c, url, key)
            r = await c.post(url, headers=s["headers"], json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = [t["name"] for t in ((_parse(r) or {}).get("result") or {}).get("tools", [])]
            ent = await call(c, url, s, "list_the_entities_you_can_access")
    except httpx.HTTPError as exc:
        return out | {"error": f"T:0 did not answer: {exc.__class__.__name__}"}
    body = (ent or {}).get("body")
    items = body if isinstance(body, list) else (body or {}).get("data") or (body or {}).get("items") or [] if isinstance(body, dict) else []
    return out | {"connected": bool(tools), "server": s["server"], "tools": tools, "entity_call_status": (ent or {}).get("status"),
                  "entities": [{"id": e.get("id") or e.get("external_id"), "name": e.get("name") or e.get("legal_name")} for e in items if isinstance(e, dict)]}
