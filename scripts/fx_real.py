# ruff: noqa: E501
"""FX exposure limits on the real Airwallex sandbox: the daily conversion cap from the journal, the hedge ratio, the open-position cap.

  python scripts/fx_real.py [--approve NAME]

Fresh book, EUR invoices to ben-eu-components (SWIFT), USD sold on the sandbox at the live quote:
  run 1  EUR 1,000 with a daily cap set above what the journal already sold today + this conversion -> autonomous, COMPLETED
  run 2  EUR 1,000 with the daily cap set to what was sold so far + USD 500 -> daily_conversion_cap_exceeded -> REFUSED without a
         person, COMPLETED with one
  run 3  two EUR 1,000 invoices on a book whose USD can fund only one conversion above the floor; min hedge 80 %
         -> the second is escalated, coverage ~50 % -> hedge_ratio_below_minimum -> approved -> the first is paid, the second is not
  run 4  a plan on a book allocated EUR (ring-fenced to what the live wallet holds) with an open-position cap of EUR 100:
         above the cap only if the wallet actually holds EUR beyond it; the decision is logged either way
Writes docs/fx_log_real.json.
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
from venthora.reporting import load_config, reporting_period  # noqa: E402

load_env()
BASE_POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 100_000_000, "swift_fee_currency": "EUR",
               "swift_fee_minor": 1285, "planning_horizon_hours": 72, "payout_fees_minor": {"LOCAL:USD": 300, "SWIFT:EUR": 1337}}


def eur(i: int, tag: str, amount: int = 100_000, priority: int = 8) -> dict:
    return {"obligation_id": f"FX-{i}-{tag}", "amount_minor": amount, "currency": "EUR", "priority": priority, "due_in_hours": 24,
            "beneficiary_id": "ben-eu-components", "payee_name": "Rheinwerk Komponenten GmbH", "payout_method": "SWIFT",
            "evidence_ref": f"ev-{i}-{tag}", "invoice_fingerprint": f"eu-components:FX-{i}-{tag}"}


def book(tag: str, obligations: list[dict], policy: dict, usd: int = 2_000_000, eur_alloc: int = 0) -> dict:
    return {"case_id": f"fx-real-{tag}", "book_id": f"fx-real-{tag}", "base_currency": "USD",
            "balances": {"USD": usd, "EUR": eur_alloc, "GBP": 0}, "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
            "obligations": obligations, "receivables": [],
            "evidence": [{"ref": o["evidence_ref"], "subject_id": o["obligation_id"], "source": "ap:invoice", "status": "verified", "summary": "invoice"} for o in obligations],
            "scenarios": [], "policy": BASE_POLICY | policy, "actor_role": "treasury_agent"}


async def main(approver: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "book": f"fx-real-{tag}", "runs": []}

    def sold_today() -> int:
        now = dt.datetime.now(dt.UTC)
        return store.payment_history(now.isoformat(), reporting_period(now, load_config()))["converted_24h_minor"]

    try:
        async def cycle(name: str, b: dict, approve: bool, execute: bool = True) -> dict:
            state = TreasuryState.model_validate(b)
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            state, _facts = apply_operational_facts(state, store)
            plan = await agent.decide(state)
            fx = next((s.details for s in plan.stages if s.engine == "Policy · FX exposure"), {})
            requires = next((s.details.get("local", {}).get("requires_approval", []) for s in plan.stages if s.engine == "Policy"), [])
            rec = {"run": name, "decision": plan.decision, "actions": {d.obligation_id: d.action for d in plan.obligations}, "requires_approval": requires,
                   "converted_24h_minor": fx.get("converted_24h_minor"), "converting_now_minor": fx.get("converting_now_minor"),
                   "rows": fx.get("rows"), "eur_held_minor": state.balances.get("EUR")}
            print(f"\n{name}: {plan.decision} {rec['actions']} {requires}  sold24h {fx.get('converted_24h_minor')} + now {fx.get('converting_now_minor')}; rows {fx.get('rows')}")
            if execute:
                approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(plan)) if (approve and plan.decision == "REQUIRES_APPROVAL") else None
                result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval))
                rec["execution"] = {"status": result.status, "message": result.message[:200],
                                    "transfers": sum(1 for s in result.steps if s.kind == "transfer" and s.status == "OK"),
                                    "conversions": sum(1 for s in result.steps if s.kind == "conversion" and s.status == "OK")}
                print(f"    execution {result.status}: {result.message[:140]}")
            log["runs"].append(rec)
            return rec

        before = sold_today()
        log["sold_24h_before_minor"] = before
        print(f"USD sold in conversions over the last 24 h before this run: {before}")
        # One book per run: a run whose real fee comes in above the assumed fee leaves its book a few cents short in EUR, and
        # that book rightly refuses its next EUR payout until a person adjusts it (cycle 16 finding, SANDBOX_RESULTS.md).
        r1 = await cycle("run 1: EUR 1,000 inside the daily cap", book(tag + "a", [eur(1, tag)], {"max_conversion_daily_minor": before + 2_000_000}), approve=False)
        mid = sold_today()
        r2a = await cycle("run 2a: EUR 1,000 over the daily cap, nobody approves", book(tag + "b", [eur(2, tag)], {"max_conversion_daily_minor": mid + 50_000}), approve=False)
        r2b = await cycle("run 2b: the same, approved", book(tag + "b", [eur(2, tag)], {"max_conversion_daily_minor": mid + 50_000}), approve=True)
        r3 = await cycle("run 3: two EUR invoices, only one fundable, min hedge 80 %",
                         book(tag + "h", [eur(3, tag, priority=9), eur(4, tag, priority=2)], {"min_hedge_ratio_bp": 8000}, usd=650_000), approve=True)
        r4 = await cycle("run 4: open-position cap EUR 1.00 on a book allocated EUR 300", book(tag + "p", [eur(5, tag)], {"max_open_position_minor": {"EUR": 100}}, eur_alloc=30_000),
                         approve=False, execute=False)
        ok = (r1["decision"] == "APPROVED" and r1["execution"]["status"] == "COMPLETED" and r1["execution"]["conversions"] == 1
              and "daily_conversion_cap_exceeded" in r2a["requires_approval"] and r2a["execution"]["status"] == "REFUSED"
              and r2b["execution"]["status"] == "COMPLETED"
              and "hedge_ratio_below_minimum" in r3["requires_approval"] and r3["execution"]["status"] == "COMPLETED" and r3["execution"]["transfers"] == 1
              and (("open_position_above_cap" in r4["requires_approval"]) == ((r4["eur_held_minor"] or 0) - 101_337 > 100)))
        log["ok"] = ok
        log["sold_24h_after_minor"] = sold_today()
        (ROOT / "docs" / "fx_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="finance manager (owner)")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(ap.parse_args().approve)))
