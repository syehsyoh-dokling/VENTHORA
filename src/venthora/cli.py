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
from .evidence.llm import ChatModel
from .evidence.reader import InboxMessage, read_inbox
from .execution.executor import Executor
from .execution.facts import apply_operational_facts
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
            print(f"    {'✓' if c.feasible else '✗'} {c.question:<48} {c.consequence}")


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
    if fake:
        return "ga-usd-1"
    # A simulated deposit lands in the account's PRIMARY currency (required_features), even if the
    # account also accepts other currencies by SWIFT (observed: a USD deposit to an HK account
    # arrived as HKD). So only an account whose required feature is USD qualifies.
    configured = env("AIRWALLEX_GLOBAL_ACCOUNT_ID")
    accounts = await bank.list_global_accounts()

    def primary_usd(a: dict) -> bool:
        return (any(f.get("currency") == "USD" for f in a.get("required_features") or [])
                and a.get("status") not in ("CLOSED", "FAILED"))

    for account in accounts:
        if primary_usd(account) and (not configured or account["id"] == configured):
            remember_env("AWX_GLOBAL_ACCOUNT_ID", account["id"])
            return account["id"]
    if configured:
        print(f"  configured Global Account {configured} is not a USD account; looking for or creating one")
    for account in accounts:
        if primary_usd(account):
            remember_env("AWX_GLOBAL_ACCOUNT_ID", account["id"])
            return account["id"]
    created = await bank.create_global_account(request_id=str(uuid.uuid4()), country_code="US", currency="USD")
    print(f"  created USD Global Account {created['id']} (status {created.get('status')})")
    remember_env("AWX_GLOBAL_ACCOUNT_ID", created["id"])
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
        fake = FakeAirwallex()
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

        if args.inbox:
            line("2 · read the inbox (LLM proposes, Verity verifies)")
            box = json.loads(Path(args.inbox).read_text(encoding="utf-8"))
            as_of = dt.date.fromisoformat(box.get("as_of") or str(dt.date.today()))
            reading = await read_inbox(state, [InboxMessage(**m) for m in box["messages"]], as_of=as_of, model=model)
            state = reading.state
            print(f"  {reading.stage.summary}; embargoed: {reading.stage.details['embargoed']}")
            for u in reading.updates:
                print(f"  {'VERIFIED' if u.verified else 'UNVERIFIED'} ({u.reader}) {u.receivable_id}: "
                      f"{u.previous_expected_in_hours}h/{u.previous_confidence_bp / 100:.0f}% -> "
                      f"{u.new_expected_in_hours}h/{u.new_confidence_bp / 100:.0f}%")
                print(f"     cites {u.doc_id}[{u.span_start}:{u.span_end}]: \"{u.quote}\"")
                for p in u.problems:
                    print(f"     problem: {p}")
            report["evidence"] = [u.model_dump() for u in reading.updates]

        if ecb.is_file() and not args.no_fx_stress:
            state = state.model_copy(update={"use_ecb_stress": True, "fx_stress_model": "horizon_m0"})

        line("3 · plan and prove (Verity · Horizon · Theorem · ProofCore)")
        plan = await agent.decide(state)
        print_plan(plan)
        report["plans"] = [plan.model_dump(mode="json")]

        if not args.no_challenge:
            line("3b · AI challenger: other recommendations through the same gate")
            report["challenges"] = await challenge(agent, state, plan, model)

        if args.receipt:
            line("4 · simulate the delayed receipt landing, then recalculate only what it affects")
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

        line("5 · approval (a person decides; the plan_hash binds the approval)")
        approval = None
        if plan.decision == "REQUIRES_APPROVAL":
            name = args.approve or (input("  Approver name (finance_manager), blank to stop: ").strip()
                                    if sys.stdin.isatty() else "")
            if not name:
                print("  not approved: nothing executed")
                return 0
            approval = Approval(approver=name, role="finance_manager")
            print(f"  approved by {name} (finance_manager)")
        elif not (args.yes or args.approve):  # an approver given on the command line consents to execution
            if not sys.stdin.isatty() or input("  Execute this plan on the sandbox? [y/N] ").strip().lower() != "y":
                print("  not executed")
                return 0

        line("6 · execute on Airwallex and reconcile")
        result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval))
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
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
