from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["supplier_invoice", "contractor_claim", "bank_change", "remittance_notice", "unknown"]
Trust = Literal["registered", "unknown_sender", "reply_to_mismatch"]
Status = Literal["VERIFIED", "NEEDS_REVIEW", "QUARANTINED", "REJECTED", "DUPLICATE"]


class Quote(BaseModel):
    """A field value and the verbatim text it was read from."""
    model_config = ConfigDict(extra="forbid")
    field: str
    value: str
    quote: str
    start: int
    end: int


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intake_id: str
    message_id: str
    received_at: str
    sender: str
    sender_domain: str
    display_name: str
    reply_to: str | None = None
    subject: str
    kind: Kind
    trust: Trust
    counterparty_id: str | None = Field(default=None, description="beneficiary or payer the sender is registered for")
    counterparty_name: str | None = None
    claimed_name: str | None = Field(default=None, description="counterparty the display name or text claims to be")
    amount_minor: int | None = None
    currency: str | None = None
    invoice_no: str | None = None
    invoice_fingerprint: str | None = None
    due_date: str | None = None
    flags: list[str] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    status: Status
    reasons: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    reviewer: str | None = None
    review_note: str | None = None
    text_sha256: str
    text: str = Field(max_length=20_000)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["verify", "reject"]
    reviewer: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=500)
