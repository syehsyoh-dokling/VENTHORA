# ruff: noqa: E501
"""Instalments on the real Airwallex sandbox: a deposit, then the balance, then a second 'balance' that must be refused.

  python scripts/controls_real.py [--approve NAME]

A fresh book with one invoice (contract USD 1,000): run 1 pays the deposit (USD 300) and the balance (USD 700) as two
payouts of the same invoice under the duplicate lock; run 2 presents the balance again and must be refused before any
money moves. Writes docs/controls_log_real.json.
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
from venthora.execution.live import with_live_balances_and_rates  # noqa: E402
from venthora.execution.models import Approval, ExecutionRequest  # noqa: E402
from venthora.execution.store import ExecutionStore  # noqa: E402
from venthora.gateways.airwallex import Airwallex  # noqa: E402
from venthora.orchestrator import Orchestrator  # noqa: E402
from venthora.proofcore.gate import ProofCore  # noqa: E402

load_env()


def book(tag: str, parts: list[tuple[str, int, int]]) -> dict:
    obs = [{"obligation_id": f"INST-{inst}-{tag}", "amount_minor": amount, "currency": "USD", "priority": 8, "due_in_hours": 24,
            "beneficiary_id": "ben-saas-vendor", "payee_name": "Cloudloom Software LLC", "payout_method": "LOCAL",
            "evidence_ref": f"ev-{inst}-{tag}", "invoice_fingerprint": f"cloudloom:CT-{tag}", "installment": inst,
            "contract_total_minor": 100_000, "paid_so_far_minor": paid} for inst, amount, paid in parts]
    return {"case_id": f"controls-real-{tag}", "book_id": f"controls-real-{tag}", "base_currency": "USD",
            "balances": {"USD": 2_000_000, "EUR": 0, "GBP": 0}, "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
            "obligations": obs, "receivables": [],
            "evidence": [{"ref": o["evidence_ref"], "subject_id": o["obligation_id"], "source": "ap:invoice", "status": "verified",
                          "summary": f"invoice CT, {o['installment']}"} for o in obs],
            "scenarios": [], "policy": {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 1_000_000,
                                        "swift_fee_currency": "EUR", "swift_fee_minor": 1285, "planning_horizon_hours": 72,
                                        "payout_fees_minor": {"LOCAL:USD": 300}},
            "actor_role": "treasury_agent"}


async def main(approver: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "book": f"controls-real-{tag}", "runs": []}
    try:
        async def cycle(name: str, parts) -> dict:
            state = await with_live_balances_and_rates(TreasuryState.model_validate(book(tag, parts)), bank, ring_fence=True)
            plan = await agent.decide(state)
            rec = {"run": name, "decision": plan.decision, "actions": {d.obligation_id.split("-")[1]: d.action for d in plan.obligations},
                   "match": next((s.details for s in plan.stages if s.engine == "Verity · three-way match"), None)}
            print(f"\n{name}: {plan.decision} {rec['actions']}")
            if plan.decision != "BLOCKED" and any(d.action in ("PAY", "CONVERT_AND_PAY") for d in plan.obligations):
                approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(plan)) if plan.decision == "REQUIRES_APPROVAL" else None
                result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval))
                rec["execution"] = {"status": result.status, "message": result.message,
                                    "transfers": {s.key: s.airwallex_id for s in result.steps if s.kind == "transfer" and s.status == "OK"},
                                    "refused": [s.detail for s in result.steps if s.status == "REFUSED"]}
                print(f"    execution {result.status}: {result.message[:140]}")
            log["runs"].append(rec)
            return rec

        r1 = await cycle("run 1: deposit and balance of one invoice", [("deposit", 30_000, 0), ("balance", 70_000, 30_000)])
        r2 = await cycle("run 2: the balance presented again", [("balance", 70_000, 30_000)])
        ok = (r1["actions"] == {"deposit": "PAY", "balance": "PAY"} and r1.get("execution", {}).get("status") == "COMPLETED"
              and len(r1["execution"]["transfers"]) == 2
              and r2.get("execution", {}).get("status") == "FAILED" and "duplicate payment blocked" in r2["execution"]["message"])
        log["ok"] = ok
        (ROOT / "docs" / "controls_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="finance manager (owner)")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(ap.parse_args().approve)))
