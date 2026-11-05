# ruff: noqa: E501
"""API Catalog: what an IT developer of another application (an ERP, a payroll or accounting system) needs to call
VENTHORA, shared as a public, read-only page.

Only the endpoints an API key may call are listed: never the key management, the bank connection, the AI vault,
the mailboxes, the agent's authority (mandate, DoA, priority policy), the edition, the demo endpoints or the inbound
webhooks. The page holds no key and no data; a developer still needs a key created in Platform Setup → App
Integration, with the scopes the endpoint names.

  /developers               the page (through the gateway: https://<public address>/developers)
  /developers/catalog.json  the endpoints with area, scope, parameters and body
  /developers/openapi.json  the same as OpenAPI 3, with the public server and the bearer scheme
  /developers/postman.json  a Postman collection (v2.1) with {{baseUrl}} and {{apiKey}}
"""
from __future__ import annotations

import copy

from . import apiaccess, edition


def public_endpoints(openapi: dict) -> list[dict]:
    out = []
    for e in apiaccess.catalog(openapi):
        if not e["scope"] or e["area"] in ("inbound",) or edition.is_demo_path(e["path"]) or "/demo" in e["path"]:
            continue
        if e["path"].startswith(apiaccess.NEVER_BY_KEY) or e["path"].startswith("/v1/developer/keys"):
            continue
        out.append(e)
    return out


def catalog(openapi: dict, base_url: str) -> dict:
    eps = public_endpoints(openapi)
    used = {e["area"] for e in eps}
    areas = [{"area": a, "label": lbl, "covers": cov, "scopes": [f"{a}:read", f"{a}:write"], "count": sum(1 for e in eps if e["area"] == a)}
             for a, (lbl, cov, _) in apiaccess.AREAS.items() if a in used]
    return {"title": "VENTHORA API", "version": openapi.get("info", {}).get("version"), "base_url": base_url, "areas": areas, "endpoints": eps,
            "auth": {"header": "Authorization: Bearer <key>", "alternative": "X-API-Key: <key>",
                     "get_a_key": "ask the VENTHORA administrator for a key (Platform Setup → App Integration) with the scopes you need",
                     "read": "GET and .../eval calls need <area>:read", "write": "every other call needs <area>:write",
                     "idempotency": f"send Idempotency-Key on a write to make retries safe (kept {apiaccess.IDEMPOTENCY_HOURS} h)",
                     "errors": "every refusal is JSON {\"detail\": ...} with the HTTP status; 401 bad key, 403 missing scope"},
            "files": {"openapi": "/developers/openapi.json", "postman": "/developers/postman.json"}}


def openapi(full: dict, base_url: str) -> dict:
    keep = {(e["method"].lower(), e["path"]) for e in public_endpoints(full)}
    doc = copy.deepcopy(full)
    doc["paths"] = {p: {m: op for m, op in ops.items() if (m, p) in keep} for p, ops in full.get("paths", {}).items()}
    doc["paths"] = {p: ops for p, ops in doc["paths"].items() if ops}
    doc["servers"] = [{"url": base_url}]
    doc.setdefault("components", {})["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer", "description": "VENTHORA API key (vth_...)"}}
    doc["security"] = [{"bearer": []}]
    doc["info"] = doc.get("info", {}) | {"description": "Call VENTHORA from your application with an API key. Every endpoint names the scope it needs."}
    return doc


def postman(full: dict, base_url: str) -> dict:
    eps = public_endpoints(full)
    folders: dict[str, list] = {}
    labels = {a: lbl for a, (lbl, _, _) in apiaccess.AREAS.items()}
    for e in eps:
        path = e["path"].replace("{", ":").replace("}", "")
        req = {"method": e["method"], "header": [{"key": "Authorization", "value": "Bearer {{apiKey}}"}],
               "url": {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"], "path": [x for x in path.split("/") if x],
                       "query": [{"key": p["name"], "value": "", "disabled": not p["required"]} for p in e["params"] if p["in"] == "query"]},
               "description": f"{e['description']}\n\nScope: {e['scope']}"}
        if e["body"]:
            req["header"].append({"key": "Content-Type", "value": "application/json"})
            req["body"] = {"mode": "raw", "raw": "{}", "options": {"raw": {"language": "json"}}}
        folders.setdefault(labels.get(e["area"] or "", e["area"] or "Other"), []).append({"name": e["summary"], "request": req})
    return {"info": {"name": "VENTHORA API", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "variable": [{"key": "baseUrl", "value": base_url}, {"key": "apiKey", "value": "vth_..."}],
            "item": [{"name": k, "item": v} for k, v in folders.items()]}
