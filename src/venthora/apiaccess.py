# ruff: noqa: E501
"""API access for other applications: keys with scopes, and the catalogue of every endpoint they may call.

VENTHORA's own dashboard runs on the same machine and needs no key. Another application (an ERP, a BI tool, a
bot) gets an API key from Settings → API access and sends it as ``Authorization: Bearer vth_...`` or
``X-API-Key: vth_...``. A key carries:

* scopes   ``<area>:read`` and/or ``<area>:write`` per area (treasury, payables, ...). GET and the
           side-effect-free ``.../eval`` calls need read; everything else needs write.
* role     the role its calls act with; the ``role`` and ``by`` query parameters are replaced by the key's,
           so a key cannot claim more than it was given (``by`` becomes ``api:<key name>``).
* expiry   optional; a revoked or expired key is refused.

Only a SHA-256 of the key is stored; the key itself is shown once, when it is created. Keys can never manage
keys (``/v1/developer/keys``), and the inbound webhooks keep their own authentication.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from .config import data_path

# area → (label, what it covers, path prefixes). Longest prefix wins.
AREAS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "treasury": ("Treasury and payments", "plans, executions, position, ledger, incidents, Airwallex balances",
                 ("/v1/plans", "/v1/executions", "/v1/treasury", "/v1/incidents", "/v1/ledger", "/v1/recon-cases",
                  "/api/airwallex", "/api/sandbox", "/api/scenarios", "/api/live", "/api/runs", "/api/audit")),
    "payables": ("Payables", "intake of invoices and claims, payees and bank details, purchase orders, approvals",
                 ("/v1/intake", "/v1/payees", "/v1/procurement", "/v1/approvals")),
    "receivables": ("Receivables", "customer invoices and ageing", ("/v1/aging",)),
    "budget": ("Budget and reporting", "budgets, period close, reporting periods and rates, tenants",
               ("/v1/budgets", "/v1/budget-allocation", "/v1/reporting", "/v1/tenants", "/api/budget")),
    "organisation": ("Organisation", "units, people and limits, projects, accounts, allocations, standards",
                     ("/v1/org",)),
    "sources": ("Data sources", "connectors, sync, uploads, master data, readiness", ("/v1/sources",)),
    "settings": ("Settings and integrations", "AI model, webhook status and events", ("/v1/settings", "/v1/webhooks")),
    "system": ("Health and evidence", "health, status, ground-truth suites, scenario matrix",
               ("/health", "/api/status", "/api/groundtruth", "/api/matrix")),
}
NO_KEY = ("/webhooks/",)               # inbound push channels: their own signatures and secrets
NEVER_BY_KEY = ("/v1/developer/keys",)  # a key cannot create or revoke keys
OPEN_WITH_KEY = ("/openapi.json", "/docs", "/v1/developer/catalog")
ROLES = ("auditor", "treasury_analyst", "finance_manager", "cfo", "admin")  # the dashboard's roles; auditor is read only


def area_of(path: str) -> str | None:
    best, size = None, -1
    for area, (_, _, prefixes) in AREAS.items():
        for p in prefixes:
            if (path == p or path.startswith(p.rstrip("/") + "/")) and len(p) > size:
                best, size = area, len(p)
    return best


def access_of(method: str, path: str) -> str:
    return "read" if method in ("GET", "HEAD", "OPTIONS") or path.rstrip("/").endswith("/eval") else "write"


def scope_for(method: str, path: str) -> str | None:
    area = area_of(path)
    return f"{area}:{access_of(method, path)}" if area else None


ALL_SCOPES = [f"{a}:{x}" for a in AREAS for x in ("read", "write")]


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="the application that will use the key")
    scopes: list[str] = Field(min_length=1)
    role: str = "auditor"
    expires_days: int | None = Field(None, ge=1, le=730)

    @field_validator("scopes")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if s not in ALL_SCOPES]
        if bad:
            raise ValueError(f"unknown scope(s) {bad}; use <area>:read or <area>:write with area in {list(AREAS)}")
        return sorted(set(v))

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError(f"role must be one of {list(ROLES)}")
        return v


SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
  key_id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL, hint TEXT NOT NULL, scopes TEXT NOT NULL,
  role TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, expires_at TEXT, revoked_at TEXT,
  last_used_at TEXT, calls INTEGER NOT NULL DEFAULT 0);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class KeyStore:
    """API keys in the master database (VENTHORA_MASTER_DB). The key itself is never stored."""

    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_MASTER_DB", "data/venthora_master.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _public(r: sqlite3.Row) -> dict:
        d = dict(r)
        d.pop("hash")
        d["scopes"] = json.loads(d["scopes"])
        exp = d["expires_at"]
        d["status"] = "revoked" if d["revoked_at"] else "expired" if exp and exp <= _now().isoformat() else "active"
        return d

    def create(self, req: KeyRequest, by: str) -> tuple[dict, str]:
        key_id = secrets.token_hex(4)
        token = f"vth_{key_id}_{secrets.token_urlsafe(24)}"
        exp = (_now() + timedelta(days=req.expires_days)).isoformat(timespec="seconds") if req.expires_days else None
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO api_keys (key_id, name, hash, hint, scopes, role, created_at, created_by, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (key_id, req.name, _hash(token), f"vth_{key_id}_…{token[-4:]}", json.dumps(req.scopes), req.role,
                       _now().isoformat(timespec="seconds"), by, exp))
        return self.get(key_id), token

    def get(self, key_id: str) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
        return self._public(r) if r else None

    def list(self) -> list[dict]:
        with self._connect() as c:
            return [self._public(r) for r in c.execute("SELECT * FROM api_keys ORDER BY created_at DESC")]

    def revoke(self, key_id: str) -> dict | None:
        with self._lock, self._connect() as c:
            c.execute("UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL", (_now().isoformat(timespec="seconds"), key_id))
        return self.get(key_id)

    def verify(self, token: str) -> dict | None:
        """The key's record when the token is a live key, else None. Counts the call."""
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != "vth":
            return None
        with self._lock, self._connect() as c:
            r = c.execute("SELECT * FROM api_keys WHERE key_id = ?", (parts[1],)).fetchone()
            if not r or not hmac.compare_digest(r["hash"], _hash(token)):
                return None
            rec = self._public(r)
            if rec["status"] != "active":
                return None
            c.execute("UPDATE api_keys SET last_used_at = ?, calls = calls + 1 WHERE key_id = ?", (_now().isoformat(timespec="seconds"), parts[1]))
        return rec


def token_of(headers) -> str | None:
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer ") and auth[7:].strip().startswith("vth_"):
        return auth[7:].strip()
    key = (headers.get("x-api-key") or "").strip()
    return key or None


def catalog(openapi: dict) -> list[dict]:
    """Every endpoint in the OpenAPI schema with its area and the scope a key needs."""
    out = []
    for path, ops in openapi.get("paths", {}).items():
        for method, op in ops.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            m = method.upper()
            if path.startswith(NO_KEY):
                area, scope = "inbound", None
            elif path.startswith(NEVER_BY_KEY):
                area, scope = "developer", None
            elif path.startswith(OPEN_WITH_KEY):
                area, scope = "developer", "any key"
            else:
                area, scope = area_of(path), scope_for(m, path)
            desc = (op.get("description") or "").strip()
            params = [{"name": p["name"], "in": p["in"], "required": p.get("required", False)} for p in op.get("parameters", [])]
            body = (op.get("requestBody") or {}).get("content", {})
            ref = next(iter(body.values()), {}).get("schema", {}).get("$ref", "") if body else ""
            out.append({"method": m, "path": path, "summary": op.get("summary") or path, "description": desc.split("\n")[0][:240],
                        "area": area, "scope": scope, "params": params, "body": ref.rsplit("/", 1)[-1] if ref else ("JSON" if body else None)})
    order = {a: i for i, a in enumerate([*AREAS, "developer", "inbound"])}
    return sorted(out, key=lambda e: (order.get(e["area"] or "", 99), e["path"], e["method"]))
