"""The card program: a staff card opens only for an approved advance whose purpose is ticked, inside its window and
amount, at the merchants allowed; Airi checkouts follow the FCO's PR, price, invoice and vendor rules."""
import datetime as dt

import pytest

from venthora import card_program, cards, controls


class Org:
    def __init__(self):
        self.rec = {}

    def get(self, kind, _id):
        return self.rec.get((kind, _id))

    def list(self, kind):
        return [v for (k, _), v in self.rec.items() if k == kind]

    def _put(self, kind, rec, by):
        self.rec[(kind, rec["_id"])] = rec


NOW = dt.datetime.now(dt.UTC)


def staff_card(org, allowances):
    c = {"_id": "card-1", "kind": "staff", "user_id": "u1", "label": "Hadi Halim · EMP-17", "payee": "Hadi Halim · EMP-17", "currency": "USD",
         "per_tx_minor": 500_000, "monthly_minor": 500_000, "merchant": "", "mcc": [], "frozen": False, "allowances": allowances}
    org._put("card", c, "t")
    return c


def req(amount, merchant="GRAB", mcc="4121"):
    return {"card_id": "card-1", "transaction_amount": amount, "transaction_currency": "USD", "merchant": {"name": merchant, "category_code": mcc},
            "card_transaction_event_id": f"e-{amount}-{merchant}", "card_transaction_lifecycle_id": f"l-{amount}-{merchant}"}


def test_program_validates_and_keeps_what_is_not_sent():
    org = Org()
    p = card_program.save(org, {"staff_cards": True, "purposes": ["travel_advance", "online_transport"], "activate_after_days": 1}, "fd")
    assert p["purposes"] == ["travel_advance", "online_transport"] and p["activate_after_days"] == 1
    p = card_program.save(org, {"merchant_mode": "selected", "merchants": [{"mcc": "5812", "name": ""}]}, "fd")
    assert p["staff_cards"] and p["purposes"] == ["travel_advance", "online_transport"]  # kept
    with pytest.raises(ValueError):
        card_program.save(org, {"purposes": ["casino"]}, "fd")
    with pytest.raises(ValueError):
        card_program.save(org, {"merchants": [{"mcc": "58", "name": ""}]}, "fd")


def test_an_advance_window_opens_after_n_days_and_closes_at_settle_by():
    prog = card_program.DEFAULT | {"activate_after_days": 2, "deactivate_after_days": 0}
    start, until = card_program.window("2026-11-14T08:00:00+00:00", "2026-11-30", prog)
    assert start.startswith("2026-11-16T08:00") and until == "2026-11-30T23:59:59+00:00"
    start, until = card_program.window("2026-11-14T08:00:00+00:00", None, prog | {"deactivate_after_days": 7})
    assert until.startswith("2026-11-23T08:00")


def test_a_staff_card_spends_only_an_open_advance_and_only_what_it_holds():
    org = Org()
    card_program.save(org, {"staff_cards": True}, "fd")
    staff_card(org, [])
    assert cards.decide(org, req(20))[0] == "DECLINED"  # no advance open: closed
    opened = (NOW - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    staff_card(org, [{"ref": "CA-1", "amount_minor": 10_000, "from": opened, "until": None, "purpose": "cash_advance"}])
    d, why = cards.decide(org, req(60))
    assert d == "AUTHORIZED", why
    cards.record_decision(org, req(60), d, why, source="test")
    d, why = cards.decide(org, req(50, merchant="OTHER"))
    assert d == "DECLINED" and "still holds" in why  # 100 approved, 60 spent: 50 is too much
    later = (NOW + dt.timedelta(days=3)).isoformat(timespec="seconds")
    staff_card(org, [{"ref": "CA-2", "amount_minor": 10_000, "from": later, "until": None, "purpose": "cash_advance"}])
    assert cards.decide(org, req(10, merchant="X"))[0] == "DECLINED"  # its window has not opened yet


def test_only_selected_merchants_and_the_ticked_purposes_categories():
    org = Org()
    card_program.save(org, {"staff_cards": True, "purposes": ["online_transport"], "merchant_mode": "selected",
                            "merchants": [{"mcc": "", "name": "Garuda Indonesia"}]}, "fd")
    opened = (NOW - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    staff_card(org, [{"ref": "TA-1", "amount_minor": 100_000, "from": opened, "until": None, "purpose": "travel_advance"}])
    assert cards.decide(org, req(15, "GRAB", "4121"))[0] == "AUTHORIZED"  # online transport's category
    assert cards.decide(org, req(300, "GARUDA INDONESIA", "3000"))[0] == "AUTHORIZED"  # a selected merchant by name
    d, why = cards.decide(org, req(40, "STEAM GAMES", "5816"))
    assert d == "DECLINED" and "not one of the selected merchants" in why


def test_airi_checkout_needs_an_approved_pr_within_its_price_an_invoice_and_a_verified_vendor():
    org = Org()
    controls.save(org, "airi", {"enabled": True, "max_per_checkout_minor": 100_000, "monthly_cap_minor": 500_000, "fco_approval_above_minor": 50_000}, "fd")
    org._put("pr", {"_id": "PR-1", "pr_number": "PR-1", "status": "APPROVED", "estimated_minor": 30_000, "currency": "USD", "supplier_id": "acme"}, "t")
    status = {"acme": "VERIFIED", "newco": "NEW"}.get
    ok = controls.airi_check(org, {"pr_number": "PR-1", "amount_minor": 25_000, "currency": "USD", "vendor": "acme", "invoice": "INV-9"}, status)
    assert ok["decision"] == "ALLOWED"
    bad = controls.airi_check(org, {"pr_number": "PR-1", "amount_minor": 40_000, "currency": "USD", "vendor": "newco", "invoice": ""}, status)
    assert bad["decision"] == "REFUSED" and len(bad["reasons"]) == 4  # over the PR, no invoice, not the PR's supplier, not verified
    assert controls.airi_check(org, {"pr_number": "PR-404", "amount_minor": 100, "vendor": "acme", "invoice": "x"}, status)["decision"] == "REFUSED"


def test_the_cards_page_api_saves_the_program_and_the_card_limit():
    from fastapi.testclient import TestClient

    from venthora.api import app

    with TestClient(app) as c:
        r = c.put("/v1/cards/program?role=super_admin&by=fd", json={"staff_cards": True, "purposes": ["petty_cash"], "card_max_minor": 300_000})
        assert r.status_code == 200, r.text
        assert r.json()["card_max_minor"] == 300_000 and r.json()["purposes"] == ["petty_cash"]
        r = c.put("/v1/cards/program?role=super_admin&by=fd", json={"card_max_minor": None})
        assert r.status_code == 200 and r.json()["card_max_minor"] is None  # no limit of its own
        d = c.get("/v1/cards").json()
        assert d["program"]["staff_cards"] and "cardholder_default" in d and d["card_limit_minor"] > 0
        assert c.post("/v1/cards/staff?role=super_admin&by=fd").status_code == 409  # no cardholder yet
