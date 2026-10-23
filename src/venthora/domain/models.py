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
    priority: int = Field(ge=1, le=10, description="10 = critical (payroll, tax)")
    due_in_hours: int = Field(ge=0, le=720)
    beneficiary_id: str = Field(min_length=1)
    payout_method: Literal["LOCAL", "SWIFT"] = "LOCAL"
    evidence_ref: str = Field(min_length=1)
    invoice_fingerprint: str | None = None
    record_kind: Literal["payable", "settled"] = "payable"


class Receivable(StrictModel):
    receivable_id: str = Field(min_length=1, max_length=80)
    payer: str = Field(min_length=1)
    amount_minor: int = Field(gt=0, le=MAX_MINOR)
    currency: Currency
    expected_in_hours: int = Field(ge=0, le=2160)
    confidence_bp: int = Field(ge=0, le=10_000, description="forecast confidence, basis points")
    evidence_ref: str | None = None


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
    autonomous_conversion_limit_minor: int = Field(ge=0, le=MAX_MINOR)
    low_confidence_conversion_limit_minor: int = Field(ge=0, le=MAX_MINOR)
    swift_fee_currency: Currency = "EUR"
    swift_fee_minor: int = Field(default=1_285, ge=0)
    planning_horizon_hours: int = Field(default=72, ge=1, le=720)
    approver_roles: list[str] = Field(default_factory=lambda: ["finance_manager"])

    @model_validator(mode="after")
    def low_confidence_is_tighter(self):
        if self.low_confidence_conversion_limit_minor > self.autonomous_conversion_limit_minor:
            raise ValueError("low-confidence conversion limit must not exceed the normal limit")
        return self


class TreasuryState(StrictModel):
    case_id: str = Field(min_length=1, max_length=120)
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


class PlanResult(StrictModel):
    case_id: str
    decision: Literal["APPROVED", "REQUIRES_APPROVAL", "BLOCKED"]
    obligations: list[ObligationDecision]
    conversions: list[Conversion]
    total_base_cost_minor: int
    projected_base_balance_minor: int
    required_base_floor_minor: int
    solver: str
    stages: list[StageResult]
    explanation: list[str]
    duration_ms: float
