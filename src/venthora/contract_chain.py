# ruff: noqa: E501
"""Reporting -> Contract blockchain: every contract (purchase order, consultant contract) as its own hash chain.

Block 0 seals what was agreed: number, supplier, currency, value, scheduled terms, charge code, dates, LOE. Every
later block is one event on the contract, in time order: an invoice received against it, a payment booked against it
(with the Airwallex transaction code), a draw in flight. Each block's hash is SHA-256 over the previous block's hash
and the block's own data, so changing any past block changes every hash after it.

The chain is sealed: the hashes seen are kept (record `contract_seal`, one per contract) and every new seal is also
written to the append-only audit log, which has its own hash chain. On the next read the chain is rebuilt from the
data and compared with the seal: same hashes = Intact; a changed block = Altered at that block (the agreed terms
changed after sealing = Amended). New blocks are added to the seal.

Next to the chain, the checks a finance controller runs on a contract, each pass / fail / not applicable:
value (paid + pending within the contract value), payee (every invoice from the contract's supplier), currency, period
(paid inside the contract dates), duplicate (no invoice number twice), terms (no scheduled term paid above its amount),
charge code (paid to the contract's charge code), LOE (days billed within the level of effort, when known).
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

CHECKS = {"chain": "Chain intact", "value": "Within contract value", "payee": "Paid to the contracted party", "currency": "Contract currency",
          "period": "Inside the contract period", "duplicate": "No duplicate invoice", "terms": "Terms not overpaid", "charge": "Contract charge code",
          "loe": "Within level of effort"}
GENESIS = "0" * 64


def kind_of(po: dict) -> str:
    desc = (po.get("description") or "").lower()
    if po.get("loe") not in (None, "") or str(po.get("po_id", "")).upper().startswith("ENG-") or desc.startswith("consultant"):
        return "Consultant contract"
    return "Purchase order"


def _h(prev: str, data: dict) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(f"{prev}|{raw}".encode()).hexdigest()


def blocks_of(po: dict, *, payments: list[dict], invoices: list, in_flight: list[dict], codes: dict[str, str], order: list[str] | None = None) -> list[dict]:
    """The chain: block 0 the agreed terms; then the blocks already sealed, in the order they were sealed; then new
    events in time order; a payment in flight last (never sealed: it becomes a booked payment)."""
    agreed = {k: po.get(k) for k in ("po_id", "supplier_id", "currency", "total_minor", "terms", "account_code", "start_date", "end_date", "expiry_date", "issued_date", "loe")}
    events = [{"key": "contract", "at": po.get("issued_date") or po.get("start_date") or "", "type": "contract", "title": "Contract sealed", "data": agreed}]
    for q in invoices:
        events.append({"key": f"invoice:{q.intake_id}", "at": q.received_at, "type": "invoice", "title": f"Invoice {q.invoice_no or q.intake_id[:8]} received",
                       "data": {"intake_id": q.intake_id, "invoice_no": q.invoice_no, "from": q.counterparty_id, "currency": q.currency,
                                "amount_minor": q.amount_minor, "installment": q.installment, "fingerprint": q.invoice_fingerprint},
                       "amount_minor": q.amount_minor, "currency": q.currency, "status": q.status, "invoice_no": q.invoice_no,
                       "flags": [f for f in (getattr(q, "flags", []) or []) if f.startswith("po_")]})
    for p in payments:
        events.append({"key": f"payment:{p['transfer_id']}", "at": p["paid_at"], "type": "payment", "title": "Payment booked",
                       "data": {k: p.get(k) for k in ("transfer_id", "installment", "currency", "amount_minor", "paid_at", "obligation_id", "invoice_fingerprint", "account_code")},
                       "amount_minor": p["amount_minor"], "currency": p["currency"], "txn_code": codes.get(p["transfer_id"]) or p["transfer_id"],
                       "installment": p.get("installment"), "account_code": p.get("account_code")})
    for d in in_flight:
        events.append({"key": f"in_flight:{d.get('payout_key')}", "at": "9999", "type": "in_flight", "title": "Payment in flight",
                       "data": {k: d.get(k) for k in ("payout_key", "installment", "currency", "amount_minor")}, "amount_minor": d["amount_minor"], "currency": d["currency"]})
    rank = {k: i for i, k in enumerate(order or [])}
    head = events[:1]
    known = sorted((e for e in events[1:] if e["key"] in rank), key=lambda e: rank[e["key"]])
    new = sorted((e for e in events[1:] if e["key"] not in rank), key=lambda e: (e["type"] == "in_flight", str(e["at"]), e["type"]))
    prev, out = GENESIS, []
    for n, e in enumerate(head + known + new):
        h = _h(prev, {"n": n, "type": e["type"], "data": e["data"]})
        out.append({k: v for k, v in e.items() if k != "data"} | {"n": n, "prev_hash": prev, "hash": h, "at": None if e["at"] == "9999" else e["at"]})
        prev = h
    return out


def compare(blocks: list[dict], seal: dict | None) -> tuple[str, int | None]:
    """Intact when every sealed block is still there, in place, with the same hash."""
    keys, hashes = (seal or {}).get("keys") or [], (seal or {}).get("hashes") or []
    for i, (k, h) in enumerate(zip(keys, hashes, strict=False)):
        if i >= len(blocks) or blocks[i]["key"] != k:
            return "Altered", i  # a sealed block is gone
        if blocks[i]["hash"] != h:
            return ("Amended" if i == 0 else "Altered"), i
    return "Intact", None


def sealable(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b["type"] != "in_flight"]


def checks(po: dict, blocks: list[dict], *, paid: int, pending: int, payee_ok: bool | None, loe_used: float | None) -> dict:
    pays = [b for b in blocks if b["type"] == "payment"]
    invs = [b for b in blocks if b["type"] == "invoice"]
    out = {}
    out["value"] = "pass" if paid + pending <= po["total_minor"] + 1 else "fail"
    out["payee"] = "n/a" if payee_ok is None else "pass" if payee_ok else "fail"
    out["currency"] = "n/a" if not (pays or invs) else "pass" if all(b.get("currency") in (None, po["currency"]) for b in pays + invs) else "fail"
    start, end = po.get("start_date") or po.get("issued_date"), po.get("end_date") or po.get("expiry_date")
    days = [str(b["at"])[:10] for b in pays if b.get("at")]
    out["period"] = "n/a" if not days else "pass" if all((not start or d >= start) and (not end or d <= end) for d in days) else "fail"
    nos = [b["invoice_no"] for b in invs if b.get("invoice_no")]
    out["duplicate"] = "n/a" if not nos else "pass" if len(nos) == len(set(nos)) else "fail"
    terms = {t["term_id"]: t["amount_minor"] for t in po.get("terms") or []}
    by_term: dict[str, int] = {}
    for b in pays:
        if b.get("installment") in terms:
            by_term[b["installment"]] = by_term.get(b["installment"], 0) + b["amount_minor"]
    out["terms"] = "n/a" if not terms else "pass" if all(v <= terms[k] + 1 for k, v in by_term.items()) else "fail"
    codes = {b.get("account_code") for b in pays if b.get("account_code")}
    out["charge"] = "n/a" if not codes else "pass" if codes <= {po.get("account_code")} else "fail"
    loe = po.get("loe")
    out["loe"] = "n/a" if loe in (None, "") or loe_used is None else "pass" if loe_used <= float(loe) else "fail"
    return out


def build(*, org, store, intake_rows: list, payees: dict, to_base, seal: bool = True, by: str = "VENTHORA (contract chain)", audit=None) -> dict:
    ledger = store.po_payments()
    transfer_ids = [p["transfer_id"] for p in ledger]
    ops = store.operations_by_ref(transfer_ids) if hasattr(store, "operations_by_ref") else {}
    codes = {r: (o.get("response") or {}).get("short_reference_id") for r, o in ops.items()}
    from .paid_reports import _Tree

    tree = _Tree(org)
    units = tree.units
    rows = []
    for po in sorted(org.list("po"), key=lambda p: p["po_id"]):
        pid = po["po_id"]
        pays = [dict(p) for p in ledger if p["po_number"] == pid]
        invs = sorted([q for q in intake_rows if (q.po_number or "").upper() == pid.upper() and q.status not in ("REJECTED", "DUPLICATE")], key=lambda q: q.received_at)
        flying = store.po_in_flight(pid) if hasattr(store, "po_in_flight") else []
        prior = org.get("contract_seal", pid)
        blocks = blocks_of(po, payments=pays, invoices=invs, in_flight=flying, codes=codes, order=(prior or {}).get("keys"))
        state, at_block = compare(blocks, prior)
        done = sealable(blocks)
        if seal and state == "Intact" and len(done) > len((prior or {}).get("hashes") or []):
            rec = {"_id": pid, "keys": [b["key"] for b in done], "hashes": [b["hash"] for b in done], "head": done[-1]["hash"],
                   "sealed_at": datetime.now(UTC).isoformat(timespec="seconds")}
            org._put("contract_seal", rec, by)
            if audit is not None:
                audit.append("contract_chain_sealed", {"contract": pid, "blocks": len(blocks), "head": rec["head"]})
            prior = rec
        n_sealed = len((prior or {}).get("hashes") or [])
        for b in blocks:
            b["sealed"] = b["n"] < n_sealed and state == "Intact"
        paid = sum(p["amount_minor"] for p in pays if p["currency"] == po["currency"])
        paid_fp = {p.get("invoice_fingerprint") for p in pays}
        from .procurement import PO_FAILS

        # an invoice the PO match already refused (e.g. above what is left) holds nothing: it is a blocked block, not pending
        pending = sum(q.amount_minor or 0 for q in invs if q.status in ("VERIFIED", "NEEDS_REVIEW") and q.invoice_fingerprint not in paid_fp
                      and q.currency == po["currency"] and not (PO_FAILS & set(getattr(q, "flags", []) or [])))
        blocked = [q.invoice_no or q.intake_id[:8] for q in invs if PO_FAILS & set(getattr(q, "flags", []) or [])]
        pending += sum(d["amount_minor"] for d in flying if d["currency"] == po["currency"])
        payee_ok = None if not invs else all((q.counterparty_id or po["supplier_id"]) == po["supplier_id"] for q in invs)
        ch = {"chain": "pass" if state == "Intact" else "fail"} | checks(po, blocks, paid=paid, pending=pending, payee_ok=payee_ok, loe_used=None)
        fails = [k for k, v in ch.items() if v == "fail"]
        code = po.get("account_code") or ""
        place = tree.of_code(code)
        unit, office = units.get(place["unit_id"] or ""), units.get(place["country_office_id"] or "")
        end = po.get("end_date") or po.get("expiry_date")
        today = datetime.now(UTC).date().isoformat()
        rows.append({"contract": pid, "type": kind_of(po), "description": po.get("description") or po.get("title"), "status": po.get("status"),
                     "supplier_id": po["supplier_id"], "supplier": (payees.get(po["supplier_id"]) or {}).get("name") or po["supplier_id"],
                     "currency": po["currency"], "value_minor": po["total_minor"], "paid_minor": paid, "pending_minor": pending,
                     "left_minor": po["total_minor"] - paid - pending, "value_base_minor": to_base(po["total_minor"], po["currency"]),
                     "paid_base_minor": to_base(paid, po["currency"]), "start_date": po.get("start_date") or po.get("issued_date"), "end_date": end,
                     "validity": "Expired" if end and end < today else "Active" if po.get("status") == "OPEN" else (po.get("status") or "").title(),
                     "charge_code": code, "unit": (unit or {}).get("name"), "country_office": (office or {}).get("name"), "loe": po.get("loe"),
                     "terms": po.get("terms") or [], "blocks": blocks, "head": blocks[-1]["hash"], "sealed_at": (prior or {}).get("sealed_at"),
                     "sealed_blocks": len((prior or {}).get("hashes") or []), "chain_state": state, "broken_at": at_block,
                     "checks": ch, "failed": fails, "verdict": "Altered" if state in ("Altered", "Amended") else "Attention" if fails else "Intact",
                     "payments": len(pays), "invoices": len(invs), "blocked_invoices": blocked})
    return {"rows": rows, "checks": CHECKS}
