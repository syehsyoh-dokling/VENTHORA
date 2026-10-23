# ruff: noqa: E501
"""Both ground-truth suites against the REAL Airwallex sandbox.

Part A — every plan / intake / incident case (84 of 100) is sent to the running service (`--base`), whose
ProofCore reads live sandbox balances for each plan: the `Airwallex · live balances` stage must be PASS or
FAIL (never SKIPPED) for the run to count as real. Same comparator as everywhere else.

Part B — the execution anomalies (16) were written for the stateful fake sandbox, where a fault can be
injected on demand. On the real rail only the faults the rail itself can produce are reproducible; those
are run here, in-process, with the real Airwallex client, the real execution store and a FRESH BOOK per
run (new obligation ids and invoice fingerprints, so the November invoices are not re-paid). The others are
listed with the reason they cannot be forced on a real rail.

  python scripts/groundtruth_real.py [--base http://127.0.0.1:8000] [--approve NAME]
Writes docs/groundtruth_log_real.json and prints a summary; docs/GROUND_TRUTH.md gets its section from
scripts/groundtruth_report.py.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from venthora.audit import AuditStore  # noqa: E402
from venthora.config import load_env  # noqa: E402
from venthora.domain.models import TreasuryState  # noqa: E402
from venthora.execution.executor import Executor, terms_digest  # noqa: E402
from venthora.execution.incidents import IncidentCommander, IncidentRequest  # noqa: E402
from venthora.execution.live import live_rates  # noqa: E402
from venthora.execution.models import Approval, ExecutionRequest  # noqa: E402
from venthora.execution.store import ExecutionStore  # noqa: E402
from venthora.gateways.airwallex import Airwallex  # noqa: E402
from venthora.orchestrator import Orchestrator  # noqa: E402
from venthora.proofcore.gate import ProofCore  # noqa: E402
from venthora.testing.groundtruth import compare, load_cases, view_of  # noqa: E402

load_env()

# Execution anomalies the real rail can reproduce, and how; the rest cannot be forced on a real rail.
REAL_PLAN = {
    "N04": "the planning rate is held stale (EUR 1.05) while the sandbox quotes the live rate (~1.13): the quote is above the slippage limit",
    "N26": "a finance manager's approval carrying another plan's terms",
    "N27": "an approval by a viewer",
    "N28": "REQUIRES_APPROVAL executed with no approval",
    "N41": "the same invoices executed twice on the real store: the payout claim refuses the second run",
    "N42": "a treasury allocation larger than the live wallet (USD 20,000,000 against ~9.98 M): live balances do not cover the plan",
    "N44": "real transfer left in flight, failed on the sandbox with BENEFICIARY_BANK_RETURNED, replaced under the duplicate lock",
}
NOT_REPRODUCIBLE = {
    "N09": "the sandbox settles a simulated deposit by itself; a deposit that never settles cannot be requested",
    "N29": "a lost HTTP response cannot be injected into the real rail (covered on the fake; the recovery path is request_id lookup, which the real rail supports)",
    "N30": "429 storms cannot be requested from the sandbox",
    "N31": "duplicate_request_id would require re-sending a used request_id, which the executor never does by design",
    "N32": "balance_insufficient at the rail needs the wallet to drop between the balance read and the transfer; the live-balance check prevents the setup",
    "N33": "sandbox conversions settle immediately; a conversion that stays SCHEDULED cannot be requested",
    "N39": "a bank return after SENT is exactly N44's mechanism on the real rail; not run twice",
    "N40": "same mechanism as N44, three times; not run to keep the sandbox ledger readable",
    "N45": "the replacement's budget check reads the live EUR wallet (~10 M), which cannot be set to EUR 1,512.85",
}


def fresh_book(state: dict, tag: str) -> dict:
    s = copy.deepcopy(state)
    s["book_id"] = f"gt-real-{tag}"
    s["case_id"] = f"gt-real-{tag}"
    for o in s["obligations"]:
        o["obligation_id"] = f"{o['obligation_id']}-{tag}"
        if o.get("invoice_fingerprint"):
            o["invoice_fingerprint"] = f"{o['invoice_fingerprint']}-{tag}"
    for e in s["evidence"]:
        if not e["subject_id"].startswith("RCV"):
            e["subject_id"] = f"{e['subject_id']}-{tag}"
    return s


async def ground(state: TreasuryState, bank: Airwallex, *, keep_rates: bool) -> TreasuryState:
    live = await bank.balances_minor()
    balances = {c: min(live.get(c, 0), a) for c, a in state.balances.items()}
    rates = state.fx_to_base if keep_rates else await live_rates(state, bank, balances)
    return TreasuryState.model_validate(state.model_dump() | {"balances": balances, "fx_to_base": rates})


async def part_b(approver: str, only: set[str] | None = None) -> list[dict]:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    executor = Executor(agent, ExecutionStore(), AuditStore(), poll_interval_s=1.0)
    out = []
    try:
        for case in load_cases("anomalies"):
            if case["kind"] != "execution" or (only and case["id"] not in only):
                continue
            if case["id"] in NOT_REPRODUCIBLE:
                out.append({"id": case["id"], "kind": "execution", "title": case["title"], "skipped": NOT_REPRODUCIBLE[case["id"]]})
                print(f"  --  {case['id']}  not reproducible on the real rail: {NOT_REPRODUCIBLE[case['id']][:80]}")
                continue
            inp = case["input"]
            tag = dt.datetime.now().strftime("%H%M%S") + case["id"].lower()
            raw = fresh_book(inp["state"], tag)
            if case["id"] == "N42":
                raw["balances"] = {"USD": 2_000_000_000, "EUR": 0, "GBP": 0}  # allocation above the live wallet
            state = TreasuryState.model_validate(raw)
            if case["id"] == "N42":
                pass  # no grounding: the stale allocation is the anomaly
            else:
                state = await ground(state, bank, keep_rates=case["id"] == "N04")
            t0 = time.perf_counter()
            plan = await agent.decide(state)
            rec: dict = {"plan": plan.decision, "execution": None, "transfers": 0, "conversions": 0, "message": None,
                         "incident": None, "incident_decision": None, "invariants_ok": True, "errors": [], "receipt_landed": None}
            approval = None
            if inp.get("approve") == "wrong-terms":
                approval = Approval(approver=approver, role="finance_manager", terms_digest="0" * 64)
            elif inp.get("approve") == "viewer":
                approval = Approval(approver=approver, role="viewer", terms_digest=terms_digest(plan))
            elif inp.get("approve", True) is True and plan.decision == "REQUIRES_APPROVAL":
                approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(plan))
            if plan.decision != "BLOCKED":
                result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval,
                                                                 simulate_settlement=not inp.get("incident")))
                rec |= {"execution": result.status, "message": result.message,
                        "transfers": sum(1 for s in result.steps if s.kind == "transfer" and s.status == "OK"),
                        "conversions": sum(1 for s in result.steps if s.kind == "conversion" and s.status == "OK")}
                if inp.get("twice") and result.status == "COMPLETED":
                    plan2 = await agent.decide(state)
                    result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan2.plan_hash, approval=approval))
                    rec |= {"execution": result.status, "message": result.message}
                if inp.get("incident"):
                    ft, hours = inp["incident"]
                    ires = await IncidentCommander(executor).handle(IncidentRequest(
                        state=state, obligation_id=state.obligations[0].obligation_id, simulate_failure_type=ft, hours_in_flight=hours))
                    rec |= {"incident": ires.status, "incident_decision": ires.decision,
                            "incident_original": ires.original_transfer_id, "incident_replacement": ires.replacement_transfer_id}
                    # Real rail: right after the FAILED transition the original still reads FAILED; the sandbox moves it to
                    # CANCELLED with the refund a few seconds later. The incident must stay open until that is recorded,
                    # so the commander is asked again, as an operator would, until the original's fate is on file.
                    for _ in range(6):
                        if ires.status != "REPLACED_AWAITING_ORIGINAL":
                            break
                        await asyncio.sleep(5)
                        ires = await IncidentCommander(executor).handle(IncidentRequest(
                            state=state, obligation_id=state.obligations[0].obligation_id, hours_in_flight=hours))
                        rec["incident_followups"] = rec.get("incident_followups", 0) + 1
                        # the first decision (REPLACE) is the one ground truth talks about; the follow-up's own verdict
                        # (RESOLVE once the replacement is PAID and the original recorded) is kept beside it
                        rec |= {"incident": ires.status, "incident_final_decision": ires.decision}
            ms = (time.perf_counter() - t0) * 1000
            verdict = compare(case["expect"], rec)
            out.append({"id": case["id"], "kind": "execution", "title": case["title"], "ms": round(ms, 1), "how": REAL_PLAN[case["id"]],
                        "book": raw["book_id"], **verdict, "view": rec})
            print(f"  {'ok ' if verdict['match'] else 'BAD'} {case['id']} {ms:8.0f} ms  {case['title'][:55]}  -> plan={plan.decision} exec={rec['execution']} incident={rec['incident']}")
    finally:
        await agent.close()
    return out


def part_a(base: str) -> list[dict]:
    out = []
    with httpx.Client(base_url=base, timeout=60) as client:
        status = client.get("/api/status").json()
        assert status["authorities"]["airwallex"], "the service is not connected to the sandbox"
        for suite in ("cases", "anomalies", "budget", "controls", "reporting", "ledger", "limits", "payees", "approvals", "fx"):
            for case in load_cases(suite):
                if case["kind"] == "execution":
                    continue
                t0 = time.perf_counter()
                if case["kind"] == "plan":
                    res = client.post("/v1/plans", json=case["input"])
                elif case["kind"] == "incident":
                    res = client.post("/v1/incidents/decide", json=case["input"])
                elif case["kind"] == "reporting":
                    res = client.post("/v1/reporting/eval", json=case["input"])
                elif case["kind"] == "ledger":
                    res = client.post("/v1/ledger/eval", json=case["input"])
                elif case["kind"] == "payees":
                    res = client.post("/v1/payees/eval", json=case["input"])
                elif case["kind"] == "approvals":
                    res = client.post("/v1/approvals/eval", json=case["input"])
                else:
                    params = {"dry_run": "true"} | ({"book": case["book"]} if case.get("book") else {})
                    res = client.post("/v1/intake/emails", content=case["input"].encode("utf-8"),
                                      headers={"content-type": "message/rfc822"}, params=params)
                ms = (time.perf_counter() - t0) * 1000
                res.raise_for_status()
                view = view_of(case["kind"], res.json())
                verdict = compare(case["expect"], view)
                real = view.get("live_balances") in ("PASS", "FAIL") if case["kind"] == "plan" else None
                out.append({"id": case["id"], "suite": suite, "kind": case["kind"], "title": case["title"], "ms": round(ms, 1),
                            "live_balances": view.get("live_balances"), "real": real, **verdict, "view": view})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--approve", default="finance manager (owner)")
    ap.add_argument("--only", help="comma-separated execution case ids to (re)run in part B; part A is kept from the log")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    log_path = ROOT / "docs" / "groundtruth_log_real.json"
    if args.only:
        payload = json.loads(log_path.read_text(encoding="utf-8"))
        a = payload["part_a"]
        only = set(args.only.split(","))
        print(f"Part B (only {', '.join(sorted(only))}) on the real rail")
        fresh = {r["id"]: r for r in asyncio.run(part_b(args.approve, only))}
        b = [fresh.get(r["id"], r) for r in payload["part_b"]]
    else:
        print("Part A: every plan / intake / incident case of all suites through the live service (real sandbox balances)")
        a = part_a(args.base)
        plans = [r for r in a if r["kind"] == "plan"]
        print(f"  {sum(r['match'] for r in a)} / {len(a)} match; {sum(bool(r['real']) for r in plans)} / {len(plans)} plans read live sandbox balances; "
              f"plan round trip p50 {statistics.median(r['ms'] for r in plans):.1f} ms")
        print("\nPart B: execution anomalies on the real rail, fresh book per run")
        b = asyncio.run(part_b(args.approve))
    ran = [r for r in b if "ms" in r]
    print(f"  {sum(r['match'] for r in ran)} / {len(ran)} match; {len(b) - len(ran)} not reproducible on a real rail")
    payload = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "part_a": a, "part_b": b}
    log_path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    bad = [r for r in a + ran if not r["match"]]
    for r in bad:
        print("  BAD", r["id"], {k: v for k, v in r["fields"].items() if not v["ok"]})
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
