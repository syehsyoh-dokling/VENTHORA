# ruff: noqa: E501
"""The standing mandate: what VENTHORA's agent may do on its own, what it must check first, and when it must call a
person, in seven categories, each with the policy it serves.

  checks     document and evidence checks the intake runs on every request before anything is payable; each has a
             mode: hold (a failure holds the request for a person), warn (a failure is flagged, nothing is held) or
             off (not checked)
  rules      live.MANDATES: permissions (grant), safeguards (guard) and critical rules (locked), each with its value
  limits     every number in one place: the rule values plus the book's allocation, reserve floor, planning horizon
             and confidence band; saved here they become the standing mandate, and the Operations run starts from
             it (each run may still narrow or widen them, and the run records what was granted)

One role holds the mandate authority (by default the super admin, the FCO): only that role changes the standing
mandate, the authority included. A change that weakens a critical control is refused until the person changing it
acknowledges the warning for that control; the acknowledgement is audited with the change.

The standing mandate is kept by the organisation store (kind standing_mandate).
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

# short names for the category summary; the full label is on the Limits and thresholds tab
SHORT = {
    "three_way": "Three-way match tolerance", "tolerance_floor": "Tolerance floor", "duplicates": "No double payment",
    "pay_first": "First payment to a payee", "cooling": "Cooling period after a bank change",
    "pay_single": "Single payout", "pay_per_payee": "Per payee, per period", "outflow_daily": "Outflow per 24 h",
    "outflow_weekly": "Outflow per 7 days", "budget_auto": "Spend within budget shares", "risk": "Reserve breach risk",
    "convert_fx": "FX conversion", "convert_daily": "FX sold per 24 h", "rely_receipts": "Rely on expected receipts",
    "fx_open": "Open FX position", "hedge": "Hedge cover",
    "overdue": "Overdue escalation", "late_interest": "Late-interest escalation", "dunning": "Reminder level escalation",
    "late_rate": "Default late interest", "incident_wait": "Unconfirmed transfer escalation",
    "recon": "Stop the book on a reconciliation difference", "evidence": "Evidence quoted verbatim",
}

MODES = ("hold", "warn", "off")
MODE_LABEL = {"hold": "Hold", "warn": "Warn only", "off": "Off"}

# the checks the intake runs on every request (src/venthora/intake/pipeline.py, procurement.py); flags are what a person
# sees. critical: weakening it opens a known fraud or loss path, so the change needs an acknowledged warning.
CHECKS: list[dict[str, Any]] = [
    {"key": "sender", "label": "Accept a request only from the counterparty's registered, authenticated mailbox",
     "fails": "an unknown or unauthenticated sender, or a Reply-To that points elsewhere, is quarantined", "flags": ["unknown_sender", "reply_to_mismatch"],
     "critical": True, "risk": "Payment-diversion fraud: an invoice from a look-alike or forged mailbox, or one whose replies go to a fraudster, is no longer quarantined. An unregistered sender is still never paid without a person."},
    {"key": "po_open", "label": "A supplier invoice quotes an open purchase order placed with the same supplier, in the same currency",
     "fails": "held for review", "flags": ["po_missing", "po_unknown", "po_not_open", "po_supplier_mismatch", "po_currency_mismatch", "po_term_mismatch"],
     "switch": "require_po", "where": "#/standards", "critical": True,
     "risk": "Invoices without an approved commitment can be paid: fictitious suppliers, invoices for goods nobody ordered, and invoices billed against another supplier's order."},
    {"key": "po_room", "label": "Paid, pending and this invoice together never exceed the purchase order total",
     "fails": "held for review", "flags": ["po_exceeded"], "critical": True,
     "risk": "Spend can pass the approved commitment: overbilling and split invoices that each look small are paid."},
    {"key": "totals", "label": "Line items, tax and the invoice total add up",
     "fails": "held for review", "flags": ["totals_do_not_add_up"], "critical": True,
     "risk": "An invoice whose total is inflated above its own lines can be paid as stated."},
    {"key": "readers", "label": "The rules reader and the AI model agree on amount, currency, invoice number and due date",
     "fails": "held for review", "flags": ["reader_disagreement"], "critical": True,
     "risk": "When the two readings of a document differ, the agent proceeds on one of them; a misread amount or due date can be paid."},
    {"key": "unit_price", "label": "Invoice and expense lines at or below the agreed unit prices",
     "fails": "held for review", "flags": ["above_unit_price"], "where": "#/prices", "critical": False,
     "risk": "Claims above the agreed unit prices are no longer marked for the approver."},
    {"key": "bank_change", "label": "A bank-details change claimed in an email is never acted on",
     "fails": "held until a call-back on the registered number", "flags": ["payee_bank_details_changed"], "critical": True,
     "risk": "The most common business email compromise: a request to pay new bank details is no longer held for a call-back on the registered number."},
    {"key": "pressure", "label": "Urgency or secrecy pressure in a payment request is flagged",
     "fails": "held for review and shown to the approver", "flags": ["urgency_pressure"], "critical": False,
     "risk": "Requests that push for speed or secrecy, a typical social-engineering sign, are no longer held."},
    {"key": "claims", "label": "An expense claim names a known category and the expense date, and arrives within the deadline",
     "fails": "flagged for the approver (every expense claim is approved by a person)", "flags": ["category_unknown", "expense_date_missing", "claim_past_deadline"],
     "where": "#/standards", "critical": False, "risk": "Late or unclassified expense claims are no longer marked for the approver."},
    {"key": "documents", "label": "Required supporting documents, dates and stamps are present",
     "fails": "a failed Required rule holds the request; a Warn rule flags it", "flags": ["doc_*"], "switch": "doc_rules", "where": "#/doc-rules",
     "critical": True, "risk": "Requests without the supporting documents the finance standards require (delivery note, tax invoice, receipt) can be paid."},
    {"key": "approver", "label": "Someone with the authority for the amount exists in the requester's unit chain",
     "fails": "flagged: no approver", "flags": ["no_approver"], "where": "#/users", "critical": False,
     "risk": "Claims nobody in the unit chain may approve are no longer marked; they wait in the queue without a named approver."},
]
CHECK_KEYS = {c["key"] for c in CHECKS}
FLAG_CHECK = {f: c["key"] for c in CHECKS for f in c["flags"] if f != "doc_*"}

# rules of MANDATES that are critical, and which direction loosens them (+1 = a higher value is looser)
CRITICAL_VALUES = {"three_way": 1, "tolerance_floor": 1, "cooling": -1, "incident_wait": 1,
                   "pay_single": 1, "pay_per_payee": 1, "pay_first": 1, "outflow_daily": 1, "outflow_weekly": 1, "convert_fx": 1, "convert_daily": 1}
# critical rules without a value that may be switched off, with what then happens
SWITCHABLE_CRITICAL = {
    "duplicates": "A request that repeats an invoice already in the book or the queue is held for a person instead of rejected. A payout already sent for the same invoice is still never sent again: that lock protects against retries and cannot be switched off.",
    "recon": "A reconciliation that does not add up still opens a case, but the book is no longer stopped: the agent keeps paying while the difference is open.",
}
# a rule that is how VENTHORA works rather than a setting: there is nothing to switch
BUILT_IN = {"evidence": "Built into how VENTHORA reads documents: a field nobody can quote is never filled, so there is no unquoted claim to act on."}

AUTHORITY_ROLES = {"super_admin": "Super admin (FCO)", "cfo": "CFO", "finance_manager": "Finance manager"}
DEFAULT_AUTHORITY = "super_admin"

# book-level numbers that are not rows of MANDATES: key -> (label, unit, category, min, max, help)
BOOK_LIMITS: dict[str, tuple[str, str, str, int, int, str]] = {
    "allocation_minor": ("Allocation ring-fenced from the wallet", "usd", "liquidity", 0, 10**13, "what this book may use at all"),
    "reserve_floor_minor": ("Reserve floor, never spent", "usd", "liquidity", 0, 10**13, "a plan that would go below it is refused"),
    "horizon_hours": ("Planning horizon", "hours", "liquidity", 1, 720, "obligations due inside it are planned now"),
    "full_confidence_bp": ("Full FX limit when forecast confidence is at least", "pct", "liquidity", 1, 10_000, "the conversion limit applies in full"),
    "zero_limit_below_bp": ("No conversions on the agent's own authority when confidence is below", "pct", "liquidity", 0, 9_999, "the limit falls to zero"),
}


class AuthorityError(PermissionError):
    pass


class NeedsAcknowledgement(ValueError):
    def __init__(self, warnings: list[dict]):
        super().__init__("critical changes need an acknowledged warning: " + ", ".join(w["key"] for w in warnings))
        self.warnings = warnings


def _bounds(unit: str | None) -> tuple[int, int]:
    return {"pct": (0, 10_000), "hours": (0, 2_160), "days": (0, 3_650), "level": (1, 3)}.get(unit or "", (0, 10**13))


def standing(org) -> dict:
    rec = org.get("standing_mandate", "standing_mandate") or {}
    return {"values": rec.get("values", {}), "enabled": rec.get("enabled", {}), "book": rec.get("book", {}),
            "checks": rec.get("checks", {}), "authority_role": rec.get("authority_role", DEFAULT_AUTHORITY),
            "updated_by": rec.get("updated_by_name"), "acknowledged": rec.get("acknowledged", [])}


def check_modes(org) -> dict[str, str]:
    """Every check's mode under the standing mandate; hold unless the mandate authority changed it."""
    saved = (org.get("standing_mandate", "standing_mandate") or {}).get("checks", {}) if org is not None else {}
    return {k: saved.get(k, "hold") for k in CHECK_KEYS}


def rule_on(org, key: str) -> bool:
    """Whether a critical rule without a value (duplicates, recon) is on under the standing mandate."""
    if org is None:
        return True
    return bool(((org.get("standing_mandate", "standing_mandate") or {}).get("enabled") or {}).get(key, True))


def _fmt(unit: str | None, v) -> str:
    if v is None:
        return "-"
    return {"usd": lambda: f"USD {v / 100:,.2f}", "pct": lambda: f"{v / 100:g}%", "hours": lambda: f"{v} hours", "days": lambda: f"{v} days"}.get(unit or "", lambda: str(v))()


def warnings(body: dict, setup: dict, org) -> list[dict]:
    """The critical changes in `body` against what is in force now; each needs an acknowledged warning."""
    out: list[dict] = []
    cur = standing(org)
    modes = check_modes(org)
    specs = {m["key"]: m for m in MANDATES}
    now = {m["key"]: m for m in setup["mandates"]}
    for c in CHECKS:
        new = (body.get("checks") or {}).get(c["key"])
        if c["critical"] and new and MODES.index(new) > MODES.index(modes[c["key"]]):
            out.append({"key": f"check:{c['key']}", "title": f"{c['label']}: {MODE_LABEL[modes[c['key']]]} to {MODE_LABEL[new]}", "risk": c["risk"]})
    for k, v in (body.get("values") or {}).items():
        sign, old = CRITICAL_VALUES.get(k), (now.get(k) or {}).get("value")
        if sign and old is not None and (v - old) * sign > 0:
            m = specs[k]
            kind = "widens the agent's own authority" if m["kind"] == "grant" else "loosens a critical control"
            out.append({"key": f"value:{k}", "title": f"{m['label']}: {_fmt(m['unit'], old)} to {_fmt(m['unit'], v)}",
                        "risk": f"This {kind}. {m.get('help', '')}".strip()})
    for k, v in (body.get("enabled") or {}).items():
        m = specs.get(k)
        was = bool((now.get(k) or {}).get("enabled", True))
        if m and was and not v and (m["kind"] in ("guard", "locked")):
            out.append({"key": f"rule:{k}", "title": f"{m['label']}: switched off",
                        "risk": SWITCHABLE_CRITICAL.get(k, "This safeguard is no longer enforced on any run.")})
    floor = (body.get("book") or {}).get("reserve_floor_minor")
    if floor is not None and floor < setup["reserve_floor_minor"]:
        out.append({"key": "book:reserve_floor_minor", "title": f"Reserve floor: {_fmt('usd', setup['reserve_floor_minor'])} to {_fmt('usd', floor)}",
                    "risk": "Less cash is protected: the agent may spend down to the new floor."})
    role = body.get("authority_role")
    if role and role != cur["authority_role"]:
        out.append({"key": "authority", "title": f"Mandate authority: {AUTHORITY_ROLES[cur['authority_role']]} to {AUTHORITY_ROLES[role]}",
                    "risk": f"From now on only the {AUTHORITY_ROLES[role]} may change the standing mandate; the {AUTHORITY_ROLES[cur['authority_role']]} can no longer change it, or take the authority back."})
    return out


def save(org, body: dict, by: str, *, role: str, setup: dict) -> dict:
    """Validate and keep the standing mandate. Only the role holding the mandate authority may save it, and every
    critical change must be named in body['acknowledged']."""
    cur = standing(org)
    if role != cur["authority_role"]:
        raise AuthorityError(f"only the {AUTHORITY_ROLES[cur['authority_role']]} holds the mandate authority; role '{role}' may read the mandate but not change it")
    specs = {m["key"]: m for m in MANDATES}
    values, enabled, book, checks = dict(cur["values"]), dict(cur["enabled"]), dict(cur["book"]), dict(cur["checks"])
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
        if m is None:
            raise ValueError(f"{k}: unknown rule")
        if k in BUILT_IN:
            raise ValueError(f"{m['label']}: {BUILT_IN[k]}")
        if m["kind"] == "locked" and k not in SWITCHABLE_CRITICAL:
            if not v:
                raise ValueError(f"{m['label']}: a critical rule with a value is changed through its value, not switched off")
            continue
        enabled[k] = bool(v)
    for k, v in (body.get("checks") or {}).items():
        if k not in CHECK_KEYS:
            raise ValueError(f"{k}: unknown check")
        if v not in MODES:
            raise ValueError(f"{k}: the mode is hold, warn or off")
        checks[k] = v
    for k, v in (body.get("book") or {}).items():
        if k not in BOOK_LIMITS:
            raise ValueError(f"{k}: unknown limit")
        lab, _, _, lo, hi, _ = BOOK_LIMITS[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"{lab}: a whole number from {lo} to {hi} is needed")
        book[k] = v
    if book.get("zero_limit_below_bp", 0) >= book.get("full_confidence_bp", 10_001):
        raise ValueError("the zero-limit confidence must be below the full-limit confidence")
    authority = body.get("authority_role") or cur["authority_role"]
    if authority not in AUTHORITY_ROLES:
        raise ValueError(f"the mandate authority is one of: {', '.join(AUTHORITY_ROLES)}")
    warn = warnings(body, setup, org)
    missing = [w for w in warn if w["key"] not in set(body.get("acknowledged") or [])]
    if missing:
        raise NeedsAcknowledgement(missing)
    org._put("standing_mandate", {"_id": "standing_mandate", "values": values, "enabled": enabled, "book": book, "checks": checks,
                                  "authority_role": authority, "updated_by_name": by, "acknowledged": [w["key"] for w in warn]}, by)
    return standing(org) | {"warnings_acknowledged": warn}


def view(setup: dict, org, doc_rules_on: bool, role: str | None = None) -> dict:
    """The page: categories with their policy, checks and rules; the limits table; the approval policy; who may change it."""
    std = org.standards()
    st = standing(org)
    modes = check_modes(org)
    rows = {m["key"]: m for m in setup["mandates"]}
    cats = []
    for c in CATEGORIES:
        checks = []
        if c["key"] == "documents":
            for ch in CHECKS:
                elsewhere = {"require_po": bool(std.get("require_po", True)), "doc_rules": doc_rules_on}.get(ch.get("switch"), True)
                mode = modes[ch["key"]] if elsewhere else "off"
                checks.append(ch | {"mode": mode, "on": mode != "off", "off_elsewhere": not elsewhere})
        cats.append(c | {"checks": checks, "rules": [rows[k] | {"category": c["key"], "short": SHORT[k], "critical": k in CRITICAL_VALUES or rows[k]["kind"] == "locked",
                                                                "switchable": k in SWITCHABLE_CRITICAL, "built_in": BUILT_IN.get(k)}
                                                     for k, cat in RULE_CATEGORY.items() if cat == c["key"] and k in rows]})
    limits = [{"key": k, "label": lab, "unit": unit, "category": cat, "min": lo, "max": hi, "help": hlp, "value": setup.get(k), "book": True,
               "critical": k == "reserve_floor_minor"}
              for k, (lab, unit, cat, lo, hi, hlp) in BOOK_LIMITS.items()]
    limits += [{"key": m["key"], "label": m["label"], "unit": m["unit"], "category": RULE_CATEGORY.get(m["key"], "control"), "kind": m["kind"],
                "min": _bounds(m["unit"])[0], "max": _bounds(m["unit"])[1], "value": m.get("value"), "enabled": m.get("enabled", True), "help": m.get("help", ""),
                "critical": m["key"] in CRITICAL_VALUES or m["kind"] == "locked"}
               for m in setup["mandates"] if m["unit"] is not None]
    limits += [{"key": k, "label": rows[k]["label"], "unit": None, "category": RULE_CATEGORY[k], "kind": "locked", "enabled": rule_on(org, k),
                "help": why, "critical": True, "switch_only": True} for k, why in SWITCHABLE_CRITICAL.items() if k in rows]
    limits += [{"key": m["key"], "label": m["label"], "unit": None, "category": RULE_CATEGORY.get(m["key"], "control"), "kind": m["kind"],
                "enabled": m.get("enabled", True), "help": m.get("help", ""), "critical": False, "switch_only": True}
               for m in setup["mandates"] if m["unit"] is None and m["kind"] != "locked"]
    order = [c["key"] for c in CATEGORIES]
    limits.sort(key=lambda x: order.index(x["category"]))
    return {"categories": cats, "limits": limits, "approval_matrix": setup["approval_matrix"], "approvers": setup["approvers"],
            "standing": st, "titles": {c["key"]: c["title"] for c in CATEGORIES}, "modes": MODE_LABEL,
            "authority": {"role": st["authority_role"], "label": AUTHORITY_ROLES[st["authority_role"]], "roles": AUTHORITY_ROLES,
                          "you_may_change": role == st["authority_role"] if role else None}}
