# ruff: noqa: E501
"""Reporting -> Contract blockchain: each PO / consultant contract is a SHA-256 chain of its terms, invoices and
payments; it is sealed on read, a changed past block or changed terms are caught, and the controller checks run."""
import copy
import types

from venthora import contract_chain


class Org:
    def __init__(self, pos):
        self.recs = {"po": {p["po_id"]: p for p in pos}, "unit": {}, "contract_seal": {}}

    def list(self, kind):
        return list(self.recs.get(kind, {}).values())

    def get(self, kind, rid):
        return copy.deepcopy(self.recs.get(kind, {}).get(rid))

    def _put(self, kind, rec, by):
        self.recs.setdefault(kind, {})[rec["_id"]] = copy.deepcopy(rec)


class Store:
    def __init__(self, ledger):
        self.ledger = ledger

    def po_payments(self, po_number=None):
        return [dict(r) for r in self.ledger if po_number in (None, r["po_number"])]

    def po_in_flight(self, po):
        return []

    def operations_by_ref(self, refs):
        return {r: {"response": {"short_reference_id": f"P-{r}"}} for r in refs}


class Audit:
    def __init__(self):
        self.events = []

    def append(self, kind, payload):
        self.events.append((kind, payload))


def inv(no, amount, fp, sup="ben-a", at="2026-10-05T09:00:00+00:00"):
    return types.SimpleNamespace(po_number="PO-1", status="VERIFIED", intake_id=f"id-{no}", invoice_no=no, counterparty_id=sup, currency="USD",
                                 amount_minor=amount, installment="T1", invoice_fingerprint=fp, received_at=at)


PO = {"po_id": "PO-1", "supplier_id": "ben-a", "currency": "USD", "total_minor": 100000, "status": "OPEN", "terms": [{"term_id": "T1", "label": "30%", "amount_minor": 30000}],
      "account_code": "US-200.OPS.6100", "start_date": "2026-10-01", "expiry_date": "2026-12-31", "issued_date": "2026-10-01", "loe": None, "description": "Freight"}
PAY = {"transfer_id": "t1", "po_number": "PO-1", "installment": "T1", "currency": "USD", "amount_minor": 30000, "base_cost_minor": 30000,
       "paid_at": "2026-10-06T10:00:00+00:00", "obligation_id": "REQ-1", "invoice_fingerprint": "fp1", "account_code": "US-200.OPS.6100"}


def run(org, store, reqs, audit=None):
    return contract_chain.build(org=org, store=store, intake_rows=reqs, payees={"ben-a": {"name": "Acme"}}, to_base=lambda m, c: m, audit=audit)["rows"][0]


def test_a_contract_chains_its_terms_invoice_and_payment_and_seals_them():
    org, audit = Org([PO]), Audit()
    r = run(org, Store([PAY]), [inv("INV-1", 30000, "fp1")], audit)
    assert [b["type"] for b in r["blocks"]] == ["contract", "invoice", "payment"]
    assert r["blocks"][0]["prev_hash"] == "0" * 64 and r["blocks"][1]["prev_hash"] == r["blocks"][0]["hash"] and r["blocks"][2]["prev_hash"] == r["blocks"][1]["hash"]
    assert r["blocks"][2]["txn_code"] == "P-t1" and r["verdict"] == "Intact" and not r["failed"] and r["sealed_blocks"] == 3
    assert audit.events[0][0] == "contract_chain_sealed" and audit.events[0][1]["head"] == r["head"]
    assert r["type"] == "Purchase order" and r["paid_minor"] == 30000 and r["left_minor"] == 70000 and r["supplier"] == "Acme"
    again = run(org, Store([PAY]), [inv("INV-1", 30000, "fp1")], audit)
    assert again["head"] == r["head"] and again["verdict"] == "Intact" and len(audit.events) == 1  # nothing new, no new seal


def test_a_changed_payment_or_changed_terms_break_the_seal():
    org = Org([PO])
    run(org, Store([PAY]), [inv("INV-1", 30000, "fp1")])
    changed = run(org, Store([PAY | {"amount_minor": 29000}]), [inv("INV-1", 30000, "fp1")])
    assert changed["chain_state"] == "Altered" and changed["broken_at"] == 2 and changed["verdict"] == "Altered" and changed["checks"]["chain"] == "fail"
    org.recs["po"]["PO-1"] = PO | {"total_minor": 200000}
    amended = run(org, Store([PAY]), [inv("INV-1", 30000, "fp1")])
    assert amended["chain_state"] == "Amended" and amended["broken_at"] == 0


def test_a_late_invoice_with_an_older_date_is_added_after_the_sealed_blocks():
    org = Org([PO])
    run(org, Store([PAY]), [inv("INV-1", 30000, "fp1")])
    later = run(org, Store([PAY]), [inv("INV-1", 30000, "fp1"), inv("INV-0", 1000, "fp0", at="2026-10-02T09:00:00+00:00")])
    assert later["chain_state"] == "Intact" and later["blocks"][-1]["invoice_no"] == "INV-0"


def test_the_checks_catch_overrun_other_payee_outside_period_duplicate_and_overpaid_term():
    po = PO | {"total_minor": 40000}
    pays = [PAY, PAY | {"transfer_id": "t2", "amount_minor": 20000, "paid_at": "2027-01-10T10:00:00+00:00", "invoice_fingerprint": "fp2", "account_code": "US-200.OPS.6900"}]
    r = run(Org([po]), Store(pays), [inv("INV-1", 30000, "fp1"), inv("INV-1", 20000, "fp2", sup="ben-b")])
    assert {k for k, v in r["checks"].items() if v == "fail"} == {"value", "payee", "period", "duplicate", "terms", "charge"}
    assert r["verdict"] == "Attention" and r["checks"]["loe"] == "n/a" and r["validity"] in ("Active", "Expired")
    cc = run(Org([PO | {"po_id": "PO-1", "loe": 20.0}]), Store([]), [])
    assert cc["type"] == "Consultant contract" and cc["blocks"][0]["type"] == "contract" and cc["payments"] == 0
