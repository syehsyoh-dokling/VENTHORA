"""venthora-intake: read external-user emails, verify, queue, and run the treasury loop on the result.

  venthora-intake --dir data/intake --scenario kit01_baseline --as-of 2026-10-24 --fake --execute
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

import httpx

from ..audit import AuditStore
from ..config import ROOT, load_env
from ..domain.models import TreasuryState
from ..domain.money import fmt
from ..execution.executor import Executor, terms_digest
from ..execution.models import Approval, ExecutionRequest
from ..execution.store import ExecutionStore
from ..gateways.airwallex import SANDBOX_URL, Airwallex
from ..orchestrator import Orchestrator
from ..proofcore.gate import ProofCore
from .pipeline import Intake, IntakeStore


def line(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 74 - len(title)))


async def run(args) -> int:
    load_env()
    scenario = json.loads((ROOT / "data" / "scenarios" / f"{args.scenario}.json").read_text(encoding="utf-8"))
    base = TreasuryState.model_validate(scenario["state"])
    if args.fake:  # the fees the real sandbox taught us (docs/SANDBOX_RESULTS.md); live runs learn them from the store
        from ..testing.fake_airwallex import REAL_SANDBOX_FEES
        fees = {f"{m}:{c}": int(f * 100) for (m, c), f in REAL_SANDBOX_FEES.items()}
        base = base.model_copy(update={"policy": base.policy.model_copy(update={"payout_fees_minor": fees})})
    as_of = dt.date.fromisoformat(args.as_of)
    store = IntakeStore(args.db) if args.db else IntakeStore()
    intake = Intake(store)

    line("1 · read the inbox: external users, five mailboxes, three providers")
    book_fps = {o.invoice_fingerprint: o.obligation_id for o in base.obligations if o.invoice_fingerprint}
    reqs = intake.ingest_dir(Path(args.dir), book_fingerprints=book_fps)
    for r in reqs:
        amt = fmt(r.amount_minor, r.currency) if r.amount_minor else "-"
        print(f"  [{r.status:<12}] {r.sender:<38} {r.kind:<17} {amt:>14}  {r.invoice_no or '-':<12} {r.trust}")
        for reason in r.reasons:
            print(f"      · {reason}")
        for q in r.quotes:
            if q.field in ("amount", "invoice_no", "due_date", "bank_change", "urgency"):
                print(f"      quote[{q.field}] \"{q.quote}\"")

    line("2 · the queue becomes part of the book (verified = payable; everything else visible, unpayable)")
    state = intake.to_state(base, as_of=as_of)
    if args.fresh:
        from ..execution.fresh import fresh_book, fresh_tag
        state = fresh_book(state, fresh_tag())
        print(f"  fresh book {state.book_id}: new obligation ids and invoice fingerprints, same amounts and evidence")
    added = [o for o in state.obligations if o.obligation_id.startswith("REQ-")]
    for o in added:
        ev = next(e for e in state.evidence if e.ref == o.evidence_ref)
        print(f"  {o.obligation_id:<22} {fmt(o.amount_minor, o.currency):>14}  {o.payee_name:<28} due {o.due_in_hours:>3}h"
              f"  evidence {ev.status}{'  flags ' + ','.join(o.policy_flags) if o.policy_flags else ''}")
    for rcv in state.receivables:
        if rcv.evidence_ref and rcv.evidence_ref.startswith("intake:"):
            print(f"  receivable {rcv.receivable_id}: {fmt(rcv.amount_minor, rcv.currency)} from {rcv.payer} in "
                  f"{rcv.expected_in_hours}h, confidence {rcv.confidence_bp / 100:.0f}% before calibration")

    if args.fake:
        from ..testing.fake_airwallex import REAL_SANDBOX_FEES, FakeAirwallex
        fake = FakeAirwallex(balances={c: f"{v / 100:.2f}" for c, v in state.balances.items()},
                             rates={f"{c}USD": r for c, r in state.fx_to_base.items()})
        fake.fees = dict(REAL_SANDBOX_FEES)
        client = httpx.AsyncClient(base_url=SANDBOX_URL, transport=httpx.MockTransport(fake.handler))
        bank = Airwallex(base_url=SANDBOX_URL, client=client, backoff_s=0)
    else:
        bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    if args.fake:  # the fake wallet starts fresh, so its books must too (not the real sandbox's ledger)
        scratch = ROOT / "data" / "runs" / "intake-fake"
        scratch.mkdir(parents=True, exist_ok=True)
        for old in scratch.glob("*.db"):
            old.unlink()
        executor = Executor(agent, ExecutionStore(str(scratch / "execution.db")), AuditStore(str(scratch / "audit.db")),
                            poll_interval_s=0)
    else:
        executor = Executor(agent, ExecutionStore(), AuditStore(), poll_interval_s=1.0)
    try:
        if not args.fake:
            from ..execution.facts import apply_operational_facts
            from ..execution.live import with_live_balances_and_rates
            line("2b · ground the book in the live sandbox and what earlier cycles taught")
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            state, facts = apply_operational_facts(state, executor.store)
            for note in facts:
                print(f"  {note}")
            print("  budget: " + ", ".join(fmt(v, c) for c, v in sorted(state.balances.items())))
        line("3 · plan and proof over the whole book")
        plan = await agent.decide(state)
        if args.receipt:
            from ..cli import simulate_receipt
            line("3b · the customer's remittance lands (sandbox deposit), re-plan only what it changes")
            rid = next((r.receivable_id for r in state.receivables if (r.evidence_ref or "").startswith("intake:")), None)
            previous = state
            state, dep = await simulate_receipt(bank, state, executor.store, fake=args.fake, poll_s=0 if args.fake else 1.0,
                                                receivable_id=rid)
            if dep and dep.get("landed"):
                plan = await agent.decide(state, previous=(previous, plan))
                rc = plan.recalculation
                print(f"  recalculation: {rc.mode}; locked {len(rc.locked)}, reconsidered {len(rc.reconsidered)}; changes: "
                      + (", ".join(f"{c.obligation_id} {c.before}→{c.after}" for c in rc.changes) or "none"))
        print(f"  decision: {plan.decision}   plan_hash: {plan.plan_hash[:16]}…")
        for s in plan.stages:
            print(f"  [{s.status:<7}] {s.engine:<28} {s.summary[:90]}")
        for d in plan.obligations:
            print(f"  {d.action:<17} {d.obligation_id:<22} {fmt(d.amount_minor, d.currency):>14}  {d.reason[:80]}")
        report = {"as_of": args.as_of, "intake": [r.model_dump(exclude={'text'}) for r in reqs],
                  "plan": {"decision": plan.decision, "plan_hash": plan.plan_hash,
                           "actions": {d.obligation_id: d.action for d in plan.obligations}}}
        if args.execute and plan.decision != "BLOCKED":
            line("4 · execute and reconcile")
            approval = None
            if plan.decision == "REQUIRES_APPROVAL":
                if not args.approve:
                    print("  REQUIRES_APPROVAL and no --approve given: nothing executed")
                    return 2
                approval = Approval(approver=args.approve, role="finance_manager", terms_digest=terms_digest(plan))
            result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval))
            for s in result.steps:
                print(f"  [{s.status:<9}] {s.kind:<11} {s.key:<24} {s.airwallex_id or ''}")
            print(f"  {result.status}: {result.message}")
            report["execution"] = {"status": result.status, "message": result.message}
        out = ROOT / "data" / "runs" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-intake{'-fake' if args.fake else ''}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"\nrun report: {out}")
        return 0
    finally:
        await agent.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="venthora-intake")
    ap.add_argument("--dir", default=str(ROOT / "data" / "intake"))
    ap.add_argument("--scenario", default="kit01_initial", help="the book the queue is added to")
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--db", help="intake database path (default data/venthora_intake.db)")
    ap.add_argument("--fake", action="store_true", help="in-process fake sandbox")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--receipt", action="store_true", help="simulate the verified remittance landing, then re-plan")
    ap.add_argument("--fresh", action="store_true", help="run as a fresh book (new ids and fingerprints) to repeat the cycle")
    ap.add_argument("--approve", metavar="NAME")
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
