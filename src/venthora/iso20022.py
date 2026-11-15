# ruff: noqa: E501
"""ISO 20022 for a bank-connected deployment: payments out as pain.001, the bank's statement in as camt.053.

VENTHORA pays through the Airwallex REST API, and Airwallex speaks to the rails. A treasury that also instructs a bank
directly, or that must hand its payment file and reconciliation to an auditor, works in ISO 20022. Two pure functions:

  pain001   a list of payments -> a Customer Credit Transfer Initiation (pain.001.001.09): one payment-information
            block per currency and execution date, one credit transfer per payment, the obligation id as the
            EndToEndId so the payment can be followed through every bank.
  camt053   a Bank-to-Customer Statement (camt.053, any version) -> its entries: date, amount, currency, credit or
            debit, EndToEndId, counterparty.
  reconcile statement entries against VENTHORA's payments by EndToEndId, amount and currency: matched, a different
            amount, on the statement only, and paid but not yet on the statement.

Amounts are VENTHORA's integer minor units (hundredths in every currency) written as ISO decimals: two places, none
for the currencies ISO 4217 gives no minor unit. The files are built and read with the standard library; `validate`
checks a document against the official XSD shipped in schemas/ (pain.001.001.09, camt.053.001.08, as published by
iso20022.org), and the pain.001 endpoint refuses to hand out a file that fails it. No bank has received one yet.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

PAIN_NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
from .iso4217 import ZERO as ZERO_DECIMAL  # noqa: E402, F401 - the one table of decimals (iso4217.py); read by execution/facts.py
from .iso4217 import decimal as _decimal  # noqa: E402

_SAFE = re.compile(r"[^A-Za-z0-9/\-?:().,'+ ]")  # the character set every bank accepts in identifiers and names


def amount(minor: int, currency: str) -> str:
    """VENTHORA's minor units as an ISO 20022 decimal amount."""
    return _decimal(minor, currency)


def minor(text: str) -> int:
    """An ISO decimal amount as VENTHORA's minor units."""
    whole, _, frac = text.strip().partition(".")
    return int(whole or "0") * 100 + int((frac + "00")[:2])


def _id(text: str, limit: int = 35) -> str:
    return _SAFE.sub("-", str(text or "").strip())[:limit] or "NOTPROVIDED"


def _name(text: str, limit: int = 140) -> str:
    return _SAFE.sub(" ", str(text or "").strip())[:limit] or "NOTPROVIDED"


def _el(parent, tag: str, text: str | None = None, **attrs) -> ET.Element:
    e = ET.SubElement(parent, tag, attrs)
    if text is not None:
        e.text = text
    return e


def _account(parent, tag: str, bank: dict) -> None:
    acct = _el(_el(parent, tag), "Id")
    if bank.get("iban"):
        _el(acct, "IBAN", re.sub(r"\s", "", bank["iban"]).upper())
    else:
        _el(_el(acct, "Othr"), "Id", _id(bank.get("account_number"), 34))


def _agent(parent, tag: str, bank: dict) -> None:
    fin = _el(_el(parent, tag), "FinInstnId")
    if bank.get("swift_code"):
        _el(fin, "BICFI", str(bank["swift_code"]).upper())
    if bank.get("routing_value"):
        _el(_el(fin, "ClrSysMmbId"), "MmbId", _id(bank["routing_value"]))
    if bank.get("bank_name"):
        _el(fin, "Nm", _name(bank["bank_name"]))
    if not len(fin):
        _el(_el(fin, "Othr"), "Id", "NOTPROVIDED")


def pain001(payments: list[dict], *, debtor_name: str, debtor_account: dict | None = None, message_id: str,
            created: datetime | None = None) -> str:
    """payments: {end_to_end_id, amount_minor, currency, execution_date (YYYY-MM-DD), creditor_name, creditor_country,
    bank: {iban | account_number, swift_code, routing_value, bank_name}, remittance}. Returns the XML document."""
    if not payments:
        raise ValueError("no payment to instruct")
    created = created or datetime.now(UTC)
    root = ET.Element("Document", {"xmlns": PAIN_NS})
    init = _el(root, "CstmrCdtTrfInitn")
    head = _el(init, "GrpHdr")
    _el(head, "MsgId", _id(message_id))
    _el(head, "CreDtTm", created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    _el(head, "NbOfTxs", str(len(payments)))
    _el(head, "CtrlSum", _control_sum(payments))
    _el(_el(head, "InitgPty"), "Nm", _name(debtor_name))
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in payments:
        groups.setdefault((p["currency"], p["execution_date"]), []).append(p)
    for n, ((ccy, day), rows) in enumerate(sorted(groups.items()), start=1):
        info = _el(init, "PmtInf")
        _el(info, "PmtInfId", _id(f"{message_id}-{n}"))
        _el(info, "PmtMtd", "TRF")
        _el(info, "NbOfTxs", str(len(rows)))
        _el(info, "CtrlSum", _control_sum(rows))
        _el(_el(info, "ReqdExctnDt"), "Dt", day)
        _el(_el(info, "Dbtr"), "Nm", _name(debtor_name))
        _account(info, "DbtrAcct", debtor_account or {})
        _agent(info, "DbtrAgt", debtor_account or {})
        for p in rows:
            tx = _el(info, "CdtTrfTxInf")
            _el(_el(tx, "PmtId"), "EndToEndId", _id(p["end_to_end_id"]))
            _el(_el(tx, "Amt"), "InstdAmt", amount(p["amount_minor"], ccy), Ccy=ccy)
            bank = p.get("bank") or {}
            _agent(tx, "CdtrAgt", bank)
            cdtr = _el(tx, "Cdtr")
            _el(cdtr, "Nm", _name(p.get("creditor_name")))
            if p.get("creditor_country"):
                _el(_el(cdtr, "PstlAdr"), "Ctry", str(p["creditor_country"]).upper()[:2])
            _account(tx, "CdtrAcct", bank)
            if p.get("remittance"):
                _el(_el(tx, "RmtInf"), "Ustrd", _name(p["remittance"]))
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


SCHEMAS = Path(__file__).with_name("schemas")  # the official XSDs, as published by iso20022.org
_SCHEMA_CACHE: dict[str, object] = {}


def validate(xml: str | bytes, message: str) -> list[str]:
    """The schema errors of an ISO 20022 document against the official XSD of `message` (e.g. "pain.001.001.09");
    an empty list when it is valid. Raises LookupError when the schema of that message is not shipped."""
    from lxml import etree

    path = SCHEMAS / f"{message}.xsd"
    if not path.is_file():
        raise LookupError(f"no schema shipped for {message}")
    if message not in _SCHEMA_CACHE:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        _SCHEMA_CACHE[message] = etree.XMLSchema(etree.parse(str(path), parser))
    schema = _SCHEMA_CACHE[message]
    doc = etree.fromstring(xml.encode() if isinstance(xml, str) else xml, etree.XMLParser(resolve_entities=False, no_network=True))
    return [] if schema.validate(doc) else [f"line {e.line}: {e.message}" for e in schema.error_log][:20]


def message_of(xml: str | bytes) -> str | None:
    """The ISO 20022 message a document declares in its namespace (urn:iso:std:iso:20022:tech:xsd:camt.053.001.08)."""
    m = re.search(rb"urn:iso:std:iso:20022:tech:xsd:([a-z]{4}\.\d{3}\.\d{3}\.\d{2})", xml.encode() if isinstance(xml, str) else xml)
    return m.group(1).decode() if m else None


def _control_sum(rows: list[dict]) -> str:
    """The sum of the amounts as written, whatever their currencies (the standard's own definition of CtrlSum)."""
    total = sum(r["amount_minor"] for r in rows)
    return f"{total // 100}.{total % 100:02d}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node, *path: str):
    for name in path:
        node = next((c for c in (node if node is not None else []) if _local(c.tag) == name), None)
        if node is None:
            return None
    return node


def _text(node, *path: str) -> str:
    found = _find(node, *path)
    return (found.text or "").strip() if found is not None else ""


def camt053(xml: str | bytes) -> list[dict]:
    """Every entry of every statement in a camt.053 document, whatever its version."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"not an XML document: {exc}") from exc
    statements = [n for n in root.iter() if _local(n.tag) == "Stmt"]
    if not statements:
        raise ValueError("no statement (Stmt) in this document: it is not a camt.053")
    out = []
    for stmt in statements:
        account = _text(stmt, "Acct", "Id", "IBAN") or _text(stmt, "Acct", "Id", "Othr", "Id")
        for entry in (n for n in stmt if _local(n.tag) == "Ntry"):
            amt = _find(entry, "Amt")
            if amt is None or not (amt.text or "").strip():
                continue
            credit = _text(entry, "CdtDbtInd") == "CRDT"
            details = [n for n in entry.iter() if _local(n.tag) == "TxDtls"] or [entry]
            for tx in details:
                tx_amt = _find(tx, "Amt") if tx is not entry else None
                party = "Dbtr" if credit else "Cdtr"
                out.append({"account": account, "booked": _text(entry, "BookgDt", "Dt") or _text(entry, "BookgDt", "DtTm")[:10],
                            "amount_minor": minor((tx_amt if tx_amt is not None else amt).text),
                            "currency": (tx_amt if tx_amt is not None else amt).get("Ccy", ""),
                            "direction": "credit" if credit else "debit", "status": _text(entry, "Sts", "Cd") or _text(entry, "Sts"),
                            "end_to_end_id": _text(tx, "Refs", "EndToEndId"),
                            "counterparty": _text(tx, "RltdPties", party, "Nm") or _text(tx, "RltdPties", party, "Pty", "Nm"),
                            "reference": _text(entry, "AcctSvcrRef") or _text(tx, "Refs", "AcctSvcrRef")})
    return out


def reconcile(entries: list[dict], payments: list[dict]) -> dict:
    """Debit entries against VENTHORA's payments ({end_to_end_id, amount_minor, currency}), by EndToEndId."""
    paid = {_id(p["end_to_end_id"]): p for p in payments}
    seen: set[str] = set()
    matched, different, unknown = [], [], []
    for e in entries:
        if e["direction"] != "debit":
            continue
        key = e["end_to_end_id"]
        p = paid.get(key)
        if p is None:
            unknown.append(e)
            continue
        seen.add(key)
        if (e["amount_minor"], e["currency"]) == (p["amount_minor"], p["currency"]):
            matched.append({"end_to_end_id": key, "amount_minor": e["amount_minor"], "currency": e["currency"], "booked": e["booked"]})
        else:
            different.append({"end_to_end_id": key, "statement": {"amount_minor": e["amount_minor"], "currency": e["currency"]},
                              "venthora": {"amount_minor": p["amount_minor"], "currency": p["currency"]}})
    missing = [{"end_to_end_id": k, "amount_minor": p["amount_minor"], "currency": p["currency"]} for k, p in paid.items() if k not in seen]
    return {"matched": matched, "different_amount": different, "on_statement_only": unknown, "not_on_statement": missing,
            "clean": not different and not unknown and not missing}
