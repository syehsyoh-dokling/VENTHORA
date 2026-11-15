# ruff: noqa: E501
"""ISO 20022: a payment run out as pain.001, a bank statement in as camt.053, and the two reconciled by EndToEndId."""
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest

from venthora import iso20022 as iso

NS = {"p": iso.PAIN_NS}
PAYMENTS = [
    {"end_to_end_id": "REQ-SLS-88412", "amount_minor": 6_600_000, "currency": "EUR", "execution_date": "2026-11-15",
     "creditor_name": "Straits Line Shipping", "creditor_country": "DE", "remittance": "SLS-88412",
     "bank": {"iban": "DE89 3704 0044 0532 0130 00", "swift_code": "deutdeff", "bank_name": "Deutsche Bank"}},
    {"end_to_end_id": "REQ-MER-2026-118", "amount_minor": 2_400_000, "currency": "EUR", "execution_date": "2026-11-15",
     "creditor_name": "Meridian Advisory GmbH", "creditor_country": "DE", "bank": {"iban": "DE89370400440532013000", "swift_code": "DEUTDEFF"}},
    {"end_to_end_id": "REQ-HCL-INST-11", "amount_minor": 3_000_000, "currency": "USD", "execution_date": "2026-11-15",
     "creditor_name": "Harbour Capital Lending", "creditor_country": "US",
     "bank": {"account_number": "1234567890", "routing_value": "026009593", "bank_name": "JPMorgan Chase"}},
    {"end_to_end_id": "REQ-SKT-7731", "amount_minor": 180_000_000, "currency": "JPY", "execution_date": "2026-11-16",
     "creditor_name": "Sakura Travel KK <Tokyo>", "creditor_country": "JP", "bank": {"account_number": "1234567", "swift_code": "BOTKJPJT"}},
]


def _doc():
    xml = iso.pain001(PAYMENTS, debtor_name="Nusantara Trading Pte Ltd", debtor_account={"account_number": "8881234", "swift_code": "DBSSSGSG"},
                      message_id="VENTHORA-2026-11-15", created=datetime(2026, 11, 15, 4, 0, tzinfo=UTC))
    return xml, ET.fromstring(xml)


def test_amounts_are_iso_decimals():
    assert iso.amount(6_600_000, "EUR") == "66000.00" and iso.amount(5, "USD") == "0.05"
    assert iso.amount(180_000_000, "JPY") == "1800000"  # no minor unit in ISO 4217
    assert iso.minor("66000.00") == 6_600_000 and iso.minor("1800000") == 180_000_000 and iso.minor("0.5") == 50
    with pytest.raises(ValueError):
        iso.amount(150, "JPY")  # 1.50 yen cannot be instructed
    with pytest.raises(ValueError):
        iso.amount(-1, "EUR")


def test_the_payment_run_is_a_credit_transfer_initiation():
    xml, root = _doc()
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>') and root.tag == f"{{{iso.PAIN_NS}}}Document"
    head = root.find("p:CstmrCdtTrfInitn/p:GrpHdr", NS)
    assert head.findtext("p:MsgId", namespaces=NS) == "VENTHORA-2026-11-15" and head.findtext("p:NbOfTxs", namespaces=NS) == "4"
    assert head.findtext("p:CreDtTm", namespaces=NS) == "2026-11-15T04:00:00+00:00"
    assert head.findtext("p:InitgPty/p:Nm", namespaces=NS) == "Nusantara Trading Pte Ltd"
    blocks = root.findall("p:CstmrCdtTrfInitn/p:PmtInf", NS)
    # one block per currency and execution date: EUR 15 Nov (two payments), USD 15 Nov, JPY 16 Nov
    assert [(b.find("p:CdtTrfTxInf/p:Amt/p:InstdAmt", NS).get("Ccy"), b.findtext("p:ReqdExctnDt/p:Dt", namespaces=NS),
             b.findtext("p:NbOfTxs", namespaces=NS)) for b in blocks] == [("EUR", "2026-11-15", "2"), ("JPY", "2026-11-16", "1"), ("USD", "2026-11-15", "1")]
    assert blocks[0].findtext("p:CtrlSum", namespaces=NS) == "90000.00" and blocks[0].findtext("p:PmtMtd", namespaces=NS) == "TRF"
    assert sum(int(b.findtext("p:NbOfTxs", namespaces=NS)) for b in blocks) == 4


def test_each_payment_carries_what_a_bank_needs():
    _, root = _doc()
    txs = {t.findtext("p:PmtId/p:EndToEndId", namespaces=NS): t for t in root.iter(f"{{{iso.PAIN_NS}}}CdtTrfTxInf")}
    assert set(txs) == {"REQ-SLS-88412", "REQ-MER-2026-118", "REQ-HCL-INST-11", "REQ-SKT-7731"}  # the obligation follows the payment
    eu = txs["REQ-SLS-88412"]
    assert eu.findtext("p:Amt/p:InstdAmt", namespaces=NS) == "66000.00"
    assert eu.findtext("p:CdtrAcct/p:Id/p:IBAN", namespaces=NS) == "DE89370400440532013000"  # spaces removed
    assert eu.findtext("p:CdtrAgt/p:FinInstnId/p:BICFI", namespaces=NS) == "DEUTDEFF" and eu.findtext("p:RmtInf/p:Ustrd", namespaces=NS) == "SLS-88412"
    us = txs["REQ-HCL-INST-11"]
    assert us.findtext("p:CdtrAcct/p:Id/p:Othr/p:Id", namespaces=NS) == "1234567890"
    assert us.findtext("p:CdtrAgt/p:FinInstnId/p:ClrSysMmbId/p:MmbId", namespaces=NS) == "026009593"
    jp = txs["REQ-SKT-7731"]
    assert jp.findtext("p:Amt/p:InstdAmt", namespaces=NS) == "1800000" and jp.findtext("p:Cdtr/p:PstlAdr/p:Ctry", namespaces=NS) == "JP"
    assert jp.findtext("p:Cdtr/p:Nm", namespaces=NS) == "Sakura Travel KK  Tokyo "  # characters a bank refuses are replaced
    with pytest.raises(ValueError):
        iso.pain001([], debtor_name="x", message_id="m")


STATEMENT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt>
 <Stmt><Id>STMT-1</Id><Acct><Id><IBAN>SG00DBSS0000008881234</IBAN></Id></Acct>
  <Ntry><Amt Ccy="EUR">66000.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts><BookgDt><Dt>2026-11-15</Dt></BookgDt><AcctSvcrRef>BANK-1</AcctSvcrRef>
   <NtryDtls><TxDtls><Refs><EndToEndId>REQ-SLS-88412</EndToEndId></Refs><RltdPties><Cdtr><Pty><Nm>Straits Line Shipping</Nm></Pty></Cdtr></RltdPties></TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="EUR">23990.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts><BookgDt><DtTm>2026-11-15T09:30:00Z</DtTm></BookgDt>
   <NtryDtls><TxDtls><Refs><EndToEndId>REQ-MER-2026-118</EndToEndId></Refs><RltdPties><Cdtr><Nm>Meridian Advisory GmbH</Nm></Cdtr></RltdPties></TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="USD">120.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-11-15</Dt></BookgDt>
   <NtryDtls><TxDtls><Refs><EndToEndId>BANK-FEE-77</EndToEndId></Refs></TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="USD">100000.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-11-15</Dt></BookgDt>
   <NtryDtls><TxDtls><Refs><EndToEndId>INV-ACME-9</EndToEndId></Refs><RltdPties><Dbtr><Nm>Acme Retail</Nm></Dbtr></RltdPties></TxDtls></NtryDtls></Ntry>
 </Stmt></BkToCstmrStmt></Document>"""


def test_a_statement_is_read_whatever_its_version():
    entries = iso.camt053(STATEMENT)
    assert [(e["direction"], e["currency"], e["amount_minor"], e["end_to_end_id"]) for e in entries] == [
        ("debit", "EUR", 6_600_000, "REQ-SLS-88412"), ("debit", "EUR", 2_399_000, "REQ-MER-2026-118"),
        ("debit", "USD", 12_000, "BANK-FEE-77"), ("credit", "USD", 10_000_000, "INV-ACME-9")]
    assert entries[0]["counterparty"] == "Straits Line Shipping" and entries[0]["booked"] == "2026-11-15" and entries[0]["reference"] == "BANK-1"
    assert entries[1]["counterparty"] == "Meridian Advisory GmbH" and entries[1]["booked"] == "2026-11-15" and entries[1]["status"] == "BOOK"
    assert entries[3]["counterparty"] == "Acme Retail" and entries[0]["account"] == "SG00DBSS0000008881234"
    with pytest.raises(ValueError):
        iso.camt053("<Document><Nothing/></Document>")
    with pytest.raises(ValueError):
        iso.camt053("not xml")


def test_the_statement_is_reconciled_with_what_venthora_paid():
    out = iso.reconcile(iso.camt053(STATEMENT), PAYMENTS)
    assert [m["end_to_end_id"] for m in out["matched"]] == ["REQ-SLS-88412"]
    assert out["different_amount"][0]["end_to_end_id"] == "REQ-MER-2026-118"  # the bank took EUR 23,990.00, VENTHORA sent EUR 24,000.00
    assert out["different_amount"][0]["statement"]["amount_minor"] == 2_399_000 and out["different_amount"][0]["venthora"]["amount_minor"] == 2_400_000
    assert [e["end_to_end_id"] for e in out["on_statement_only"]] == ["BANK-FEE-77"]  # a debit VENTHORA did not make
    assert {m["end_to_end_id"] for m in out["not_on_statement"]} == {"REQ-HCL-INST-11", "REQ-SKT-7731"} and out["clean"] is False
    paid_one = [p for p in PAYMENTS if p["end_to_end_id"] == "REQ-SLS-88412"]
    clean = iso.reconcile([e for e in iso.camt053(STATEMENT) if e["end_to_end_id"] == "REQ-SLS-88412"], paid_one)
    assert clean["clean"] is True and len(clean["matched"]) == 1


def test_the_endpoints_export_the_days_payments_and_reconcile_a_statement():
    from fastapi.testclient import TestClient

    from venthora.api import app

    with TestClient(app) as c:
        assert c.get("/v1/iso20022/pain.001?date=2026-11-15").status_code == 404  # nothing was paid that day
        assert c.get("/v1/iso20022/pain.001?date=15-11-2026").status_code == 422
        store = c.app.state.executor.store
        store.book_payment({"transfer_id": "tr-1", "book_id": "b", "obligation_id": "REQ-SLS-88412", "currency": "EUR", "amount_minor": 6_600_000,
                            "base_cost_minor": 7_325_788, "paid_at": "2026-11-15T09:00:00+00:00", "reporting_period": "2026-11",
                            "beneficiary_id": "no-such-payee"})
        r = c.get("/v1/iso20022/pain.001?date=2026-11-15")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml") and "pain.001-2026-11-15.xml" in r.headers["content-disposition"]
        root = ET.fromstring(r.text)
        assert root.findtext("p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf/p:PmtId/p:EndToEndId", namespaces=NS) == "REQ-SLS-88412"
        assert root.findtext("p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf/p:Amt/p:InstdAmt", namespaces=NS) == "66000.00"
        out = c.post("/v1/iso20022/camt.053", content=STATEMENT).json()
        assert out["entries"] == 4 and out["from"] == out["to"] == "2026-11-15"
        assert [m["end_to_end_id"] for m in out["reconciliation"]["matched"]] == ["REQ-SLS-88412"]
        assert len(out["reconciliation"]["on_statement_only"]) == 2 and out["reconciliation"]["not_on_statement"] == []
        assert c.post("/v1/iso20022/camt.053", content="<a/>").status_code == 422
        assert c.post("/v1/iso20022/camt.053", content="").status_code == 413


def test_what_is_written_can_be_read_back():
    """A pain.001 is not a statement, but its amounts and ids survive the same decimal rules both ways."""
    _, root = _doc()
    for tx in root.iter(f"{{{iso.PAIN_NS}}}CdtTrfTxInf"):
        amt = tx.find("p:Amt/p:InstdAmt", NS)
        src = next(p for p in PAYMENTS if p["end_to_end_id"] == tx.findtext("p:PmtId/p:EndToEndId", namespaces=NS))
        assert iso.minor(amt.text) == src["amount_minor"] and amt.get("Ccy") == src["currency"]
