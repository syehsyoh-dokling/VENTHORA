# ruff: noqa: E501
"""VENTHORA's pain.001 against the official ISO 20022 schema (pain.001.001.09.xsd, as published by iso20022.org)."""
from datetime import UTC, datetime

import pytest
from test_iso20022 import PAYMENTS

from venthora import iso20022 as iso

pytest.importorskip("lxml")


def test_the_payment_run_validates_against_the_official_schema():
    xml = iso.pain001(PAYMENTS, debtor_name="Nusantara Trading Pte Ltd", debtor_account={"account_number": "8881234", "swift_code": "DBSSSGSG"},
                      message_id="VENTHORA-2026-11-15", created=datetime(2026, 11, 15, 4, 0, tzinfo=UTC))
    assert iso.message_of(xml) == "pain.001.001.09" and iso.validate(xml, "pain.001.001.09") == []


def test_a_document_that_breaks_the_schema_is_named():
    xml = iso.pain001(PAYMENTS[:1], debtor_name="X", message_id="M-1", created=datetime(2026, 11, 15, tzinfo=UTC))
    broken = xml.replace("<NbOfTxs>1</NbOfTxs>", "<NbOfTxs>one</NbOfTxs>", 1)
    errors = iso.validate(broken, "pain.001.001.09")
    assert errors and "NbOfTxs" in errors[0]
    with pytest.raises(LookupError):
        iso.validate(xml, "pain.002.001.10")


def test_a_statement_is_checked_against_the_schema_of_its_own_version():
    doc = ('<?xml version="1.0" encoding="UTF-8"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt>'
           '<GrpHdr><MsgId>S1</MsgId><CreDtTm>2026-11-15T23:00:00</CreDtTm></GrpHdr></BkToCstmrStmt></Document>')
    assert iso.message_of(doc) == "camt.053.001.08"
    assert iso.validate(doc, "camt.053.001.08")  # no statement in it: the schema says so
