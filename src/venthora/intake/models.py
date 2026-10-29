from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["supplier_invoice", "contractor_claim", "expense_claim", "bank_change", "remittance_notice", "unknown"]
Trust = Literal["registered", "staff", "unknown_sender", "reply_to_mismatch", "failed_authentication"]
Status = Literal["VERIFIED", "NEEDS_REVIEW", "QUARANTINED", "REJECTED", "DUPLICATE"]


class Attachment(BaseModel):
    """A file that came with the email; only a text-based PDF is read, and its text joins the quoted document."""
    model_config = ConfigDict(extra="forbid")
    name: str
    content_type: str
    bytes: int
    sha256: str
    pages: int = 0
    read: bool = False
    problem: str | None = None


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
    dunning_level: int = Field(default=0, ge=0, le=3, description="0 none, 1 reminder, 2 second reminder, 3 final notice")
    quotes: list[Quote] = Field(default_factory=list)
    status: Status
    reasons: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    reviewer: str | None = None
    review_note: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    authentication: dict[str, str] = Field(default_factory=dict, description="receiving server's SPF / DKIM / DMARC verdicts")
    reader: Literal["rules", "model+rules"] = "rules"
    # supplier invoices against a purchase order: which PO, which term, which account code
    po_number: str | None = None
    installment: str | None = Field(default=None, description="the PO term it pays (T1, T2...), or 'partial'")
    account_code: str | None = None
    # expense claims from staff: who claims, from which unit, what for, and who may approve it
    unit_id: str | None = None
    category: str | None = None
    expense_date: str | None = None
    approvers: list[str] = Field(default_factory=list, description="eligible approvers, nearest unit first; never the claimant")
    text_sha256: str
    text: str = Field(max_length=40_000)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["verify", "reject"]
    reviewer: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=500)
