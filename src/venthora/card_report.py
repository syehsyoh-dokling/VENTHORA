# ruff: noqa: E501
"""Reporting -> Virtual card (Visa): what happened on every Visa virtual card VENTHORA issued through Airwallex.

Two sources, joined on the card transaction's lifecycle id:
* VENTHORA's own decisions: every authorization Airwallex asked about (remote authorization) or a local test sent
  through the same check, with the rule that decided it (per-transaction, monthly or mandate limit, merchant,
  merchant category, duplicate, frozen card, currency, unknown card, signature);
* what Airwallex recorded on the card: the authorization, its clearing (the amount billed), a refund or reversal.
A charge Airwallex decided alone (remote authorization off) has no VENTHORA decision and says so.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

RULES = [("inside the mandate", "Inside the mandate"), ("per-transaction limit", "Per-transaction limit"), ("monthly limit", "Monthly limit"), ("mandate", "Mandate limit"),
         ("is not the card's vendor", "Merchant not allowed"), ("merchant category", "Merchant category not allowed"),
         ("already authorized", "Duplicate charge"), ("frozen", "Card frozen"), ("card currency", "Currency"),
         ("not issued by venthora", "Unknown card"), ("signature", "Signature"), ("not signed", "Signature"), ("too old", "Signature")]


def rule_of(reason: str | None, decision: str | None) -> str:
    low = (reason or "").lower()
    for needle, label in RULES:
        if needle in low:
            return label
    return "Inside the mandate" if decision == "AUTHORIZED" else "Other"


def _minor(v) -> int | None:
    if v is None or v == "":
        return None
    return int((Decimal(str(v)) * 100).to_integral_value())


def build(*, org, airwallex: list[dict] | None, to_base, program: dict, tap: dict) -> dict:
    cards = {c["_id"]: c for c in org.list("card")}
    decisions = sorted(org.list("card_auth"), key=lambda d: d.get("at", ""))
    posted: dict[str, list[dict]] = {}
    for t in airwallex or []:
        posted.setdefault(t.get("lifecycle_id") or t.get("card_transaction_lifecycle_id") or t.get("transaction_id") or "", []).append(t)
    month = dt.datetime.now(dt.UTC).strftime("%Y-%m")
    rows, seen = [], set()

    def post_info(ts: list[dict]) -> dict | None:
        if not ts:
            return None
        last = sorted(ts, key=lambda t: str(t.get("posted_date") or t.get("transaction_date") or ""))[-1]
        return {"status": last.get("status"), "type": last.get("transaction_type"), "billed_minor": _minor(last.get("billing_amount")),
                "billed_currency": last.get("billing_currency"), "posted_date": last.get("posted_date") or last.get("transaction_date"),
                "auth_code": last.get("auth_code"), "steps": [f"{t.get('transaction_type') or ''} {t.get('status') or ''}".strip() for t in ts]}

    for d in decisions:
        card = cards.get(d.get("card_id") or "") or {}
        life = d.get("lifecycle_id")
        seen.add(life)
        rows.append({"at": d.get("at"), "card_id": d.get("card_id"), "masked": card.get("masked"), "payee": d.get("payee") or card.get("payee"),
                     "merchant": d.get("merchant"), "mcc": d.get("mcc"), "amount_minor": d.get("amount_minor"), "currency": d.get("currency"),
                     "base_minor": to_base(d.get("amount_minor"), d.get("currency")), "decision": d.get("decision"), "reason": d.get("reason"),
                     "rule": rule_of(d.get("reason"), d.get("decision")), "decided_by": "VENTHORA", "type": d.get("type"),
                     "source": "Local test" if d.get("source") == "test" else "Airwallex (live)", "event_id": d.get("_id"), "lifecycle_id": life,
                     "posted": post_info(posted.get(life or "", []))})
    for life, ts in posted.items():
        if life in seen:
            continue
        t = ts[0]
        card = cards.get(t.get("card_id") or "") or {}
        ok = str(t.get("status") or "").upper() in ("APPROVED", "AUTHORIZED", "CLEARED", "SETTLED", "PENDING")
        amount = _minor(t.get("transaction_amount"))
        rows.append({"at": t.get("transaction_date") or t.get("created_at"), "card_id": t.get("card_id"), "masked": t.get("masked_card_number") or card.get("masked"),
                     "payee": card.get("payee"), "merchant": (t.get("merchant") or {}).get("name"), "mcc": (t.get("merchant") or {}).get("category_code"),
                     "amount_minor": amount, "currency": t.get("transaction_currency"), "base_minor": to_base(amount, t.get("transaction_currency")),
                     "decision": "AUTHORIZED" if ok else "DECLINED", "reason": t.get("failure_reason") or "decided by the card's controls on Airwallex",
                     "rule": "Airwallex controls", "decided_by": "Airwallex", "type": t.get("transaction_type"), "source": "Airwallex (live)",
                     "event_id": t.get("transaction_id"), "lifecycle_id": life, "posted": post_info(ts)})
    out_cards = []
    for c in cards.values():
        mine = [r for r in rows if r["card_id"] == c["_id"]]
        spent = sum(r["amount_minor"] or 0 for r in mine if r["decision"] == "AUTHORIZED" and str(r["at"] or "").startswith(month) and r["source"] != "Local test")
        out_cards.append({"card_id": c["_id"], "masked": c.get("masked"), "payee": c.get("payee"), "brand": c.get("brand") or "VISA", "status": c.get("status"),
                          "frozen": bool(c.get("frozen")), "currency": c.get("currency"), "per_tx_minor": c.get("per_tx_minor"), "monthly_minor": c.get("monthly_minor"),
                          "month_spent_minor": spent, "merchant": c.get("merchant"), "mcc": c.get("mcc") or [], "issued_at": c.get("issued_at"), "issued_by": c.get("issued_by"),
                          "authorized": sum(1 for r in mine if r["decision"] == "AUTHORIZED"), "declined": sum(1 for r in mine if r["decision"] == "DECLINED"),
                          "last_at": max((r["at"] or "" for r in mine), default=None)})
    return {"cards": sorted(out_cards, key=lambda c: c.get("issued_at") or ""), "rows": rows[::-1], "program": program,
            "tap": {"key_published": bool(tap.get("keys")), "key_id": (tap.get("keys") or [{}])[0].get("key_id") if tap.get("keys") else None}}
