# ruff: noqa: E501
"""Reporting -> FX & transfer costs, per line: the Airwallex transaction code and value date, the vendor and the payout
rail from the transfer Airwallex returned, and a conversion tied to the payment it funded."""
import json
import types

from venthora import fx_cost_report


class FakeStore:
    def __init__(self, ops, transfers, ledger):
        self.ops, self.transfers, self.ledger = ops, transfers, ledger

    def operations_by_ref(self, refs):
        return {r: self.ops[r] for r in refs if r in self.ops}

    def transfers_in(self, ids):
        return [t for t in self.transfers if t["execution_id"] in set(ids)]

    def paid_rows(self):
        return self.ledger


class FakeOrg:
    def list(self, kind):
        return [{"unit_id": "uk", "name": "UK Country Office", "kind": "COUNTRY_OFFICE", "parent_id": None}] if kind == "unit" else []


def test_lines_carry_the_transaction_code_vendor_rail_and_what_a_conversion_funded():
    transfer = {"short_reference_id": "P261029-340D1X7", "status": "PAID", "transfer_date": "2026-10-29", "transfer_method": "LOCAL",
                "transfer_currency": "GBP", "transfer_amount": 450.0, "source_currency": "GBP", "source_amount": 450.0, "fee_paid_by": "PAYER",
                "beneficiary": {"bank_details": {"account_name": "Thames Freight Ltd", "local_clearing_system": "FASTER_PAYMENTS", "bank_country_code": "GB", "account_number": "123"}}}
    ops = {"T1": {"op_key": "E1:transfer:REQ-TF-1", "execution_id": "E1", "kind": "transfer", "response": transfer},
           "C1": {"op_key": "E1:conversion:GBP", "execution_id": "E1", "kind": "conversion", "response": {"conversion": {"status": "SETTLED"}}}}
    store = FakeStore(ops, [{"op_key": "E1:transfer:REQ-TF-1", "execution_id": "E1", "airwallex_id": "T1", "response": transfer}],
                      [{"transfer_id": "T1", "base_cost_minor": 60570, "paid_at": "2026-10-29T14:01:30+00:00", "line": "logistics", "invoice_fingerprint": "fp1"}])
    req = types.SimpleNamespace(invoice_fingerprint="fp1", counterparty_name="Thames Freight Ltd", invoice_no="TF-1", po_number="PO-1", account_code=None, unit_id="uk")
    rep = {"base_currency": "USD", "rows": [
        {"ref": "T1", "kind": "payout_fee", "at": "2026-10-29T14:01:25+00:00", "currency": "GBP", "sold_currency": None, "obligation_id": "REQ-TF-1", "spread_base_minor": None},
        {"ref": "C1", "kind": "conversion", "at": "2026-10-29T14:01:22+00:00", "currency": "GBP", "sold_currency": "USD", "obligation_id": None, "spread_base_minor": None}]}
    out = fx_cost_report.lines(rep, store, intake_rows=[req], org=FakeOrg())
    fee, conv = out["rows"]
    assert fee["txn_code"] == "P261029-340D1X7" and fee["value_date"] == "2026-10-29" and fee["rail"] == "LOCAL · FASTER_PAYMENTS"
    assert fee["payee"] == "Thames Freight Ltd" and fee["invoice_no"] == "TF-1" and fee["budget_line"] == "logistics" and fee["country_office"] == "UK Country Office"
    assert fee["paid_base_minor"] == 60570 and fee["spread_status"] == "n/a" and "123" not in json.dumps(fee)
    assert conv["obligation_id"] == "REQ-TF-1" and conv["funds"][0]["txn_code"] == "P261029-340D1X7" and conv["pair"] == "USD→GBP"
    assert conv["spread_status"] == "not measured" and conv["paid_base_minor"] is None and conv["payee"] == "Thames Freight Ltd"
