# ruff: noqa: E501
"""Payee lifecycle: NEW -> VERIFIED -> ACTIVE, cooling after a bank change, the call-back is mandatory."""
import asyncio
import datetime as dt

import pytest

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.payees import PayeeError, evaluate, payee_reasons
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("payees")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_payee_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_payee_reasons_from_rows():
    f = {"attached": True, "rows": [{"status": "VERIFIED", "in_cooling": True}, {"status": "VERIFIED", "in_cooling": False}, {"status": "ACTIVE", "in_cooling": False}]}
    assert payee_reasons(f) == ["payee_in_cooling_period", "payee_not_yet_active"]
    assert payee_reasons({"attached": False, "rows": [{"status": "VERIFIED", "in_cooling": True}]}) == []


def test_store_applies_and_logs_events(tmp_path):
    from venthora.execution.store import ExecutionStore
    store = ExecutionStore(str(tmp_path / "e.db"))
    assert store.seed_payees([{"beneficiary_id": "p", "status": "ACTIVE", "bank_fingerprint": "bank:a", "registered_phone": "+1 555 0100", "paid_count": 3}]) == 1
    assert store.seed_payees([{"beneficiary_id": "p", "status": "NEW"}]) == 0  # a live record is never overwritten
    rec = store.payee_event("p", {"kind": "bank_change", "bank_fingerprint": "bank:b", "source": "test"}, cooling_hours=48, now="2026-10-23T12:00:00+00:00")
    assert rec["status"] == "CHANGED" and rec["cooling_until"] == "2026-10-25T12:00:00+00:00"
    with pytest.raises(PayeeError):
        store.payee_event("p", {"kind": "verify", "by": "x", "role": "finance_manager", "called_number": "+1 555 0999"})
    rec = store.payee_event("p", {"kind": "verify", "by": "x", "role": "finance_manager", "called_number": "+1 (555) 0100"})
    assert rec["status"] == "VERIFIED" and rec["cooling_until"] == "2026-10-25T12:00:00+00:00"
    events = store.payee_events("p")
    assert [e["outcome"] for e in events] == ["APPLIED", "REFUSED", "APPLIED"]
    rec = store.payee_event("p", {"kind": "paid", "at": "2026-10-25T12:00:00+00:00"})
    assert rec["status"] == "ACTIVE" and rec["paid_count"] == 4


def test_onboarding_records_carry_the_bank_fingerprint():
    from venthora.payees import onboarding_records
    recs = {r["beneficiary_id"]: r for r in onboarding_records()}
    assert recs["ben-saas-vendor"]["status"] == "ACTIVE"
    assert recs["ben-saas-vendor"]["bank_fingerprint"].startswith("bank:")
    assert recs["ben-saas-vendor"]["registered_phone"]


def test_evaluate_defaults_now_when_no_at():
    out = evaluate({"record": None, "events": [{"kind": "register", "beneficiary_id": "q", "bank_fingerprint": "bank:q", "registered_phone": "1"}]})
    assert out["status"] == "NEW" and out["applied"] == 1
    assert dt.datetime.now(dt.UTC).year >= 2026
