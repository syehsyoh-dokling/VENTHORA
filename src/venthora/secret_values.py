# ruff: noqa: E501
"""Write-only secrets kept on this machine (data/venthora_secrets.db, git-ignored like .env): the card remote
authorization shared secret, the agent's TAP signing key, the Visa Developer project credentials.

A secret is written once and read only by the code that uses it; the dashboard sees whether it is set and a hint
(the last four characters), never the value. Environment variables of the same name win when they are set.
Every value is sealed at rest with the secrets key (vault.py): a copy of this file alone does not give the secrets.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from . import vault
from .config import data_path, env

_LOCK = threading.RLock()


def _db() -> Path:
    p = data_path("VENTHORA_SECRETS_DB", "data/venthora_secrets.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(_db(), timeout=15)
    c.execute("CREATE TABLE IF NOT EXISTS secret_values (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL,"
              " updated_by TEXT NOT NULL)")
    return c


def put(name: str, value: str, by: str) -> None:
    with _LOCK, _connect() as c:
        c.execute("INSERT INTO secret_values VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value,"
                  " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                  (name, vault.seal(value), datetime.now(UTC).isoformat(timespec="seconds"), by))


def get(name: str, env_name: str | None = None) -> str:
    if env_name and env(env_name):
        return env(env_name)
    with _LOCK, _connect() as c:
        r = c.execute("SELECT value FROM secret_values WHERE name = ?", (name,)).fetchone()
    return vault.unseal(r[0]) if r else ""


def delete(name: str) -> None:
    with _LOCK, _connect() as c:
        c.execute("DELETE FROM secret_values WHERE name = ?", (name,))


def info(name: str, env_name: str | None = None) -> dict:
    """What the dashboard may know: set or not, where from, a hint, when and by whom."""
    if env_name and env(env_name):
        v = env(env_name)
        return {"set": True, "source": "env", "hint": f"…{v[-4:]}" if len(v) >= 8 else "set", "updated_at": None, "updated_by": None}
    with _LOCK, _connect() as c:
        r = c.execute("SELECT value, updated_at, updated_by FROM secret_values WHERE name = ?", (name,)).fetchone()
    if not r:
        return {"set": False, "source": None, "hint": None, "updated_at": None, "updated_by": None}
    v = vault.unseal(r[0])
    return {"set": True, "source": "settings", "hint": f"…{v[-4:]}" if len(v) >= 8 else "set", "updated_at": r[1], "updated_by": r[2]}


def seal_store() -> int:
    """Seal every value still stored in clear (written before encryption was switched on). Returns how many."""
    with _LOCK, _connect() as c:
        rows = [(n, v) for n, v in c.execute("SELECT name, value FROM secret_values") if v and not v.startswith(vault.PREFIX)]
        for n, v in rows:
            c.execute("UPDATE secret_values SET value = ? WHERE name = ?", (vault.seal(v), n))
    return len(rows)
