# ruff: noqa: E501
"""Payee lifecycle on the real Airwallex sandbox: a bank change stops every payment until a call-back, then cooling, then ACTIVE.

  python scripts/payees_real.py [--approve NAME]

ben-saas-vendor (Cloudloom, ACTIVE since onboarding) on a fresh book, USD 1,000 invoices:
  step 1  a "new IBAN" email-style change is recorded -> CHANGED, cooling 72 h. Plan: the invoice is ESCALATED, nothing is paid;
          an execution with a finance manager's approval is still REFUSED (no approval can pay a CHANGED payee).
  step 2  call-backs that must fail: a number taken from the request (refused), a viewer (refused).
  step 3  the call-back on the registered number by a finance manager -> VERIFIED, cooling kept. Plan: REQUIRES_APPROVAL
          payee_in_cooling_period; without approval REFUSED; with approval COMPLETED; the payee stays VERIFIED (inside cooling).
  step 4  a second change with cooling 0 h (what the policy allows the admin to set), call-back -> VERIFIED, cooling over at once.
          Plan: REQUIRES_APPROVAL payee_not_yet_active; approved -> COMPLETED -> the payee becomes ACTIVE.
  step 5  the next invoice is paid autonomously (APPROVED, COMPLETED).
Writes docs/payees_log_real.json. The payee ends ACTIVE on the details recorded in step 4 (the sandbox beneficiary is unchanged:
the fingerprint is what the registry tracks).
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
from venthora.payees import PayeeError  # noqa: E402
from venthora.proofcore.gate import ProofCore  # noqa: E402

load_env()
PAYEE = "ben-saas-vendor"
POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 1_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72, "payout_fees_minor": {"LOCAL:USD": 300}}


def book(tag: str, i: int, amount: int) -> dict:
    return {"case_id": f"payees-real-{tag}", "book_id": f"payees-real-{tag}", "base_currency": "USD",
            "balances": {"USD": 2_000_000, "EUR": 0, "GBP": 0}, "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
            "obligations": [{"obligation_id": f"PAY-{i}-{tag}", "amount_minor": amount, "currency": "USD", "priority": 8, "due_in_hours": 24,
                             "beneficiary_id": PAYEE, "payee_name": "Cloudloom Software LLC", "payout_method": "LOCAL",
                             "evidence_ref": f"ev-{i}-{tag}", "invoice_fingerprint": f"cloudloom:PAY-{i}-{tag}"}],
            "receivables": [], "evidence": [{"ref": f"ev-{i}-{tag}", "subject_id": f"PAY-{i}-{tag}", "source": "ap:invoice", "status": "verified", "summary": "invoice"}],
            "scenarios": [], "policy": POLICY, "actor_role": "treasury_agent"}


async def main(approver: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "book": f"payees-real-{tag}", "payee": PAYEE, "steps": []}
    start = store.payee(PAYEE)
    print(f"{PAYEE} before: {start['status']} fp {start['bank_fingerprint']} paid {start['paid_count']}")
    log["before"] = start
    phone = start["registered_phone"]

    def event(name: str, ev: dict, **kw) -> dict:
        try:
            rec = store.payee_event(PAYEE, ev, **kw)
            out = {"step": name, "event": ev["kind"], "outcome": "APPLIED", "status": rec["status"], "cooling_until": rec["cooling_until"]}
        except PayeeError as exc:
            out = {"step": name, "event": ev["kind"], "outcome": "REFUSED", "reason": str(exc), "status": store.payee(PAYEE)["status"]}
        print(f"\n{name}: {out['outcome']} -> {out['status']}" + (f" ({out.get('reason')})" if out.get("reason") else ""))
        log["steps"].append(out)
        return out

    try:
        async def cycle(name: str, i: int, approve: bool) -> dict:
            state = TreasuryState.model_validate(book(tag, i, 100_000))
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            state, _facts = apply_operational_facts(state, store)
            plan = await agent.decide(state)
            actions = {d.obligation_id: d.action for d in plan.obligations}
            requires = next((s.details.get("local", {}).get("requires_approval", []) for s in plan.stages if s.engine == "Policy"), [])
            life = next((s.summary for s in plan.stages if s.engine == "Payee · lifecycle"), None)
            rec = {"step": name, "decision": plan.decision, "actions": actions, "requires_approval": requires, "lifecycle": life,
                   "registry_status": store.payee(PAYEE)["status"]}
            print(f"\n{name}: {plan.decision} {actions} {requires}  [{life}]")
            approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(plan)) if approve else None
            # Sandbox transfers stay SENT for a while; the sandbox simulation (SENT -> PAID) settles them, because a payee becomes
            # ACTIVE only on a PAID payout (a create response, or an in-flight transfer, is never proof of payment).
            result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval, simulate_settlement=True))
            rec["execution"] = {"status": result.status, "message": result.message[:200],
                                "transfers": sum(1 for s in result.steps if s.kind == "transfer" and s.status == "OK")}
            rec["registry_after"] = store.payee(PAYEE)["status"]
            print(f"    execution {result.status}: {result.message[:140]}  -> registry {rec['registry_after']}")
            log["steps"].append(rec)
            return rec

        # step 1: the change
        c1 = event("1 · bank change announced (new IBAN in an email)", {"kind": "bank_change", "bank_fingerprint": f"iban:DE-new-{tag}", "source": "payees_real: supplier email"})
        r1 = await cycle("1 · plan + approved execution against a CHANGED payee", 1, approve=True)
        # step 2: call-backs that must fail
        c2a = event("2a · call-back to the number printed in the email", {"kind": "verify", "by": approver, "role": "finance_manager", "called_number": "+49 30 555 0101"})
        c2b = event("2b · call-back by a viewer", {"kind": "verify", "by": "viewer", "role": "viewer", "called_number": phone})
        # step 3: the real call-back, cooling kept
        c3 = event("3 · call-back on the registered number by a finance manager", {"kind": "verify", "by": approver, "role": "finance_manager", "called_number": phone})
        r3a = await cycle("3a · plan inside the cooling period, nobody approves", 2, approve=False)
        r3b = await cycle("3b · the same, approved", 2, approve=True)
        # step 4: a change with cooling 0 (admin policy), call-back, approved first payment -> ACTIVE
        c4 = event("4 · second change, cooling 0 h", {"kind": "bank_change", "bank_fingerprint": f"iban:DE-final-{tag}", "source": "payees_real: portal"}, cooling_hours=0)
        c4b = event("4b · call-back on the registered number", {"kind": "verify", "by": approver, "role": "finance_manager", "called_number": phone})
        r4 = await cycle("4c · first payment on the new details, approved", 3, approve=True)
        # step 5: autonomous
        r5 = await cycle("5 · next invoice, autonomous", 4, approve=False)
        ok = (c1["status"] == "CHANGED" and r1["actions"] and all(a == "ESCALATE" for a in r1["actions"].values()) and r1["execution"]["transfers"] == 0
              and c2a["outcome"] == "REFUSED" and c2b["outcome"] == "REFUSED" and c3["status"] == "VERIFIED" and c3["cooling_until"]
              and "payee_in_cooling_period" in r3a["requires_approval"] and r3a["execution"]["status"] == "REFUSED"
              and r3b["execution"]["status"] == "COMPLETED" and r3b["registry_after"] == "VERIFIED"
              and c4["status"] == "CHANGED" and c4b["status"] == "VERIFIED"
              and "payee_not_yet_active" in r4["requires_approval"] and r4["execution"]["status"] == "COMPLETED" and r4["registry_after"] == "ACTIVE"
              and r5["decision"] == "APPROVED" and r5["execution"]["status"] == "COMPLETED")
        log["ok"] = ok
        log["after"] = store.payee(PAYEE)
        (ROOT / "docs" / "payees_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="finance manager (owner)")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(ap.parse_args().approve)))
