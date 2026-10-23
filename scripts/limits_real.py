# ruff: noqa: E501
"""Limits and velocity on the real Airwallex sandbox: the books' own history decides what the agent may do alone.

  python scripts/limits_real.py [--approve NAME]

A fresh book pays the same payee three times with a per-payee period limit of USD 2,500 and a first-payment limit of
USD 1,000 for a payee the books have never paid:
  run 1  USD 1,200 to a known payee (ben-saas-vendor, paid in earlier cycles) -> inside every limit -> autonomous, executed
  run 2  USD 1,500 to the same payee -> paid this period 1,200 (+ fee) + 1,500 > 2,500 -> payee_period_limit_exceeded
         -> a person approves -> executed
  run 3  USD 1,200 to a payee the books have never paid (a fresh beneficiary id is not possible on the sandbox registry,
         so the rule is shown through the live service: see docs) -> instead: USD 2,600 single payout with
         max_single_payment 2,500 -> single_payment_above_limit -> executed without approval is REFUSED, with approval it runs
Writes docs/limits_log_real.json.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from venthora.audit import AuditStore  # noqa: E402
from venthora.config import load_env  # noqa: E402
from venthora.domain.models import TreasuryState  # noqa: E402
from venthora.execution.executor import Executor, terms_digest  # noqa: E402
from venthora.execution.facts import apply_operational_facts  # noqa: E402
from venthora.execution.live import with_live_balances_and_rates  # noqa: E402
from venthora.execution.models import Approval, ExecutionRequest  # noqa: E402
from venthora.execution.store import ExecutionStore  # noqa: E402
from venthora.gateways.airwallex import Airwallex  # noqa: E402
from venthora.orchestrator import Orchestrator  # noqa: E402
from venthora.proofcore.gate import ProofCore  # noqa: E402

load_env()
POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 1_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72, "payout_fees_minor": {"LOCAL:USD": 300},
          "max_per_payee_period_minor": 250_000, "max_single_payment_minor": 250_000, "max_first_payment_minor": 100_000}


def book(tag: str, i: int, amount: int) -> dict:
    return {"case_id": f"limits-real-{tag}", "book_id": f"limits-real-{tag}", "base_currency": "USD",
            "balances": {"USD": 2_000_000, "EUR": 0, "GBP": 0}, "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
            "obligations": [{"obligation_id": f"LIM-{i}-{tag}", "amount_minor": amount, "currency": "USD", "priority": 8, "due_in_hours": 24,
                             "beneficiary_id": "ben-saas-vendor", "payee_name": "Cloudloom Software LLC", "payout_method": "LOCAL",
                             "evidence_ref": f"ev-{i}-{tag}", "invoice_fingerprint": f"cloudloom:LIM-{i}-{tag}"}],
            "receivables": [], "evidence": [{"ref": f"ev-{i}-{tag}", "subject_id": f"LIM-{i}-{tag}", "source": "ap:invoice", "status": "verified", "summary": "invoice"}],
            "scenarios": [], "policy": POLICY, "actor_role": "treasury_agent"}


async def main(approver: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "book": f"limits-real-{tag}", "runs": []}
    # The payee limit is per period across every book: set it relative to what the ledger already holds for this payee
    # today, so run 1 (USD 1,200 + fee) fits and run 2 (USD 1,500 more) does not.
    from venthora.reporting import load_config, reporting_period

    now = dt.datetime.now(dt.UTC)
    already = store.payment_history(now.isoformat(), reporting_period(now, load_config()))["paid_per_payee_period_minor"].get("ben-saas-vendor", 0)
    POLICY["max_per_payee_period_minor"] = already + 130_000
    log["payee_paid_this_period_before_minor"] = already
    log["max_per_payee_period_minor"] = POLICY["max_per_payee_period_minor"]
    print(f"ben-saas-vendor already paid this period: {already}; per-payee limit set to {POLICY['max_per_payee_period_minor']}")
    try:
        async def cycle(name: str, i: int, amount: int, approve: bool) -> dict:
            state = TreasuryState.model_validate(book(tag, i, amount))
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            state, facts = apply_operational_facts(state, store)
            plan = await agent.decide(state)
            lim = next((s.details for s in plan.stages if s.engine == "Policy · limits"), {})
            requires = next((s.details.get("local", {}).get("requires_approval", []) for s in plan.stages if s.engine == "Policy"), [])
            rec = {"run": name, "decision": plan.decision, "requires_approval": requires,
                   "payee_history": lim.get("payees"), "history_payees": len(state.history.payees_paid_before if state.history else []),
                   "outflow_24h_minor": lim.get("outflow_24h_minor")}
            print(f"\n{name}: {plan.decision} {requires}  payee {lim.get('payees')}")
            approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(plan)) if (approve and plan.decision == "REQUIRES_APPROVAL") else None
            result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval))
            rec["execution"] = {"status": result.status, "message": result.message[:160]}
            print(f"    execution {result.status}: {result.message[:120]}")
            log["runs"].append(rec)
            return rec

        r1 = await cycle("run 1: USD 1,200 to a known payee", 1, 120_000, approve=False)
        r2 = await cycle("run 2: USD 1,500 more to the same payee this period", 2, 150_000, approve=True)
        r3a = await cycle("run 3a: USD 2,600 single payout, nobody approves", 3, 260_000, approve=False)
        r3b = await cycle("run 3b: the same, a finance manager approves", 3, 260_000, approve=True)
        ok = (r1["decision"] == "APPROVED" and r1["execution"]["status"] == "COMPLETED"
              and "payee_period_limit_exceeded" in r2["requires_approval"] and r2["execution"]["status"] == "COMPLETED"
              and "single_payment_above_limit" in r3a["requires_approval"] and r3a["execution"]["status"] == "REFUSED"
              and r3b["execution"]["status"] == "COMPLETED")
        log["ok"] = ok
        (ROOT / "docs" / "limits_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="finance manager (owner)")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(ap.parse_args().approve)))
