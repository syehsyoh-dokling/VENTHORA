# ruff: noqa: E501
"""Platform Setup → Optional: what the FCO switches on or off once the Developer has set the services up.

  visa    which Visa features VENTHORA may use (each refused by the server while it is off)
  airi    whether staff may check out with Airi, and within what limits
  metal   which kinds of record are anchored on Metal

The technical setup of the same services (keys, credentials, endpoints) is in the Developer Console; these are the
company's choices. The FCO changes them; a Developer only reads them.
"""
from __future__ import annotations

import datetime as dt
import hashlib

KIND = "controls"
DEFAULTS = {
    "visa": {"vic_purchase_instructions": True, "visa_fx_rate": True, "agent_signed_requests": True, "supplier_match": True},
    "airi": {"enabled": False, "staff_wallets_only": True, "currency": "USD", "max_per_checkout_minor": 50_000, "monthly_cap_minor": 200_000,
             "fco_approval_above_minor": 20_000, "blocked_categories": ["gambling", "cash", "crypto"],
             "require_approved_pr": True, "price_within_pr": True, "require_vendor_invoice": True, "require_verified_vendor": True,
             "same_vendor_as_pr": True},
    "metal": {"audit_chain": True, "contract_chains": True, "payments": True, "approvals": True, "cards": True, "placements": True,
              "bank_checks": False, "procurement": False, "settings_changes": False},
}
LABELS = {
    "airi": {"require_approved_pr": "Airi pays only against an approved purchase request (PR number valid)",
             "price_within_pr": "The checkout total may not exceed the PR's approved estimate",
             "require_vendor_invoice": "A vendor invoice (or order confirmation) must be attached to the checkout",
             "require_verified_vendor": "The vendor must be a verified payee (its bank details checked; none waiting for a call-back)",
             "same_vendor_as_pr": "The vendor must be the PR's supplier, when the PR names one"},
    "visa": {"vic_purchase_instructions": "Visa Intelligent Commerce: turn a card mandate into Visa's purchase instruction",
             "visa_fx_rate": "Visa's daily FX rate in reports and checks",
             "agent_signed_requests": "Agent identity (TAP): the agent signs its requests to merchants",
             "supplier_match": "Visa Supplier Matching: check whether a vendor accepts Visa"},
    "metal": {"audit_chain": "The whole audit chain (its latest hash)", "contract_chains": "Every contract chain",
              "payments": "Payments made (transfers, payouts, e-wallet payouts, placements sent)",
              "approvals": "Approvals and authority (plans, delegation, decisions, call-backs)",
              "cards": "Cards (issued, frozen, charges, advances)", "placements": "Cash placements and their providers",
              "bank_checks": "Bank checks and payee changes", "procurement": "Procurement (requests, RFQs, awards)",
              "settings_changes": "Settings changes (keys, connections, policies)"},
}
# the audit event kinds behind each Metal category (prefixes)
METAL_KINDS = {
    "payments": ("execution", "autonomous_execution", "agent_plan_executed", "payment_order", "paid_on_arrival", "intake_paid_from_pending",
                 "wallet_payout_", "cash_placement_sent", "card_account_"),
    "approvals": ("plan", "doa_", "purchase_request_decided", "fund_request_decided", "payroll_", "payee_verified", "cash_placement_approved",
                  "standing_mandate", "priority_sop_saved", "replan"),
    "cards": ("card_", "advance_card_issued", "issuing_cardholder"),
    "placements": ("cash_placement_", "placement_provider_"),
    "bank_checks": ("beneficiary_", "payee_", "company_bank_details"),
    "procurement": ("procurement_", "pr_from_email", "purchase_request"),
    "settings_changes": ("ai_settings", "ai_key", "airwallex_connected", "airwallex_disconnected", "xendit_settings", "visa_", "email_", "outbox_smtp",
                         "org_standards", "controls_saved"),
}
# Visa routes the server refuses while their switch is off
VISA_ROUTES = {"/v1/visa/vic/preview": "vic_purchase_instructions", "/v1/visa/fx-rate": "visa_fx_rate",
               "/v1/visa/tap/sign-test": "agent_signed_requests", "/v1/visa/supplier-match": "supplier_match",
               "/v1/visa/supplier-match/vendors": "supplier_match"}


def get(org, group: str) -> dict:
    rec = org.get(KIND, group) or {}
    return DEFAULTS[group] | {k: rec[k] for k in DEFAULTS[group] if k in rec}


def save(org, group: str, body: dict, by: str) -> dict:
    if group not in DEFAULTS:
        raise KeyError(group)
    new = {}
    for k, default in DEFAULTS[group].items():
        if k not in body:
            continue
        v = body[k]
        if isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, int):
            v = int(v)
            if v < 0:
                raise ValueError(f"{k} cannot be negative")
        elif isinstance(default, list):
            v = [str(x).strip().lower()[:40] for x in (v or []) if str(x).strip()][:30]
        else:
            v = str(v).strip().upper()[:3] if k == "currency" else str(v)[:200]
        new[k] = v
    cur = get(org, group) | new
    if group == "airi" and cur["fco_approval_above_minor"] > cur["max_per_checkout_minor"]:
        raise ValueError("the FCO approval threshold cannot be above the limit per checkout")
    if group == "airi" and cur["max_per_checkout_minor"] > cur["monthly_cap_minor"]:
        raise ValueError("the limit per checkout cannot be above the monthly cap")
    org._put(KIND, {"_id": group} | cur | {"updated_by": by, "updated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}, by)
    return cur


def view(org, group: str) -> dict:
    rec = org.get(KIND, group) or {}
    return {"values": get(org, group), "defaults": DEFAULTS[group], "labels": LABELS.get(group, {}),
            "updated_by": rec.get("updated_by"), "updated_at": rec.get("updated_at")}


def airi_check(org, co: dict, payee_status) -> dict:
    """Whether VENTHORA approves one Airi checkout: the FCO's Airi rules, each with its reason. co: pr_number,
    amount_minor, currency, vendor (payee id or name), invoice (a reference or file name), category. payee_status(id)
    -> the payee's lifecycle status or None. Answers allowed, held for the FCO, or refused."""
    r = get(org, "airi")
    refused, notes = [], []
    if not r["enabled"]:
        return {"decision": "REFUSED", "reasons": ["Airi checkouts are switched off (Platform Setup → Airi usage)"]}
    amount, ccy = int(co.get("amount_minor") or 0), str(co.get("currency") or r["currency"]).upper()
    pr = org.get("pr", str(co.get("pr_number") or "")) if co.get("pr_number") else None
    if r["require_approved_pr"]:
        if pr is None:
            refused.append("no valid PR number")
        elif pr.get("status") not in ("APPROVED", "ORDERED"):
            refused.append(f"PR {pr['pr_number']} is {str(pr.get('status')).lower()}, not approved")
    if pr is not None and r["price_within_pr"] and pr.get("currency", "USD") == ccy and amount > int(pr.get("estimated_minor") or 0):
        refused.append(f"{ccy} {amount / 100:,.2f} is above PR {pr['pr_number']}'s approved {ccy} {int(pr['estimated_minor']) / 100:,.2f}")
    if r["require_vendor_invoice"] and not str(co.get("invoice") or "").strip():
        refused.append("no vendor invoice attached")
    vendor = str(co.get("vendor") or "").strip()
    if r["same_vendor_as_pr"] and pr is not None and pr.get("supplier_id") and vendor and vendor != pr["supplier_id"]:
        refused.append(f"the vendor is not PR {pr['pr_number']}'s supplier ({pr['supplier_id']})")
    if r["require_verified_vendor"]:
        st = payee_status(vendor) if vendor else None
        if st not in ("VERIFIED", "ACTIVE"):
            refused.append(f"the vendor is not a verified payee ({str(st or 'unknown').lower()})")
    if str(co.get("category") or "").strip().lower() in r["blocked_categories"]:
        refused.append(f"blocked merchant category {co['category']}")
    if ccy == r["currency"] and amount > r["max_per_checkout_minor"]:
        refused.append(f"above the limit per checkout ({ccy} {r['max_per_checkout_minor'] / 100:,.2f})")
    if refused:
        return {"decision": "REFUSED", "reasons": refused}
    if ccy == r["currency"] and amount > r["fco_approval_above_minor"]:
        notes.append(f"above {ccy} {r['fco_approval_above_minor'] / 100:,.2f}: the FCO approves it first")
        return {"decision": "HELD_FOR_FCO", "reasons": notes}
    return {"decision": "ALLOWED", "reasons": ["inside every Airi rule"]}


def visa_refusal(org, path: str) -> str | None:
    key = VISA_ROUTES.get(path)
    if key and not get(org, "visa")[key]:
        return f"switched off by the FCO: {LABELS['visa'][key]} (Platform Setup → Visa controls)"
    return None


def metal_heads(org, audit, base: dict) -> dict:
    """The heads Metal anchors, as the FCO chose them: the whole audit chain and the contract chains when selected,
    and one digest per selected category over its events' hashes (so a category can be proven on its own)."""
    sel = get(org, "metal")
    out = {"categories": {}}
    if sel["audit_chain"]:
        out["audit"] = base["audit"]
    if sel["contract_chains"]:
        out["contracts"] = base["contracts"]
    with audit._connect() as c:
        rows = c.execute("SELECT event_type, current_hash FROM events ORDER BY seq").fetchall()
    for cat, prefixes in METAL_KINDS.items():
        if not sel.get(cat):
            continue
        hs = [r["current_hash"] for r in rows if r["event_type"].startswith(prefixes)]
        out["categories"][cat] = {"events": len(hs), "digest": hashlib.sha256("".join(hs).encode()).hexdigest() if hs else None}
    return out
