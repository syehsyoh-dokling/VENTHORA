# ruff: noqa: E501
"""Invoice aging and dunning on the real Airwallex sandbox: an overdue invoice is paid with its interest on record, an unfundable
one stops for a person, and a final-notice email raises the book's dunning level.

  python scripts/aging_real.py [--approve NAME]

Fresh books dated today (as_of), ben-saas-vendor (ACTIVE), USD invoices:
  run 1  USD 1,000 due 45 days ago, escalation at 30 days -> paid now: APPROVED, accrued interest reported, COMPLETED
  run 2  USD 1,000 due 45 days ago on a book of USD 5,500 (floor 5,000): ESCALATE, overdue_obligation_unpaid and the
         exposure above its cap -> REFUSED without a person; with one: nothing moves (an escalation is never paid), COMPLETED
  run 3  a final-notice email for an invoice already in the book (in-process intake, dry run): DUPLICATE, level 3 -> the
         obligation's dunning level becomes 3 -> plan with dunning_escalation_level 3 and the invoice fundable: paid, no reason;
         the same with the invoice unfundable: final_notice_unpaid
Writes docs/aging_log_real.json.
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
from venthora.intake.pipeline import Intake, IntakeStore  # noqa: E402
from venthora.orchestrator import Orchestrator  # noqa: E402
from venthora.proofcore.gate import ProofCore  # noqa: E402

load_env()
POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 1_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72, "payout_fees_minor": {"LOCAL:USD": 300},
          "overdue_escalation_days": 30, "max_late_interest_exposure_minor": 500, "dunning_escalation_level": 3}


def book(tag: str, i: int, due: str, usd: int, invoice_no: str | None = None, dunning: int = 0) -> dict:
    oid = f"AGE-{i}-{tag}"
    inv = invoice_no or oid
    return {"case_id": f"aging-real-{tag}{i}", "book_id": f"aging-real-{tag}{i}", "base_currency": "USD", "as_of": dt.date.today().isoformat(),
            "balances": {"USD": usd, "EUR": 0, "GBP": 0}, "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
            "obligations": [{"obligation_id": oid, "amount_minor": 100_000, "currency": "USD", "priority": 8, "due_in_hours": 0, "due_date": due,
                             "beneficiary_id": "ben-saas-vendor", "payee_name": "Cloudloom Software LLC", "payout_method": "LOCAL",
                             "evidence_ref": f"ev-{i}-{tag}", "invoice_fingerprint": f"cloudloom:{inv}", "dunning_level": dunning}],
            "receivables": [], "evidence": [{"ref": f"ev-{i}-{tag}", "subject_id": oid, "source": "ap:invoice", "status": "verified", "summary": "invoice"}],
            "scenarios": [], "policy": POLICY, "actor_role": "treasury_agent"}


async def main(approver: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    today = dt.date.today()
    due45 = (today - dt.timedelta(days=45)).isoformat()
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "as_of": today.isoformat(), "runs": []}
    try:
        async def cycle(name: str, b: dict | TreasuryState, approve: bool, execute: bool = True) -> dict:
            state = b if isinstance(b, TreasuryState) else TreasuryState.model_validate(b)
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            state, _facts = apply_operational_facts(state, store)
            plan = await agent.decide(state)
            requires = next((s.details.get("local", {}).get("requires_approval", []) for s in plan.stages if s.engine == "Policy"), [])
            rows = plan.aging.get("rows", [])
            rec = {"run": name, "decision": plan.decision, "actions": {d.obligation_id: d.action for d in plan.obligations}, "requires_approval": requires,
                   "aging": [{k: r[k] for k in ("obligation_id", "days_overdue", "bucket", "dunning_level", "accrued_interest_minor", "projected_interest_minor")} for r in rows],
                   "exposure_minor": plan.aging.get("exposure_minor")}
            print(f"\n{name}: {plan.decision} {rec['actions']} {requires}  aging {rec['aging']} exposure {rec['exposure_minor']}")
            if execute:
                approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(plan)) if (approve and plan.decision == "REQUIRES_APPROVAL") else None
                result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval))
                rec["execution"] = {"status": result.status, "message": result.message[:200],
                                    "transfers": sum(1 for s in result.steps if s.kind == "transfer" and s.status == "OK")}
                print(f"    execution {result.status}: {result.message[:140]}")
            log["runs"].append(rec)
            return rec

        r1 = await cycle("run 1: overdue 45 d, fundable", book(tag, 1, due45, 2_000_000), approve=False)
        r2a = await cycle("run 2a: overdue 45 d, unfundable, nobody approves", book(tag, 2, due45, 550_000), approve=False)
        r2b = await cycle("run 2b: the same, approved (an escalation is never paid)", book(tag, 2, due45, 550_000), approve=True)
        # run 3: a final-notice email about the invoice in book 3, triaged by the intake pipeline (dry run, in-process)
        inv = f"CL-{tag}"
        b3 = TreasuryState.model_validate(book(tag, 3, (today + dt.timedelta(days=5)).isoformat(), 2_000_000, invoice_no=inv))
        email = (f"Message-ID: <final-{tag}@cloudloom.example>\nDate: {today:%a, %d %b %Y} 09:00:00 +0000\nFrom: Cloudloom Billing <billing@cloudloom.example>\n"
                 f"To: accounts@venthora.example\nSubject: FINAL NOTICE: invoice {inv}\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
                 f"This is our final notice. Invoice {inv} for USD 1,000.00 remains unpaid. Without payment within 7 days we will suspend your account.\n")
        intake = Intake(IntakeStore(str(ROOT / "data" / "runs" / f"aging-intake-{tag}.db")))
        req = intake.ingest(email.encode("utf-8"), book_fingerprints={o.invoice_fingerprint: o.obligation_id for o in b3.obligations}, save=False)
        print(f"\nintake: {req.status} trust {req.trust} dunning L{req.dunning_level} duplicate_of {req.duplicate_of}")
        log["intake"] = {"status": req.status, "trust": req.trust, "dunning_level": req.dunning_level, "duplicate_of": req.duplicate_of}
        s3 = intake.to_state(b3, as_of=today, requests=[req])
        # The plan-only unfundable variant goes first: once 3a has paid the invoice, the books mark it settled (REJECT).
        s3b = s3.model_copy(update={"balances": {"USD": 550_000, "EUR": 0, "GBP": 0}, "book_id": f"aging-real-{tag}3b", "case_id": f"aging-real-{tag}3b"})
        r3b = await cycle("run 3b: after the final notice, unfundable -> final_notice_unpaid (plan only)", s3b, approve=False, execute=False)
        r3a = await cycle("run 3a: after the final notice, fundable -> paid, no reason", s3, approve=False)
        ok = (r1["decision"] == "APPROVED" and r1["execution"]["status"] == "COMPLETED" and r1["aging"][0]["accrued_interest_minor"] == 986
              and "overdue_obligation_unpaid" in r2a["requires_approval"] and "late_interest_exposure_above_limit" in r2a["requires_approval"]
              and r2a["execution"]["status"] == "REFUSED" and r2b["execution"]["status"] == "COMPLETED" and r2b["execution"]["transfers"] == 0
              and req.status == "DUPLICATE" and req.dunning_level == 3 and len(s3.obligations) == 1 and s3.obligations[0].dunning_level == 3
              and r3a["decision"] == "APPROVED" and r3a["execution"]["status"] == "COMPLETED"
              and "final_notice_unpaid" in r3b["requires_approval"])
        log["ok"] = ok
        (ROOT / "docs" / "aging_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="finance manager (owner)")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(ap.parse_args().approve)))
