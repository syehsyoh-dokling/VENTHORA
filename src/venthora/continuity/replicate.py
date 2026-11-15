# ruff: noqa: E501
"""Copy every store to another database (PostgreSQL in production), so a standby has something to start from.

For each SQLite store under the data directory:
  1. a consistent snapshot is taken with SQLite's online backup, so a store being written is copied whole;
  2. each of its tables is created in the target if it is not there (same columns, PostgreSQL types), in a schema
     named after the store, so two stores with a table of the same name never collide;
  3. the target table is emptied and filled again inside ONE transaction: a reader of the target sees the old copy
     or the new one, never half of each; a copy that fails leaves the old one in place;
  4. the row count of every table is compared with the source, and the run is recorded (replication_log).

It is one-way: the stores stay the source. Run it after every payment run and on a timer; how old the copy is, is how
much of people's work (approvals, call-backs, settings) a takeover can lose. Money is never in that gap: a takeover
reads payments from Airwallex.

    python -m venthora.continuity.replicate <data-dir> <target-url>
"""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from . import db

LOG = """CREATE TABLE IF NOT EXISTS replication_log (
    run_at REAL NOT NULL, store TEXT NOT NULL, tables INTEGER NOT NULL, rows INTEGER NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL)"""
TYPE = {"INTEGER": "BIGINT", "INT": "BIGINT", "REAL": "DOUBLE PRECISION", "BLOB": "BYTEA", "TEXT": "TEXT", "NUMERIC": "NUMERIC", "": "TEXT"}
# never copied: the store of keys, passwords and second factors. A standby gets its secrets from where secrets are
# kept (a secret manager), not from a copy lying in another database
EXCLUDE = {"secrets.db", "venthora_secrets.db"}


def _pg_value(v, blob: bool):
    """A SQLite value as PostgreSQL takes it in a copy. SQLite lets a column hold any type, PostgreSQL does not, so
    the copy's columns are text (bytes for a BLOB column) and every value is written as its text: nothing is lost,
    and a copy never fails on a value its column's declared type did not expect."""
    if v is None:
        return None
    if blob:
        return bytes(v) if isinstance(v, (bytes, memoryview)) else str(v).encode()
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    return str(v).replace("\x00", "")


def prefix(store: Path, data_dir: Path) -> str:
    """The name a store's tables carry in the target: its path under the data directory, as an identifier."""
    rel = store.relative_to(data_dir).with_suffix("").as_posix()
    return re.sub(r"[^a-z0-9]+", "_", rel.lower()).strip("_")


def _snapshot(store: Path, tmp: Path) -> Path:
    copy = tmp / (store.name + ".snapshot")
    with closing(sqlite3.connect(store, timeout=30)) as s, closing(sqlite3.connect(copy)) as d:
        s.backup(d)
    return copy


def _tables(snap: Path) -> dict[str, list[tuple[str, str]]]:
    with closing(sqlite3.connect(snap)) as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: [(col[1], (col[2] or "").upper().split("(")[0].strip()) for col in c.execute(f'PRAGMA table_info("{n}")')] for n in names}


def copy_store(store: Path, data_dir: Path, target: db.Connection, tmp: Path) -> dict:
    """One store into the target, in one transaction. Returns {store, tables, rows, ok, detail}."""
    name, snap = prefix(store, data_dir), _snapshot(store, tmp)
    tables, total, problems = _tables(snap), 0, []
    with closing(sqlite3.connect(snap)) as src, target as t:
        for table, cols in tables.items():
            dest = f"{name}__{table}"
            types = ", ".join(f'"{c}" {("BYTEA" if ty == "BLOB" else "TEXT") if target.postgres else (ty or "TEXT")}' for c, ty in cols)
            t.execute(f'CREATE TABLE IF NOT EXISTS "{dest}" ({types})')
            t.execute(f'DELETE FROM "{dest}"')
            rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
            marks = ", ".join("?" for _ in cols)
            if target.postgres:
                rows = [tuple(_pg_value(v, ty == "BLOB") for v, (_, ty) in zip(r, cols, strict=True)) for r in rows]
            t.executemany(f'INSERT INTO "{dest}" VALUES ({marks})', [tuple(r) for r in rows])
            copied = t.execute(f'SELECT COUNT(*) AS n FROM "{dest}"')[0]["n"]
            if copied != len(rows):
                problems.append(f"{table}: {len(rows)} rows in the store, {copied} in the copy")
            total += len(rows)
        if problems:  # leave the previous copy in place rather than keep one that does not match
            raise RuntimeError("; ".join(problems))
    return {"store": name, "tables": len(tables), "rows": total, "ok": True, "detail": "row counts match"}


def run(data_dir: Path, target_url: str, now: float | None = None) -> list[dict]:
    """Every store under `data_dir`, one after the other; a store that fails is reported and the others still copy."""
    stores = sorted(p for p in Path(data_dir).rglob("*.db") if p.is_file() and p.name not in EXCLUDE)
    target = db.connect(target_url)
    out, run_at = [], now or time.time()  # one time for the whole run: that is how a run is told from the next
    try:
        with target as t:
            t.execute(LOG)
        with tempfile.TemporaryDirectory(prefix="venthora_repl_") as tmp:
            for store in stores:
                try:
                    out.append(copy_store(store, Path(data_dir), target, Path(tmp)))
                except Exception as exc:  # noqa: BLE001 - one store's failure is recorded; the old copy of it stays
                    out.append({"store": prefix(store, Path(data_dir)), "tables": 0, "rows": 0, "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
        with target as t:
            t.executemany("INSERT INTO replication_log VALUES (?, ?, ?, ?, ?, ?)",
                          [(run_at, r["store"], r["tables"], r["rows"], int(r["ok"]), r["detail"]) for r in out])
    finally:
        target.close()
    return out


def last_run(target_url: str) -> dict | None:
    """When the copy was last made and whether every store copied: what a takeover reads to know how old its data is."""
    conn = db.connect(target_url)
    try:
        with conn as c:
            c.execute(LOG)
            rows = c.execute("SELECT run_at, COUNT(*) AS stores, SUM(ok) AS ok, SUM(rows) AS rows FROM replication_log"
                             " GROUP BY run_at ORDER BY run_at DESC LIMIT 1")
    finally:
        conn.close()
    return ({"run_at": rows[0]["run_at"], "stores": rows[0]["stores"], "ok": rows[0]["ok"] == rows[0]["stores"], "rows": rows[0]["rows"]}
            if rows else None)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    results = run(Path(sys.argv[1]), sys.argv[2])
    for r in results:
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['store']}: {r['tables']} tables, {r['rows']} rows - {r['detail']}")
    sys.exit(0 if all(r["ok"] for r in results) else 1)
