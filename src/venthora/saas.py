# ruff: noqa: E501
"""SaaS: one VENTHORA per company (a silo), behind one platform.

The platform is VENTHORA's operator (the Developer). It holds the Airwallex platform account and its keys (.env) and a
registry of the companies it serves (data/saas/platform.db). Each company gets:

* its own data directory (data/saas/tenants/<id>/): organisation, payments, audit, intake, webhooks, master data and
  secrets, every store a VENTHORA keeps;
* its own VENTHORA process on its own local port, started by the platform with VENTHORA_TENANT_ID set;
* its own FCO (the Super admin seeded into its organisation) and its own dashboard.

Nothing one company enters is read by another: they share code, not data. Money moves only through the company's own
Airwallex account, one its FCO opens under the platform (a connected account, called with x-on-behalf-of) or one it
already had (linked with its own key). Until then its VENTHORA refuses every money call (gateways/airwallex.py), so the
platform's own account is never a company's. The platform console reads each company's totals through a token only the
platform holds.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from .config import ROOT, data_path

BASE_PORT = 8100
# every store a VENTHORA keeps, each pointed into the company's own directory
STORES = {"VENTHORA_EXECUTION_DB": "execution.db", "VENTHORA_INTAKE_DB": "intake.db", "VENTHORA_AUDIT_DB": "audit.db",
          "VENTHORA_ORG_DB": "org.db", "VENTHORA_WEBHOOK_DB": "webhooks.db", "VENTHORA_MASTER_DB": "master.db",
          "VENTHORA_SECRETS_DB": "secrets.db", "VENTHORA_AI_DB": "ai.db", "VENTHORA_REPORTING_CONFIG": "reporting.json",
          "VENTHORA_PROCUREMENT_DIR": "procurement"}
SCHEMA = """CREATE TABLE IF NOT EXISTS tenant (tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, fco_name TEXT NOT NULL,
  fco_email TEXT NOT NULL, country TEXT NOT NULL, currency TEXT NOT NULL, port INTEGER NOT NULL UNIQUE,
  created_at TEXT NOT NULL, created_by TEXT NOT NULL)"""


def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def saas_dir() -> Path:
    return data_path("VENTHORA_SAAS_DIR", "data/saas")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-")
    return s or "company"


class Registry:
    """The companies the platform serves."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else saas_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.execute(SCHEMA)
            c.execute("CREATE TABLE IF NOT EXISTS secret (id INTEGER PRIMARY KEY CHECK (id = 1), token TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS root (id INTEGER PRIMARY KEY CHECK (id = 1), salt BLOB NOT NULL, hash BLOB NOT NULL, set_at TEXT NOT NULL)")
            if "email" not in {r[1] for r in c.execute("PRAGMA table_info(root)")}:  # Root signs in at /login with an email
                c.execute("ALTER TABLE root ADD COLUMN email TEXT")
            if c.execute("SELECT COUNT(*) FROM secret").fetchone()[0] == 0:
                c.execute("INSERT INTO secret VALUES (1, ?)", (secrets.token_urlsafe(32),))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.root / "platform.db", timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    # -- email sign-in apps (OAuth): VENTHORA's own, one per provider, the same for every company -------------------
    def mail_oauth(self) -> dict:
        """{provider: {client_id, client_secret}}, redirect_uri: what each company's process is given."""
        with self._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS mail_oauth (provider TEXT PRIMARY KEY, client_id TEXT NOT NULL, client_secret TEXT NOT NULL,"
                      " updated_at TEXT NOT NULL)")
            rows = c.execute("SELECT * FROM mail_oauth").fetchall()
        out: dict = {r["provider"]: {"client_id": r["client_id"], "client_secret": r["client_secret"], "updated_at": r["updated_at"]} for r in rows}
        if "_redirect" in out:
            out["redirect_uri"] = out.pop("_redirect")["client_id"]
        return out

    def set_mail_oauth(self, provider: str, client_id: str, client_secret: str | None) -> None:
        """Keep one provider's app; a blank secret keeps the saved one; a blank client ID removes the app."""
        self.mail_oauth()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._connect() as c:
            if not client_id:
                c.execute("DELETE FROM mail_oauth WHERE provider = ?", (provider,))
                return
            old = c.execute("SELECT client_secret FROM mail_oauth WHERE provider = ?", (provider,)).fetchone()
            sec = client_secret or (old["client_secret"] if old else "")
            c.execute("INSERT INTO mail_oauth VALUES (?,?,?,?) ON CONFLICT(provider) DO UPDATE SET client_id = excluded.client_id,"
                      " client_secret = excluded.client_secret, updated_at = excluded.updated_at", (provider, client_id, sec, now))

    def token(self) -> str:
        """The platform's token: each company's process accepts the console's summary call with it, nothing else does."""
        with self._connect() as c:
            return c.execute("SELECT token FROM secret WHERE id = 1").fetchone()[0]

    # -- Root: the platform's operator, one password, chosen at the first visit of the console --------------------
    def root_set(self) -> bool:
        with self._connect() as c:
            return c.execute("SELECT 1 FROM root WHERE id = 1").fetchone() is not None

    def root_setup(self, password: str, email: str = "root") -> None:
        """Only while no Root exists (the first visit of /login); afterwards Root signs in with that email and password."""
        from .auth import check_password

        check_password(password)
        email = email.strip().lower() or "root"
        if email != "root" and "@" not in email:
            raise ValueError("enter Root's email")
        salt = secrets.token_bytes(16)
        with self._lock, self._connect() as c:
            if c.execute("SELECT 1 FROM root WHERE id = 1").fetchone():
                raise ValueError("Root already has a password: sign in")
            c.execute("INSERT INTO root (id, salt, hash, set_at, email) VALUES (1, ?, ?, ?, ?)",
                      (salt, hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32),
                       datetime.now(UTC).isoformat(timespec="seconds"), email))

    def root_email(self) -> str | None:
        with self._connect() as c:
            r = c.execute("SELECT email FROM root WHERE id = 1").fetchone()
        return (r["email"] or "root") if r else None

    def root_verify(self, password: str) -> bool:
        with self._connect() as c:
            r = c.execute("SELECT salt, hash FROM root WHERE id = 1").fetchone()
        if r is None:
            return False
        return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=r["salt"], n=2**14, r=8, p=1, dklen=32), r["hash"])

    def root_cookie(self, hours: int = 12) -> str:
        exp = int(datetime.now(UTC).timestamp()) + hours * 3600
        return f"{exp}.{hmac.new(self.token().encode(), f'root.{exp}'.encode(), hashlib.sha256).hexdigest()}"

    def root_ok(self, cookie: str | None) -> bool:
        try:
            exp, sig = (cookie or "").split(".", 1)
            good = hmac.new(self.token().encode(), f"root.{exp}".encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, good) and int(exp) > datetime.now(UTC).timestamp()
        except ValueError:
            return False

    def list(self) -> list[dict]:
        with self._connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM tenant ORDER BY created_at")]

    def get(self, tenant_id: str) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT * FROM tenant WHERE tenant_id = ?", (tenant_id,)).fetchone()
        return dict(r) if r else None

    def tenant_dir(self, tenant_id: str) -> Path:
        return self.root / "tenants" / tenant_id

    def create(self, *, name: str, fco_name: str, fco_email: str, country: str = "SG", currency: str = "USD", by: str) -> dict:
        """Register a company and give it its own, empty VENTHORA: one head office named after it and its FCO."""
        name, fco_name, fco_email = name.strip(), fco_name.strip(), fco_email.strip().lower()
        if not name or not fco_name or "@" not in fco_email:
            raise ValueError("enter the company name, its FCO's name and the FCO's email")
        with self._lock, self._connect() as c:
            if c.execute("SELECT 1 FROM tenant WHERE lower(name) = lower(?)", (name,)).fetchone():
                raise ValueError(f"{name} is already a VENTHORA company")
            base, n, tid = slug(name), 2, slug(name)
            while c.execute("SELECT 1 FROM tenant WHERE tenant_id = ?", (tid,)).fetchone():
                tid, n = f"{base}-{n}", n + 1
            port = max([BASE_PORT - 1] + [r[0] for r in c.execute("SELECT port FROM tenant")]) + 1
            while not _port_free(port):  # another process (another platform, another app) already listens there
                port += 1
            rec = {"tenant_id": tid, "name": name, "fco_name": fco_name, "fco_email": fco_email, "country": country.upper()[:2],
                   "currency": currency.upper()[:3], "port": port, "created_at": datetime.now(UTC).isoformat(timespec="seconds"), "created_by": by}
            self._seed(rec)
            c.execute("INSERT INTO tenant VALUES (:tenant_id, :name, :fco_name, :fco_email, :country, :currency, :port, :created_at, :created_by)", rec)
        return rec

    def _seed(self, t: dict) -> None:
        """The company's organisation store, blank but its own: the head office carries its name, the Super admin is its FCO."""
        from .organisation import OrgStore

        org = OrgStore(str(self.tenant_dir(t["tenant_id"]) / STORES["VENTHORA_ORG_DB"]))
        hq = org.get("unit", "hq")
        if hq:
            org._put("unit", hq | {"name": t["name"], "country": t["country"], "currency": t["currency"]}, "VENTHORA platform")
        st = org.get("standards", "standards") or {}
        org._put("standards", st | {"_id": "standards", "company_name": t["name"], "base_currency": t["currency"]}, "VENTHORA platform")
        admin = org.get("user", "u-admin")
        if admin:
            org._put("user", admin | {"name": t["fco_name"], "email": t["fco_email"]}, "VENTHORA platform")

    def env_for(self, t: dict) -> dict[str, str]:
        """The environment the company's own VENTHORA process starts with."""
        d = self.tenant_dir(t["tenant_id"])
        d.mkdir(parents=True, exist_ok=True)
        out = {k: str(d / v) for k, v in STORES.items()}
        out |= {"VENTHORA_TENANT_ID": t["tenant_id"], "VENTHORA_TENANT_NAME": t["name"], "VENTHORA_DEPLOYMENT": "saas",
                "VENTHORA_PLATFORM_TOKEN": self.token(), "PYTHONPATH": str(ROOT / "src")}
        return out


def command(t: dict) -> list[str]:
    """How the platform starts a company's VENTHORA (the same API every VENTHORA runs)."""
    return [sys.executable, "-m", "uvicorn", "venthora.api:app", "--host", "127.0.0.1", "--port", str(t["port"]), "--log-level", "warning"]


def environ(reg: Registry, t: dict) -> dict[str, str]:
    env = dict(os.environ)
    for k in list(env):  # a store path set for the platform itself must never leak into a company's process
        if k in STORES or k.startswith("VENTHORA_TENANT"):
            env.pop(k)
    return env | reg.env_for(t)
