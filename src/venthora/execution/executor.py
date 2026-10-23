"""Execute a verified plan on Airwallex, then prove the outcome.

Order of operations (each step can halt the run; nothing later runs after a halt):

1. preflight   re-plan from the submitted state; plan_hash must match what was
               approved; BLOCKED never runs; REQUIRES_APPROVAL needs an approver
               whose role the policy allows; Airwallex must be sandbox.
2. quote       one fresh single-use quote per conversion. Before booking anything,
               re-check slippage and the reserve floor with the *quoted* sell
               amounts against *live* balances.
3. conversion  book each quote once (persisted request_id, used-quote registry).
4. transfer    claim the obligation (blocks duplicates across executions),
               resolve the beneficiary, create the transfer.
5. simulation  sandbox only: SENT -> PAID. SENT is in flight, never final.
6. reconcile   re-read transfers and balances; expected = before + conversions -
               amount_payer_pays; any difference or floor breach is reported.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..audit import AuditStore
from ..config import ROOT
from ..domain.models import PlanResult, TreasuryState
from ..domain.money import fmt
from ..domain.projection import obligation_needs
from ..gateways.airwallex import Airwallex, AirwallexError, ConversionResult, Quote, TransferResult
from ..orchestrator import Orchestrator
from .models import ExecutionRequest, ExecutionResult, Reconciliation, Step
from .store import DuplicatePayout, ExecutionStore, QuoteReused

PAYING_ACTIONS = ("PAY", "CONVERT_AND_PAY")
BENEFICIARIES = ROOT / "data" / "beneficiaries.json"


class Halt(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def load_beneficiaries(path: Path = BENEFICIARIES) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


class Executor:
    def __init__(self, agent: Orchestrator, store: ExecutionStore, audit: AuditStore,
                 beneficiaries: dict | None = None, poll_attempts: int = 5, poll_interval_s: float = 1.0):
        self.agent = agent
        self.bank: Airwallex = agent.proof.airwallex
        self.store = store
        self.audit = audit
        self.beneficiaries = beneficiaries if beneficiaries is not None else load_beneficiaries()
        self.poll_attempts = poll_attempts
        self.poll_interval_s = poll_interval_s
        self._run_lock = asyncio.Lock()

    # -- entry points ----------------------------------------------------------

    async def execute(self, req: ExecutionRequest) -> ExecutionResult:
        steps: list[Step] = []
        try:
            plan = await self._preflight(req, steps)
        except Halt as h:
            return ExecutionResult(execution_id=None, plan_hash=req.plan_hash, status=h.status, message=str(h), steps=steps)
        async with self._run_lock:
            before = await self.bank.balances_minor()
            execution_id = self.store.create_execution(plan.plan_hash, {
                "request": req.model_dump(mode="json"), "plan": plan.model_dump(mode="json"), "balances_before": before})
            return await self._run(execution_id, req.state, plan, before, req, steps)

    async def resume(self, execution_id: str) -> ExecutionResult:
        """Continue an interrupted run with the same request_ids (no new approval needed)."""
        record = self.store.get_execution(execution_id)
        if record is None:
            raise KeyError(execution_id)
        stored = record["request"]
        req = ExecutionRequest.model_validate(stored["request"])
        plan = PlanResult.model_validate(stored["plan"])
        steps = [Step(kind="preflight", key="resume", status="OK", detail={"previous_status": record["status"]})]
        async with self._run_lock:
            return await self._run(execution_id, req.state, plan, stored["balances_before"], req, steps)

    # -- phases ----------------------------------------------------------------

    async def _preflight(self, req: ExecutionRequest, steps: list[Step]) -> PlanResult:
        def refuse(message: str):
            steps.append(Step(kind="preflight", key="authorization", status="REFUSED", detail={"reason": message}))
            raise Halt("REFUSED", message)

        if not self.bank.configured:
            refuse("Airwallex sandbox credentials are not configured")
        if not self.bank.sandbox:
            refuse("execution is restricted to the Airwallex sandbox")
        plan = await self.agent.decide(req.state)
        if plan.plan_hash != req.plan_hash:
            refuse("state or plan changed since it was reviewed; re-plan and approve again")
        if plan.decision == "BLOCKED":
            refuse("ProofCore blocked this plan")
        policy = req.state.policy
        if plan.decision == "REQUIRES_APPROVAL":
            if req.approval is None:
                refuse("plan requires a person's approval: " + ", ".join(_approval_reasons(plan)))
            if req.approval.role not in policy.approver_roles:
                refuse(f"role {req.approval.role!r} may not approve; allowed: {policy.approver_roles}")
        steps.append(Step(kind="preflight", key="authorization", status="OK", detail={
            "decision": plan.decision, "approved_by": req.approval.approver if req.approval else "policy (autonomous)"}))
        return plan

    async def _run(self, eid: str, state: TreasuryState, plan: PlanResult, before: dict[str, int],
                   req: ExecutionRequest, steps: list[Step]) -> ExecutionResult:
        conversions: list[ConversionResult] = []
        transfers: dict[str, tuple[str, TransferResult]] = {}  # obligation_id -> (payout_key, transfer)
        status, message = "COMPLETED", "executed and reconciled"
        claims = self._claim_all(eid, state, plan, steps)
        try:
            if claims is None:
                raise Halt("FAILED", "duplicate payment blocked before any money moved")
            quotes = await self._quote_and_check(eid, state, plan, before, req, steps)
            for quote in quotes:
                conversions.append(await self._convert(eid, quote, steps))
            for d in plan.obligations:
                if d.action in PAYING_ACTIONS:
                    transfers[d.obligation_id] = await self._transfer(eid, state, d.obligation_id, steps)
            if req.simulate_settlement:
                await self._simulate(transfers, steps)
        except Halt as h:
            status, message = h.status, str(h)
            for oid, key in (claims or {}).items():  # release claims that never became transfers
                if oid not in transfers:
                    self.store.settle_payout(key, "RELEASED", None)

        transfers = await self._poll(transfers)
        recon = await self._reconcile(state, plan, before, conversions, transfers, steps)
        if status == "COMPLETED":
            failed = [oid for oid, (_, t) in transfers.items() if t.status in ("CANCELLED", "FAILED")]
            pending = [oid for oid, (_, t) in transfers.items() if not t.terminal]
            if failed:
                status, message = "FAILED", f"transfers failed: {', '.join(failed)}"
            elif pending:
                status, message = "IN_FLIGHT", f"transfers still in flight: {', '.join(pending)}"
            elif recon.discrepancies_minor or not recon.base_floor_ok:
                status, message = "RECONCILIATION_MISMATCH", "live balances differ from expected or reserve breached"
            else:
                base = state.base_currency
                message = (f"executed and reconciled; {base} reserve (managed budget) confirmed at "
                           f"{fmt(recon.managed_after_minor.get(base, 0), base)} >= {fmt(recon.base_floor_minor, base)}")
                if recon.plan_variance_minor:
                    message += ("; variance vs plan " + ", ".join(fmt(v, c) for c, v in sorted(recon.plan_variance_minor.items()))
                                + " (actual Airwallex fees; learned for the next plan)")
                over = {c: v for c, v in recon.managed_after_minor.items() if v < 0}
                if over:
                    message += "; budget exceeded by " + ", ".join(fmt(-v, c) for c, v in sorted(over.items()))

        result = ExecutionResult(execution_id=eid, plan_hash=plan.plan_hash, status=status, message=message,
                                 steps=steps, reconciliation=recon)
        self.store.finish_execution(eid, status, result.model_dump(mode="json"))
        await asyncio.to_thread(self.audit.append, "execution", {
            "execution_id": eid, "plan_hash": plan.plan_hash, "status": status,
            "approval": req.approval.model_dump() if req.approval else None,
            "operations": self.store.operations(eid), "reconciliation": recon.model_dump()})
        return result

    def _claim_all(self, eid: str, state: TreasuryState, plan: PlanResult, steps: list[Step]) -> dict[str, str] | None:
        """Claim every payout up front. Returns obligation_id -> payout_key, or None on a duplicate."""
        by_id = {o.obligation_id: o for o in state.obligations}
        claims: dict[str, str] = {}
        for d in plan.obligations:
            if d.action not in PAYING_ACTIONS:
                continue
            key = _payout_key(by_id[d.obligation_id])
            try:
                self.store.claim_payout(key, d.obligation_id, eid)
            except DuplicatePayout as exc:
                steps.append(Step(kind="preflight", key=f"claim:{d.obligation_id}", status="REFUSED",
                                  detail={"reason": str(exc)}))
                for claimed in claims.values():
                    self.store.settle_payout(claimed, "RELEASED", None)
                return None
            claims[d.obligation_id] = key
        steps.append(Step(kind="preflight", key="payout-claims", status="OK", detail={"claimed": sorted(claims)}))
        return claims

    async def _quote_and_check(self, eid, state, plan, before, req, steps) -> list[Quote]:
        base = state.base_currency
        by_id = {o.obligation_id: o for o in state.obligations}
        base_payments = sum(obligation_needs(by_id[d.obligation_id], state).get(base, 0)
                            for d in plan.obligations if d.action in PAYING_ACTIONS)
        quotes = []
        for conv in plan.conversions:
            op = self.store.operation(eid, f"conversion:{conv.buy_currency}", "conversion")
            if op["status"] == "FAILED":
                raise Halt("FAILED", f"conversion to {conv.buy_currency} failed in an earlier attempt; re-plan")
            if op["status"] in ("SUBMITTED", "SUCCEEDED") and op["response"]:
                quotes.append(_quote_from_record(op["response"]["quote"]))  # resume: never re-quote a booked op
                continue
            try:
                quote = await self.bank.create_quote(sell_currency=conv.sell_currency, buy_currency=conv.buy_currency,
                                                     buy_amount_minor=conv.buy_amount_minor)
            except AirwallexError as exc:
                steps.append(Step(kind="quote", key=conv.buy_currency, status="FAILED", detail={"error": str(exc)}))
                raise Halt("FAILED", f"quote for {conv.buy_currency} failed: {exc}") from exc
            limit = -(-conv.estimated_sell_minor * (10_000 + req.max_quote_slippage_bp) // 10_000)
            steps.append(Step(kind="quote", key=conv.buy_currency, status="OK", airwallex_id=quote.quote_id, detail={
                "sell_minor": quote.sell_amount_minor, "estimate_minor": conv.estimated_sell_minor,
                "slippage_limit_minor": limit, "client_rate": str(quote.client_rate), "usage": quote.usage}))
            if quote.buy_amount_minor != conv.buy_amount_minor or quote.buy_currency != conv.buy_currency:
                raise Halt("FAILED", f"quote for {conv.buy_currency} does not match the plan")
            if quote.sell_amount_minor > limit:
                raise Halt("FAILED", f"{conv.buy_currency} quote costs {fmt(quote.sell_amount_minor, base)}, above the "
                                     f"slippage limit {fmt(limit, base)}; nothing booked, re-plan")
            quotes.append(quote)

        projected = before.get(base, 0) - base_payments - sum(q.sell_amount_minor for q in quotes)
        ok = projected >= plan.required_base_floor_minor
        steps.append(Step(kind="preflight", key="reserve-at-quoted-rates", status="OK" if ok else "FAILED", detail={
            "live_base_minor": before.get(base, 0), "projected_after_minor": projected,
            "floor_minor": plan.required_base_floor_minor}))
        if not ok:
            raise Halt("FAILED", "at quoted rates the plan would breach the reserve floor; nothing booked")
        return quotes

    async def _convert(self, eid: str, quote: Quote, steps: list[Step]) -> ConversionResult:
        op = self.store.operation(eid, f"conversion:{quote.buy_currency}", "conversion")
        if op["status"] == "SUCCEEDED":
            result = _conversion_from_record(op["response"]["conversion"])
            steps.append(Step(kind="conversion", key=quote.buy_currency, status="OK", request_id=op["request_id"],
                              airwallex_id=result.conversion_id, detail={"resumed": True}))
            return result
        result = await self._find(self.bank.find_conversion, op["request_id"]) if op["status"] == "SUBMITTED" else None
        if result is None:
            if quote.expired():
                raise Halt("FAILED", f"quote {quote.quote_id} expired before booking; re-plan")
            if op["status"] == "PENDING":
                try:
                    self.store.use_quote(quote.quote_id, eid)
                except QuoteReused as exc:
                    raise Halt("FAILED", str(exc)) from exc
                self.store.update_operation(op["op_key"], "SUBMITTED", response={"quote": _quote_record(quote)})
            result = await self._book_conversion(op, quote, steps)
        self.store.update_operation(op["op_key"], "SUCCEEDED" if result.status == "SETTLED" else "SUBMITTED",
                                    result.conversion_id, {"quote": _quote_record(quote),
                                                           "conversion": _conversion_record(result)})
        steps.append(Step(kind="conversion", key=quote.buy_currency, status="OK" if result.status == "SETTLED" else "IN_FLIGHT",
                          request_id=op["request_id"], airwallex_id=result.conversion_id, detail={
                              "status": result.status, "sell_minor": result.sell_amount_minor,
                              "buy_minor": result.buy_amount_minor, "client_rate": str(result.client_rate)}))
        if result.status != "SETTLED":
            raise Halt("IN_FLIGHT", f"conversion {result.conversion_id} is {result.status}; transfers held")
        return result

    async def _book_conversion(self, op: dict, quote: Quote, steps: list[Step]) -> ConversionResult:
        try:
            return await self.bank.create_conversion(request_id=op["request_id"], quote=quote)
        except AirwallexError as exc:
            result = await self._recover(exc, self.bank.find_conversion, op["request_id"])
            if result is not None:
                return result
            self.store.update_operation(op["op_key"], "FAILED", response={"quote": _quote_record(quote), "error": str(exc)})
            steps.append(Step(kind="conversion", key=quote.buy_currency, status="FAILED",
                              request_id=op["request_id"], detail={"error": str(exc), "code": exc.code}))
            raise Halt("FAILED", f"conversion to {quote.buy_currency} failed: {exc}") from exc

    async def _transfer(self, eid: str, state: TreasuryState, obligation_id: str,
                        steps: list[Step]) -> tuple[str, TransferResult]:
        o = next(x for x in state.obligations if x.obligation_id == obligation_id)
        payout_key = _payout_key(o)
        op = self.store.operation(eid, f"transfer:{o.obligation_id}", "transfer")
        if op["status"] == "SUCCEEDED":
            transfer = await self.bank.get_transfer(op["airwallex_id"])
            steps.append(Step(kind="transfer", key=o.obligation_id, status="OK", request_id=op["request_id"],
                              airwallex_id=transfer.transfer_id, detail={"resumed": True, "status": transfer.status}))
            return payout_key, transfer
        if op["status"] == "FAILED":
            raise Halt("FAILED", f"transfer {o.obligation_id} failed in an earlier attempt; re-plan")
        try:
            self.store.claim_payout(payout_key, o.obligation_id, eid)
        except DuplicatePayout as exc:
            steps.append(Step(kind="transfer", key=o.obligation_id, status="REFUSED", detail={"reason": str(exc)}))
            raise Halt("FAILED", f"duplicate payment blocked: {exc}") from exc

        transfer = await self._find(self.bank.find_transfer, op["request_id"]) if op["status"] == "SUBMITTED" else None
        if transfer is None:
            transfer = await self._create_transfer(op, o, payout_key, steps)
        self.store.update_operation(op["op_key"], "SUCCEEDED", transfer.transfer_id, transfer.raw)
        self.store.record_fee(f"{o.payout_method}:{transfer.source_currency or o.currency}", transfer.fee_minor,
                              transfer.transfer_id)
        self.store.settle_payout(payout_key, "IN_FLIGHT", transfer.transfer_id)
        steps.append(Step(kind="transfer", key=o.obligation_id, status="OK", request_id=op["request_id"],
                          airwallex_id=transfer.transfer_id, detail={
                              "status": transfer.status, "amount": fmt(o.amount_minor, o.currency),
                              "method": o.payout_method, "payer_pays_minor": transfer.amount_payer_pays_minor,
                              "fee_minor": transfer.fee_minor}))
        return payout_key, transfer

    async def _create_transfer(self, op: dict, o, payout_key: str, steps: list[Step]) -> TransferResult:
        beneficiary_id = await self._beneficiary(o.beneficiary_id, steps)
        reason = self.beneficiaries[o.beneficiary_id].get("transfer_reason", "business_expenses")
        self.store.update_operation(op["op_key"], "SUBMITTED")
        try:
            return await self.bank.create_transfer(
                request_id=op["request_id"], beneficiary_id=beneficiary_id, currency=o.currency,
                amount_minor=o.amount_minor, transfer_method=o.payout_method,
                reference=f"{o.obligation_id} {o.invoice_fingerprint or ''}".strip(), reason=reason)
        except AirwallexError as exc:
            transfer = await self._recover(exc, self.bank.find_transfer, op["request_id"])
            if transfer is not None:
                return transfer
            self.store.update_operation(op["op_key"], "FAILED", response={"error": str(exc), "code": exc.code})
            self.store.settle_payout(payout_key, "FAILED", None)
            steps.append(Step(kind="transfer", key=o.obligation_id, status="FAILED", request_id=op["request_id"],
                              detail={"error": str(exc), "code": exc.code}))
            raise Halt("FAILED", f"transfer {o.obligation_id} failed: {exc}") from exc

    async def _beneficiary(self, internal_id: str, steps: list[Step]) -> str:
        existing = self.store.beneficiary(internal_id)
        if existing:
            return existing
        entry = self.beneficiaries.get(internal_id)
        if entry is None:
            steps.append(Step(kind="beneficiary", key=internal_id, status="FAILED", detail={"reason": "not registered"}))
            raise Halt("FAILED", f"beneficiary {internal_id} is not in the approved payee registry")
        try:
            airwallex_id = await self.bank.create_beneficiary(entry["payload"])
        except AirwallexError as exc:
            steps.append(Step(kind="beneficiary", key=internal_id, status="FAILED",
                              detail={"error": str(exc), "code": exc.code}))
            raise Halt("FAILED", f"beneficiary {internal_id} rejected by Airwallex: {exc}") from exc
        self.store.save_beneficiary(internal_id, airwallex_id)
        steps.append(Step(kind="beneficiary", key=internal_id, status="OK", airwallex_id=airwallex_id))
        return airwallex_id

    async def _simulate(self, transfers: dict[str, tuple[str, TransferResult]], steps: list[Step]) -> None:
        for oid, (key, t) in list(transfers.items()):
            for next_status in ("SENT", "PAID"):
                if t.terminal or t.status == next_status:
                    continue
                try:
                    t = await self.bank.simulate_transfer_transition(t.transfer_id, next_status)
                except AirwallexError as exc:
                    steps.append(Step(kind="simulation", key=oid, status="FAILED", airwallex_id=t.transfer_id,
                                      detail={"next_status": next_status, "error": str(exc)}))
                    break
                steps.append(Step(kind="simulation", key=oid, status="OK", airwallex_id=t.transfer_id,
                                  detail={"status": t.status}))
            transfers[oid] = (key, t)

    async def _poll(self, transfers: dict[str, tuple[str, TransferResult]]) -> dict[str, tuple[str, TransferResult]]:
        """Re-read every transfer from Airwallex; a create response is never proof of payment."""
        out = {}
        for oid, (key, t) in transfers.items():
            for attempt in range(self.poll_attempts):
                with contextlib.suppress(AirwallexError):  # keep the last known status
                    t = await self.bank.get_transfer(t.transfer_id)
                if t.terminal:
                    break
                if attempt < self.poll_attempts - 1:
                    await asyncio.sleep(self.poll_interval_s)
            status = "PAID" if t.status == "PAID" else "FAILED" if t.status in ("CANCELLED", "FAILED") else "IN_FLIGHT"
            self.store.settle_payout(key, status, t.transfer_id)
            out[oid] = (key, t)
        return out

    async def _reconcile(self, state, plan, before, conversions, transfers, steps) -> Reconciliation:
        expected = dict(before)
        for c in conversions:
            expected[c.sell_currency] = expected.get(c.sell_currency, 0) - c.sell_amount_minor
            expected[c.buy_currency] = expected.get(c.buy_currency, 0) + c.buy_amount_minor
        for _, t in transfers.values():
            if t.status not in ("CANCELLED", "FAILED"):
                expected[t.source_currency] = expected.get(t.source_currency, 0) - t.amount_payer_pays_minor
        try:
            live = await self.bank.balances_minor()
        except AirwallexError as exc:
            steps.append(Step(kind="reconcile", key="balances", status="FAILED", detail={"error": str(exc)}))
            live = {}
        diffs = {c: live.get(c, 0) - v for c, v in expected.items() if live.get(c, 0) != v}
        # The managed budget (the plan's balances, possibly ring-fenced below the wallet) moves by
        # exactly what moved in the live wallet; the reserve floor is checked on that budget.
        managed = {c: v + live.get(c, 0) - before.get(c, 0) for c, v in state.balances.items()} if live else {}
        # Plan vs actual: what the plan projected for the budget, against what really moved
        # (e.g. a fee Airwallex charged that the plan did not assume).
        gate = next((s for s in plan.stages if s.engine == "ProofCore · invariants"), None)
        planned = (gate.details.get("balances_after") if gate else None) or {}
        variance = {c: managed[c] - planned[c] for c in managed if c in planned and managed[c] != planned[c]}
        base = state.base_currency
        floor_ok = managed.get(base, 0) >= plan.required_base_floor_minor if live else False
        steps.append(Step(kind="reconcile", key="balances", status="OK" if not diffs and floor_ok else "FAILED", detail={
            "expected_minor": {c: expected[c] for c in state.balances if c in expected}, "discrepancies_minor": diffs,
            "managed_after_minor": managed, "plan_variance_minor": variance, "base_floor_ok": floor_ok}))
        return Reconciliation(balances_before_minor=before, expected_after_minor=expected, live_after_minor=live,
                              managed_after_minor=managed, plan_variance_minor=variance, discrepancies_minor=diffs,
                              base_floor_minor=plan.required_base_floor_minor, base_floor_ok=floor_ok)

    @staticmethod
    async def _find(finder, request_id: str):
        try:
            return await finder(request_id)
        except AirwallexError:
            return None

    @staticmethod
    async def _recover(exc: AirwallexError, finder, request_id: str):
        """A duplicate_request_id or lost response means Airwallex may already have the operation."""
        if exc.code == "duplicate_request_id" or exc.status is None or exc.status >= 500:
            try:
                return await finder(request_id)
            except AirwallexError:
                return None
        return None


def _payout_key(o) -> str:
    return f"invoice:{o.invoice_fingerprint}" if o.invoice_fingerprint else f"obligation:{o.obligation_id}"


def _approval_reasons(plan: PlanResult) -> list[str]:
    policy = next((s for s in plan.stages if s.engine == "Policy"), None)
    return (policy.details.get("local", {}).get("requires_approval", []) if policy else []) or ["policy"]


def _quote_record(q: Quote) -> dict:
    return {"quote_id": q.quote_id, "sell_currency": q.sell_currency, "buy_currency": q.buy_currency,
            "sell_amount_minor": q.sell_amount_minor, "buy_amount_minor": q.buy_amount_minor,
            "client_rate": str(q.client_rate), "valid_to_at": q.valid_to_at.isoformat() if q.valid_to_at else None,
            "usage": q.usage}


def _quote_from_record(r: dict) -> Quote:
    return Quote(quote_id=r["quote_id"], sell_currency=r["sell_currency"], buy_currency=r["buy_currency"],
                 sell_amount_minor=r["sell_amount_minor"], buy_amount_minor=r["buy_amount_minor"],
                 client_rate=Decimal(r["client_rate"]),
                 valid_to_at=datetime.fromisoformat(r["valid_to_at"]) if r["valid_to_at"] else None,
                 usage=r["usage"], raw={})


def _conversion_record(c: ConversionResult) -> dict:
    return {"conversion_id": c.conversion_id, "status": c.status, "sell_currency": c.sell_currency,
            "buy_currency": c.buy_currency, "sell_amount_minor": c.sell_amount_minor,
            "buy_amount_minor": c.buy_amount_minor, "client_rate": str(c.client_rate), "quote_id": c.quote_id}


def _conversion_from_record(r: dict) -> ConversionResult:
    return ConversionResult(conversion_id=r["conversion_id"], status=r["status"], sell_currency=r["sell_currency"],
                            buy_currency=r["buy_currency"], sell_amount_minor=r["sell_amount_minor"],
                            buy_amount_minor=r["buy_amount_minor"], client_rate=Decimal(r["client_rate"]),
                            quote_id=r["quote_id"], raw={})
