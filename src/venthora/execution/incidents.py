# ruff: noqa: E501
"""Kit 03: Payment Ops Incident Commander.

A supplier says the money never arrived. The agent decides to WAIT, REPLACE or ESCALATE, and
code enforces what the guide asks for: finality (never replace a transfer that may still clear),
idempotency (a replacement is a new operation with its own request_id), a duplicate lock (one
live payout per invoice), the second SWIFT fee budgeted, and an incident that is not closed when
the replacement reaches PAID but only once the original's fate is recorded as well.

Decision table (code, not prompt). `failure_type` is what Airwallex reports on a CANCELLED
transfer; CANCELLED never means a person cancelled it.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Literal

from pydantic import Field

from ..domain.models import StrictModel, TreasuryState
from ..domain.money import fmt
from ..gateways.airwallex import TERMINAL_TRANSFER_STATUSES, AirwallexError, TransferResult

# Funds come back and nothing about the payee was wrong: a replacement is the right move.
REPLACE_AFTER_RETURN = frozenset({
    "BENEFICIARY_BANK_RETURNED", "CHANNEL_TIMEOUT", "CHANNEL_POLICY", "SYSTEM_ERROR", "UNABLE_TO_APPLY",
    "INSUFFICIENT_FUNDS",
})
# The payee record itself is wrong: paying the same details again pays the wrong account again.
ESCALATE_BENEFICIARY = frozenset({
    "INVALID_ACCOUNT_NAME_OR_NUMBER", "INVALID_BANK_INFORMATION", "INVALID_BANK_OR_BRANCH_CODE",
    "INVALID_BENEFICIARY_DETAILS", "BENEFICIARY_NAME_MISMATCH", "ACCOUNT_CLOSED", "ACCOUNT_INACTIVE_OR_DORMANT",
    "ACCOUNT_UNDER_RESTRICTION", "INVALID_SWIFT_BIC_CODE", "ACCOUNT_CURRENCY_MISMATCH",
    "INVALID_CORRESPONDENT_BANK_INFORMATION", "INVALID_SPECIAL_CHARACTER", "INVALID_PAYMENT_PURPOSE",
    "INBOUND_BENFICIARY_VALIDATION_ERROR", "INBOUND_COMPANY_REGISTRATION_NUMBER_ERROR", "INBOUND_ORDER_ERROR",
    "INBOUND_2B_CARD_NOT_SUPPORTED",
})
# Somebody asked for this outcome, or a limit or control intervened: a person must decide.
ESCALATE_INTENT_OR_CONTROL = frozenset({
    "RECALL_REQUESTED", "DUPLICATION_RETURN", "CLIENT_REQUESTED", "BENEFICIARY_REQUESTED",
    "EXCEEDED_TRANSACTION_AMOUNT_OR_LIMIT", "TRANSACTION_AMOUNT_NOT_COVERING_FEE", "TM_SUSPENDED",
    "CARD_ISSUER_ERROR", "OTHER",
})

Action = Literal["WAIT", "REPLACE", "ESCALATE", "RESOLVE"]


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def decide(transfer: TransferResult, hours_in_flight: int, policy, replacements_so_far: int) -> Decision:
    if transfer.status == "PAID":
        return Decision("RESOLVE", "the original transfer is PAID; nothing to replace")
    if transfer.status not in TERMINAL_TRANSFER_STATUSES:
        if hours_in_flight <= policy.incident_wait_hours:
            return Decision("WAIT", f"{transfer.status} is not final; {hours_in_flight}h in flight is within the "
                                    f"{policy.incident_wait_hours}h wait window; a transfer that may still clear is "
                                    "never replaced")
        return Decision("ESCALATE", f"{transfer.status} for {hours_in_flight}h, beyond the {policy.incident_wait_hours}h window, "
                                    "and still not final: a person must chase the rail; replacing now could pay twice")
    ft = (transfer.failure_type or "").upper() or "UNKNOWN"  # the typed code, not the free-text reason
    if ft in REPLACE_AFTER_RETURN:
        if replacements_so_far >= policy.max_replacements:
            return Decision("ESCALATE", f"{ft}: {replacements_so_far} replacement(s) already attempted; a person decides")
        return Decision("REPLACE", f"{ft}: funds returned and the payee record is not in question; replace under the "
                                   "duplicate lock with a new request_id")
    if ft in ESCALATE_BENEFICIARY:
        return Decision("ESCALATE", f"{ft}: the payee's bank details are wrong; paying them again pays the wrong account again")
    if ft in ESCALATE_INTENT_OR_CONTROL:
        return Decision("ESCALATE", f"{ft}: a person or a control stopped this payment; only a person may restart it")
    return Decision("ESCALATE", f"{ft}: unknown failure type; a person decides")


class IncidentRequest(StrictModel):
    state: TreasuryState
    obligation_id: str = Field(min_length=1, max_length=80)
    simulate_failure_type: str | None = Field(default=None, max_length=64,
                                              description="sandbox only: fail the in-flight transfer with this type")
    hours_in_flight: int = Field(default=0, ge=0, le=720)
    simulate_settlement: bool = True


class IncidentEvent(StrictModel):
    step: str
    status: Literal["OK", "FAILED", "REFUSED", "INFO"]
    detail: dict = Field(default_factory=dict)


class IncidentResult(StrictModel):
    incident_id: str
    obligation_id: str
    status: Literal["OPEN", "WAITING", "ESCALATED", "REPLACED_AWAITING_ORIGINAL", "RESOLVED"]
    decision: Action
    reason: str
    original_transfer_id: str
    original_status: str
    original_failure_type: str | None
    replacement_transfer_id: str | None
    replacement_status: str | None
    events: list[IncidentEvent]
    reconciliation: dict = Field(default_factory=dict)


class IncidentCommander:
    def __init__(self, executor):
        self.executor = executor
        self.bank = executor.bank
        self.store = executor.store

    async def handle(self, req: IncidentRequest) -> IncidentResult:
        started = perf_counter()
        events: list[IncidentEvent] = []
        state, policy = req.state, req.state.policy
        o = next((x for x in state.obligations if x.obligation_id == req.obligation_id), None)
        if o is None:
            raise KeyError(req.obligation_id)
        from .executor import _payout_key  # local import: executor imports this module

        key = _payout_key(o)
        payout = self.store.payout(key)
        if not payout or not payout.get("transfer_id"):
            raise LookupError(f"no transfer on record for {o.obligation_id}")
        # An operator asking again about an invoice that already has an incident must get THAT incident: once a
        # replacement exists the payout record points at the replacement, which is not a new original.
        # (Found on the real sandbox, 23 Oct 2026: a follow-up opened a second incident on the replacement.)
        latest = self.store.latest_incident(key)
        if latest and latest["status"] == "REPLACED_AWAITING_ORIGINAL":
            return await self._follow_up(latest, o, req, started)
        if latest and latest["status"] == "RESOLVED" and latest.get("replacement_transfer_id") == payout["transfer_id"]:
            return self._as_result(latest, o, events=[IncidentEvent(step="lookup", status="INFO", detail={
                "note": "the incident on this invoice is already resolved; the live payout is its replacement"})])
        original_id = payout["transfer_id"]
        incident = self.store.open_incident(key, o.obligation_id, original_id)

        transfer = await self.bank.get_transfer(original_id)
        if req.simulate_failure_type and self.bank.sandbox and not transfer.terminal:
            # Guide: simulate SENT before PAID; a failure type applies only after SENT; prefer a
            # FAILED transition with a failure type over a bare cancellation.
            if transfer.status != "SENT":
                transfer = await self.bank.simulate_transfer_transition(original_id, "SENT")
                events.append(IncidentEvent(step="simulate", status="INFO", detail={"status": transfer.status}))
            transfer = await self.bank.simulate_transfer_transition(original_id, "FAILED", req.simulate_failure_type)
            events.append(IncidentEvent(step="simulate", status="INFO",
                                        detail={"status": transfer.status, "failure_type": transfer.failure_type}))
        transfer = await self.bank.get_transfer(original_id)  # the rail's word, never the simulation response
        events.append(IncidentEvent(step="original", status="INFO", detail={
            "transfer_id": original_id, "status": transfer.status, "failure_type": transfer.failure_type,
            "amount": fmt(transfer.transfer_amount_minor, transfer.transfer_currency)}))

        replacements = self.store.replacements(incident["incident_id"])
        decision = decide(transfer, req.hours_in_flight, policy, len(replacements))
        events.append(IncidentEvent(step="decision", status="INFO",
                                    detail={"action": decision.action, "reason": decision.reason}))
        replacement: TransferResult | None = None
        recon: dict = {}
        status: str

        if decision.action == "RESOLVE":
            self.store.settle_payout(key, "PAID", original_id)
            status = "RESOLVED"
        elif decision.action == "WAIT":
            status = "WAITING"
        elif decision.action == "ESCALATE":
            status = "ESCALATED"
        else:
            replacement, recon, ok = await self._replace(state, o, key, incident, transfer, req, events)
            status = "REPLACED_AWAITING_ORIGINAL" if ok else "ESCALATED"
            if ok and transfer.status == "CANCELLED" and transfer.failure_type and recon.get("refund_observed"):
                status = "RESOLVED"  # replacement paid AND the original's fate (CANCELLED, type, refund) is recorded

        self.store.update_incident(incident["incident_id"], status=status, decision=decision.action, reason=decision.reason,
                                   failure_type=transfer.failure_type, replacement_transfer_id=replacement.transfer_id
                                   if replacement else None, record={"events": [e.model_dump() for e in events],
                                                                      "reconciliation": recon})
        await self.executor.audit_async("incident", {
            "incident_id": incident["incident_id"], "obligation_id": o.obligation_id, "original": original_id,
            "original_status": transfer.status, "failure_type": transfer.failure_type, "decision": decision.action,
            "replacement": replacement.transfer_id if replacement else None, "status": status,
            "duration_ms": (perf_counter() - started) * 1000})
        return IncidentResult(
            incident_id=incident["incident_id"], obligation_id=o.obligation_id, status=status, decision=decision.action,
            reason=decision.reason, original_transfer_id=original_id, original_status=transfer.status,
            original_failure_type=transfer.failure_type,
            replacement_transfer_id=replacement.transfer_id if replacement else None,
            replacement_status=replacement.status if replacement else None, events=events, reconciliation=recon)

    async def _follow_up(self, incident: dict, o, req: IncidentRequest, started: float) -> IncidentResult:
        """The replacement is out; the incident closes only when the ORIGINAL's fate is on file: its terminal
        status, its typed failure and the refund (recorded at replacement time). Until then it stays open."""
        events = [IncidentEvent(step="lookup", status="INFO", detail={"incident_id": incident["incident_id"],
                                                                        "status": incident["status"]})]
        original = await self.bank.get_transfer(incident["original_transfer_id"])
        replacement = await self.bank.get_transfer(incident["replacement_transfer_id"]) if incident.get("replacement_transfer_id") else None
        events.append(IncidentEvent(step="original", status="INFO", detail={
            "transfer_id": original.transfer_id, "status": original.status, "failure_type": original.failure_type}))
        full = self.store.get_incident(incident["incident_id"]) or {}
        record = full.get("record") or {}
        refund = (record.get("reconciliation") or {}).get("refund_observed", False)
        if original.status in ("CANCELLED", "FAILED") and original.failure_type and refund:
            reason = (f"original {original.transfer_id[:8]}… recorded {original.status} / {original.failure_type} with the refund; "
                      f"replacement {replacement.transfer_id[:8] if replacement else '-'}… {replacement.status if replacement else '-'}")
            record["events"] = record.get("events", []) + [e.model_dump() for e in events]
            self.store.close_incident(incident["incident_id"], reason=reason, record=record)
            status = "RESOLVED"
        else:
            reason = (f"original {original.transfer_id[:8]}… is {original.status}"
                      f"{' / ' + original.failure_type if original.failure_type else ' without a typed failure'}; "
                      "the incident stays open until its terminal status, failure type and refund are on file")
            status = "REPLACED_AWAITING_ORIGINAL"
        await self.executor.audit_async("incident", {
            "incident_id": incident["incident_id"], "obligation_id": o.obligation_id, "original": original.transfer_id,
            "original_status": original.status, "failure_type": original.failure_type, "decision": incident["decision"],
            "replacement": incident.get("replacement_transfer_id"), "status": status, "follow_up": True,
            "duration_ms": (perf_counter() - started) * 1000})
        return IncidentResult(
            incident_id=incident["incident_id"], obligation_id=o.obligation_id, status=status, decision=incident["decision"],
            reason=reason, original_transfer_id=original.transfer_id, original_status=original.status,
            original_failure_type=original.failure_type, replacement_transfer_id=incident.get("replacement_transfer_id"),
            replacement_status=replacement.status if replacement else None, events=events,
            reconciliation=record.get("reconciliation") or {})

    def _as_result(self, incident: dict, o, events: list[IncidentEvent]) -> IncidentResult:
        return IncidentResult(
            incident_id=incident["incident_id"], obligation_id=o.obligation_id, status=incident["status"],
            decision=incident["decision"], reason=incident.get("reason") or "", original_transfer_id=incident["original_transfer_id"],
            original_status="CANCELLED", original_failure_type=incident.get("failure_type"),
            replacement_transfer_id=incident.get("replacement_transfer_id"), replacement_status="PAID", events=events)

    async def _replace(self, state, o, key, incident, original, req, events):
        """Replacement under the duplicate lock, with the second fee budgeted and the refund verified."""
        from .executor import Halt, Step  # noqa: F401

        fee = self.store.observed_fees().get(f"{o.payout_method}:{o.currency}")
        if fee is None:
            fee = state.policy.swift_fee_minor if o.payout_method == "SWIFT" else 0
        before = await self.bank.balances_minor()
        available = before.get(o.currency, 0)
        need = o.amount_minor + fee
        events.append(IncidentEvent(step="budget", status="INFO", detail={
            "replacement_cost_minor": need, "second_fee_minor": fee, "wallet_available_minor": available,
            "currency": o.currency}))
        if available < need:
            events.append(IncidentEvent(step="budget", status="REFUSED", detail={
                "reason": f"{o.currency} wallet {fmt(available, o.currency)} does not cover the replacement plus its fee "
                          f"{fmt(need, o.currency)}; a conversion would be needed: re-plan"}))
            return None, {}, False

        # Duplicate lock: the original payout is recorded FAILED, then the same key is claimed for the
        # replacement. At most one live payout per invoice, ever.
        self.store.settle_payout(key, "FAILED", original.transfer_id)
        n = len(self.store.replacements(incident["incident_id"])) + 1
        op = self.store.operation(incident["incident_id"], f"replacement:{o.obligation_id}:{n}", "replacement")
        try:
            self.store.claim_payout(key, o.obligation_id, incident["incident_id"])
        except Exception as exc:  # another live payout for this invoice exists
            events.append(IncidentEvent(step="duplicate-lock", status="REFUSED", detail={"reason": str(exc)}))
            return None, {}, False
        events.append(IncidentEvent(step="duplicate-lock", status="OK", detail={
            "payout_key": key, "original_request_id": original.raw.get("request_id"),
            "replacement_request_id": op["request_id"]}))
        assert op["request_id"] != original.raw.get("request_id")

        beneficiary_id = self.store.beneficiary(o.beneficiary_id)
        if beneficiary_id is None:
            events.append(IncidentEvent(step="replacement", status="REFUSED", detail={"reason": "beneficiary not on record"}))
            self.store.settle_payout(key, "RELEASED", None)
            return None, {}, False
        reason = self.executor.beneficiaries.get(o.beneficiary_id, {}).get("transfer_reason", "business_expenses")
        self.store.update_operation(op["op_key"], "SUBMITTED")
        try:
            replacement = await self.bank.create_transfer(
                request_id=op["request_id"], beneficiary_id=beneficiary_id, currency=o.currency, amount_minor=o.amount_minor,
                transfer_method=o.payout_method,
                reference=f"{o.obligation_id} {o.invoice_fingerprint or ''} REPLACES {original.transfer_id[:8]}".strip(),
                reason=reason)
        except AirwallexError as exc:
            replacement = await self.executor._recover(exc, self.bank.find_transfer, op["request_id"])
            if replacement is None:
                self.store.update_operation(op["op_key"], "FAILED", response={"error": str(exc), "code": exc.code})
                self.store.settle_payout(key, "FAILED", None)
                events.append(IncidentEvent(step="replacement", status="FAILED", detail={"error": str(exc), "code": exc.code}))
                return None, {}, False
        self.store.update_operation(op["op_key"], "SUCCEEDED", replacement.transfer_id, replacement.raw)
        self.store.settle_payout(key, "IN_FLIGHT", replacement.transfer_id)
        events.append(IncidentEvent(step="replacement", status="OK", detail={
            "transfer_id": replacement.transfer_id, "request_id": op["request_id"], "status": replacement.status,
            "fee_minor": replacement.fee_minor}))

        if req.simulate_settlement and self.bank.sandbox:
            for nxt in ("SENT", "PAID"):
                if replacement.terminal or replacement.status == nxt:
                    continue
                try:
                    replacement = await self.bank.simulate_transfer_transition(replacement.transfer_id, nxt)
                except AirwallexError as exc:
                    events.append(IncidentEvent(step="simulate", status="FAILED", detail={"error": str(exc)}))
                    break
        # The book ledger: the replacement is a second commit on the same book (the original's refund is posted by
        # the poll when the rail reports it returned).
        from .. import ledger as book_ledger

        self.executor._book_id = state.book_id
        if self.store.has_book(state.book_id):
            self.executor._post(state.book_id, book_ledger.commit(replacement.source_currency or o.currency,
                                                                  replacement.transfer_amount_minor, replacement.fee_minor,
                                                                  replacement.transfer_id), None, [])
        polled = await self.executor._poll({o.obligation_id: (key, replacement)})
        _, replacement = polled[o.obligation_id]
        after = await self.bank.balances_minor()
        # Refund of the original (amount only; the guide says budget for a second fee) and the replacement's debit.
        # On the real sandbox the refund lands a few seconds after the FAILED transition: it may already be in
        # `before`, or it may arrive between `before` and `after` (observed 23 Oct 2026). Both are the same money.
        b, a = before.get(o.currency, 0), after.get(o.currency, 0)
        expected_refund_before = b - replacement.amount_payer_pays_minor
        expected_refund_during = expected_refund_before + original.transfer_amount_minor
        if a == expected_refund_before:
            expected, timing = expected_refund_before, "refund already in the wallet before the replacement"
        elif a == expected_refund_during:
            expected, timing = expected_refund_during, "refund landed while the replacement was being made"
        else:
            expected, timing = expected_refund_before, "unexplained"
        recon = {"before_minor": b, "after_minor": a, "expected_after_minor": expected, "discrepancy_minor": a - expected,
                 "second_fee_minor": replacement.fee_minor, "refund_observed": timing != "unexplained",
                 "refund_timing": timing, "currency": o.currency, "replacement_status": replacement.status}
        events.append(IncidentEvent(step="reconcile", status="OK" if recon["discrepancy_minor"] == 0 else "FAILED", detail=recon))
        return replacement, recon, replacement.status == "PAID"
