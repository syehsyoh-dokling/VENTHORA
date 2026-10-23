"""Append-only decision log with a SHA-256 hash chain (tamper-evident, SQLite)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .config import data_path


class AuditStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_AUDIT_DB", "data/venthora_audit.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL, current_hash TEXT NOT NULL)""")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _digest(prev: str, event_id: str, at: str, kind: str, raw: str) -> str:
        return hashlib.sha256("|".join([prev, event_id, at, kind, raw]).encode()).hexdigest()

    def append(self, kind: str, payload: dict) -> dict:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        event_id, at = str(uuid4()), datetime.now(UTC).isoformat()
        with self._lock, self._connect() as c:
            last = c.execute("SELECT current_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["current_hash"] if last else "GENESIS"
            digest = self._digest(prev, event_id, at, kind, raw)
            c.execute("INSERT INTO events(event_id, created_at, event_type, payload_json, previous_hash, current_hash)"
                      " VALUES (?, ?, ?, ?, ?, ?)", (event_id, at, kind, raw, prev, digest))
        return {"event_id": event_id, "hash": digest, "created_at": at}

    def list(self, limit: int = 20) -> list[dict]:
        with self._lock, self._connect() as c:
            rows = c.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        # sqlite3.Row iterates values, so keys() is required here.
        return [{k: r[k] for k in r.keys() if k != "payload_json"} | {"payload": json.loads(r["payload_json"])}  # noqa: SIM118
                for r in rows]

    def verify(self) -> dict:
        with self._lock, self._connect() as c:
            prev, count = "GENESIS", 0
            for r in c.execute("SELECT * FROM events ORDER BY seq"):
                expected = self._digest(prev, r["event_id"], r["created_at"], r["event_type"], r["payload_json"])
                if r["previous_hash"] != prev or r["current_hash"] != expected:
                    return {"verified": False, "checked": count, "broken_seq": r["seq"]}
                prev, count = r["current_hash"], count + 1
        return {"verified": True, "checked": count, "head": prev}
