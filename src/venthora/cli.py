"""`venthora-sandbox`: run the full Kit 01 loop against the Airwallex sandbox.

    read inbox (LLM + Verity) -> ground in live balances/rates -> Horizon FX stress ->
    plan (Theorem) -> ProofCore -> human approval -> execute on Airwallex -> reconcile

Examples
    venthora-sandbox --fake                                  # offline, in-process fake sandbox
    venthora-sandbox --receipt --approve "Dana"              # real sandbox (.env credentials)
    venthora-sandbox --plan-only                             # stop after the verified plan

LLM: set MODEL_ENDPOINT/MODEL_NAME (e.g. scripts/local_llm_server.py on :8001). Without one,
the deterministic lexicon reader is used. Nothing here can run against production: the
executor refuses any non-sandbox Airwallex URL.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import sys
import uuid
from pathlib import Path

import httpx

from .audit import AuditStore
from .config import ROOT, env, load_env
from .data.ecb import ecb_path
from .domain.models import TreasuryState
from .domain.money import fmt
from .engines.llm_planner import llm_proposal, naive_proposal
from .evidence.explain import explain_exceptions
from .evidence.llm import ChatModel
from .evidence.reader import InboxMessage, read_inbox
from .execution.executor import Executor, terms_digest
from .execution.facts import apply_operational_facts
from .execution.incidents import IncidentCommander, IncidentRequest
from .execution.live import with_live_balances_and_rates
from .execution.models import Approval, ExecutionRequest
from .execution.store import ExecutionStore
from .gateways.airwallex import SANDBOX_URL, Airwallex, to_minor
from .orchestrator import Orchestrator
from .proofcore.gate import ProofCore

ORIGINALS = ROOT / "src" / "venthora" / "originals"


def line(title: str = "") -> None:
    print(f"\n── {title} " + "─" * max(0, 72 - len(title)) if title else "─" * 76)


def originals_status() -> dict[str, bool]:
    text = (ORIGINALS / "PROVENANCE.md").read_text(encoding="utf-8")
    out = {}
    for rel in ("theorem_qf/fx.py", "horizon_core/m0_exact.py", "verity_core/corpus.py",
                "verity_core/retriever.py", "verity_core/validators.py"):
        digest = hashlib.sha256((ORIGINALS / rel).read_bytes()).hexdigest()
        out[rel] = digest in text
    return out


async def model_status(model: ChatModel) -> str:
    if not model.configured:
        return "not configured -> lexicon fallback"
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            base = model.endpoint.removesuffix("/chat/completions").removesuffix("/v1")
            res = await c.get(f"{base}/v1/models")
        return f"{model.model} @ {model.endpoint} ({'reachable' if res.status_code < 500 else res.status_code})"
    except httpx.HTTPError as exc:
        return f"{model.model} @ {model.endpoint} (UNREACHABLE: {type(exc).__name__}) -> lexicon fallback"


def print_plan(plan, *, counterfactuals: bool = True) -> None:
    print(f"decision: {plan.decision}    plan_hash: {plan.plan_hash[:16]}…    solver: {plan.solver}")
    for s in plan.stages:
        print(f"  [{s.status:<7}] {s.engine:<28} {s.summary}")
    if plan.recalculation:
        r = plan.recalculation
        print(f"\n  recalculation: {r.mode}; triggered by {', '.join(r.triggers) or 'nothing'}")
        if r.locked:
            print(f"    kept (unaffected): {', '.join(r.locked)}   re-optimised: {', '.join(r.reconsidered) or '-'}")
        if r.fallback_reason:
            print(f"    {r.fallback_reason}")
        for c in r.changes:
            print(f"    CHANGED {c.obligation_id}: {c.before} -> {c.after} ({c.reason})")
        if not r.changes:
            print("    no decision changed")
    print()
    for text in plan.explanation:
        print(f"  • {text}")
    if counterfactuals and plan.counterfactuals:
        print("\n  counterfactual proof (same invariants as the plan):")
        for c in plan.counterfactuals:
            risk = f"  P(breach) {c.breach_probability_bp / 100:.1f}%" if c.breach_probability_bp is not None else ""
            print(f"    {'✓' if c.feasible else '✗'} {c.question:<48} {c.consequence}{risk}")
    sim = plan.simulation
    if sim:
        print(f"\n  72h simulation ({sim['fx_model']}): P(reserve breached) {sim['breach_probability_bp'] / 100:.1f}% "
              f"over {sim['paths']:,} paths; min balance p05 {fmt(sim['min_balance_p05_minor'], 'USD')}")
        print(f"    {sim['events']} scheduled events; {sim['path_events']:,} path-events applied in {sim['loop_ms']:.1f} ms "
              f"= {sim['path_events_per_second']:,}/s (vectorised numpy, wall clock; FX setup {sim['fx_setup_ms']:.0f} ms "
              "excluded)")
        for oid, p in sim["unfunded_probability_bp"].items():
            print(f"    P({oid} unfunded at due) {p / 100:.1f}%")


async def print_exception_notes(state, plan, model) -> list[dict]:
    notes = await explain_exceptions(state, plan, model)
    if notes:
        print("\n  notes for the person who decides each escalation:")
    for n in notes:
        print(f"    {n['obligation_id']} [{n['author']}]: {n['note']}")
    return notes


def print_approval_terms(plan) -> None:
    """Guide: bind each approval to the amount, currency, counterparty, terms and evidence the approver saw."""
    print("  terms of this plan:")
    for t in plan.approval_terms:
        if t["kind"] == "payout":
            ev = t["evidence"]
            print(f"    pay   {t['amount']:>16}  to {t['counterparty']} ({t['method']}, due {t['due_in_hours']}h)"
                  f"  evidence {ev['ref']} [{ev['source']}]: {ev['summary']}")
        else:
            print(f"    buy   {t['buy']:>16}  for about {t['estimated_cost']}  ({t['terms']})")
    reasons = next((s.details.get("local", {}).get("requires_approval") for s in plan.stages if s.engine == "Policy"), [])
    if reasons:
        print(f"  why a person is needed: {', '.join(reasons)}")


async def run_incident(executor, state: TreasuryState, spec: str, *, fake: bool) -> dict:
    """`--incident OBLIGATION[:FAILURE_TYPE[:HOURS]]`: fail the in-flight transfer (sandbox) and let the agent decide."""
    parts = spec.split(":")
    obligation_id, failure = parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)
    hours = int(parts[2]) if len(parts) > 2 else 0
    req = IncidentRequest(state=state, obligation_id=obligation_id, simulate_failure_type=failure, hours_in_flight=hours)
    try:
        result = await IncidentCommander(executor).handle(req)
    except (KeyError, LookupError) as exc:
        print(f"  no transfer on record for {obligation_id}: {exc}")
        return {"error": str(exc)}
    for e in result.events:
        detail = ", ".join(f"{k}={v}" for k, v in e.detail.items() if k not in ("events",))
        print(f"  [{e.status:<7}] {e.step:<14} {detail[:150]}")
    print(f"\n  decision: {result.decision}: {result.reason}")
    print(f"  original {result.original_transfer_id[:8]}… {result.original_status}"
          f"{' / ' + result.original_failure_type if result.original_failure_type else ''}"
          + (f"; replacement {result.replacement_transfer_id[:8]}… {result.replacement_status}"
             if result.replacement_transfer_id else ""))
    print(f"  incident {result.incident_id[:8]}… {result.status}"
          + ("" if result.status == "RESOLVED" else " (not closed: a person, or the original's fate, is still pending)"))
    return result.model_dump(mode="json")


def print_reading(reading) -> None:
    print(f"  {reading.stage.summary}; embargoed: {reading.stage.details['embargoed']}")
    for u in reading.updates:
        print(f"  {'VERIFIED' if u.verified else 'UNVERIFIED'} ({u.reader}) {u.receivable_id}: "
              f"{u.previous_expected_in_hours}h/{u.previous_confidence_bp / 100:.0f}% -> "
              f"{u.new_expected_in_hours}h/{u.new_confidence_bp / 100:.0f}%, 90% window {u.arrival_window_hours}h")
        print(f"     cites {u.doc_id}[{u.span_start}:{u.span_end}]: \"{u.quote}\"")
        print(f"     calibration: {u.calibration}")
        for p in u.problems:
            print(f"     problem: {p}")
    for f in reading.flags:
        print(f"  POLICY EXCEPTION ({'verified' if f.verified else 'UNVERIFIED, still escalated'}, {f.reader}) "
              f"{f.obligation_id} / {f.payee}: {f.flag}")
        print(f"     cites {f.doc_id}[{f.span_start}:{f.span_end}]: \"{f.quote}\"")


async def challenge(agent: Orchestrator, state: TreasuryState, plan, model: ChatModel) -> list[dict]:
    """Run outside recommendations through the same gate and compare them with the plan."""
    out = []
    proposals = [await llm_proposal(state, model)]
    if proposals[0][1].startswith("LLM"):
        proposals.append((naive_proposal(state), "naive assistant ('pay everything that is due')"))
    chosen = {d.obligation_id for d in plan.obligations if d.action in ("PAY", "CONVERT_AND_PAY")}
    for ids, source in proposals:
        verdict = await agent.challenge(state, ids, source)
        print(f"  {source} recommends paying: {', '.join(ids) or 'nothing'}")
        print(f"    ProofCore verdict: {verdict.decision}")
        for s in verdict.stages:
            if s.status == "FAIL":
                print(f"    ✗ {s.engine}: {s.summary}")
        missed = sorted(chosen - set(ids))
        extra = sorted(set(ids) - chosen)
        if verdict.decision != "BLOCKED" and missed:
            print(f"    safe, but misses what VENTHORA pays: {', '.join(missed)}")
        if extra:
            print(f"    would also pay what VENTHORA refuses or defers: {', '.join(extra)}")
        out.append({"source": source, "proposal": ids, "decision": verdict.decision,
                    "failures": [s.summary for s in verdict.stages if s.status == "FAIL"]})
    return out


def remember_env(name: str, value: str) -> None:
    """Persist a discovered, non-secret setting (e.g. a Global Account id) into .env."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if any(ln.startswith(f"{name}=") for ln in lines):
        lines = [f"{name}={value}" if ln.startswith(f"{name}=") else ln for ln in lines]
    else:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def usd_global_account(bank: Airwallex, *, fake: bool) -> str:
    """A Global Account that receives USD: configured, found, or created (guide: list first)."""
    # A simulated deposit lands in the account's PRIMARY currency (required_features), even if the
    # account also accepts other currencies by SWIFT (observed: a USD deposit to an HK account
    # arrived as HKD). So only an account whose required feature is USD qualifies.
    configured = "" if fake else env("AIRWALLEX_GLOBAL_ACCOUNT_ID")
    save = (lambda *_: None) if fake else remember_env  # a fake run never writes the real .env
    accounts = await bank.list_global_accounts()

    def primary_usd(a: dict) -> bool:
        return (any(f.get("currency") == "USD" for f in a.get("required_features") or [])
                and a.get("status") not in ("CLOSED", "FAILED"))

    for account in accounts:
        if primary_usd(account) and (not configured or account["id"] == configured):
            save("AWX_GLOBAL_ACCOUNT_ID", account["id"])
            return account["id"]
    if configured:
        print(f"  configured Global Account {configured} is not a USD account; looking for or creating one")
    for account in accounts:
        if primary_usd(account):
            save("AWX_GLOBAL_ACCOUNT_ID", account["id"])
            return account["id"]
    created = await bank.create_global_account(request_id=str(uuid.uuid4()), country_code="US", currency="USD")
    print(f"  created USD Global Account {created['id']} (status {created.get('status')})")
    save("AWX_GLOBAL_ACCOUNT_ID", created["id"])
    return created["id"]


def _matches(d: dict, r) -> bool:
    payer = (d.get("payer") or {}).get("name") or d.get("payer_name")
    try:
        amount = to_minor(d.get("amount", 0))
    except (ValueError, ArithmeticError):
        return False
    return payer == r.payer and d.get("currency") == r.currency and amount == r.amount_minor


async def simulate_receipt(bank: Airwallex, state: TreasuryState, store: ExecutionStore, *, fake: bool,
                           poll_s: float = 1.0, polls: int = 20) -> tuple[TreasuryState, dict | None]:
    """Kit 01: 'simulate the deposit, recalculate'.

    The receipt counts only when Airwallex's deposit record says SETTLED (the balance can lag:
    observed on the real sandbox). A settled deposit already matching the receivable and not yet
    counted is reused instead of depositing twice."""
    pending = sorted(state.receivables, key=lambda r: r.confidence_bp)
    if not pending:
        print("  no receivable to simulate")
        return state, None
    r = pending[0]
    counted = store.counted_deposits()
    found = [d for d in await bank.list_deposits()
             if d.get("status") == "SETTLED" and d.get("id") not in counted and _matches(d, r)]
    if found:
        deposit = found[-1]
        print(f"  found settled deposit {deposit['id']} from {r.payer} ({fmt(r.amount_minor, r.currency)}) "
              "not yet counted: using it instead of depositing again")
    else:
        account = await usd_global_account(bank, fake=fake)
        created = await bank.simulate_deposit(global_account_id=account, amount_minor=r.amount_minor, payer_name=r.payer)
        print(f"  {r.payer} deposit {fmt(r.amount_minor, r.currency)} via Global Account {account}: "
              f"created {created.get('id')} ({created.get('status')}); waiting for SETTLED")
        deposit = created
        for _ in range(polls):
            record = next((d for d in await bank.list_deposits() if d.get("id") == created.get("id")), None)
            if record and record.get("status") == "SETTLED":
                deposit = record
                break
            await asyncio.sleep(poll_s)
    if deposit.get("status") != "SETTLED" or not _matches(deposit, r):
        print(f"  deposit not settled as {r.currency} {fmt(r.amount_minor, r.currency)}: the receipt is NOT counted")
        return state, deposit | {"landed": False}
    data = state.model_dump()
    data["balances"][r.currency] = state.balance(r.currency) + r.amount_minor
    data["receivables"] = [x for x in data["receivables"] if x["receivable_id"] != r.receivable_id]
    data["evidence"].append({"ref": f"airwallex:deposit:{deposit['id']}", "subject_id": r.receivable_id,
                             "source": "airwallex:deposits", "status": "verified",
                             "summary": f"deposit {deposit['id']} SETTLED at {deposit.get('settled_at')}"})
    live = await bank.balances_minor()  # guide: re-read balances after a deposit before recalculating
    print(f"  re-read balances: live {r.currency} wallet {fmt(live.get(r.currency, 0), r.currency)}")
    print(f"  receipt confirmed by Airwallex (SETTLED {deposit.get('settled_at', '')}): managed {r.currency} budget "
          f"+{fmt(r.amount_minor, r.currency)}, {r.receivable_id} settled")
    return TreasuryState.model_validate(data), deposit | {"landed": True, "receivable_id": r.receivable_id,
                                                          "amount_minor": r.amount_minor, "currency": r.currency}


async def run(args) -> int:
    load_env()
    scenario = json.loads((ROOT / "data" / "scenarios" / f"{args.scenario}.json").read_text(encoding="utf-8"))
    state = TreasuryState.model_validate(scenario["state"])
    model = ChatModel()

    line("VENTHORA · engines")
    for rel, ok in originals_status().items():
        print(f"  {'✓' if ok else '✗ MODIFIED'} original {rel}")
    print(f"  LLM reader: {await model_status(model)}")
    ecb = ecb_path()
    print(f"  Horizon FX stress: {'Horizon M0 on ECB history' if ecb.is_file() and not args.no_fx_stress else 'off'}")

    if args.fake:
        from .testing.fake_airwallex import FakeAirwallex
        # Like the real sandbox: a large pre-funded wallet, guide fees; the agent plans on its ring-fenced budget.
        fake = FakeAirwallex(balances={"USD": "10000000", "EUR": "10000000", "GBP": "10000000"})
        client = httpx.AsyncClient(base_url=SANDBOX_URL, transport=httpx.MockTransport(fake.handler))
        import os
        os.environ.setdefault("AIRWALLEX_CLIENT_ID", "fake-sandbox")
        os.environ.setdefault("AIRWALLEX_API_KEY", "fake-sandbox")
        bank = Airwallex(base_url=SANDBOX_URL, client=client, backoff_s=0)
        print("  Airwallex: in-process FAKE sandbox (no network)")
    else:
        bank = Airwallex()
        if not bank.configured:
            print("\nAirwallex sandbox credentials missing. Set AWX_CLIENT_ID and AWX_API_KEY in .env,"
                  " or run with --fake.")
            return 2
        print(f"  Airwallex: {bank.base_url} (sandbox={bank.sandbox})")

    run_dir = ROOT / "data" / "runs"
    agent = Orchestrator(ProofCore(airwallex=bank))
    executor = Executor(agent, ExecutionStore(env("VENTHORA_EXECUTION_DB") or None), AuditStore(),
                        poll_interval_s=0 if args.fake else 1.0)
    report: dict = {"scenario": args.scenario, "started_at": dt.datetime.now(dt.UTC).isoformat()}
    try:
        line("1 · ground the scenario in live balances and rates")
        state = await with_live_balances_and_rates(state, bank, ring_fence=not args.full_wallet)
        live = await bank.balances_minor()
        for c in sorted(state.balances):
            print(f"  {c}: managed budget {fmt(state.balances[c], c):>16}   live wallet {fmt(live.get(c, 0), c)}")
        print("  rates to USD (live, indicative): " + ", ".join(f"{c} {r}" for c, r in sorted(state.fx_to_base.items())))
        if not args.full_wallet:
            print("  ring-fenced: the agent may spend only its treasury allocation, never the whole wallet")
        state, facts = apply_operational_facts(state, executor.store)
        for note in facts:
            print(f"  learned from earlier executions: {note}")

        if ecb.is_file() and not args.no_fx_stress:
            state = state.model_copy(update={"use_ecb_stress": True, "fx_stress_model": "horizon_m0"})

        box = json.loads(Path(args.inbox).read_text(encoding="utf-8")) if args.inbox else None
        timeline = (box.get("timeline") or [box.get("as_of") or str(dt.date.today())]) if box else []
        report["plans"], report["evidence"] = [], []
        plan = None

        async def read(as_of_text: str, title: str):
            line(title)
            reading = await read_inbox(state, [InboxMessage(**m) for m in box["messages"]],
                                       as_of=dt.date.fromisoformat(as_of_text), model=model)
            print_reading(reading)
            report["evidence"].append({"as_of": as_of_text, "updates": [u.model_dump() for u in reading.updates],
                                       "flags": [f.model_dump() for f in reading.flags]})
            return reading.state

        # Initial plan: what the inbox says on day 1.
        if timeline:
            state = await read(timeline[0], f"2 · read the inbox as of {timeline[0]} (LLM proposes, Verity verifies)")
        line("3 · initial plan and proof (Verity · Horizon · Theorem · ProofCore)")
        plan = await agent.decide(state)
        print_plan(plan)
        report["exception_notes"] = await print_exception_notes(state, plan, model)
        report["plans"].append(plan.model_dump(mode="json"))

        if not args.no_challenge:
            line("3b · AI challenger: other recommendations through the same gate")
            report["challenges"] = await challenge(agent, state, plan, model)

        # New information: later messages change the forecast; recalculate only what they affect.
        for day in timeline[1:]:
            previous_state = state
            state = await read(day, f"4 · new information as of {day}")
            line("4b · revised decision (only affected decisions recalculated)")
            plan = await agent.decide(state, previous=(previous_state, plan))
            print_plan(plan, counterfactuals=True)
            report["exception_notes"] = await print_exception_notes(state, plan, model)
            report["plans"].append(plan.model_dump(mode="json"))

        if args.receipt:
            line("5 · simulate the delayed receipt landing, then recalculate only what it affects")
            previous_state = state
            state, deposit = await simulate_receipt(bank, state, executor.store, fake=args.fake,
                                                    poll_s=0 if args.fake else 1.0)
            report["deposit"] = deposit
            if deposit and deposit.get("landed"):
                plan = await agent.decide(state, previous=(previous_state, plan))
                print_plan(plan, counterfactuals=False)
                report["plans"].append(plan.model_dump(mode="json"))
        report["state"] = state.model_dump(mode="json")

        if args.plan_only or plan.decision == "BLOCKED":
            return 0 if plan.decision != "BLOCKED" else 1
        if not any(d.action in ("PAY", "CONVERT_AND_PAY") for d in plan.obligations):
            print("\n  nothing to execute: the plan moves no money (escalations go to a person)")
            return 0

        line("6 · approval (a person decides; the terms shown here are what the approval binds to)")
        approval = None
        print_approval_terms(plan)
        if plan.decision == "REQUIRES_APPROVAL":
            name = args.approve or (input("  Approver name (finance_manager), blank to stop: ").strip()
                                    if sys.stdin.isatty() else "")
            if not name:
                print("  not approved: nothing executed")
                return 0
            approval = Approval(approver=name, role="finance_manager", terms_digest=terms_digest(plan))
            print(f"  approved by {name} (finance_manager) on terms {terms_digest(plan)[:16]}…")
        elif not (args.yes or args.approve):  # an approver given on the command line consents to execution
            if not sys.stdin.isatty() or input("  Execute this plan on the sandbox? [y/N] ").strip().lower() != "y":
                print("  not executed")
                return 0

        line("7 · execute on Airwallex and reconcile")
        if args.incident:
            print("  (Kit 03 demo: transfers are left in flight, not settled, so the incident can be staged)")
        result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval,
                                                         simulate_settlement=not args.incident))
        for s in result.steps:
            ident = s.airwallex_id or s.request_id or ""
            print(f"  [{s.status:<9}] {s.kind:<11} {s.key:<24} {ident}")
        if result.reconciliation:
            rec = result.reconciliation
            print("  managed budget after: " + ", ".join(fmt(v, c) for c, v in sorted(rec.managed_after_minor.items())))
            print("  live wallet after:    " + ", ".join(fmt(rec.live_after_minor.get(c, 0), c)
                                                     for c in sorted(rec.managed_after_minor)))
            print(f"  discrepancies: {rec.discrepancies_minor or 'none'};"
                  f" reserve floor {fmt(rec.base_floor_minor, 'USD')} held: {rec.base_floor_ok}")
        print(f"\n  {result.status}: {result.message}")
        report["execution"] = result.model_dump(mode="json")
        dep = report.get("deposit") or {}
        if result.status == "COMPLETED" and dep.get("landed"):
            executor.store.count_deposit(dep["id"], dep["receivable_id"], dep["amount_minor"], dep["currency"],
                                         result.execution_id)
        if args.incident:
            line("8 · Kit 03 incident: the supplier says the money never arrived")
            report["incident"] = await run_incident(executor, state, args.incident, fake=args.fake)
            return 0 if report["incident"].get("status") in ("RESOLVED", "WAITING", "ESCALATED") else 1
        return 0 if result.status == "COMPLETED" else 1
    finally:
        await agent.close()
        run_dir.mkdir(parents=True, exist_ok=True)
        out = run_dir / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.scenario}{'-fake' if args.fake else ''}.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        line()
        print(f"run report: {out}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="venthora-sandbox", description="Run the VENTHORA Kit 01 loop on Airwallex sandbox.")
    ap.add_argument("--scenario", default="kit01_baseline")
    ap.add_argument("--inbox", default=str(ROOT / "data" / "inbox" / "kit01_inbox.json"),
                    help="JSON inbox to read ('' to skip)")
    ap.add_argument("--fake", action="store_true", help="use the in-process fake sandbox (offline)")
    ap.add_argument("--receipt", action="store_true",
                    help="after the first plan, simulate the delayed receipt landing (sandbox deposit) and re-plan")
    ap.add_argument("--full-wallet", action="store_true", help="plan on the whole live wallet instead of the allocation")
    ap.add_argument("--approve", metavar="NAME", help="finance manager approving a REQUIRES_APPROVAL plan")
    ap.add_argument("--yes", action="store_true", help="execute an APPROVED plan without asking")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--no-fx-stress", action="store_true", help="skip Horizon FX stress")
    ap.add_argument("--no-challenge", action="store_true", help="skip the AI-challenger demo")
    ap.add_argument("--incident", metavar="OBLIGATION[:FAILURE_TYPE[:HOURS]]",
                    help="Kit 03: after execution, fail that transfer on the sandbox and let the agent decide")
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
