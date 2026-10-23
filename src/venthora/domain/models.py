"""Typed contracts shared by every engine.

Money is always an integer in minor units. The planning base currency is USD;
EUR and GBP obligations are funded by converting from the USD wallet.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .money import rate_ppm

Currency = Literal["USD", "EUR", "GBP"]
MAX_MINOR = 10**13
MAX_OBLIGATIONS = 12


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Obligation(StrictModel):
    obligation_id: str = Field(min_length=1, max_length=80)
    amount_minor: int = Field(gt=0, le=MAX_MINOR)
    currency: Currency
    priority: int = Field(ge=1, le=10, description="cost of not paying on time: 10 = operations halt (payroll, "
                                                    "shipping), 1 = cheapest to defer")
    due_in_hours: int = Field(ge=0, le=720)
    beneficiary_id: str = Field(min_length=1)
    payee_name: str | None = Field(default=None, max_length=200, description="used to match inbox evidence")
    payout_method: Literal["LOCAL", "SWIFT"] = "LOCAL"
    evidence_ref: str = Field(min_length=1)
    invoice_fingerprint: str | None = None
    record_kind: Literal["payable", "settled"] = "payable"
    policy_flags: list[str] = Field(default_factory=list,
                                    description="policy exceptions raised by evidence, e.g. payee_bank_details_changed")


class PayerHistory(StrictModel):
    """How often this payer has kept its payment promises (calibrates receipt confidence)."""
    kept: int = Field(default=0, ge=0, le=10_000)
    missed: int = Field(default=0, ge=0, le=10_000)


class Receivable(StrictModel):
    receivable_id: str = Field(min_length=1, max_length=80)
    payer: str = Field(min_length=1)
    amount_minor: int = Field(gt=0, le=MAX_MINOR)
    currency: Currency
    expected_in_hours: int = Field(ge=0, le=2160)
    confidence_bp: int = Field(ge=0, le=10_000, description="forecast confidence, basis points")
    evidence_ref: str | None = None
    arrival_window_hours: list[int] | None = Field(
        default=None, min_length=2, max_length=2,
        description="90% arrival interval [earliest, latest]; reliance uses the latest bound")
    payer_history: PayerHistory | None = None

    @property
    def latest_arrival_hours(self) -> int:
        return max(self.arrival_window_hours) if self.arrival_window_hours else self.expected_in_hours


class Evidence(StrictModel):
    ref: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    status: Literal["verified", "rejected", "pending"]
    summary: str = ""


class StressScenario(StrictModel):
    name: str = Field(min_length=1)
    unexpected_outflow_minor: int = Field(ge=0, le=MAX_MINOR)


class TreasuryPolicy(StrictModel):
    """Thresholds live here, in code-checked data, never in prompts."""

    reserve_floor_minor: int = Field(ge=0, le=MAX_MINOR)
    min_receivable_confidence_bp: int = Field(default=8_000, ge=0, le=10_000)
    autonomous_conversion_limit_minor: int = Field(
        ge=0, le=MAX_MINOR, description="autonomous FX conversion limit when the receipt forecast is fully supported")
    full_confidence_bp: int = Field(default=9_000, ge=1, le=10_000,
                                    description="forecast confidence at/above which the full limit applies")
    zero_limit_below_bp: int = Field(default=5_000, ge=0, le=10_000,
                                     description="at/below this confidence every conversion needs a person")
    escalate_flags: list[str] = Field(default_factory=lambda: ["payee_bank_details_changed"],
                                      description="policy exceptions that always go to a person, whatever the cash")
    swift_fee_currency: Currency = "EUR"
    swift_fee_minor: int = Field(default=1_285, ge=0, description="Kit 01 flat SWIFT fee (guide)")
    payout_fees_minor: dict[str, int] = Field(
        default_factory=dict,
        description="per-payout fee by 'METHOD:CCY' (e.g. 'SWIFT:EUR', 'LOCAL:USD'), charged in that currency; "
                    "overrides the flat Kit fee. Filled from fees observed on Airwallex.")
    planning_horizon_hours: int = Field(default=72, ge=1, le=720)
    approver_roles: list[str] = Field(default_factory=lambda: ["finance_manager"])

    @model_validator(mode="after")
    def confidence_band(self):
        if self.zero_limit_below_bp >= self.full_confidence_bp:
            raise ValueError("zero_limit_below_bp must be below full_confidence_bp")
        return self


class TreasuryState(StrictModel):
    case_id: str = Field(min_length=1, max_length=120)
    book_id: str = Field(default="default", min_length=1, max_length=120,
                         description="treasury book whose managed budget carries across cycles")
    base_currency: Literal["USD"] = "USD"
    balances: dict[Currency, int]
    fx_to_base: dict[Currency, str] = Field(
        default_factory=dict, description="base units per 1 unit of currency, e.g. EUR: '1.0850'"
    )
    obligations: list[Obligation] = Field(min_length=1, max_length=MAX_OBLIGATIONS)
    receivables: list[Receivable] = Field(default_factory=list, max_length=50)
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)
    scenarios: list[StressScenario] = Field(default_factory=list, max_length=20)
    policy: TreasuryPolicy
    actor_role: Literal["treasury_agent", "finance_manager", "viewer"] = "treasury_agent"
    use_ecb_stress: bool = False
    fx_stress_model: Literal["horizon_m0", "ecb_empirical"] = Field(
        default="horizon_m0", description="horizon_m0 = original Horizon scenario engine; ecb_empirical = 5-day p95")

    @model_validator(mode="after")
    def consistent(self):
        ids = [o.obligation_id for o in self.obligations]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate obligation_id")
        refs = [e.ref for e in self.evidence]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate evidence ref")
        if any(v < 0 or v > MAX_MINOR for v in self.balances.values()):
            raise ValueError("balances must be between 0 and 10^13 minor units")
        used = {o.currency for o in self.obligations} | {r.currency for r in self.receivables}
        used |= set(self.balances)
        if any(o.payout_method == "SWIFT" for o in self.obligations):
            used.add(self.policy.swift_fee_currency)
        for ccy in used - {self.base_currency}:
            if ccy not in self.fx_to_base:
                raise ValueError(f"missing fx_to_base rate for {ccy}")
            rate_ppm(self.fx_to_base[ccy], round_up=True)
        return self

    def balance(self, ccy: str) -> int:
        return self.balances.get(ccy, 0)

    def ppm_up(self, ccy: str) -> int:
        return 1_000_000 if ccy == self.base_currency else rate_ppm(self.fx_to_base[ccy], round_up=True)

    def ppm_down(self, ccy: str) -> int:
        return 1_000_000 if ccy == self.base_currency else rate_ppm(self.fx_to_base[ccy], round_up=False)


StageStatus = Literal["PASS", "FAIL", "WARN", "INFO", "SKIPPED"]


class StageResult(StrictModel):
    engine: str
    status: StageStatus
    summary: str
    details: dict
    duration_ms: float


class Conversion(StrictModel):
    sell_currency: Currency
    buy_currency: Currency
    buy_amount_minor: int
    estimated_sell_minor: int


Action = Literal["PAY", "CONVERT_AND_PAY", "DEFER", "ESCALATE", "REJECT"]


class ObligationDecision(StrictModel):
    obligation_id: str
    action: Action
    amount_minor: int
    currency: Currency
    base_cost_minor: int
    reason: str


class PlanChange(StrictModel):
    obligation_id: str
    before: Action | None
    after: Action
    reason: str


class Counterfactual(StrictModel):
    """An alternative the agent did not take, proven against the same invariants as the plan."""
    name: str
    question: str
    pays: list[str]
    feasible: bool
    base_after_minor: int
    floor_minor: int
    headroom_minor: int = Field(description="base balance after minus floor; negative = reserve breach")
    consequence: str


class Recalculation(StrictModel):
    mode: Literal["full", "incremental"]
    triggers: list[str]
    locked: list[str]
    reconsidered: list[str]
    changes: list[PlanChange]
    fallback_reason: str | None = None


class PlanResult(StrictModel):
    case_id: str
    plan_hash: str = Field(description="sha256 of state + decisions; an approval binds to exactly this plan")
    decision: Literal["APPROVED", "REQUIRES_APPROVAL", "BLOCKED"]
    obligations: list[ObligationDecision]
    conversions: list[Conversion]
    total_base_cost_minor: int
    projected_base_balance_minor: int
    required_base_floor_minor: int
    solver: str
    stages: list[StageResult]
    explanation: list[str]
    counterfactuals: list[Counterfactual] = Field(default_factory=list)
    recalculation: Recalculation | None = None
    forecast_confidence_bp: int | None = None
    conversion_limit_minor: int = 0
    approval_terms: list[dict] = Field(default_factory=list,
                                       description="what an approver signs: amount, currency, counterparty, evidence")
    frame: dict = Field(default_factory=dict, description="worked-problem frame: given / asked / formula / answer")
    duration_ms: float
