"""Durable execution state (SQLite). Survives restarts; enforces once-only effects.

* operations   - every money-moving call with its request_id, written *before*
                 the call so a crash or timeout can be resumed with the same id.
* payouts      - one live claim per obligation (by invoice fingerprint when
                 present). Blocks paying the same invoice twice, including
                 after Airwallex's 7-day request_id window has expired.
* used_quotes  - an FX quote is booked at most once.
* beneficiaries - internal beneficiary id -> Airwallex beneficiary id.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..config import ROOT, env

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, status TEXT NOT NULL,
    request_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations (
    op_key TEXT PRIMARY KEY, execution_id TEXT NOT NULL, kind TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, airwallex_id TEXT,
    response_json TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payouts (
    payout_key TEXT PRIMARY KEY, obligation_id TEXT NOT NULL, execution_id TEXT NOT NULL,
    status TEXT NOT NULL, transfer_id TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS used_quotes (
    quote_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, used_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS beneficiaries (
    internal_id TEXT PRIMARY KEY, airwallex_id TEXT NOT NULL, created_at TEXT NOT NULL);
"""

LIVE_PAYOUT_STATUSES = ("IN_FLIGHT", "PAID")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DuplicatePayout(RuntimeError):
    pass


class QuoteReused(RuntimeError):
    pass


class ExecutionStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or env("VENTHORA_EXECUTION_DB", str(ROOT / "data" / "venthora_execution.db")))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        return conn

    # -- executions ----------------------------------------------------------

    def create_execution(self, plan_hash: str, request: dict) -> str:
        execution_id = str(uuid4())
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO executions VALUES (?, ?, 'RUNNING', ?, NULL, ?, ?)",
                      (execution_id, plan_hash, json.dumps(request, default=str), _now(), _now()))
        return execution_id

    def finish_execution(self, execution_id: str, status: str, result: dict) -> None:
        with self._lock, self._connect() as c:
            c.execute("UPDATE executions SET status = ?, result_json = ?, updated_at = ? WHERE execution_id = ?",
                      (status, json.dumps(result, default=str), _now(), execution_id))

    def list_executions(self, limit: int = 50) -> list[dict]:
        with self._lock, self._connect() as c:
            rows = c.execute("SELECT execution_id, plan_hash, status, created_at, updated_at FROM executions"
                             " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_execution(self, execution_id: str) -> dict | None:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT * FROM executions WHERE execution_id = ?", (execution_id,)).fetchone()
        if row is None:
            return None
        return {"execution_id": row["execution_id"], "plan_hash": row["plan_hash"], "status": row["status"],
                "request": json.loads(row["request_json"]),
                "result": json.loads(row["result_json"]) if row["result_json"] else None}

    # -- operations ----------------------------------------------------------

    def operation(self, execution_id: str, op_key: str, kind: str) -> dict:
        """Return the operation, creating it (with a fresh request_id) on first use."""
        key = f"{execution_id}:{op_key}"
        with self._lock, self._connect() as c:
            row = c.execute("SELECT * FROM operations WHERE op_key = ?", (key,)).fetchone()
            if row is None:
                c.execute("INSERT INTO operations VALUES (?, ?, ?, ?, 'PENDING', NULL, NULL, ?)",
                          (key, execution_id, kind, str(uuid4()), _now()))
                row = c.execute("SELECT * FROM operations WHERE op_key = ?", (key,)).fetchone()
        return {"op_key": row["op_key"], "kind": row["kind"], "request_id": row["request_id"], "status": row["status"],
                "airwallex_id": row["airwallex_id"],
                "response": json.loads(row["response_json"]) if row["response_json"] else None}

    def update_operation(self, op_key: str, status: str, airwallex_id: str | None = None,
                         response: dict | None = None) -> None:
        with self._lock, self._connect() as c:
            c.execute("UPDATE operations SET status = ?, airwallex_id = COALESCE(?, airwallex_id),"
                      " response_json = COALESCE(?, response_json), updated_at = ? WHERE op_key = ?",
                      (status, airwallex_id, json.dumps(response, default=str) if response is not None else None,
                       _now(), op_key))

    def operations(self, execution_id: str) -> list[dict]:
        with self._lock, self._connect() as c:
            rows = c.execute("SELECT op_key, kind, request_id, status, airwallex_id FROM operations"
                             " WHERE execution_id = ? ORDER BY rowid", (execution_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- payouts (duplicate-payment defense) ---------------------------------

    def claim_payout(self, payout_key: str, obligation_id: str, execution_id: str) -> None:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT * FROM payouts WHERE payout_key = ?", (payout_key,)).fetchone()
            if row is not None and row["status"] in LIVE_PAYOUT_STATUSES and row["execution_id"] != execution_id:
                raise DuplicatePayout(f"{obligation_id} already {row['status'].lower()} by execution {row['execution_id']}")
            c.execute("INSERT OR REPLACE INTO payouts VALUES (?, ?, ?, 'IN_FLIGHT', ?, ?)",
                      (payout_key, obligation_id, execution_id, row["transfer_id"] if row and
                       row["execution_id"] == execution_id else None, _now()))

    def settle_payout(self, payout_key: str, status: str, transfer_id: str | None) -> None:
        with self._lock, self._connect() as c:
            c.execute("UPDATE payouts SET status = ?, transfer_id = COALESCE(?, transfer_id), updated_at = ?"
                      " WHERE payout_key = ?", (status, transfer_id, _now(), payout_key))

    def payout(self, payout_key: str) -> dict | None:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT * FROM payouts WHERE payout_key = ?", (payout_key,)).fetchone()
        return dict(row) if row else None

    # -- quotes and beneficiaries --------------------------------------------

    def use_quote(self, quote_id: str, execution_id: str) -> None:
        try:
            with self._lock, self._connect() as c:
                c.execute("INSERT INTO used_quotes VALUES (?, ?, ?)", (quote_id, execution_id, _now()))
        except sqlite3.IntegrityError as exc:
            raise QuoteReused(f"quote {quote_id} was already booked") from exc

    def beneficiary(self, internal_id: str) -> str | None:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT airwallex_id FROM beneficiaries WHERE internal_id = ?", (internal_id,)).fetchone()
        return row["airwallex_id"] if row else None

    def save_beneficiary(self, internal_id: str, airwallex_id: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT OR REPLACE INTO beneficiaries VALUES (?, ?, ?)", (internal_id, airwallex_id, _now()))
