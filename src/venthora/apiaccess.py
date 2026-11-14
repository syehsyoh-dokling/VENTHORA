# ruff: noqa: E501
"""API access for other applications: keys with scopes, and the catalogue of every endpoint they may call.

VENTHORA's own dashboard runs on the same machine and needs no key. Another application (an ERP, a BI tool, a
bot) gets an API key from Platform Setup → App Integration and sends it as ``Authorization: Bearer vth_...`` or
``X-API-Key: vth_...``. A key carries:

* scopes   ``<area>:read`` and/or ``<area>:write`` per area (treasury, payables, ...). GET and the
           side-effect-free ``.../eval`` calls need read; everything else needs write.
* role     the role its calls act with; the ``role`` and ``by`` query parameters are replaced by the key's,
           so a key cannot claim more than it was given (``by`` becomes ``api:<key name>``).
* expiry   optional; a revoked or expired key is refused.

Only a SHA-256 of the key is stored; the key itself is shown once, when it is created. Keys can never manage
keys (``/v1/developer/keys``), the bank connection or the AI provider vault, and the inbound webhooks keep their
own authentication. Internal endpoints (apidocs.INTERNAL) are not for keys at all.

Every call made with a key is logged (``api_requests``, the last 1,000 per key). A write that carries an
``Idempotency-Key`` header is answered once: a retry with the same key and body gets the stored response,
a retry with another body is refused (``idempotency``, kept 24 hours).
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

from .apidocs import is_internal
from .config import data_path

# area → (label, what it covers, path prefixes). Longest prefix wins.
AREAS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "treasury": ("Treasury and payments", "accounts, balances, plans, executions, position, ledger, incidents",
                 ("/v1/plans", "/v1/transfers", "/v1/executions", "/v1/exceptions", "/v1/agent-plan", "/v1/notifications", "/v1/transaction-log", "/v1/treasury", "/v1/incidents", "/v1/ledger", "/v1/recon-cases",
                  "/v1/accounts", "/v1/balances", "/v1/collection-accounts", "/v1/acting-as", "/v1/reference",
                  "/api/airwallex", "/v1/airwallex", "/api/audit", "/v1/audit")),
    "payables": ("Payables", "intake of invoices and claims, payees, vendors, consultants and customers with their bank details, purchase orders, approvals",
                 ("/v1/intake", "/v1/payees", "/v1/beneficiaries", "/v1/procurement", "/v1/commitments", "/v1/approvals", "/v1/purchase-requests", "/v1/fund-requests", "/v1/cards", "/v1/card-authorization", "/v1/wallet-payouts")),
    "receivables": ("Receivables", "customer invoices and ageing, balance received", ("/v1/aging", "/v1/receipts")),
    "budget": ("Budget and reporting", "budgets, period close, reporting periods and rates, tenants",
               ("/v1/budgets", "/v1/budget-lines", "/v1/budget-allocation", "/v1/cash-placement", "/v1/placement-providers", "/v1/reporting", "/v1/reports", "/v1/tenants", "/api/budget")),
    "organisation": ("Organisation", "units, people and limits, projects, accounts, allocations, standards",
                     ("/v1/org",)),
    "sources": ("Data sources", "connectors, sync, uploads, master data, readiness", ("/v1/sources",)),
    "settings": ("Settings and integrations", "AI model, webhook status and events", ("/v1/settings", "/v1/webhooks", "/v1/prompt-system", "/v1/visa", "/v1/outbox", "/v1/metal", "/v1/blockchain-record", "/v1/local-rails", "/v1/fco-notify", "/v1/mcp", "/v1/providers", "/v1/controls", "/v1/airi", "/v1/t0", "/v1/placement-connections", "/v1/company-account", "/v1/tenant", "/v1/funding-source")),
    "system": ("Health and evidence", "health, status, ground-truth suites, scenario matrix",
               ("/health", "/api/status", "/v1/status", "/v1/evidence", "/v1/demo")),
}
NO_KEY = ("/webhooks/",)               # inbound push channels: their own signatures and secrets
NEVER_BY_KEY = ("/v1/developer/keys", "/v1/airwallex/connection", "/v1/settings/ai", "/v1/settings/email", "/v1/mandate", "/v1/doa", "/v1/priority-sop", "/v1/prompt-system", "/v1/card-authorization", "/v1/visa", "/v1/edition", "/v1/providers", "/v1/placement-providers", "/v1/company-account", "/v1/funding-source")  # keys, bank, AI vault, mailboxes, the agent's authority, the edition
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
    if is_internal(path):
        return None
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
CREATE TABLE IF NOT EXISTS api_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL, at TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
  status INTEGER NOT NULL, ms INTEGER NOT NULL, client TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS api_requests_key ON api_requests (key_id, id);
CREATE TABLE IF NOT EXISTS idempotency (
  owner TEXT NOT NULL, idem_key TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
  status INTEGER, body BLOB, content_type TEXT, PRIMARY KEY (owner, idem_key));
"""
LOG_KEEP = 1000
IDEMPOTENCY_HOURS = 24


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


    # -- request log -----------------------------------------------------------------------------------------------
    def log(self, key_id: str, method: str, path: str, status: int, ms: int, client: str = "") -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO api_requests (key_id, at, method, path, status, ms, client) VALUES (?,?,?,?,?,?,?)",
                      (key_id, _now().isoformat(timespec="seconds"), method, path[:300], status, ms, client[:64]))
            c.execute("DELETE FROM api_requests WHERE key_id = ? AND id <= (SELECT id FROM api_requests WHERE key_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                      (key_id, key_id, LOG_KEEP))

    def requests(self, key_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as c:
            rows = c.execute("SELECT at, method, path, status, ms, client FROM api_requests WHERE key_id = ? ORDER BY id DESC LIMIT ?",
                             (key_id, max(1, min(limit, LOG_KEEP)))).fetchall()
        return [dict(r) for r in rows]

    # -- idempotency -------------------------------------------------------------------------------------------------
    def idem_begin(self, owner: str, idem_key: str, fingerprint: str) -> tuple[str, dict | None]:
        """("new", None) the first time; ("replay", stored) for a finished retry with the same body;
        ("conflict", None) for the same key with another body; ("busy", None) while the first is still running."""
        cutoff = (_now() - timedelta(hours=IDEMPOTENCY_HOURS)).isoformat(timespec="seconds")
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM idempotency WHERE created_at < ?", (cutoff,))
            r = c.execute("SELECT * FROM idempotency WHERE owner = ? AND idem_key = ?", (owner, idem_key)).fetchone()
            if r is None:
                c.execute("INSERT INTO idempotency (owner, idem_key, fingerprint, created_at) VALUES (?,?,?,?)",
                          (owner, idem_key, fingerprint, _now().isoformat(timespec="seconds")))
                return "new", None
            if r["fingerprint"] != fingerprint:
                return "conflict", None
            if r["status"] is None:
                return "busy", None
            return "replay", {"status": r["status"], "body": r["body"], "content_type": r["content_type"]}

    def idem_finish(self, owner: str, idem_key: str, status: int, body: bytes, content_type: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("UPDATE idempotency SET status = ?, body = ?, content_type = ? WHERE owner = ? AND idem_key = ?",
                      (status, body, content_type, owner, idem_key))

    def idem_drop(self, owner: str, idem_key: str) -> None:
        """A failed attempt (5xx) is forgotten, so the client may retry it."""
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM idempotency WHERE owner = ? AND idem_key = ?", (owner, idem_key))


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
