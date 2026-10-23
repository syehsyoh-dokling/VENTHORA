"""The VENTHORA agent loop: understand -> anticipate -> optimize -> prove.

    Verity  ┐
            ├─> Theorem ─> ProofCore ─> APPROVED | REQUIRES_APPROVAL | BLOCKED
    Horizon ┘

Verity and Horizon run concurrently; Theorem plans on their outputs; ProofCore
re-derives the outcome independently and has the final word.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from time import perf_counter

from .domain import diff
from .domain.models import ObligationDecision, PlanResult, Recalculation, StageResult, TreasuryState
from .domain.money import fmt
from .domain.projection import obligation_needs
from .engines import adapters
from .engines.horizon import HorizonResult, horizon
from .engines.theorem import TheoremResult, _cost, classify, theorem
from .engines.verity import VerityResult, verity
from .proofcore.counterfactual import counterfactuals
from .proofcore.gate import ProofCore


def theorem_cost(state: TreasuryState, o) -> int:
    return _cost(state, [o], (1,))

ACTION_LABELS = {"PAY": "Pay", "CONVERT_AND_PAY": "Convert & pay", "DEFER": "Defer",
                 "ESCALATE": "Escalate", "REJECT": "Reject"}


def plan_hash(state: TreasuryState, decision: str, decisions, conversions) -> str:
    canonical = {"state": state.model_dump(mode="json"), "decision": decision,
                 "obligations": [d.model_dump(mode="json") for d in decisions],
                 "conversions": [c.model_dump(mode="json") for c in conversions]}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _remote_stage(name: str, status: str, summary: str) -> StageResult:
    return StageResult(engine=f"{name} · remote engine", status=status, summary=summary, details={}, duration_ms=0.0)


class Orchestrator:
    def __init__(self, proofcore: ProofCore | None = None, max_inflight: int = 8):
        self.proof = proofcore or ProofCore()
        self._inflight = asyncio.Semaphore(max_inflight)

    async def close(self) -> None:
        await self.proof.close()

    async def decide(self, state: TreasuryState,
                     previous: tuple[TreasuryState, PlanResult] | None = None) -> PlanResult:
        """Plan and prove. With `previous` (the earlier state and plan), recalculate only the
        decisions the new information affects: earlier payments whose own inputs did not change
        stay locked, open decisions are re-optimised, and the result carries a decision diff."""
        started = perf_counter()
        locked = diff.locked_payments(previous[0], previous[1], state) if previous else None
        async with self._inflight:
            v, h = await asyncio.gather(verity(state), horizon(state))
            remote_stages = await self._apply_remote_evidence_and_risk(state, v, h)
            t = await theorem(state, v.eligible, v.rejected, h, locked=locked)
            remote_stages += await self._apply_remote_theorem(state, v, h, t)
            gate = await self.proof.verify(state, t.selected, set(v.eligible), t.decisions,
                                           h.stress_minor, h.low_confidence)
        plan = self._assemble(state, v, h, t, gate, remote_stages, started)
        if previous:
            recalc = t.stage.details["recalculation"]
            plan.recalculation = Recalculation(
                mode=recalc["mode"], triggers=diff.input_changes(previous[0], state), locked=recalc["locked"],
                reconsidered=sorted(set(v.eligible) - set(recalc["locked"])),
                changes=diff.decision_changes(previous[1], plan), fallback_reason=recalc["fallback_reason"])
        return plan

    async def challenge(self, state: TreasuryState, proposal: list[str], source: str) -> PlanResult:
        """Run an outside recommendation (e.g. an LLM's 'pay these') through the same gate.
        The proposal replaces Theorem; ProofCore and policy judge it exactly like a plan."""
        started = perf_counter()
        async with self._inflight:
            v, h = await asyncio.gather(verity(state), horizon(state))
            by_id = {o.obligation_id: o for o in state.obligations}
            chosen = [i for i in dict.fromkeys(proposal) if i in by_id]
            decisions = []
            for o in state.obligations:
                cost = theorem_cost(state, o)
                if o.obligation_id in chosen:
                    foreign = any(c != state.base_currency for c in obligation_needs(o, state))
                    decisions.append(ObligationDecision(
                        obligation_id=o.obligation_id, action="CONVERT_AND_PAY" if foreign else "PAY",
                        amount_minor=o.amount_minor, currency=o.currency, base_cost_minor=cost,
                        reason=f"proposed by {source}"))
                else:
                    decisions.append(ObligationDecision(
                        obligation_id=o.obligation_id, action="DEFER", amount_minor=o.amount_minor,
                        currency=o.currency, base_cost_minor=cost, reason=f"not proposed by {source}"))
            stage = StageResult(engine=f"{source} · proposal", status="INFO",
                                summary=f"proposes paying {len(chosen)}: {', '.join(chosen) or 'nothing'}",
                                details={"proposal": proposal, "unknown_ids": sorted(set(proposal) - set(by_id))},
                                duration_ms=0.0)
            t = TheoremResult(chosen, decisions, source, 0, stage)
            gate = await self.proof.verify(state, list(proposal), set(v.eligible), decisions,
                                           h.stress_minor, h.low_confidence)
        return self._assemble(state, v, h, t, gate, [], started)

    def _assemble(self, state, v, h, t, gate, remote_stages, started) -> PlanResult:
        stages = [v.stage, h.stage, *remote_stages, t.stage, *gate.stages]
        if any(s.status == "FAIL" for s in stages):
            decision = "BLOCKED"
        elif gate.requires_approval:
            decision = "REQUIRES_APPROVAL"
        else:
            decision = "APPROVED"

        base = state.base_currency
        floor = state.policy.reserve_floor_minor + h.stress_minor
        base_after = gate.projection.balances_after.get(base, state.balance(base))
        return PlanResult(
            case_id=state.case_id, plan_hash=plan_hash(state, decision, t.decisions, gate.projection.conversions),
            decision=decision, obligations=t.decisions,
            conversions=gate.projection.conversions, total_base_cost_minor=gate.projection.base_cost_minor,
            projected_base_balance_minor=base_after, required_base_floor_minor=floor, solver=t.solver,
            stages=stages, explanation=_explain(state, t, gate, stages, decision, base_after, floor),
            counterfactuals=counterfactuals(state, t.selected, t.decisions, set(v.eligible), h.stress_minor,
                                            [r for r in h.receivables if r.reliable]),
            duration_ms=(perf_counter() - started) * 1000)

    async def _apply_remote_evidence_and_risk(self, state, v: VerityResult, h: HorizonResult) -> list[StageResult]:
        stages = []
        for name in ("VERITY", "HORIZON"):
            try:
                url = adapters.endpoint(name)
                if not url:
                    continue
                if name == "VERITY":
                    body = await adapters.call(url, {"contract": "venthora.verity.v1", "state": state.model_dump()})
                    keep = adapters.narrow_eligible(set(v.eligible), body)
                    for oid in set(v.eligible) - keep:
                        v.rejected[oid] = "remote Verity did not confirm the evidence"
                        del v.eligible[oid]
                else:
                    body = await adapters.call(url, {"contract": "venthora.horizon.v1", "state": state.model_dump(),
                                                     "local_stress_minor": h.stress_minor})
                    h.stress_minor = adapters.widen_stress(h.stress_minor, body)
                stages.append(_remote_stage(name.title(), "PASS", "remote result applied (narrowing only)"))
            except Exception as exc:  # fail closed
                stages.append(_remote_stage(name.title(), "FAIL", f"remote engine error: {type(exc).__name__}"))
        return stages

    async def _apply_remote_theorem(self, state, v: VerityResult, h: HorizonResult, t: TheoremResult):
        try:
            url = adapters.endpoint("THEOREM")
            if not url:
                return []
            body = await adapters.call(url, {"contract": "venthora.theorem.v1", "state": state.model_dump(),
                                             "eligible_ids": sorted(v.eligible), "budget_minor": t.budget_minor})
            t.selected = adapters.checked_selection(set(v.eligible), body)
            t.decisions = classify(state, t.selected, v.eligible, v.rejected, h)
            t.solver = "remote-theorem"
            return [_remote_stage("Theorem", "PASS", "remote selection accepted for verification")]
        except Exception as exc:
            t.selected = []
            t.decisions = classify(state, [], v.eligible, v.rejected, h)
            return [_remote_stage("Theorem", "FAIL", f"remote engine error: {type(exc).__name__}")]


def _explain(state, t: TheoremResult, gate, stages, decision: str, base_after: int, floor: int) -> list[str]:
    base = state.base_currency
    lines = []
    for c in gate.projection.conversions:
        lines.append(f"Convert {fmt(c.estimated_sell_minor, c.sell_currency)} -> {fmt(c.buy_amount_minor, c.buy_currency)}"
                     " (estimate; executed only against a fresh Airwallex quote).")
    for d in t.decisions:
        lines.append(f"{ACTION_LABELS[d.action]} {d.obligation_id} {fmt(d.amount_minor, d.currency)}: {d.reason}.")
    lines.append(f"{base} balance after plan {fmt(base_after, base)} vs required floor {fmt(floor, base)}.")
    if decision == "REQUIRES_APPROVAL":
        lines.append("A finance manager must approve: " + ", ".join(gate.requires_approval) + ".")
    elif decision == "BLOCKED":
        lines.append("Blocked: " + "; ".join(f"{s.engine}: {s.summary}" for s in stages if s.status == "FAIL") + ".")
    return lines
