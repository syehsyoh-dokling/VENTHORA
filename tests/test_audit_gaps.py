"""Gaps found by the adversarial audit, closed and pinned."""
import asyncio
import copy
import json
from pathlib import Path

import httpx

from conftest import decide, scenario
from venthora.audit import AuditStore
from venthora.domain.models import PayerHistory, TreasuryState
from venthora.evidence.llm import ChatModel
from venthora.evidence.reader import InboxMessage, calibrate_delay, read_inbox
from venthora.execution.executor import Executor, terms_digest
from venthora.execution.models import Approval, ExecutionRequest
from venthora.execution.store import ExecutionStore
from venthora.gateways.airwallex import SANDBOX_URL, Airwallex
from venthora.orchestrator import Orchestrator
from venthora.proofcore.gate import ProofCore
from venthora.testing.fake_airwallex import FakeAirwallex

ROOT = Path(__file__).resolve().parents[1]
INBOX = json.loads((ROOT / "data" / "inbox" / "kit01_inbox.json").read_text(encoding="utf-8"))


# Kit 01 #51: a contradicting customer email must lower the limit even for a payer with a spotless record.
def test_contradicting_email_lowers_the_limit_even_for_a_reliable_payer():
    s = scenario("kit01_baseline")
    s["receivables"][0]["payer_history"] = {"kept": 60, "missed": 0}
    import datetime as dt
    r = asyncio.run(read_inbox(TreasuryState.model_validate(s), [InboxMessage(**m) for m in INBOX["messages"]],
                               as_of=dt.date(2026, 10, 23), model=ChatModel(endpoint="", model="")))
    assert r.updates[0].new_confidence_bp == 7000  # capped below the fully-supported band
    plan = decide(r.state.model_dump())
    assert plan.conversion_limit_minor == 500_000 and plan.decision == "REQUIRES_APPROVAL"


def test_calibration_ceiling_is_a_policy_knob():
    _, conf, _, why = calibrate_delay(24, 9000, PayerHistory(kept=60, missed=0), 5, True, ceiling_bp=8000)
    assert conf == 8000 and "capped at 80%" in why


# Guide #38: the approval binds to the terms the approver saw.
def test_approval_on_different_terms_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "c")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "k")
    fake = FakeAirwallex(balances={"USD": "22500", "EUR": "0", "GBP": "0"})

    async def go():
        client = httpx.AsyncClient(base_url=SANDBOX_URL, transport=httpx.MockTransport(fake.handler))
        agent = Orchestrator(ProofCore(airwallex=Airwallex(base_url=SANDBOX_URL, client=client, backoff_s=0)))
        executor = Executor(agent, ExecutionStore(str(tmp_path / "e.db")), AuditStore(str(tmp_path / "a.db")),
                            poll_interval_s=0)
        try:
            state = TreasuryState.model_validate(scenario("kit01_receipt_delayed"))
            plan = await agent.decide(state)
            seen = copy.deepcopy(plan.approval_terms)
            seen[0]["amount"] = "USD 1.00"  # the approver was shown something else
            stale = Approval(approver="Dana", role="finance_manager",
                             terms_digest=terms_digest(plan.model_copy(update={"approval_terms": seen})))
            refused = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=stale))
            moved_after_refusal = len(fake.transfers) + len(fake.conversions)
            good = Approval(approver="Dana", role="finance_manager", terms_digest=terms_digest(plan))
            ok = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=good))
            return refused, moved_after_refusal, ok
        finally:
            await agent.close()

    refused, moved_after_refusal, ok = asyncio.run(go())
    assert refused.status == "REFUSED" and "different terms" in refused.message and moved_after_refusal == 0
    assert ok.status == "COMPLETED"


def test_approval_terms_cover_every_payout_and_conversion():
    r = decide(scenario("kit01_initial"))
    payouts = {t["obligation_id"] for t in r.approval_terms if t["kind"] == "payout"}
    assert payouts == {d.obligation_id for d in r.obligations if d.action in ("PAY", "CONVERT_AND_PAY")}
    assert sum(t["kind"] == "conversion" for t in r.approval_terms) == len(r.conversions)
    for t in r.approval_terms:
        if t["kind"] == "payout":
            assert t["amount"] and t["counterparty"] and t["evidence"]["ref"]
