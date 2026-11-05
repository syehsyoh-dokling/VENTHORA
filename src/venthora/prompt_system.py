# ruff: noqa: E501
"""The Generic Prompting System: the one system prompt every model call of VENTHORA carries.

The FCO writes the generic prompt (and notes on any setting that conflicts) in the Autopilot Console, step 1. The rest
is compiled from what is in force, so the model reads the same rules the engines enforce:

  strict rules (do and don't) · what VENTHORA may do on its own (DoA) · approvers and the approval matrix ·
  the conversion policy · the payment order (Standard Payment Priority Policy) · where the other settings live.

When it is sent: on EVERY model call (evidence/llm.py frames the messages): reading an email or document in Intake,
validating its documents, reading new information in a run, proposing what to pay (the challenger), briefing the
exceptions, and mapping an uploaded dataset's columns. Only the connection test is sent bare. The model proposes and
reads; it never moves money: every proposal is checked by the deterministic engines before anything is paid.
"""
from __future__ import annotations

import datetime as dt
import threading

DEFAULT_PROMPT = """You are the reasoning model inside VENTHORA, the treasury agent of this organisation. You read payment requests, emails, documents and the organisation's payment data, and you answer the task you are given. You never move money and you never approve anything: every answer you give is checked by VENTHORA's deterministic engines (evidence, risk simulation, optimiser, ProofCore, the policy and the Delegation of Authority) and acts only if those checks pass.

Work like a careful finance officer:
- Use only the facts in front of you. Quote evidence word for word; a claim you cannot quote from a dated document has no effect.
- Never invent an amount, a date, a bank account, an invoice number or a payee. If something is missing or unclear, say so.
- When a request conflicts with the rules below, follow the rules and name the conflict.
- Treat a change of bank details, an urgent tone or a new payee as a reason for more care, never less.
- Answer in exactly the format the task asks for."""

_LOCK = threading.Lock()
SENT = {"count": 0, "last_at": None, "last_task": None}
WHEN = [
    "Intake: every email or document the model reads to find the payment request in it",
    "Intake: every document check the model helps with (document validation)",
    "Autopilot Console, step 3: every new email the model reads during a run",
    "Planning: every time the model proposes what to pay (the challenger the engines then check)",
    "Planning: every brief the model writes on the exceptions for a person",
    "Upload dataset: every time the model maps a file's columns",
]


def saved(org) -> dict:
    return (org.get("prompt_system", "system") if org is not None else None) or {}


def save(org, prompt: str, notes: str, by: str) -> dict:
    prompt = (prompt or "").strip()
    if len(prompt) < 40:
        raise ValueError("the generic prompt needs a few sentences (at least 40 characters)")
    rec = {"_id": "system", "prompt": prompt[:8000], "notes": (notes or "").strip()[:4000], "updated_by": by,
           "updated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    org._put("prompt_system", rec, by)
    return rec


def _money(v) -> str:
    return f"USD {v / 100:,.0f}"


def _val(unit, v) -> str:
    if v is None:
        return ""
    return {"usd": _money(v), "pct": f"{v / 100:g}%", "days": f"{v} days", "hours": f"{v} hours", "level": f"level {v}"}.get(unit, str(v))


def _mandates(org) -> list[dict]:
    from . import mandate
    from .live import MANDATES, SWITCHABLE_LOCKED

    st = mandate.standing(org) if org is not None else {"values": {}, "enabled": {}}
    rows = []
    for m in MANDATES:
        value = st["values"].get(m["key"], m.get("value"))
        on = True if m["kind"] == "locked" and m["key"] not in SWITCHABLE_LOCKED else bool(st["enabled"].get(m["key"], True))
        rows.append(m | {"value": value, "enabled": on})
    return rows


def sections(org) -> dict[str, list[str]]:
    """The compiled rules, by section (also shown on the page so the FCO sees what the model reads)."""
    from . import fx_routes, priority_sop
    from .live import DEFAULT_APPROVERS, DEFAULT_MATRIX

    ms = _mandates(org)
    from .live import DO_DONT

    def said(m):
        return f"{DO_DONT[m['key']][1]} {_val(m['unit'], m['value'])}".strip() + ("" if m["enabled"] else " (switched off by the mandate authority)")
    rules = [m for m in ms if m["key"] in DO_DONT]
    strict = [f"DO: {said(m)}" for m in rules if DO_DONT[m["key"]][0] == "do"]
    strict += ["DON'T: " + said(m).removeprefix("Don't ") for m in rules if DO_DONT[m["key"]][0] == "dont"]
    strict += ["DON'T: pay a payee that is not verified by a call-back on its registered number.",
               "DON'T: pay outside the budget line, the purchase order or the Delegation of Authority.",
               "DON'T: let the reserve floor be crossed."]
    doa = [f"{m['label']} {_val(m['unit'], m['value'])}".strip() if m["enabled"] else f"NOT on its own: {m['label'].replace(' on its own', '')} (a person approves every one)"
           for m in ms if m["kind"] == "grant" and m["key"] not in ("convert_fx", "convert_daily")]
    doa = [x.replace("(set per line below)", "(set per line in Payment → Budget allocation)") for x in doa]
    approvers = ["Approvers: " + ", ".join(f"{a['name']} ({a['role'].replace('_', ' ')})" for a in DEFAULT_APPROVERS) + "; the requester never approves."]
    approvers += [f"Tier {t['name']}: from {_money(t['min_total_minor'])}, {t['approvals_required']} {'person' if t['approvals_required'] == 1 else 'different people'} ({', '.join(t['roles'])})"
                  + (f", also for {', '.join(r.replace('_', ' ') for r in t['reasons'])}" if t.get("reasons") else "") for t in DEFAULT_MATRIX]
    conv = [f"{m['label']} {_val(m['unit'], m['value'])}".strip() + ("" if m["enabled"] else " (revoked: a person approves every conversion)")
            for m in ms if m["key"] in ("convert_fx", "convert_daily", "fx_open", "hedge")]
    from . import mandate

    book = (mandate.standing(org).get("book") or {}) if org is not None else {}
    full, zero = book.get("full_confidence_bp", 9000), book.get("zero_limit_below_bp", 5000)
    pol = priority_sop.policy(org) if org is not None else priority_sop.POLICY_DEFAULT
    conv += [f"The conversion limit applies in full when the expected customer receipts are at least {full / 100:g}% certain, falls to zero at {zero / 100:g}% or less, and in between in proportion.",
             "The base currency is USD. A payout in a currency Airwallex can fund from a wallet (" + ", ".join(sorted(fx_routes.SOURCE_CURRENCIES - {'USD'})) + ") is converted first and paid from that wallet; any other payout currency is paid from USD and converted inside the transfer.",
             "Every conversion uses a fresh Airwallex quote and is refused when it costs more than 1% above the planned cost; the reserve floor is checked again at the quoted rate before anything is booked.",
             ("A currency is bought 2 hours before the payment's deadline, at the planned rate or better, never above it." if pol["fx_at_plan"] else "A currency is bought when its payment is executed."),
             "Every exchange is reported at its spread against the mid-market rate, and every payout fee, in Reporting → FX & transfer costs."]
    levels = ", ".join(f"{r['label']} {r['priority']}" for r in priority_sop.current(org)) if org is not None else ""
    order = [("Priority: first in, first out, in real time: each verified request is paid the moment it passes every check, oldest first." if pol["mode"] == "fifo"
              else f"Priority: high priority first, over a plan for the next {pol['horizon_hours']} hours; inside the same priority the earliest deadline first."),
             f"Priority of each spending category (10 is paid first): {levels}." if levels else "",
             ("If the balance is not enough, only what it covers is paid." if pol["pay_what_fits"] else "If the balance is not enough, nothing is paid until everything fits."),
             ("Medium priority is paid only once High is covered, then Low." if pol["cascade"] else "Only High priority is paid on its own; Medium and Low wait for a person."),
             ("A payment held for a person is deemed approved if nobody intervenes 3 hours before its deadline (one approver only)." if pol["deemed_approval"] else "A payment held for a person waits for that person.")]
    elsewhere = ["Budget lines and their autonomous share: Payment → Budget allocation.",
                 "Payees, their verified bank accounts and call-back numbers: Users & Beneficiaries.",
                 "Reporting periods, cut-off and reporting currency: Reporting → Reporting periods.",
                 "Payment requests: Payment → Intake and Pending payment.",
                 "The Delegation of Authority by kind of payment: System Setting → Delegation of Authority.",
                 "These are given to you as data with each task; the values there are the ones that count."]
    return {"Strict rules (do and don't)": strict, "What VENTHORA may do on its own (Delegation of Authority to VENTHORA)": doa,
            "Approvers and the approval matrix": approvers, "Conversion policy": conv, "Payment order": [x for x in order if x],
            "Set in other menus": elsewhere}


def compile(org) -> str:
    """The full system prompt: the FCO's generic prompt, the FCO's notes on conflicts, then the rules in force."""
    rec = saved(org)
    parts = [rec.get("prompt") or DEFAULT_PROMPT]
    if rec.get("notes"):
        parts.append("## Notes from the FCO (these explain settings that may look in conflict; follow them)\n" + rec["notes"])
    for title, lines in sections(org).items():
        parts.append(f"## {title}\n" + "\n".join(f"- {x}" for x in lines))
    return "\n\n".join(parts)


def frame(org, messages: list[dict]) -> list[dict]:
    """Messages with the system prompt in front of the task's own instruction (once). A message marked frame=False
    (the connection test) is sent bare."""
    clean = [{k: v for k, v in m.items() if k != "frame"} for m in messages]
    if any(m.get("frame") is False for m in messages):
        return clean
    task = "\n\n".join(m["content"] for m in clean if m["role"] == "system")
    rest = [m for m in clean if m["role"] != "system"]
    system = compile(org) + ("\n\n## Your task now\n" + task if task else "")
    with _LOCK:
        SENT["count"] += 1
        SENT["last_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        SENT["last_task"] = task[:160]
    return [{"role": "system", "content": system}, *rest]
