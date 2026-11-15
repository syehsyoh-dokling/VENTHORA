# ruff: noqa: E501
"""The copy to PostgreSQL: values as text so a loosely typed SQLite column never breaks it, and the secrets store is never copied."""
import sqlite3

from venthora.continuity import replicate


def test_a_value_is_copied_as_its_text_and_bytes_stay_bytes_in_a_blob_column():
    assert replicate._pg_value(None, False) is None
    assert replicate._pg_value(12, False) == "12" and replicate._pg_value(1.5, False) == "1.5" and replicate._pg_value("a\x00b", False) == "ab"
    assert replicate._pg_value(b"\x01\x02", True) == b"\x01\x02" and replicate._pg_value(b"\x01\x02", False) == "0102"


def test_the_secrets_store_is_never_copied(tmp_path):
    for name in ("org.db", "secrets.db"):
        with sqlite3.connect(tmp_path / name) as c:
            c.execute("CREATE TABLE t (k TEXT, v INTEGER)")
            c.execute("INSERT INTO t VALUES ('a', 1)")
    out = replicate.run(tmp_path, str(tmp_path / "replica.sqlite"))
    assert [r["store"] for r in out] == ["org"] and out[0]["ok"] and out[0]["rows"] == 1
