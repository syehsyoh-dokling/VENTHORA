# ruff: noqa: E501
"""The standing mandate: what VENTHORA's agent may do on its own, what it must check first, and when it must call a
person, in seven categories, each with the policy it serves.

  checks     document and evidence checks the intake runs on every request before anything is payable; they are
             code, not settings (a few are switched by Finance standards or Document validation)
  rules      live.MANDATES: permissions (grant), safeguards (guard) and rules that are always on (locked), each with
             its value
  limits     every number in one place: the rule values plus the book's allocation, reserve floor, planning horizon
             and confidence band; saved here they become the standing mandate, and the Operations run starts from
             it (each run may still narrow or widen them, and the run records what was granted)

The standing mandate is kept by the organisation store (kind standing_mandate); changing it is audited.
"""
from __future__ import annotations

from typing import Any

from .live import MANDATES

CATEGORIES: list[dict[str, str]] = [
    {"key": "documents", "title": "Document and evidence checks",
     "policy": "No payment without a verified document trail: the right sender, an open purchase order with room, amounts that add up and match, and evidence quoted from a dated document."},
    {"key": "payees", "title": "Payees and bank details",
     "policy": "Money goes only to verified payees; a new payee or changed bank details are confirmed by a call-back before any payment, and a person approves until the cooling period has passed."},
    {"key": "authority", "title": "Payment authority",
     "policy": "The agent pays on its own only within these limits; above any limit, or with a limit revoked, the payment waits for an approver."},
    {"key": "liquidity", "title": "Cash, FX and liquidity",
     "policy": "The reserve floor is never spent; foreign-currency exposure stays within limits, and conversions on the agent's own authority shrink as forecast confidence falls."},
    {"key": "escalation", "title": "Escalation to a person",
     "policy": "Overdue invoices, late-interest exposure, final notices and unconfirmed transfers are put in front of a person before they become losses."},
    {"key": "approval", "title": "Approval policy",
     "policy": "Every exception is decided by an approver with the authority for its amount; two approvals come from two different people, and nobody approves their own request."},
    {"key": "control", "title": "Reconciliation and control",
     "policy": "Every book reconciles to the bank to the cent; when it does not, the book stops until a person closes the case."},
]

RULE_CATEGORY = {
    "three_way": "documents", "tolerance_floor": "documents", "duplicates": "documents", "evidence": "documents",
    "pay_first": "payees", "cooling": "payees",
    "pay_single": "authority", "pay_per_payee": "authority", "outflow_daily": "authority", "outflow_weekly": "authority",
    "budget_auto": "authority", "risk": "authority",
    "convert_fx": "liquidity", "convert_daily": "liquidity", "rely_receipts": "liquidity", "fx_open": "liquidity", "hedge": "liquidity",
    "overdue": "escalation", "late_interest": "escalation", "dunning": "escalation", "late_rate": "escalation", "incident_wait": "escalation",
    "recon": "control",
}

# the checks the intake runs on every request (src/venthora/intake/pipeline.py, procurement.py); flags are what a person sees
CHECKS: list[dict[str, Any]] = [
    {"key": "sender", "label": "Accept a request only from the counterparty's registered mailbox",
     "fails": "an unknown sender, or a Reply-To that points elsewhere, is quarantined", "flags": ["unknown_sender", "reply_to_mismatch"]},
    {"key": "po_open", "label": "A supplier invoice quotes an open purchase order placed with the same supplier, in the same currency",
     "fails": "held for review", "flags": ["po_missing", "po_unknown", "po_not_open", "po_supplier_mismatch", "po_currency_mismatch"],
     "switch": "require_po", "where": "#/standards"},
    {"key": "po_room", "label": "Paid, pending and this invoice together never exceed the purchase order total",
     "fails": "held for review", "flags": ["po_exceeded"]},
    {"key": "totals", "label": "Line items, tax and the invoice total add up",
     "fails": "held for review", "flags": ["totals_do_not_add_up"]},
    {"key": "readers", "label": "The rules reader and the AI model agree on amount, currency, invoice number and due date",
     "fails": "held for review", "flags": ["reader_disagreement"]},
    {"key": "unit_price", "label": "Invoice and expense lines at or below the agreed unit prices",
     "fails": "held for review", "flags": ["above_unit_price"], "where": "#/prices"},
    {"key": "bank_change", "label": "A bank-details change claimed in an email is never acted on",
     "fails": "held until a call-back on the registered number", "flags": ["payee_bank_details_changed"]},
    {"key": "pressure", "label": "Urgency or secrecy pressure in a payment request is flagged",
     "fails": "shown to the approver", "flags": ["urgency_pressure"]},
    {"key": "claims", "label": "An expense claim names a known category and the expense date, and arrives within the deadline",
     "fails": "held for review", "flags": ["category_unknown", "expense_date_missing", "claim_past_deadline"], "where": "#/standards"},
    {"key": "documents", "label": "Required supporting documents, dates and stamps are present",
     "fails": "a failed Required rule holds the request; a Warn rule flags it", "flags": ["doc_*"], "switch": "doc_rules", "where": "#/doc-rules"},
    {"key": "approver", "label": "Someone with the authority for the amount exists in the requester's unit chain",
     "fails": "held: no approver", "flags": ["no_approver"], "where": "#/users"},
]

# book-level numbers that are not rows of MANDATES: key -> (label, unit, category, min, max, help)
BOOK_LIMITS: dict[str, tuple[str, str, str, int, int, str]] = {
    "allocation_minor": ("Allocation ring-fenced from the wallet", "usd", "liquidity", 0, 10**13, "what this book may use at all"),
    "reserve_floor_minor": ("Reserve floor, never spent", "usd", "liquidity", 0, 10**13, "a plan that would go below it is refused"),
    "horizon_hours": ("Planning horizon", "hours", "liquidity", 1, 720, "obligations due inside it are planned now"),
    "full_confidence_bp": ("Full FX limit when forecast confidence is at least", "pct", "liquidity", 1, 10_000, "the conversion limit applies in full"),
    "zero_limit_below_bp": ("No conversions on the agent's own authority when confidence is below", "pct", "liquidity", 0, 9_999, "the limit falls to zero"),
}


def _bounds(unit: str | None) -> tuple[int, int]:
    return {"pct": (0, 10_000), "hours": (0, 2_160), "days": (0, 3_650), "level": (1, 3)}.get(unit or "", (0, 10**13))


def standing(org) -> dict:
    rec = org.get("standing_mandate", "standing_mandate") or {}
    return {"values": rec.get("values", {}), "enabled": rec.get("enabled", {}), "book": rec.get("book", {}),
            "updated_by": rec.get("updated_by_name")}


def save(org, body: dict, by: str) -> dict:
    """Validate and keep the standing mandate: rule values, which permissions and safeguards are on, book numbers."""
    specs = {m["key"]: m for m in MANDATES}
    values, enabled, book = {}, {}, {}
    for k, v in (body.get("values") or {}).items():
        m = specs.get(k)
        if m is None or m["unit"] is None:
            raise ValueError(f"{k}: not a rule with a value")
        lo, hi = _bounds(m["unit"])
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"{m['label']}: a whole number from {lo} to {hi} is needed")
        values[k] = v
    for k, v in (body.get("enabled") or {}).items():
        m = specs.get(k)
        if m is None or m["kind"] == "locked":
            raise ValueError(f"{k}: always on, it cannot be switched off")
        enabled[k] = bool(v)
    for k, v in (body.get("book") or {}).items():
        if k not in BOOK_LIMITS:
            raise ValueError(f"{k}: unknown limit")
        lab, _, _, lo, hi, _ = BOOK_LIMITS[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"{lab}: a whole number from {lo} to {hi} is needed")
        book[k] = v
    if book.get("zero_limit_below_bp", 0) >= book.get("full_confidence_bp", 10_001):
        raise ValueError("the zero-limit confidence must be below the full-limit confidence")
    org._put("standing_mandate", {"_id": "standing_mandate", "values": values, "enabled": enabled, "book": book, "updated_by_name": by}, by)
    return standing(org)


def view(setup: dict, org, doc_rules_on: bool) -> dict:
    """The page: categories with their policy, checks and rules; the limits table; the approval policy."""
    std = org.standards()
    rows = {m["key"]: m for m in setup["mandates"]}
    cats = []
    for c in CATEGORIES:
        checks = []
        if c["key"] == "documents":
            for ch in CHECKS:
                on = {"require_po": bool(std.get("require_po", True)), "doc_rules": doc_rules_on}.get(ch.get("switch"), True)
                checks.append(ch | {"on": on})
        cats.append(c | {"checks": checks, "rules": [rows[k] | {"category": c["key"]} for k, cat in RULE_CATEGORY.items() if cat == c["key"] and k in rows]})
    limits = [{"key": k, "label": lab, "unit": unit, "category": cat, "min": lo, "max": hi, "help": hlp, "value": setup.get(k), "book": True}
              for k, (lab, unit, cat, lo, hi, hlp) in BOOK_LIMITS.items()]
    limits += [{"key": m["key"], "label": m["label"], "unit": m["unit"], "category": RULE_CATEGORY.get(m["key"], "control"), "kind": m["kind"],
                "min": _bounds(m["unit"])[0], "max": _bounds(m["unit"])[1], "value": m.get("value"), "enabled": m.get("enabled", True), "help": m.get("help", "")}
               for m in setup["mandates"] if m["unit"] is not None]
    order = [c["key"] for c in CATEGORIES]
    limits.sort(key=lambda x: order.index(x["category"]))
    return {"categories": cats, "limits": limits, "approval_matrix": setup["approval_matrix"], "approvers": setup["approvers"],
            "standing": standing(org), "titles": {c["key"]: c["title"] for c in CATEGORIES}}
