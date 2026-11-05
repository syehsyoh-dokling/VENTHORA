# ruff: noqa: E501
"""Reporting -> Virtual card (Visa): VENTHORA's decisions joined with what Airwallex recorded, the rule named, local
tests marked, a charge Airwallex decided alone kept, and each card's spend this month against its limit."""
import datetime as dt

from venthora import card_report

NOW = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class Org:
    def __init__(self, recs):
        self.recs = recs

    def list(self, kind):
        return list(self.recs.get(kind, []))


CARD = {"_id": "c1", "payee": "Cloudly", "masked": "4111 •••• 1234", "currency": "USD", "per_tx_minor": 50000, "monthly_minor": 100000,
        "merchant": "Cloudly", "mcc": ["5734"], "frozen": False, "issued_at": NOW, "issued_by": "Rosa", "status": "ACTIVE"}
AUTHS = [{"_id": "e1", "at": NOW, "card_id": "c1", "payee": "Cloudly", "amount_minor": 30000, "currency": "USD", "merchant": "Cloudly Inc", "mcc": "5734",
          "type": "AUTHORIZATION", "lifecycle_id": "L1", "decision": "AUTHORIZED", "reason": "inside the mandate: Cloudly, USD 300.00", "source": "airwallex"},
         {"_id": "e2", "at": NOW, "card_id": "c1", "payee": "Cloudly", "amount_minor": 90000, "currency": "USD", "merchant": "Cloudly Inc", "mcc": "5734",
          "type": "AUTHORIZATION", "lifecycle_id": "L2", "decision": "DECLINED", "reason": "above the card's per-transaction limit of USD 500.00", "source": "test"}]
TXNS = [{"lifecycle_id": "L1", "card_id": "c1", "transaction_type": "CLEARING", "status": "APPROVED", "billing_amount": 300, "billing_currency": "USD", "posted_date": NOW},
        {"lifecycle_id": "L9", "card_id": "c1", "transaction_type": "AUTHORIZATION", "status": "FAILED", "transaction_amount": 12.5, "transaction_currency": "USD",
         "merchant": {"name": "Other Shop", "category_code": "5999"}, "failure_reason": "MCC_NOT_ALLOWED", "transaction_date": NOW, "transaction_id": "t9"}]


def test_decisions_are_joined_with_airwallex_and_explained():
    rep = card_report.build(org=Org({"card": [CARD], "card_auth": AUTHS}), airwallex=TXNS, to_base=lambda m, c: m,
                            program={"cards_issued": 1}, tap={"keys": [{"key_id": "k1"}]})
    by = {r["event_id"]: r for r in rep["rows"]}
    assert by["e1"]["rule"] == "Inside the mandate" and by["e1"]["posted"]["status"] == "APPROVED" and by["e1"]["posted"]["billed_minor"] == 30000
    assert by["e2"]["rule"] == "Per-transaction limit" and by["e2"]["source"] == "Local test" and by["e2"]["posted"] is None
    assert by["t9"]["decided_by"] == "Airwallex" and by["t9"]["decision"] == "DECLINED" and by["t9"]["amount_minor"] == 1250 and by["t9"]["merchant"] == "Other Shop"
    c = rep["cards"][0]
    assert c["month_spent_minor"] == 30000 and c["authorized"] == 1 and c["declined"] == 2  # a local test never counts as spend
    assert rep["tap"] == {"key_published": True, "key_id": "k1"}


def test_the_rule_names_cover_every_reason_the_decision_gives():
    for reason, rule in [("card frozen in VENTHORA", "Card frozen"), ("this charge was already authorized", "Duplicate charge"),
                         ("above the card's monthly limit of USD 1.00", "Monthly limit"), ("above the mandate's single payout limit of USD 1.00", "Mandate limit"),
                         ("merchant 'X' is not the card's vendor (Y)", "Merchant not allowed"), ("merchant category not allowed on this card", "Merchant category not allowed"),
                         ("charge in EUR with no amount in the card currency USD", "Currency"), ("card not issued by VENTHORA", "Unknown card"), ("the signature does not match", "Signature")]:
        assert card_report.rule_of(reason, "DECLINED") == rule
