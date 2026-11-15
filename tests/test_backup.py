# ruff: noqa: E501
"""The backup of the stores can be restored: taken while a store is written, verified, damaged copies refused."""
import importlib.util
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from venthora.audit import AuditStore

spec = importlib.util.spec_from_file_location("backup_stores", Path(__file__).resolve().parents[1] / "scripts" / "backup_stores.py")
bk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bk)


def _data(tmp_path) -> Path:
    data = tmp_path / "data"
    (data / "saas" / "acme").mkdir(parents=True)
    audit = AuditStore(str(data / "venthora_audit.db"))
    for i in range(5):
        audit.append("payment", {"n": i})
    with closing(sqlite3.connect(data / "saas" / "acme" / "org.db")) as c:
        c.execute("CREATE TABLE records (id TEXT, body TEXT)")
        c.executemany("INSERT INTO records VALUES (?, ?)", [(str(i), "x") for i in range(3)])
        c.commit()
    return data


def test_a_backup_is_taken_verified_and_restored(tmp_path):
    data = _data(tmp_path)
    out = bk.backup(data, tmp_path / "backups", now=datetime(2026, 11, 15, 4, 0, tzinfo=UTC))
    assert out.name == "20261115T040000Z" and bk.verify(out) == []
    assert sorted(p.name for p in out.rglob("*.db")) == ["org.db", "venthora_audit.db"]  # every store, in its own folder
    # after the backup the live store moves on, then is lost
    AuditStore(str(data / "venthora_audit.db")).append("payment", {"n": 99})
    (data / "saas" / "acme" / "org.db").unlink()
    assert sorted(bk.restore(out, data)) == ["saas/acme/org.db", "venthora_audit.db"]
    restored = AuditStore(str(data / "venthora_audit.db"))
    assert len(restored.list(100)) == 5 and restored.verify()["verified"]  # the audit chain as it was, still intact
    assert (data / "venthora_audit.db.before-restore").is_file()  # what was replaced is kept, not deleted
    with closing(sqlite3.connect(data / "saas" / "acme" / "org.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 3


def test_a_damaged_backup_is_not_restored(tmp_path):
    data = _data(tmp_path)
    out = bk.backup(data, tmp_path / "backups")
    (out / "venthora_audit.db").write_bytes(b"not a database")
    assert any("changed since the backup" in p for p in bk.verify(out))
    with pytest.raises(SystemExit):
        bk.restore(out, data)
    assert AuditStore(str(data / "venthora_audit.db")).verify()["verified"]  # the live store was not touched
    assert "not a backup" in bk.verify(tmp_path)[0]
    with pytest.raises(SystemExit):
        bk.backup(tmp_path / "empty", tmp_path / "backups")
