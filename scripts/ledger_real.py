# ruff: noqa: E501
"""Two books on one real sandbox wallet: each keeps its own ledger, each reconciles to the cent, neither spends the other's.

  python scripts/ledger_real.py [--approve NAME]

Book A and book B are fresh Kit 01 books (same amounts, new ids) sharing the sandbox wallet. A executes, then B; each
run's reconciliation explains the wallet's movement by the cash lines of every book since its own `before` read, so
B's activity never shows up as A's discrepancy. Then book C is opened with a ledger that says it has almost nothing
left, although the wallet holds millions: its execution is refused before any money moves. Writes docs/ledger_log_real.json.
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
from venthora import ledger  # noqa: E402
from venthora.audit import AuditStore  # noqa: E402
from venthora.config import load_env  # noqa: E402
from venthora.domain.models import TreasuryState  # noqa: E402
from venthora.execution.executor import Executor, terms_digest  # noqa: E402
from venthora.execution.fresh import fresh_book  # noqa: E402
from venthora.execution.live import with_live_balances_and_rates  # noqa: E402
from venthora.execution.models import Approval, ExecutionRequest  # noqa: E402
from venthora.execution.store import ExecutionStore  # noqa: E402
from venthora.gateways.airwallex import Airwallex  # noqa: E402
from venthora.orchestrator import Orchestrator  # noqa: E402
from venthora.proofcore.gate import ProofCore  # noqa: E402

load_env()
KIT = json.loads((ROOT / "data" / "scenarios" / "kit01_initial.json").read_text(encoding="utf-8"))["state"]
KIT["policy"]["payout_fees_minor"] = {"SWIFT:EUR": 1338, "LOCAL:USD": 300, "LOCAL:GBP": 224}


async def main(approver: str) -> int:
    bank = Airwallex()
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore()
    executor = Executor(agent, store, AuditStore(), poll_interval_s=1.0)
    tag = dt.datetime.now().strftime("%H%M%S")
    log: dict = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "runs": []}
    try:
        async def run_book(name: str, state: TreasuryState) -> dict:
            state = await with_live_balances_and_rates(state, bank, ring_fence=True)
            plan = await agent.decide(state)
            approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(plan)) if plan.decision == "REQUIRES_APPROVAL" else None
            result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approval=approval))
            wb = next((s.detail for s in result.steps if s.kind == "ledger" and s.key == "wallet-vs-books"), {})
            funds = next((s.detail for s in result.steps if s.kind == "ledger" and s.key == "funds"), {})
            rec = {"book": state.book_id, "plan": plan.decision, "execution": result.status, "message": result.message[:160],
                   "wallet_delta": wb.get("wallet_delta_minor"), "this_book": wb.get("this_book_cash_delta_minor"),
                   "other_books": wb.get("other_books_cash_delta_minor"), "unexplained": wb.get("unexplained_minor"),
                   "funds": funds, "balances": store.book_balances(state.book_id) if store.has_book(state.book_id) else None}
            print(f"\n{name} ({state.book_id}): plan {plan.decision}, execution {result.status}")
            print(f"    wallet delta {rec['wallet_delta']} = this book {rec['this_book']} + other books {rec['other_books']}; unexplained {rec['unexplained']}")
            if funds.get("overdrawn"):
                print(f"    refused before money moved: {funds}")
            log["runs"].append(rec)
            return rec

        a = await run_book("book A", fresh_book(TreasuryState.model_validate(KIT), f"A{tag}"))
        b = await run_book("book B", fresh_book(TreasuryState.model_validate(KIT), f"B{tag}"))
        # Book C: the wallet is rich, the book is not. Its ledger is opened with the allocation and an earlier spend.
        c_state = fresh_book(TreasuryState.model_validate(KIT), f"C{tag}")
        store.post(c_state.book_id, ledger.open_book(c_state.balances, "allocation"))
        store.post(c_state.book_id, ledger.adjust("USD", -(c_state.balances["USD"] - 10_000), "earlier spend recorded on this book"))
        c = await run_book("book C (ledger says USD 100 left)", c_state)
        ok = (a["execution"] == "COMPLETED" and b["execution"] == "COMPLETED" and not a["unexplained"] and not b["unexplained"]
              and c["execution"] == "FAILED" and "cannot fund" in c["message"])
        log["ok"] = ok
        (ROOT / "docs" / "ledger_log_real.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print("\nOK" if ok else "\nNOT AS EXPECTED")
        return 0 if ok else 1
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", default="finance manager (owner)")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(ap.parse_args().approve)))
