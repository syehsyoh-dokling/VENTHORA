"""Execution contracts: what a person approves and what the agent reports back."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..domain.models import StrictModel, TreasuryState


class Approval(StrictModel):
    approver: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=500)
    terms_digest: str | None = Field(
        default=None, min_length=64, max_length=64,
        description="sha256 of the approval terms the approver saw (PlanResult.approval_terms); when given, "
                    "execution refuses if the plan's terms differ")


class ExecutionRequest(StrictModel):
    state: TreasuryState
    plan_hash: str = Field(min_length=64, max_length=64, description="plan_hash of the plan being approved")
    approval: Approval | None = None
    simulate_settlement: bool = Field(default=True, description="sandbox: move transfers SENT -> PAID")
    max_quote_slippage_bp: int = Field(default=100, ge=0, le=1000)


class Step(StrictModel):
    kind: Literal["preflight", "quote", "conversion", "beneficiary", "transfer", "simulation", "reconcile"]
    key: str
    status: Literal["OK", "SKIPPED", "IN_FLIGHT", "FAILED", "REFUSED"]
    request_id: str | None = None
    airwallex_id: str | None = None
    detail: dict = Field(default_factory=dict)


class Reconciliation(StrictModel):
    balances_before_minor: dict[str, int]
    expected_after_minor: dict[str, int]
    live_after_minor: dict[str, int]
    managed_after_minor: dict[str, int] = Field(
        default_factory=dict, description="the plan's (possibly ring-fenced) budget after execution")
    plan_variance_minor: dict[str, int] = Field(
        default_factory=dict, description="managed budget after minus what the plan projected (e.g. unexpected fees)")
    discrepancies_minor: dict[str, int]
    base_floor_minor: int
    base_floor_ok: bool


ExecutionStatus = Literal["COMPLETED", "IN_FLIGHT", "FAILED", "RECONCILIATION_MISMATCH", "REFUSED"]


class ExecutionResult(StrictModel):
    execution_id: str | None
    plan_hash: str
    status: ExecutionStatus
    message: str
    steps: list[Step]
    reconciliation: Reconciliation | None = None
