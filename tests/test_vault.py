# ruff: noqa: E501
"""Secrets sealed at rest, the audit chain keyed, the receiving server's verdict only, and what a requester reads."""
import sqlite3
from email import message_from_string

import pytest

from venthora import access, vault
from venthora.audit import AuditStore
from venthora.intake import documents


@pytest.fixture(autouse=True)
def keys(monkeypatch, tmp_path):
    monkeypatch.setenv("VENTHORA_SECRETS_KEY_FILE", str(tmp_path / "s.key"))
    monkeypatch.setenv("VENTHORA_AUDIT_KEY_FILE", str(tmp_path / "a.key"))
    monkeypatch.delenv("VENTHORA_SECRETS_KEY", raising=False)
    monkeypatch.delenv("VENTHORA_AUDIT_KEY", raising=False)
    vault.reset_cache()
    yield
    vault.reset_cache()


def test_a_secret_is_stored_sealed_and_read_back_whole(monkeypatch, tmp_path):
    from venthora import secret_values

    monkeypatch.setenv("VENTHORA_SECRETS_DB", str(tmp_path / "secrets.db"))
    secret_values.put("visa.vdp.api_key", "ABCD1234SECRETVALUE", "test")
    raw = sqlite3.connect(tmp_path / "secrets.db").execute("SELECT value FROM secret_values").fetchone()[0]
    assert raw.startswith("enc1:") and "SECRETVALUE" not in raw
    assert secret_values.get("visa.vdp.api_key") == "ABCD1234SECRETVALUE" and secret_values.info("visa.vdp.api_key")["hint"] == "…ALUE"
    with sqlite3.connect(tmp_path / "secrets.db") as c:  # a value written before encryption was switched on
        c.execute("INSERT INTO secret_values VALUES ('old', 'plain-old-value', 'x', 'x')")
    assert secret_values.get("old") == "plain-old-value" and secret_values.seal_store() == 1
    assert secret_values.get("old") == "plain-old-value"
    vault.reset_cache()
    (tmp_path / "s.key").write_text(vault.base64.urlsafe_b64encode(b"x" * 32).decode())
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):  # another key opens nothing
        secret_values.get("visa.vdp.api_key")


def test_the_audit_chain_is_sealed_with_a_key_the_file_does_not_hold(tmp_path):
    a = AuditStore(str(tmp_path / "audit.db"))
    for i in range(3):
        a.append("x", {"i": i})
    assert a.verify() | {"head": None} == {"verified": True, "checked": 3, "sealed": 3, "head": None}
    with sqlite3.connect(tmp_path / "audit.db") as c:  # someone who can write the file rewrites an event and its hash chain
        rows = c.execute("SELECT seq, event_id, created_at, event_type, payload_json FROM events ORDER BY seq").fetchall()
        prev = "GENESIS"
        for seq, eid, at, kind, raw in rows:
            raw = raw.replace('"i":1', '"i":99')
            h = AuditStore._digest(prev, eid, at, kind, raw)
            c.execute("UPDATE events SET payload_json = ?, previous_hash = ?, current_hash = ? WHERE seq = ?", (raw, prev, h, seq))
            prev = h
    v = a.verify()
    assert v["verified"] is False and "seal" in v["reason"]  # the chain was rebuilt; the seal cannot be


def test_only_the_receiving_servers_own_verdict_counts(monkeypatch):
    forged = ("Authentication-Results: mx.receiver.example; spf=fail; dkim=none\n"
              "Authentication-Results: attacker.example; spf=pass; dkim=pass; dmarc=pass\nFrom: a@b.example\nSubject: x\n\nbody")
    assert documents.authentication(message_from_string(forged)) == {"spf": "fail", "dkim": "none"}
    monkeypatch.setenv("VENTHORA_AUTHSERV_IDS", "amazonses.com")
    assert documents.authentication(message_from_string(forged)) == {}  # the topmost header is not from a named receiver
    ok = "Authentication-Results: inbound-smtp.us-east-1.amazonses.com; spf=pass; dkim=pass\nFrom: a@b.example\n\nx"
    assert documents.authentication(message_from_string(ok)) == {"spf": "pass", "dkim": "pass"}


def test_a_requester_does_not_read_the_audit_trail_or_payment_runs():
    for path in ("/api/audit", "/v1/executions", "/v1/ledger", "/v1/reports/staff-spend", "/v1/doa/payroll", "/v1/transaction-log"):
        assert access.check("requester", "GET", path) is not None, path
        assert access.check("finance_manager", "GET", path) is None, path
    assert access.check("requester", "GET", "/v1/fund-requests") is None and access.check("auditor", "GET", "/api/audit") is None
