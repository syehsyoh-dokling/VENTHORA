# ruff: noqa: E501
"""Budget · mandate on the real Airwallex sandbox: the agent stops at the line, asks for mandate, a person grants, it continues.

  python scripts/budget_real.py [--approve NAME]

A fresh book (new obligation ids and invoice fingerprints) with its own period and one line, so nothing already paid
is touched and the realized spend starts at zero:
  run 1  line 'suppliers' mandate USD 5,000; O1 USD 3,000 (priority 10) and O2 USD 3,000 (priority 5)
         -> O1 PAY, O2 ESCALATE with a BUDGET REQUEST for 1,000; executed on the sandbox (real transfer)
  run 2  the store now holds realized 3,000 (+ fee) on the line -> O2 still cannot be funded; request stands
  grant  a finance manager grants USD 1,100 on the line (role-checked, audited)
  run 3  mandate 6,100 - realized -> O2 fits -> PAY; executed on the sandbox
Writes docs/budget_log_real.json.
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


def book(tag: str) -> dict:
    def ob(i, amount, priority):
        return {"obligation_id": f"BUD-O{i}-{tag}", "amount_minor": amount, "currency": "USD", "priority": priority, "due_in_hours": 24,
                "beneficiary_id": "ben-saas-vendor", "payee_name": "Cloudloom Software LLC", "payout_method": "LOCAL",
                "evidence_ref": f"ev-{i}-{tag}", "invoice_fingerprint": f"cloudloom:BUD-{i}-{tag}", "budget_line": "suppliers"}
    obs = [ob(1, 300_000, 10), ob(2, 300_000, 5)]
    return {"case_id": f"budget-real-{tag}", "book_id": f"budget-real-{tag}", "base_currency": "USD",
            "balances": {"USD": 2_000_000, "EUR": 0, "GBP": 0}, "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
            "obligations": obs, "receivables": [],
            "evidence": [{"ref": o["evidence_ref"], "subject_id": o["obligation_id"], "source": "ap:invoice", "status": "verified",
                          "summary": "invoice"} for o in obs],
            "scenarios": [], "policy": {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 1_000_000,
                                        "swift_fee_currency": "EUR", "swift_fee_minor": 1285, "planning_horizon_hours": 72,
                                        "payout_fees_minor": {"LOCAL:USD": 300}},
            "actor_role": "treasury_agent",
            "budget": {"period": f"BUD-{tag}", "lines": [{"line": "suppliers", "amount_minor": 500_000, "auto_share_bp": 10_000,
                                                            "alert_share_bp": 8_000}]}}


def summarize(plan) -> dict:
    line = plan.budget["lines"][0]
    return {"decision": plan.decision, "actions": {d.obligation_id.split("-")[1]: d.action for d in plan.obligations},
            "line": {k: line[k] for k in ("mandate_minor", "realized_minor", "paying_now_minor", "committed_minor", "available_minor", "minus_minor", "utilisation_bp", "status")},
            "alerts": [a["message"] for a in plan.budget["alerts"]], "requests": [q["message"] for q in plan.budget["requests"]],
            "requires_approval": next((s.details.get("local", {}).get("requires_approval", []) for s in plan.stages if s.engine == "Policy"), [])}


async def main(approver: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "book": f"budget-real-{tag}", "runs": []}
    try:
        async def cycle(name: str, execute: bool) -> dict:
            state = TreasuryState.model_validate(book(tag))
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            state, facts = apply_operational_facts(state, store)
            plan = await agent.decide(state)
            rec = {"run": name, "facts": facts, **summarize(plan)}
            print(f"\n{name}: {plan.decision} {rec['actions']}  line {rec['line']}")
            for m in rec["alerts"] + rec["requests"]:
                print("   ", m)
            if execute and plan.decision != "BLOCKED" and any(d.action in ("PAY", "CONVERT_AND_PAY") for d in plan.obligations):
                approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(plan)) if plan.decision == "REQUIRES_APPROVAL" else None
                result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval))
                rec["execution"] = {"status": result.status, "message": result.message,
                                    "transfers": [s.airwallex_id for s in result.steps if s.kind == "transfer" and s.status == "OK"]}
                print(f"    execution {result.status}: {result.message[:120]}")
            log["runs"].append(rec)
            return rec

        r1 = await cycle("run 1: plan and pay inside the line", True)
        r2 = await cycle("run 2: realized spend now on the line", False)
        grant = store.grant(f"BUD-{tag}", "suppliers", 110_000, approver, "finance_manager", "budget request: O2 still owed")
        await executor.audit_async("budget_grant", grant)
        log["grant"] = grant
        print(f"\ngrant: +USD 1,100.00 on 'suppliers' by {approver}")
        r3 = await cycle("run 3: after the grant", True)
        ok = (r1["actions"] == {"O1": "PAY", "O2": "ESCALATE"} and r1["requests"] and r1.get("execution", {}).get("status") == "COMPLETED"
              and r2["line"]["realized_minor"] >= 300_000 and r2["actions"]["O2"] == "ESCALATE"
              and r3["actions"]["O2"] == "PAY" and r3.get("execution", {}).get("status") == "COMPLETED")
        log["ok"] = ok
        (ROOT / "docs" / "budget_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="finance manager (owner)")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(ap.parse_args().approve)))
