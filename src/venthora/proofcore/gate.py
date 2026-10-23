"""Independent verification of a proposed plan.

ProofCore recomputes the post-action balances from the raw state with its own
projection code, checks every invariant, evaluates policy (locally and, when
configured, in OPA), and cross-checks live balances. Any FAIL blocks the plan;
nothing here trusts a number produced by Theorem or a model.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter

from ..domain.models import Conversion, ObligationDecision, StageResult, TreasuryState
from ..domain.projection import Projection, project
from ..engines.theorem_fx import check_conversion_legs
from ..gateways.airwallex import Airwallex
from ..gateways.native import native_gate
from ..gateways.opa import OPA
from ..gateways.tigerbeetle import TigerBeetleLedger
from . import policy as local_policy


@dataclass
class GateResult:
    stages: list[StageResult]
    projection: Projection
    requires_approval: list[str]

    @property
    def failed(self) -> bool:
        return any(s.status == "FAIL" for s in self.stages)


def _stage(engine, ok, summary, details, started) -> StageResult:
    return StageResult(engine=engine, status="PASS" if ok else "FAIL", summary=summary, details=details,
                       duration_ms=(perf_counter() - started) * 1000)


class ProofCore:
    def __init__(self, airwallex: Airwallex | None = None):
        self.opa = OPA()
        self.ledger = TigerBeetleLedger()
        self.airwallex = airwallex or Airwallex()

    async def close(self) -> None:
        await asyncio.gather(self.airwallex.close(), self.ledger.close())

    async def verify(self, state: TreasuryState, selected: list[str], eligible: set[str],
                     decisions: list[ObligationDecision], stress_minor: int, low_confidence: bool) -> GateResult:
        started = perf_counter()
        by_id = {o.obligation_id: o for o in state.obligations}
        base = state.base_currency
        floor = state.policy.reserve_floor_minor + stress_minor

        structural = []
        if len(selected) != len(set(selected)):
            structural.append("obligation selected twice")
        if not set(selected) <= eligible:
            structural.append(f"unverified obligation selected: {sorted(set(selected) - eligible)}")
        fingerprints = [by_id[i].invoice_fingerprint for i in selected if i in by_id and by_id[i].invoice_fingerprint]
        if len(fingerprints) != len(set(fingerprints)):
            structural.append("two selected obligations share an invoice fingerprint")
        # Project even a structurally invalid proposal so every reason it fails is reported.
        proj = project(state, [i for i in dict.fromkeys(selected) if i in by_id])

        base_after = proj.balances_after.get(base, state.balance(base))
        # The floor binds any plan that spends base cash; a plan funded entirely from
        # foreign wallets leaves the base balance untouched.
        spends_base = proj.base_cost_minor > 0
        native = (native_gate(state.balance(base), proj.base_cost_minor, state.policy.reserve_floor_minor, stress_minor)
                  if spends_base else None)
        problems = structural + proj.errors
        if spends_base and base_after < floor:
            problems.append(f"{base} balance after plan {base_after} is below floor {floor}")
        if native is False:
            problems.append("native C++ gate rejected the reserve check")
        stages = [_stage("ProofCore · invariants", not problems,
                         "all invariants hold" if not problems else "; ".join(problems),
                         {"base_balance_after_minor": base_after, "required_floor_minor": floor,
                          "balances_after": proj.balances_after,
                          "conversions": [c.model_dump() for c in proj.conversions],
                          "native_gate": "not-configured" if native is None else native}, started)]
        if proj.conversions:
            legs_started = perf_counter()
            legs_ok, legs = check_conversion_legs(state, proj.conversions)
            stages.append(_stage("Theorem · FX legs", legs_ok,
                                 "quant toolkit nets conversions to the projected shortfalls" if legs_ok
                                 else "; ".join(legs["problems"]), legs, legs_started))

        conversion_cost = sum(c.estimated_sell_minor for c in proj.conversions)
        escalations = [d.obligation_id for d in decisions if d.action == "ESCALATE"]
        inp = local_policy.policy_input(state, conversion_cost, escalations, low_confidence)
        decision = local_policy.evaluate(inp)
        stages.append(await self._opa_stage(inp, decision))
        stages.append(await self._balance_stage(state, proj.conversions))
        stages.append(await self._ledger_stage(state, proj.base_cost_minor, floor))
        return GateResult(stages, proj, decision["requires_approval"] if decision["allow"] else ["actor_not_permitted"])

    async def _opa_stage(self, inp: dict, local: dict) -> StageResult:
        started = perf_counter()
        if not local["allow"]:
            return _stage("Policy", False, f"role {inp['actor_role']} may not plan payments", {"local": local}, started)
        if not self.opa.configured:
            return StageResult(engine="Policy", status="PASS",
                               summary="local policy evaluated" + (f"; approval needed: {', '.join(local['requires_approval'])}"
                                                                   if local["requires_approval"] else ""),
                               details={"local": local, "opa": "not-configured"},
                               duration_ms=(perf_counter() - started) * 1000)
        try:
            remote = await self.opa.decide(inp)
        except Exception as exc:  # fail closed
            return _stage("Policy", False, f"OPA unavailable: {type(exc).__name__}", {"local": local}, started)
        agree = remote == local
        return _stage("Policy", agree, "OPA and local policy agree" if agree else "OPA disagrees with local policy",
                      {"local": local, "opa": remote}, started)

    async def _balance_stage(self, state: TreasuryState, conversions: list[Conversion]) -> StageResult:
        started = perf_counter()
        if not self.airwallex.configured:
            return StageResult(engine="Airwallex · live balances", status="SKIPPED",
                               summary="sandbox not configured; planned on supplied balances",
                               details={}, duration_ms=(perf_counter() - started) * 1000)
        try:
            live = await self.airwallex.balances_minor()
        except Exception as exc:
            return _stage("Airwallex · live balances", False, f"read failed: {type(exc).__name__}", {}, started)
        stale = {c: {"plan": v, "live": live.get(c, 0)} for c, v in state.balances.items() if live.get(c, 0) < v}
        return _stage("Airwallex · live balances", not stale,
                      "live balances cover the plan" if not stale else "plan uses more cash than Airwallex shows",
                      {"live": live, "stale": stale}, started)

    async def _ledger_stage(self, state: TreasuryState, base_cost: int, floor: int) -> StageResult:
        started = perf_counter()
        if not self.ledger.configured:
            return StageResult(engine="TigerBeetle · ledger", status="SKIPPED", summary="ledger not configured",
                               details={}, duration_ms=(perf_counter() - started) * 1000)
        try:
            available = await self.ledger.balance_minor()
        except Exception as exc:
            return _stage("TigerBeetle · ledger", False, f"lookup failed: {type(exc).__name__}", {}, started)
        ok = available - base_cost >= floor
        return _stage("TigerBeetle · ledger", ok, f"ledger balance {available} after debit {base_cost}",
                      {"ledger_available_minor": available}, started)
