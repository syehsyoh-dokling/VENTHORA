# ruff: noqa: E501
"""Platform Setup → Blockchain Record (powered by Alta, alta.finance: a payments blockchain).

What the Finance Director sees and chooses: why the company would keep a blockchain record, what it costs, Alta's
products and which of them VENTHORA uses, whether the company wants it, and which documents go on the record (all,
or only those from a value up). Each chosen document kind gets its own digest in the anchor, next to the activity
categories (controls.METAL_KINDS), so it can be proven on its own. Only digests go on chain: never a document.
"""
from __future__ import annotations

import hashlib
import json

KIND, ID = "blockchain_record", "settings"
PROVIDER = {"name": "Alta", "company": "Alta (alta.finance)", "site": "https://alta.finance", "docs": "https://docs.metalntwx.com", "network": "Alta Testnet (chain 63825)"}

# the cost model, from Alta's published rates (docs.metalntwx.com → Network → Credits): RPC is prepaid in USD, no
# subscription and no invoice, USD 0.05 per 1,000 base requests; gas is paid in USDZ (18 decimals) at 1 base unit
# per gas, so an anchor's ~23,000 gas is a negligible fraction of a cent.
RPC_PER_1000_USD = 0.05
REQUESTS_PER_ANCHOR = 6
GAS_PER_ANCHOR = 23_000


def cost_estimate(anchors_per_day: int = 24) -> dict:
    per_month = anchors_per_day * 30 * REQUESTS_PER_ANCHOR / 1000 * RPC_PER_1000_USD
    return {"anchors_per_day": anchors_per_day, "per_month_usd": round(per_month, 2), "per_year_usd": round(per_month * 12, 2),
            "subscription": None, "gas_per_anchor_usdz": GAS_PER_ANCHOR / 10**18,
            "note": "prepaid, no subscription and no invoice: about USD 0.05 per 1,000 requests; gas is a negligible fraction of a cent. "
                    "On Alta Testnet today the tokens come from the faucet: free."}


BENEFITS = {
    "finance_director": ["Proof that every payment, approval and change kept the mandate, which anyone can verify without trusting VENTHORA's server",
                         "An audit trail that cannot be rewritten after the fact, not even by an administrator",
                         "Choose exactly what is proven: every document, or only those above the value that matters"],
    "company": ["Lower audit effort and cost: the auditor checks a digest instead of re-performing every sample",
                "Stronger controls story for investors, lenders and donors; disputes settled with a record no one can alter",
                "Private by design: only fingerprints go on chain, never amounts, names or documents"],
    "clients": ["Vendors and consultants can verify that their PO, contract or payment record is the one that was approved",
                "A contract edited after signing shows as altered, protecting both sides",
                "Faster dispute resolution: one shared, tamper-evident reference"],
    "transactions": ["Each payment's decision (checks passed, approver, mandate) is anchored within about a second of finality",
                     "Duplicate or altered invoices are provable against the anchored original",
                     "Money still moves on Airwallex: the blockchain proves, it does not hold funds"],
    "promotions": ["Alta Testnet is free: VENTHORA tops up its own test tokens from the faucet and buys its RPC access itself (x402)",
                   "No subscription: pay per use only when moving to the production network"],
}

# Alta's products, what each does, what it costs, and whether VENTHORA uses it today
PRODUCTS = [
    {"key": "proof_of_record", "label": "Proof of record (anchoring)", "status": "active",
     "what": "The fingerprints of VENTHORA's records (audit chain, contracts, chosen documents) written on Alta in one transaction, final in about a second, readable by anyone on the explorer.",
     "cost": "About 6 RPC requests and 23,000 gas per anchor: roughly USD 0.22 a month at one anchor an hour."},
    {"key": "agent_payments", "label": "Agent payments (x402)", "status": "active",
     "what": "The agent pays for its own services in USDZ stablecoin, without a card: VENTHORA already buys its Alta RPC access this way, under on-chain spending limits.",
     "cost": "A USD 1 credit pack per purchase; the facilitator pays the gas."},
    {"key": "payment_controls", "label": "Programmable payment controls", "status": "planned",
     "what": "Rules checked by the network during a transfer (limits, allowed payees); a refused transfer reverts or is quarantined. VENTHORA's mandate could be mirrored here.",
     "cost": "Network gas only (stablecoin), no licence fee."},
    {"key": "shielded_payments", "label": "Shielded (private) payments", "status": "planned",
     "what": "Payments whose asset, amount and parties are hidden from the public while compliance is still enforced: suited to payroll and staff advances.",
     "cost": "Network gas only (stablecoin), no licence fee."},
    {"key": "stablecoin_rail", "label": "Stablecoin payments and tokenised assets (MIP20)", "status": "planned",
     "what": "Stablecoins and tokenised deposits as a second rail to vendors that accept them, with the same policy checks; Airwallex stays the main rail.",
     "cost": "Network gas only, paid in the stablecoin itself."},
    {"key": "regulatory_evidence", "label": "Evidence and regulatory access", "status": "planned",
     "what": "Proofs a regulator or auditor can verify without opening the underlying data (IVMS evidence, policy proofs).",
     "cost": "Included with the network; no separate fee."},
]

# the documents VENTHORA keeps that can go on the record, the field their value is read from, and their label
# only long-lived commitments: payees' bank changes, advances, card charges and payments are already proven by the
# activity categories (controls.METAL_KINDS), so they are not offered twice here
DOCUMENTS = [
    ("pr", "Purchase requests", "estimated_minor"),
    ("po", "Purchase orders", "total_minor"),
    ("engagement", "Consultant contracts", "fee_minor"),
    ("project", "Projects (with their contracts, long-running)", None),
]
DOC_LABEL = {k: lbl for k, lbl, _ in DOCUMENTS}
DEFAULT = {"interested": False, "product": "proof_of_record",
           "documents": {k: {"on": k in ("po", "engagement", "project"), "min_minor": 0, "only": []} for k, *_ in DOCUMENTS}}


def get(org) -> dict:
    rec = org.get(KIND, ID) or {}
    docs = DEFAULT["documents"] | {k: v for k, v in (rec.get("documents") or {}).items() if k in DOC_LABEL}
    return {"interested": bool(rec.get("interested", DEFAULT["interested"])), "product": rec.get("product") or DEFAULT["product"], "documents": docs}


def save(org, body: dict, by: str) -> dict:
    cur = get(org)
    if "interested" in body:
        cur["interested"] = bool(body["interested"])
    if body.get("product"):
        if body["product"] not in {p["key"] for p in PRODUCTS}:
            raise ValueError(f"unknown product {body['product']}")
        cur["product"] = body["product"]
    for k, v in (body.get("documents") or {}).items():
        if k not in DOC_LABEL:
            raise ValueError(f"unknown document kind {k}")
        m = int((v or {}).get("min_minor") or 0)
        if m < 0:
            raise ValueError("a value threshold cannot be negative")
        only = [str(x).strip().upper() for x in (v or {}).get("only") or [] if str(x).strip()][:200]
        cur["documents"][k] = {"on": bool((v or {}).get("on")), "min_minor": m, "only": only if k == "project" else []}
    org._put(KIND, {"_id": ID} | cur, by)
    return cur


def document_heads(org, payees: list[dict] | None = None) -> dict:
    """One digest per chosen document kind, over the records at or above its value threshold (projects: all, or the
    ones chosen), canonical JSON sorted, so each kind can be proven on its own. Nothing when the company is not interested."""
    s = get(org)
    if not s["interested"]:
        return {}
    out = {}
    for kind, label, field in DOCUMENTS:
        d = s["documents"].get(kind) or {}
        if not d.get("on"):
            continue
        rows = [r for r in org.list(kind) if not field or not d.get("min_minor") or int(r.get(field) or 0) >= d["min_minor"]]
        if kind == "project" and d.get("only"):
            rows = [r for r in rows if str(r.get("code") or "").upper() in set(d["only"])]
        blob = "".join(json.dumps(r, sort_keys=True, default=str) for r in sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str)))
        out[kind] = {"label": label, "records": len(rows), "min_minor": d.get("min_minor") or 0,
                     "digest": hashlib.sha256(blob.encode()).hexdigest() if rows else None}
    return out


def view(org, payees: list[dict] | None = None) -> dict:
    projects = sorted(({"code": p.get("code"), "name": p.get("name")} for p in org.list("project") if p.get("code")), key=lambda p: p["code"])
    return {"provider": PROVIDER, "benefits": BENEFITS, "cost": cost_estimate(), "products": PRODUCTS,
            "documents": [{"key": k, "label": lbl, "by_value": bool(f)} for k, lbl, f in DOCUMENTS], "projects": projects,
            "settings": get(org), "preview": document_heads(org, payees)}
