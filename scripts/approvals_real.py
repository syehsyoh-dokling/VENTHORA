# ruff: noqa: E501
"""The approval matrix (four-eyes) on the real Airwallex sandbox: how many different people a plan needs before money moves.

  python scripts/approvals_real.py [--fm NAME] [--cfo NAME]

Policy: every payout above USD 500 stops for a person (max_single_payment); the matrix says one finance manager up to
USD 1,500 of paying total, two different people (finance manager or CFO) from USD 1,500. Fresh book, ben-saas-vendor (ACTIVE).
  run 1  USD 1,000: tier 'one' -> one finance manager -> COMPLETED
  run 2  USD 2,000: tier 'two' ->
         2a one approval                     REFUSED (needs 2 different people)
         2b the same person twice            REFUSED (one person counts once)
         2c the requester among the approvers REFUSED (four-eyes)
         2d finance manager + CFO            COMPLETED, both named in the authorization step
Writes docs/approvals_log_real.json.
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
MATRIX = [{"name": "one", "min_total_minor": 0, "reasons": [], "approvals_required": 1, "roles": ["finance_manager"]},
          {"name": "two", "min_total_minor": 150_000, "reasons": ["payee_in_cooling_period", "payee_not_yet_active"], "approvals_required": 2, "roles": ["finance_manager", "cfo"]}]
POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 1_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72, "payout_fees_minor": {"LOCAL:USD": 300},
          "max_single_payment_minor": 50_000, "approver_roles": ["finance_manager", "cfo"], "approval_matrix": MATRIX}


def book(tag: str, i: int, amount: int) -> dict:
    return {"case_id": f"approvals-real-{tag}", "book_id": f"approvals-real-{tag}", "base_currency": "USD",
            "balances": {"USD": 2_000_000, "EUR": 0, "GBP": 0}, "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
            "obligations": [{"obligation_id": f"APR-{i}-{tag}", "amount_minor": amount, "currency": "USD", "priority": 8, "due_in_hours": 24,
                             "beneficiary_id": "ben-saas-vendor", "payee_name": "Cloudloom Software LLC", "payout_method": "LOCAL",
                             "evidence_ref": f"ev-{i}-{tag}", "invoice_fingerprint": f"cloudloom:APR-{i}-{tag}"}],
            "receivables": [], "evidence": [{"ref": f"ev-{i}-{tag}", "subject_id": f"APR-{i}-{tag}", "source": "ap:invoice", "status": "verified", "summary": "invoice"}],
            "scenarios": [], "policy": POLICY, "actor_role": "treasury_agent"}


async def main(fm: str, cfo: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "book": f"approvals-real-{tag}", "matrix": MATRIX, "runs": []}
    try:
        async def cycle(name: str, i: int, amount: int, approvers: list[tuple[str, str]], requested_by: str = "treasury agent (scheduled run)") -> dict:
            state = TreasuryState.model_validate(book(tag, i, amount))
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            state, _facts = apply_operational_facts(state, store)
            plan = await agent.decide(state)
            req = plan.approval_requirement or {}
            rec = {"run": name, "decision": plan.decision, "tier": req.get("tier"), "approvals_required": req.get("approvals_required"),
                   "roles": req.get("roles"), "approvers": [a for a, _ in approvers], "requested_by": requested_by}
            print(f"\n{name}: {plan.decision} tier {req.get('tier')} needs {req.get('approvals_required')} ({req.get('roles')})")
            approvals = [Approval(approver=a, role=r, terms_digest=terms_digest(plan)) for a, r in approvers]
            result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approvals=approvals, requested_by=requested_by))
            rec["execution"] = {"status": result.status, "message": result.message[:220],
                                "approved_by": result.steps[0].detail.get("approved_by") if result.steps else None}
            print(f"    execution {result.status}: {result.message[:160]}")
            log["runs"].append(rec)
            return rec

        r1 = await cycle("run 1: USD 1,000, one finance manager", 1, 100_000, [(fm, "finance_manager")])
        r2a = await cycle("run 2a: USD 2,000, one approval", 2, 200_000, [(fm, "finance_manager")])
        r2b = await cycle("run 2b: USD 2,000, the same person twice", 2, 200_000, [(fm, "finance_manager"), (fm, "finance_manager")])
        r2c = await cycle("run 2c: USD 2,000, the requester approves", 2, 200_000, [(fm, "finance_manager"), (cfo, "cfo")], requested_by=fm)
        r2d = await cycle("run 2d: USD 2,000, finance manager + CFO", 2, 200_000, [(fm, "finance_manager"), (cfo, "cfo")])
        ok = (r1["tier"] == "one" and r1["execution"]["status"] == "COMPLETED"
              and r2a["tier"] == "two" and r2a["execution"]["status"] == "REFUSED" and "different people" in r2a["execution"]["message"]
              and r2b["execution"]["status"] == "REFUSED" and "twice" in r2b["execution"]["message"]
              and r2c["execution"]["status"] == "REFUSED" and "four-eyes" in r2c["execution"]["message"]
              and r2d["execution"]["status"] == "COMPLETED" and r2d["execution"]["approved_by"] == [fm, cfo])
        log["ok"] = ok
        (ROOT / "docs" / "approvals_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fm", default="finance manager (owner)")
    ap.add_argument("--cfo", default="CFO (owner, second signature)")
    sys.stdout.reconfigure(encoding="utf-8")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.fm, a.cfo)))
