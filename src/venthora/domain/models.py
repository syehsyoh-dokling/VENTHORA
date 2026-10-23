# ruff: noqa: E501
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
    budget_line: str | None = Field(default=None, max_length=60, description="budget line this obligation draws on")
    # Instalments and the three-way match: an invoice may be settled in parts (deposit, balance, term-n); each part is
    # its own payout under the duplicate lock, and the parts may never exceed what the contract / PO says.
    installment: str | None = Field(default=None, max_length=40, description="deposit | balance | term-N; None = whole invoice")
    contract_total_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR,
                                             description="PO / contract amount (three-way match)")
    paid_so_far_minor: int = Field(default=0, ge=0, le=MAX_MINOR, description="earlier instalments already paid on this invoice")
    due_date: str | None = Field(default=None, description="ISO date; with as_of it places the obligation in a period")
    posted_at: str | None = Field(default=None, description="ISO datetime the document arrived; decides its REPORT period")


class BudgetLine(StrictModel):
    """One line of the organisation's plan for the period: the agent's mandate on that line.

    amount + granted_extra is the mandate; auto_share is the part the agent may spend on its own;
    alert_share is where a warning is raised (and, if require_approval_on_alert, a person is required);
    a bypass (unlimited line) is honoured only when a person signed it."""
    line: str = Field(min_length=1, max_length=60)
    amount_minor: int = Field(ge=0, le=MAX_MINOR)
    auto_share_bp: int = Field(default=10_000, ge=0, le=10_000, description="share of the mandate spendable autonomously")
    alert_share_bp: int = Field(default=8_000, ge=0, le=10_000, description="utilisation at which an alert is raised")
    require_approval_on_alert: bool = Field(default=False, description="an alert also requires a person")
    bypass: bool = Field(default=False, description="unlimited line (no cap); must be signed to count")
    bypass_approved_by: str | None = Field(default=None, max_length=120)
    realized_minor: int = Field(default=0, ge=0, le=MAX_MINOR, description="already paid this period")
    granted_extra_minor: int = Field(default=0, ge=0, le=MAX_MINOR, description="mandate added on a budget request")

    @property
    def mandate_minor(self) -> int:
        return self.amount_minor + self.granted_extra_minor

    @property
    def bypass_signed(self) -> bool:
        return self.bypass and bool(self.bypass_approved_by)


class Budget(StrictModel):
    period: str = Field(min_length=1, max_length=40, description="e.g. 2026-11, 2026-Q4, 2026")
    lines: list[BudgetLine] = Field(min_length=1, max_length=50)
    unbudgeted_requires_approval: bool = Field(default=True, description="an obligation outside every line needs a person")
    period_start: str | None = Field(default=None, description="ISO date; with period_end it defines the cut-off")
    period_end: str | None = Field(default=None, description="ISO date (inclusive); obligations due after it carry forward")
    closed: bool = Field(default=False, description="the period was closed (cut-off report issued); no more spend on it")

    @model_validator(mode="after")
    def unique_lines(self):
        names = [ln.line for ln in self.lines]
        if len(names) != len(set(names)):
            raise ValueError("duplicate budget line")
        return self

    def line(self, name: str | None) -> BudgetLine | None:
        return next((ln for ln in self.lines if ln.line == name), None) if name else None


PayeeStatus = Literal["NEW", "VERIFIED", "ACTIVE", "CHANGED", "SUSPENDED"]


class PayeeRecord(StrictModel):
    """Where a payee stands in its lifecycle (src/venthora/payees.py holds the transitions).

    NEW        registered, bank details never confirmed by a call-back      -> no payment at all (escalated)
    VERIFIED   a person confirmed the details on the registered number       -> payments need a person until the first PAID
    ACTIVE     verified and paid at least once                               -> autonomous inside every other control
    CHANGED    the bank details changed; a call-back is required again       -> no payment at all (escalated)
    SUSPENDED  a person suspended the payee                                  -> no payment at all (escalated)
    A bank change starts a cooling period (policy.payee_cooling_hours): even after the call-back, payments until
    cooling_until need a person, and the payee becomes ACTIVE only when paid after it."""
    beneficiary_id: str = Field(min_length=1, max_length=120)
    status: PayeeStatus = "NEW"
    bank_fingerprint: str | None = Field(default=None, max_length=200, description="fingerprint of the bank details on file")
    registered_phone: str | None = Field(default=None, max_length=40, description="the number a call-back must use")
    verified_at: str | None = None
    verified_by: str | None = Field(default=None, max_length=160)
    changed_at: str | None = Field(default=None, description="ISO instant of the last bank-details change")
    cooling_until: str | None = Field(default=None, description="ISO instant; payments before it need a person")
    paid_count: int = Field(default=0, ge=0)


class PayeeRegistry(StrictModel):
    """The payee records the plan reasons from; as_of is the instant cooling periods are judged at (None = now)."""
    as_of: str | None = None
    records: list[PayeeRecord] = Field(default_factory=list, max_length=500)

    def get(self, beneficiary_id: str) -> PayeeRecord | None:
        return next((r for r in self.records if r.beneficiary_id == beneficiary_id), None)


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


class ApprovalTier(StrictModel):
    """One row of the approval matrix: who, and how many, must approve a plan that stopped for a person.

    A tier applies when the plan's paying total (base currency) reaches min_total_minor, or when any of its reason codes
    was raised. Tiers are listed from loosest to strictest; the last applicable tier decides. approvals_required > 1
    means that many DIFFERENT people (four-eyes); the requester never counts."""
    name: str = Field(min_length=1, max_length=60)
    min_total_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR)
    reasons: list[str] = Field(default_factory=list, max_length=30)
    approvals_required: int = Field(default=1, ge=1, le=3)
    roles: list[str] = Field(default_factory=lambda: ["finance_manager"], min_length=1)

    @model_validator(mode="after")
    def applies_to_something(self):
        if self.min_total_minor is None and not self.reasons:
            raise ValueError("an approval tier needs a min_total_minor or reason codes")
        return self


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
    contradicted_confidence_ceiling_bp: int = Field(
        default=7_000, ge=0, le=10_000,
        description="a forecast its own payer has contradicted is no longer 'supported by bank and invoice data': "
                    "its confidence is capped here (below the full band), so the autonomous limit always drops")
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
    approval_matrix: list[ApprovalTier] = Field(default_factory=list, max_length=10,
                                                description="approval tiers by amount and reason code (loosest first); "
                                                            "empty = one approver from approver_roles")
    simulation_paths: int = Field(default=2_000, ge=100, le=200_000,
                                  description="paths for the 72-hour discrete-event reserve simulation")
    max_breach_probability_bp: int = Field(
        default=500, ge=0, le=10_000,
        description="a plan whose simulated probability of breaching the reserve exceeds this needs a person")
    incident_wait_hours: int = Field(default=48, ge=0, le=720,
                                     description="Kit 03: how long an in-flight transfer may stay unconfirmed before escalation")
    max_replacements: int = Field(default=1, ge=0, le=3, description="Kit 03: replacement transfers per obligation")
    amount_tolerance_bp: int = Field(default=50, ge=0, le=10_000, description="three-way match tolerance, share of expected")
    amount_tolerance_minor: int = Field(default=500, ge=0, le=MAX_MINOR, description="three-way match tolerance, absolute floor")
    # Limits and velocity (base-currency cost). None = no limit. Above a limit the agent may not act alone.
    max_single_payment_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR, description="one payout at most")
    max_per_payee_period_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR,
                                                   description="paid + paying now to one payee in the report period")
    max_outflow_daily_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR, description="rolling 24 h, all payees")
    max_outflow_weekly_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR, description="rolling 7 d, all payees")
    max_first_payment_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR,
                                                description="a payee never paid before gets at most this much first")
    # Payee lifecycle: after a bank-details change, payments need a person for this long even once verified.
    payee_cooling_hours: int = Field(default=72, ge=0, le=2_160)

    @model_validator(mode="after")
    def confidence_band(self):
        if self.zero_limit_below_bp >= self.full_confidence_bp:
            raise ValueError("zero_limit_below_bp must be below full_confidence_bp")
        return self


class PaymentHistory(StrictModel):
    """What the books remember, supplied to the plan (facts.py fills it from the spend ledger)."""
    as_of: str | None = None
    paid_per_payee_period_minor: dict[str, int] = Field(default_factory=dict, description="beneficiary_id -> base cost this period")
    outflow_24h_minor: int = Field(default=0, ge=0)
    outflow_7d_minor: int = Field(default=0, ge=0)
    payees_paid_before: list[str] = Field(default_factory=list, description="beneficiary ids with at least one PAID payout")


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
    budget: Budget | None = Field(default=None, description="the period's plan: the agent's mandate per line")
    as_of: str | None = Field(default=None, description="ISO date the book is planned on (period cut-off, due dates)")
    history: PaymentHistory | None = Field(default=None, description="payments already made (limits and velocity)")
    payees: PayeeRegistry | None = Field(default=None, description="payee lifecycle records; None = lifecycle not enforced")
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
    breach_probability_bp: int | None = Field(default=None, description="simulated P(reserve breached within the window)")


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
    simulation: dict = Field(default_factory=dict, description="72-hour discrete-event reserve simulation")
    budget: dict = Field(default_factory=dict, description="budget pipeline: plan / realized / paying now / committed / "
                                                           "available / minus per line, alerts, and budget requests")
    approval_requirement: dict | None = Field(default=None, description="approval matrix: tier, how many different "
                                                                        "people, which roles (None = no approval needed)")
    duration_ms: float
