# ruff: noqa: E501
"""Book and tenant segregation on the real Airwallex sandbox: a tenant's book pays only its payees, is signed only by its people, and
runs under its own policy; an unknown book under a strict registry cannot spend.

  python scripts/tenancy_real.py [--approve NAME]

The registry (data/tenants.json) gives the pattern `tenant-real-*` to Acme: payees [ben-saas-vendor], approver "finance manager
(owner)" (finance_manager), override max_single_payment USD 500 (so every payout here stops for a person). Fresh books:
  run 1  USD 1,000 to ben-saas-vendor, approved by the book's approver -> COMPLETED
  run 2  the same, approved by "Pat Lee" (finance_manager, not this book's person) -> REFUSED
  run 3  USD 1,000 to ben-eu-components (another tenant's payee) -> ESCALATE, nothing moves even with the approval
  run 4  plan only: the same book id resolved against a STRICT copy of the registry with the pattern removed -> BLOCKED
Writes docs/tenancy_log_real.json.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
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
from venthora.tenancy import attach, load_registry  # noqa: E402

load_env()
POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 1_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72, "payout_fees_minor": {"LOCAL:USD": 300, "SWIFT:EUR": 1343},
          "approver_roles": ["finance_manager"]}


def book(tag: str, i: int, ben: str, ccy: str, amount: int) -> dict:
    oid = f"TEN-{i}-{tag}"
    return {"case_id": f"tenant-real-{tag}{i}", "book_id": f"tenant-real-{tag}{i}", "base_currency": "USD",
            "balances": {"USD": 2_000_000, "EUR": 0, "GBP": 0}, "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
            "obligations": [{"obligation_id": oid, "amount_minor": amount, "currency": ccy, "priority": 8, "due_in_hours": 24,
                             "beneficiary_id": ben, "payee_name": ben, "payout_method": "LOCAL" if ccy == "USD" else "SWIFT",
                             "evidence_ref": f"ev-{i}-{tag}", "invoice_fingerprint": f"{ben}:{oid}"}],
            "receivables": [], "evidence": [{"ref": f"ev-{i}-{tag}", "subject_id": oid, "source": "ap:invoice", "status": "verified", "summary": "invoice"}],
            "scenarios": [], "policy": POLICY, "actor_role": "treasury_agent"}


async def main(approver: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "runs": []}
    try:
        async def cycle(name: str, b: dict, approvals: list[tuple[str, str]], execute: bool = True, registry: dict | None = None) -> dict:
            state = TreasuryState.model_validate(b)
            if registry is not None:
                state = attach(state, registry)
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            state, _facts = apply_operational_facts(state, store)
            plan = await agent.decide(state)
            ten = next((s.details for s in plan.stages if s.engine == "Tenancy · book"), {})
            requires = next((s.details.get("local", {}).get("requires_approval", []) for s in plan.stages if s.engine == "Policy"), [])
            rec = {"run": name, "book": state.book_id, "decision": plan.decision, "actions": {d.obligation_id: d.action for d in plan.obligations},
                   "requires_approval": requires, "tenant": ten.get("tenant"), "matched_by": ten.get("matched_by"), "book_known": ten.get("book_known"),
                   "disallowed": ten.get("disallowed_obligations"), "single_limit": state.policy.max_single_payment_minor}
            print(f"\n{name}: {plan.decision} {rec['actions']} {requires}  tenant {ten.get('tenant')} ({ten.get('matched_by')}) known {ten.get('book_known')} disallowed {ten.get('disallowed_obligations')}")
            if execute:
                aps = [Approval(approver=a, role=r, terms_digest=terms_digest(plan)) for a, r in approvals]
                result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approvals=aps, requested_by="treasury agent"))
                rec["execution"] = {"status": result.status, "message": result.message[:220],
                                    "transfers": sum(1 for s in result.steps if s.kind == "transfer" and s.status == "OK")}
                print(f"    execution {result.status}: {result.message[:160]}")
            log["runs"].append(rec)
            return rec

        fm = (approver, "finance_manager")
        r1 = await cycle("run 1: the book's own payee, the book's approver", book(tag, 1, "ben-saas-vendor", "USD", 100_000), [fm])
        r2 = await cycle("run 2: the same, approved by a finance manager who is not this book's person", book(tag, 2, "ben-saas-vendor", "USD", 100_000), [("Pat Lee", "finance_manager")])
        r3 = await cycle("run 3: another tenant's payee, with the approval attached", book(tag, 3, "ben-eu-components", "EUR", 100_000), [fm])
        strict = copy.deepcopy(load_registry())
        strict["strict"] = True
        strict["tenants"]["acme-group"]["books"].pop("tenant-real-*", None)
        r4 = await cycle("run 4: the same book id under a strict registry that does not list it (plan only)", book(tag, 4, "ben-saas-vendor", "USD", 100_000), [], execute=False, registry=strict)
        ok = (r1["tenant"] == "acme-group" and r1["single_limit"] == 50_000 and r1["decision"] == "REQUIRES_APPROVAL" and r1["execution"]["status"] in ("COMPLETED", "IN_FLIGHT") and r1["execution"]["transfers"] == 1
              and r2["execution"]["status"] == "REFUSED" and "not an approver of this book" in r2["execution"]["message"]
              and r3["actions"] and all(a == "ESCALATE" for a in r3["actions"].values()) and r3["execution"]["transfers"] == 0
              and r4["decision"] == "BLOCKED" and r4["book_known"] is False)
        log["ok"] = ok
        (ROOT / "docs" / "tenancy_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="finance manager (owner)")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(ap.parse_args().approve)))
