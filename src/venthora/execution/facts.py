# ruff: noqa: E501
"""Feed what execution learned back into the next plan.

* Fees Airwallex actually charged (per 'METHOD:CCY') replace assumed fees. The Kit 01 guide says
  EUR 12.85 per SWIFT payout and free LOCAL payouts; the sandbox charged EUR 13.37 and USD 3.00
  on 23 Oct 2026. A plan built on stale fees overspends its budget by exactly that difference.
* The managed budget continues from the last reconciliation (money already paid out is gone),
  capped by the scenario allocation and floored at zero.
* Obligations already PAID (by invoice fingerprint or id) become 'settled', so the next plan
  neither proposes nor re-pays them. The payout claim would block a re-payment anyway; this keeps
  the plan honest instead of relying on the block.
"""
from __future__ import annotations

from ..domain.models import TreasuryState
from ..domain.money import fmt
from .store import ExecutionStore


def payout_key(obligation) -> str:
    get = obligation.get if isinstance(obligation, dict) else lambda k: getattr(obligation, k)
    fp, oid, inst = get("invoice_fingerprint"), get("obligation_id"), get("installment")
    return (f"invoice:{fp}" + (f"#{inst}" if inst else "")) if fp else f"obligation:{oid}"


def apply_operational_facts(state: TreasuryState, store: ExecutionStore) -> tuple[TreasuryState, list[str]]:
    data = state.model_dump()
    notes: list[str] = []
    fees = store.observed_fees()
    if fees:
        merged = data["policy"]["payout_fees_minor"] | fees
        data["policy"]["payout_fees_minor"] = merged
        notes.append("observed Airwallex fees: " + ", ".join(
            f"{k} {fmt(v, k.split(':')[1])}" for k, v in sorted(fees.items())))
    last = store.last_managed_budget(state.book_id)
    if store.has_book(state.book_id):
        # The book ledger is the book's truth: its own cash (not the shared wallet, not the last reconciliation's
        # managed figure) is what the next plan may spend; money in flight is not available until it comes back.
        book = store.book_balances(state.book_id)
        for ccy in data["balances"]:
            cash = book.get(ccy, {}).get("cash", 0)
            data["balances"][ccy] = max(0, min(cash, data["balances"][ccy]))
            if cash < 0:
                # A post-fact surprise (a rail fee above the one assumed) left the book short; the plan starts from 0 but the
                # ledger pre-flight will refuse a payout in this currency until a person adjusts the book (POST /v1/ledger/{book}/adjust).
                notes.append(f"book {state.book_id} is overdrawn by {fmt(-cash, ccy)} in {ccy}; a person must adjust the book "
                             f"before it can pay in {ccy}")
        notes.append("book ledger " + state.book_id + ": cash " + ", ".join(fmt(v, c) for c, v in sorted(data["balances"].items()))
                     + "; in flight " + ", ".join(fmt(a.get("committed", 0), c) for c, a in sorted(book.items()) if a.get("committed")))
    elif last:
        execution_id, managed = last
        for ccy in data["balances"]:
            if ccy in managed:
                value = managed[ccy]
                if value < 0:
                    notes.append(f"{ccy} budget was overdrawn by {fmt(-value, ccy)} in {execution_id[:8]}; starting from 0")
                data["balances"][ccy] = max(0, min(value, data["balances"][ccy]))
        notes.append("budget carried forward from execution " + execution_id[:8] + ": "
                     + ", ".join(fmt(v, c) for c, v in sorted(data["balances"].items())))
    paid = store.paid_payouts()
    for o in data["obligations"]:
        if o["record_kind"] == "payable" and payout_key(o) in paid:
            o["record_kind"] = "settled"
            notes.append(f"{o['obligation_id']} already paid (transfer {paid[payout_key(o)]['transfer_id']})")
    # Limits and velocity: what the spend ledger remembers, as of now, for this report period.
    if data.get("history") is None:
        from datetime import UTC, datetime

        from ..reporting import load_config, reporting_period

        now = datetime.now(UTC)
        cfg = load_config()
        hist = store.payment_history(now.isoformat(), reporting_period(now, cfg))
        data["history"] = hist
        if hist["payees_paid_before"] or hist["outflow_7d_minor"]:
            notes.append(f"history: {len(hist['payees_paid_before'])} payee(s) paid before; outflow 24h "
                         f"{fmt(hist['outflow_24h_minor'], data['base_currency'])}, 7d {fmt(hist['outflow_7d_minor'], data['base_currency'])}")
    result = TreasuryState.model_validate(data)
    # Payee lifecycle: the store's live records for the payees this book names (unregistered payees are escalated).
    if result.payees is None:
        records = store.list_payees()
        if records:
            from ..payees import attach_registry

            result = attach_registry(result, records)
            by_status: dict[str, list[str]] = {}
            for r in result.payees.records:
                by_status.setdefault(r.status, []).append(r.beneficiary_id)
            missing = sorted({o.beneficiary_id for o in result.obligations} - {r.beneficiary_id for r in result.payees.records})
            notes.append("payees: " + ", ".join(f"{k} {', '.join(v)}" for k, v in sorted(by_status.items()))
                         + (f"; unregistered {', '.join(missing)}" if missing else ""))
    # Budget · mandate: the period's plan with what the store knows: realized spend per line, mandate granted by a person.
    from ..proofcore.budget import attach_budget, load_budget

    period = result.budget.period if result.budget else (load_budget().period if load_budget() else None)
    if period and (result.budget or any(o.budget_line for o in result.obligations)):
        realized, grants = store.realized(period), store.grants(period)
        result = attach_budget(result, period, realized=realized, grants=grants)
        if result.budget:
            notes.append(f"budget {period}: " + ", ".join(
                f"{ln.line} {fmt(ln.realized_minor, result.base_currency)} realized of "
                f"{fmt(ln.mandate_minor, result.base_currency)}"
                + (f" (+{fmt(ln.granted_extra_minor, result.base_currency)} granted)" if ln.granted_extra_minor else "")
                for ln in result.budget.lines))
    return result, notes
