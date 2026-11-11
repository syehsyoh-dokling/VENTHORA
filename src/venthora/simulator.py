# ruff: noqa: E501
"""Demo-Only -> Invoice simulation: many supplier emails into the live company, the way they really arrive, so the
agent plan can be seen changing and re-planning batch after batch.

136 emails by default, 8 kinds x 17, from the vendors registered at that moment and against their open purchase
orders: clean invoices due inside the plan's horizon, invoices due later (they move the 3-month forecast), final
notices for overdue invoices (priority goes up), duplicates of an invoice already sent, invoices above what is left on
the order, invoices without an order, a stranger claiming to be a vendor, and a vendor announcing new bank details.
Every email goes through the same intake and checks as a real one; a simulation never pays on arrival and never
changes a real payee's bank status. Each email says SIMULATION in its subject so it is never mistaken for a real bill.
"""
from __future__ import annotations

import datetime as dt
import random
import re
import secrets
from email.message import EmailMessage
from email.utils import format_datetime

KINDS = {
    "clean": "Clean invoice, inside the order, due in the next two days",
    "due_later": "Clean invoice due in 10 to 60 days (moves the 3-month forecast)",
    "final_notice": "Final notice for an overdue invoice (priority goes up)",
    "duplicate": "The same invoice sent a second time",
    "above_po": "Amount above what is left on the purchase order",
    "no_po": "Invoice that quotes no purchase order",
    "stranger": "A stranger's address claiming to be the vendor",
    "bank_change": "Vendor announces new bank details in the invoice",
}
DECIMALS0 = {"IDR", "JPY", "KRW", "VND"}


def _money(minor: int, ccy: str) -> str:
    return f"{ccy} {minor / 100:,.2f}"


def vendors(registry, org) -> list[dict]:
    """Registered vendors with an email address on file, each with its open purchase orders."""
    pos = [p for p in org.list("po") if str(p.get("status", "OPEN")).upper() == "OPEN"]
    out = []
    for vid, rec in registry.payees.items():
        if rec.get("kind") == "employee" or not rec.get("contacts"):
            continue
        mine = [p for p in pos if p.get("supplier_id") == vid]
        out.append({"id": vid, "name": rec["name"], "email": rec["contacts"][0], "currency": rec.get("currency") or "USD",
                    "prefix": rec.get("invoice_prefix") or vid, "pos": mine})
    return [v for v in out if v["pos"]] or out


def build(vendor_list: list[dict], count: int, run: str, today: dt.date, seed: int | None = None) -> list[dict]:
    """The emails, mixed so every batch has a bit of everything: [{n, kind, vendor, invoice_no, amount, ccy, eml}]."""
    if not vendor_list:
        raise ValueError("no registered vendor with an email address: add vendors first (Vendors, or an ERP sync)")
    rnd = random.Random(seed if seed is not None else int(run, 16))
    kinds = list(KINDS)
    order = [kinds[i % len(kinds)] for i in range(count)]
    rnd.shuffle(order)
    sent: list[dict] = []
    out = []
    for n, kind in enumerate(order, 1):
        v = vendor_list[(n - 1) % len(vendor_list)]
        po = v["pos"][(n - 1) % len(v["pos"])] if v["pos"] else None
        ccy = (po or {}).get("currency") or v["currency"]
        total = int((po or {}).get("total_minor") or 100_000)
        amount = max(int(total * rnd.uniform(0.004, 0.02)), 1_000)
        if ccy in DECIMALS0:
            amount = amount // 100_000 * 100_000 or 100_000
        due = today + dt.timedelta(days=rnd.randint(1, 2))  # inside the 72-hour plan
        code = "".join(w[0] for w in re.findall(r"[A-Za-z]+", v["name"]))[:4].upper() or "INV"
        inv = f"{code}-SIM{n:04d}-{run[:4].upper()}"  # read by the invoice-number rule: letters, then letters+digits
        sender, name = v["email"], v["name"]
        lines = []
        po_ref = f"against purchase order {po['po_id']}" if po else ""
        subject = f"[SIMULATION] Invoice {inv}"
        if kind == "due_later":
            due = today + dt.timedelta(days=rnd.randint(10, 60))
        elif kind == "final_notice":
            due = today - dt.timedelta(days=rnd.randint(3, 12))
            subject = f"[SIMULATION] FINAL NOTICE: Invoice {inv} is overdue"
            lines.append("FINAL NOTICE: this invoice is overdue. Please pay immediately to avoid late interest and legal action.")
        elif kind == "duplicate" and sent:
            src = rnd.choice(sent)
            inv, amount, ccy, due, sender, name, po_ref = src["invoice_no"], src["amount"], src["ccy"], src["due"], src["sender"], src["vendor"], src["po_ref"]
            subject = f"[SIMULATION] Invoice {inv} (copy)"
        elif kind == "above_po":
            amount = int(total * rnd.uniform(1.3, 1.8))
        elif kind == "no_po":
            po_ref = ""
        elif kind == "stranger":
            sender = f"billing@{v['name'].split()[0].lower()}-payments.example"
            lines.append("We have moved our billing to this new address.")
        elif kind == "bank_change":
            lines.append("Please note our bank details have changed. Kindly remit to our new bank account number 0098765432 at the same bank from now on.")
        body = (f"Dear Accounts Payable,\n\nThis is a SIMULATED invoice for a VENTHORA demo run {run}.\n"
                f"Invoice {inv} {po_ref}.\nAmount due: {_money(amount, ccy)}, due {due.isoformat()}.\n" + "\n".join(lines) + f"\n\nRegards,\n{name}\n")
        msg = EmailMessage()
        msg["From"] = f"{name} <{sender}>"
        msg["To"] = "ap@venthora.local"
        msg["Subject"] = subject
        msg["Date"] = format_datetime(dt.datetime.now(dt.UTC))
        msg["Message-ID"] = f"<sim-{run}-{n:03d}-{secrets.token_hex(4)}@venthora.local>"
        msg["Authentication-Results"] = "venthora.local; spf=pass; dkim=pass; dmarc=pass" if kind != "stranger" else "venthora.local; spf=none; dkim=none"
        msg.set_content(body)
        rec = {"n": n, "kind": kind, "vendor": name, "sender": sender, "invoice_no": inv, "amount": amount, "ccy": ccy, "due": due, "po_ref": po_ref,
               "eml": msg.as_bytes()}
        if kind in ("clean", "due_later"):
            sent.append(rec)
        out.append(rec)
    return out
