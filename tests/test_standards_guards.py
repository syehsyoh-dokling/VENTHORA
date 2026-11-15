# ruff: noqa: E501
"""ISO 4217 decimals, where a typed address may lead, who VENTHORA answers without sign-in, and what a requester
reads of other people."""
import pytest
from fastapi.testclient import TestClient

from venthora import iso4217, iso20022, net_guard


def test_iso_4217_decides_what_an_amount_may_be():
    assert [iso4217.exponent(c) for c in ("USD", "IDR", "JPY", "KRW", "BHD", "KWD", "CLF")] == [2, 2, 0, 0, 3, 3, 4]
    assert iso4217.decimal(1_800_000_000, "JPY") == "18000000" and iso4217.decimal(123_45, "USD") == "123.45" and iso4217.decimal(1_23, "BHD") == "1.23"
    assert iso20022.amount(1_800_000_000, "JPY") == "18000000" and iso20022.ZERO_DECIMAL is iso4217.ZERO  # one table
    with pytest.raises(ValueError, match="no decimals"):
        iso4217.decimal(1_800_000_050, "JPY")
    with pytest.raises(ValueError, match="unit of account"):
        iso4217.check_amount(100, "CLF")
    assert iso4217.NUMERIC["USD"] == "840" and iso4217.NUMERIC["IDR"] == "360" and iso4217.NUMERIC["JPY"] == "392"


@pytest.mark.parametrize(("url", "saas", "ok"), [
    ("http://169.254.169.254/latest/meta-data/", False, False),  # the cloud metadata service: never
    ("http://169.254.169.254/latest/meta-data/", True, False),
    ("ftp://example.com/x", False, False), ("not a url", False, False),
    ("http://127.0.0.1:8000/v1", False, True),  # a self-hosted VENTHORA reaches its own machine (a local model)
    ("http://127.0.0.1:8000/v1", True, False),  # a multi-company one does not
    ("http://10.0.0.8/erp", True, False), ("http://192.168.1.20/erp", False, True)])
def test_a_typed_address_never_reaches_what_only_the_server_can_reach(url, saas, ok):
    if ok:
        assert net_guard.check_url(url, saas=saas) == url
    else:
        with pytest.raises(net_guard.Refused):
            net_guard.check_url(url, saas=saas)


def test_without_sign_in_venthora_answers_its_own_machine_only(monkeypatch):
    from venthora.api import app

    monkeypatch.delenv("VENTHORA_LOGIN", raising=False)
    monkeypatch.delenv("VENTHORA_OPEN", raising=False)
    with TestClient(app) as local:
        assert local.get("/v1/fund-requests").status_code == 200  # this machine
    with TestClient(app, client=("203.0.113.9", 50000)) as far:
        r = far.get("/v1/fund-requests")
        assert r.status_code == 403 and "answers its own machine only" in r.json()["detail"]
        assert far.post("/v1/executions", json={}).status_code == 403  # nothing is reachable, not even to be refused later
        assert far.get("/health").status_code == 200
        monkeypatch.setenv("VENTHORA_OPEN", "1")  # the operator opens it knowingly
        assert far.get("/v1/fund-requests").status_code == 200


def test_a_requester_sees_who_people_are_not_what_they_earn(monkeypatch):
    from venthora.api import app

    with TestClient(app) as c:
        org = c.app.state.org
        u = next(x for x in org.list("user") if x["user_id"] == "u-lee")
        org._put("user", u | {"salary_minor": 500_000, "salary_currency": "USD", "approval_limit_minor": 10_000}, "test")
        full = next(x for x in c.get("/v1/org/users?role=finance_manager").json() if x["user_id"] == "u-lee")
        assert full["salary_minor"] == 500_000 and full["approval_limit_minor"] == 10_000
        seen = next(x for x in c.get("/v1/org/users?role=requester").json() if x["user_id"] == "u-lee")
        assert seen["name"] == "Lee Park" and seen["role"] and "salary_minor" not in seen and "approval_limit_minor" not in seen and "payee_id" not in seen


def test_an_advance_approved_on_the_dashboard_with_no_account_of_its_own_is_allocated(monkeypatch):
    from venthora.api import app

    with TestClient(app) as c:
        fm = "role=finance_manager&by=Dana%20Finch"
        r = c.post(f"/v1/org/fund-requests?{fm}", json={"kind": "cash_advance", "requested_by": "u-lee", "amount_minor": 12_000, "currency": "USD",
                                                        "charge_code": "US-200.OPS.6900", "purpose": "Field visit", "settle_by": "2099-12-31"})
        assert r.status_code == 200, r.text
        n = r.json()["request_number"]
        done = c.post(f"/v1/fund-requests/{n}/decision?{fm}", json={"decision": "approve"})
        assert done.status_code == 200, done.text
        rec = done.json()
        if rec.get("payee_id"):  # the person has an account in this currency: a person chooses Pay or Card
            assert "allocation" not in rec
        else:
            assert rec["allocation"]["status"] in ("ALLOCATED", "WAITING") and rec["allocation"]["route"] in ("virtual_card", "staff_card")
