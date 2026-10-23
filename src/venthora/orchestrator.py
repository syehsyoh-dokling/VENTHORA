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
from .proofcore import frame
from .proofcore.counterfactual import counterfactuals
from .proofcore.gate import ProofCore
from .proofcore.policy import forecast_confidence
from .proofcore.simulate import Simulation, simulate


def theorem_cost(state: TreasuryState, o) -> int:
    return _cost(state, [o], (1,))


def _policy_limit(stages) -> int:
    policy = next((s for s in stages if s.engine == "Policy"), None)
    return int((policy.details.get("local") or {}).get("conversion_limit_minor", 0)) if policy else 0


def approval_terms(state: TreasuryState, decisions, conversions) -> list[dict]:
    """Exactly what an approver signs (guide: amount, currency, counterparty, terms, evidence)."""
    evidence = {e.ref: e for e in state.evidence}
    by_id = {o.obligation_id: o for o in state.obligations}
    terms = []
    for d in decisions:
        if d.action not in ("PAY", "CONVERT_AND_PAY"):
            continue
        o = by_id[d.obligation_id]
        ev = evidence.get(o.evidence_ref)
        terms.append({"kind": "payout", "obligation_id": o.obligation_id, "amount": fmt(o.amount_minor, o.currency),
                      "counterparty": o.payee_name or o.beneficiary_id, "beneficiary_id": o.beneficiary_id,
                      "method": o.payout_method, "due_in_hours": o.due_in_hours,
                      "evidence": {"ref": o.evidence_ref, "source": ev.source if ev else None,
                                   "summary": ev.summary if ev else None}})
    for c in conversions:
        terms.append({"kind": "conversion", "sell": c.sell_currency, "buy": fmt(c.buy_amount_minor, c.buy_currency),
                      "estimated_cost": fmt(c.estimated_sell_minor, c.sell_currency),
                      "terms": "fresh single-use quote; aborted if above slippage limit or reserve at quoted rate"})
    return terms

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
            t = await theorem(state, v.eligible, v.rejected, h, locked=locked, exceptions=v.exceptions)
            remote_stages += await self._apply_remote_theorem(state, v, h, t)
            sim = await asyncio.to_thread(simulate, state, t.decisions, h.stress_minor, h.receivables)
            gate = await self.proof.verify(state, t.selected, set(v.eligible), t.decisions,
                                           h.stress_minor, sim.breach_probability_bp)
        plan = self._assemble(state, v, h, t, gate, remote_stages, started, sim)
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
            sim = await asyncio.to_thread(simulate, state, decisions, h.stress_minor, h.receivables)
            gate = await self.proof.verify(state, list(proposal), set(v.eligible), decisions,
                                           h.stress_minor, sim.breach_probability_bp)
        return self._assemble(state, v, h, t, gate, [], started, sim)

    def _assemble(self, state, v, h, t, gate, remote_stages, started, sim: Simulation) -> PlanResult:
        sim_stage = StageResult(
            engine="Horizon · 72h simulation",
            status="WARN" if sim.breach_probability_bp > state.policy.max_breach_probability_bp else "INFO",
            summary=(f"P(reserve breached within {state.policy.planning_horizon_hours}h) = "
                     f"{sim.breach_probability_bp / 100:.1f}% over {sim.paths} paths; "
                     f"{sim.path_events_per_second:,} path-events/s"),
            details=sim.as_dict(), duration_ms=sim.elapsed_ms)
        cfs = counterfactuals(state, t.selected, t.decisions, set(v.eligible), h.stress_minor,
                              [r for r in h.receivables if r.reliable])
        for cf in cfs:  # the same probabilistic question, asked of every alternative
            cf.breach_probability_bp = simulate(state, _as_decisions(t.decisions, cf.pays), h.stress_minor,
                                                h.receivables, n_paths=min(state.policy.simulation_paths, 1000)
                                                ).breach_probability_bp
        frame_started = perf_counter()
        worked = frame.build(state, t.decisions, gate.projection.conversions, h.stress_minor, set(v.eligible))
        frame_problems = frame.check(state, worked, t.decisions)
        frame_stage = StageResult(
            engine="Theorem · decision frame", status="FAIL" if frame_problems else "PASS",
            summary=("given/asked/formula/answer complete; identities recompute"
                     + ("; " + worked["answer"]["not_payable_as_posed"]["statement"]
                        if "not_payable_as_posed" in worked["answer"] else ""))
            if not frame_problems else "; ".join(frame_problems),
            details={"problems": frame_problems}, duration_ms=(perf_counter() - frame_started) * 1000)
        stages = [v.stage, h.stage, sim_stage, *remote_stages, t.stage, *gate.stages, frame_stage]
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
            counterfactuals=cfs, forecast_confidence_bp=forecast_confidence(state),
            conversion_limit_minor=_policy_limit(gate.stages), approval_terms=approval_terms(state, t.decisions,
                                                                                            gate.projection.conversions),
            frame=worked, simulation=sim.as_dict(), duration_ms=(perf_counter() - started) * 1000,
            budget=next((s.details for s in gate.stages if s.engine == "Budget · mandate"), {"attached": False}),
            approval_requirement=next((s.details for s in gate.stages if s.engine == "Policy · approvals"), None)
            if decision == "REQUIRES_APPROVAL" else None,
            aging=next((s.details for s in gate.stages if s.engine == "Verity · aging"), {}))


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
            t.decisions = classify(state, t.selected, v.eligible, v.rejected, h, v.exceptions)
            t.solver = "remote-theorem"
            return [_remote_stage("Theorem", "PASS", "remote selection accepted for verification")]
        except Exception as exc:
            t.selected = []
            t.decisions = classify(state, [], v.eligible, v.rejected, h, v.exceptions)
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
    pipe = next((s.details for s in gate.stages if s.engine == "Budget · mandate"), None)
    if pipe:
        for r in pipe["lines"]:
            lines.append(f"Budget line '{r['line']}' ({pipe['period']}): mandate {fmt(r['mandate_minor'], base)}, realized "
                         f"{fmt(r['realized_minor'], base)}, paying now {fmt(r['paying_now_minor'], base)}, still owed "
                         f"{fmt(r['committed_minor'], base)}, available {fmt(r['available_minor'], base)}"
                         + (f", MINUS {fmt(r['minus_minor'], base)}" if r["minus_minor"] else "") + f" [{r['status']}].")
        for a in pipe["alerts"]:
            lines.append(f"ALERT: {a['message']}.")
        for q in pipe["requests"]:
            lines.append(f"BUDGET REQUEST: {q['message']} (obligations {', '.join(q['obligations'])}).")
        for u in pipe["unbudgeted"]:
            lines.append(f"Unbudgeted: {u['obligation_id']} draws on no line of {pipe['period']}; a person must place it.")
    conf = forecast_confidence(state)
    limit = _policy_limit(gate.stages)
    conversion = sum(c.estimated_sell_minor for c in gate.projection.conversions)
    lines.append(f"Autonomous FX limit {fmt(limit, base)} at forecast confidence "
                 f"{'n/a (no open receipt)' if conf is None else f'{conf / 100:.0f}%'}; planned conversions "
                 f"{fmt(conversion, base)}.")
    escalated = [d.obligation_id for d in t.decisions if d.action == "ESCALATE"]
    if escalated:
        lines.append("Escalated to a person (not paid, the rest of the plan can proceed): " + ", ".join(escalated) + ".")
    if decision == "REQUIRES_APPROVAL":
        req = next((s.details for s in gate.stages if s.engine == "Policy · approvals"), None)
        roles = ", ".join(req["roles"]) if req else "finance manager"
        who = (f"{req['approvals_required']} different people ({roles}) must approve"
               if req and req["approvals_required"] > 1 else f"a person ({roles}) must approve")
        lines.append(f"{who[0].upper() + who[1:]}: " + ", ".join(gate.requires_approval) + ".")
    elif decision == "BLOCKED":
        lines.append("Blocked: " + "; ".join(f"{s.engine}: {s.summary}" for s in stages if s.status == "FAIL") + ".")
    return lines


def _as_decisions(decisions, pays: list[str]):
    """Decisions for a counterfactual: `pays` are paid now; everything else keeps its non-paying action."""
    out = []
    for d in decisions:
        if d.obligation_id in pays:
            out.append(d.model_copy(update={"action": "PAY"}))
        elif d.action in ("PAY", "CONVERT_AND_PAY"):
            out.append(d.model_copy(update={"action": "DEFER"}))
        else:
            out.append(d)
    return out

