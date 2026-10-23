# ruff: noqa: E501
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

from ..aging import active as aging_active
from ..aging import aging_facts, aging_reasons
from ..aging import describe as describe_aging
from ..domain.models import Conversion, ObligationDecision, StageResult, TreasuryState
from ..domain.projection import Projection, project
from ..engines.theorem_fx import check_conversion_legs
from ..gateways.airwallex import Airwallex
from ..gateways.native import native_gate
from ..gateways.opa import OPA
from ..gateways.tigerbeetle import TigerBeetleLedger
from ..payees import describe as describe_payees
from ..payees import payee_facts, payee_reasons
from ..tenancy import describe as describe_tenancy
from ..tenancy import tenancy_facts
from . import budget
from . import policy as local_policy
from .approvals import describe as describe_approval
from .fx_exposure import active as fx_active
from .fx_exposure import describe as describe_fx
from .fx_exposure import fx_facts, fx_reasons
from .limits import describe as describe_limits
from .limits import limit_facts, limit_reasons
from .match import three_way_match


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
                     decisions: list[ObligationDecision], stress_minor: int,
                     breach_probability_bp: int | None = None) -> GateResult:
        started = perf_counter()
        by_id = {o.obligation_id: o for o in state.obligations}
        base = state.base_currency
        floor = state.policy.reserve_floor_minor + stress_minor

        structural = []
        if len(selected) != len(set(selected)):
            structural.append("obligation selected twice")
        if not set(selected) <= eligible:
            structural.append(f"unverified obligation selected: {sorted(set(selected) - eligible)}")
        # one payout per invoice, or per (invoice, instalment) when an invoice is settled in parts
        fingerprints = [f"{by_id[i].invoice_fingerprint}#{by_id[i].installment or ''}" for i in selected
                        if i in by_id and by_id[i].invoice_fingerprint]
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
        budget_started = perf_counter()
        pipe = budget.pipeline(state, decisions)
        if pipe["attached"]:
            n_alert, n_req = len(pipe["alerts"]), len(pipe["requests"])
            summary = (f"period {pipe['period']}: " + ", ".join(f"{r['line']} {r['utilisation_bp'] / 100:.0f}% ({r['status']})"
                                                               for r in pipe["lines"])
                       + (f"; {n_alert} alert(s)" if n_alert else "") + (f"; {n_req} budget request(s)" if n_req else "")
                       + (f"; {len(pipe['unbudgeted'])} unbudgeted" if pipe["unbudgeted"] else ""))
            attention = n_alert or n_req or pipe["unbudgeted"]
            stages.append(StageResult(engine="Budget · mandate",
                                      status="FAIL" if pipe["problems"] else ("WARN" if attention else "PASS"),
                                      summary="; ".join(pipe["problems"]) if pipe["problems"] else summary, details=pipe,
                                      duration_ms=(perf_counter() - budget_started) * 1000))
        facts = budget.policy_facts(state, pipe)
        # Three-way match: contract / PO − paid so far vs what the invoice claims now.
        match_started = perf_counter()
        twm = three_way_match(state, decisions)
        if twm["rows"]:
            summary = (f"{sum(r['verdict'] == 'matched' for r in twm['rows'])} matched"
                       + (f", {len(twm['over_claims'])} over-claim(s): {', '.join(twm['over_claims'])}"
                          if twm["over_claims"] else "")
                       + (f", {len(twm['under_claims'])} under-claim(s)" if twm["under_claims"] else ""))
            stages.append(StageResult(engine="Verity · three-way match", status="WARN" if twm["over_claims"] else "PASS",
                                      summary=summary, details=twm, duration_ms=(perf_counter() - match_started) * 1000))
        # Limits and velocity: how much, to whom, how often — from the books' history and this plan's payouts.
        lim_started = perf_counter()
        lim = limit_facts(state, decisions)
        lim_reasons = limit_reasons(lim)
        if any(v is not None for k, v in lim.items() if k.startswith("max_")):
            stages.append(StageResult(engine="Policy · limits", status="WARN" if lim_reasons else "PASS",
                                      summary=describe_limits(lim, lim_reasons), details=lim | {"reasons": lim_reasons},
                                      duration_ms=(perf_counter() - lim_started) * 1000))
        # Payee lifecycle: a verified payee is paid by a person until its first PAID payout, and throughout the cooling
        # period after a bank change; NEW / CHANGED / SUSPENDED payees were already escalated by Verity.
        pay_started = perf_counter()
        pf = payee_facts(state, decisions)
        pf_reasons = payee_reasons(pf)
        if pf["attached"]:
            stages.append(StageResult(engine="Payee · lifecycle", status="WARN" if pf_reasons else "PASS",
                                      summary=describe_payees(pf, pf_reasons), details=pf | {"reasons": pf_reasons},
                                      duration_ms=(perf_counter() - pay_started) * 1000))
        # FX exposure: what the plan leaves open per currency, sells today, and leaves unhedged inside the horizon.
        fx_started = perf_counter()
        fx = fx_facts(state, decisions, proj)
        fx_rs = fx_reasons(fx)
        if fx_active(fx):
            stages.append(StageResult(engine="Policy · FX exposure", status="WARN" if fx_rs else "PASS",
                                      summary=describe_fx(fx, fx_rs), details=fx | {"reasons": fx_rs},
                                      duration_ms=(perf_counter() - fx_started) * 1000))
        # Invoice aging and dunning: how old what we owe is, who has been reminding us, what lateness costs.
        ag_started = perf_counter()
        ag = aging_facts(state, decisions)
        ag_rs = aging_reasons(ag)
        if aging_active(ag):
            stages.append(StageResult(engine="Verity · aging", status="WARN" if ag_rs else "PASS",
                                      summary=describe_aging(ag, ag_rs), details=ag | {"reasons": ag_rs},
                                      duration_ms=(perf_counter() - ag_started) * 1000))
        # Tenant segregation: whose book this is, what it may pay, who may approve it.
        ten = tenancy_facts(state, decisions)
        if ten["attached"]:
            stages.append(StageResult(engine="Tenancy · book", status="FAIL" if not ten["book_known"] else ("WARN" if ten["disallowed_obligations"] else "PASS"),
                                      summary=describe_tenancy(ten), details=ten, duration_ms=0.0))
        inp = local_policy.policy_input(state, conversion_cost, escalations, breach_probability_bp, facts,
                                        paying_over_claims=twm["paying_over_claims"], limits=lim, payees=pf, fx=fx, aging=ag,
                                        tenancy=ten)
        decision = local_policy.evaluate(inp)
        stages.append(await self._opa_stage(inp, decision))
        # Approval matrix: who, and how many different people, must approve what the policy stopped.
        if decision.get("approval"):
            stages.append(StageResult(engine="Policy · approvals", status="WARN", summary=describe_approval(decision["approval"]),
                                      details=decision["approval"] | {"reasons": decision["requires_approval"],
                                                                      "paying_total_minor": inp["paying_total_minor"]},
                                      duration_ms=0.0))
        stages.append(await self._balance_stage(state, proj.conversions))
        stages.append(await self._ledger_stage(state, proj.base_cost_minor, floor))
        return GateResult(stages, proj, decision["requires_approval"] if decision["allow"] else ["actor_not_permitted"])

    async def _opa_stage(self, inp: dict, local: dict) -> StageResult:
        started = perf_counter()
        if not local["allow"]:
            why = (f"role {inp['actor_role']} may not plan payments" if inp["actor_role"] == "viewer"
                   else f"book {(inp.get('tenancy') or {}).get('book_id')} is unknown to the tenant registry; no plan may spend from it")
            return _stage("Policy", False, why, {"local": local}, started)
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
