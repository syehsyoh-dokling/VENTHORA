# ruff: noqa: E501
"""Sign-in at the platform's front door (saas_gateway.py): the company that knows the person signs them in; a wrong
password and an unknown email get the same answer; a person with a second factor is asked for the code."""
import json

import httpx
import pytest
from starlette.testclient import TestClient

from venthora import auth, saas, saas_gateway

EMAIL, PASSWORD, CODE = "fitri@alpha.test", "Treasury2026ok", "123456"


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_fails", {})
    reg = saas.Registry(tmp_path)
    alpha = reg.create(name="Alpha Pte Ltd", fco_name="Fitri Hakim", fco_email=EMAIL, by="t")
    reg.create(name="Beta Pte Ltd", fco_name="Ben Beta", fco_email="ben@beta.test", by="t")
    state = {"mfa": False, "logins": []}

    def company(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        mine = request.url.port == alpha["port"]
        if request.url.path == "/v1/auth/platform-lookup":
            return httpx.Response(200, json={"exists": mine and body.get("email") == EMAIL})
        if request.url.path == "/v1/auth/login":
            state["logins"].append(body)
            if not mine or body.get("password") != PASSWORD:
                return httpx.Response(401, json={"detail": "wrong email or password"})
            if state["mfa"] and body.get("code") != CODE:
                return httpx.Response(401, json={"detail": "enter the six-digit code from your authenticator app", "mfa_required": True})
            return httpx.Response(200, json={"name": "Fitri Hakim", "role": "super_admin"}, headers={"set-cookie": "vs_alpha=session-token; HttpOnly; Path=/"})
        return httpx.Response(404, json={"detail": "not found"})

    real = httpx.AsyncClient
    monkeypatch.setattr(saas_gateway.httpx, "AsyncClient", lambda *a, **kw: real(*a, **(kw | {"transport": httpx.MockTransport(company)})))
    with TestClient(saas_gateway.create_app(tmp_path, spawn=False)) as gw:
        gw.state_ = state
        yield gw


def _sign_in(gw, **extra):
    return gw.post("/login/api", json={"email": EMAIL, "password": PASSWORD} | extra)


def test_the_right_password_opens_the_persons_company(gateway):
    r = _sign_in(gateway)
    assert r.status_code == 200 and r.json()["company"] == "Alpha Pte Ltd" and r.json()["role"] == "super_admin" and r.json()["redirect"] == "/#/overview"
    assert "vs_alpha=session-token" in r.headers["set-cookie"]  # the company's own session cookie reaches the browser
    assert gateway.state_["logins"][-1]["code"] is None  # no code typed, none sent


def test_wrong_password_and_unknown_email_get_the_same_answer(gateway):
    wrong = gateway.post("/login/api", json={"email": EMAIL, "password": "nope"})
    unknown = gateway.post("/login/api", json={"email": "nobody@nowhere.test", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json() == {"detail": "wrong username or password"}
    assert gateway.post("/login/api", json={"email": EMAIL}).status_code == 422


def test_a_person_with_a_second_factor_is_asked_for_the_code(gateway):
    gateway.state_["mfa"] = True
    r = _sign_in(gateway)
    assert r.status_code == 401 and r.json()["mfa_required"] is True and "code" in r.json()["detail"]
    assert "set-cookie" not in r.headers  # the password alone opened nothing
    bad = _sign_in(gateway, code="000000")
    assert bad.status_code == 401 and bad.json()["mfa_required"] is True
    good = _sign_in(gateway, code=f" {CODE} ")  # as typed, with spaces
    assert good.status_code == 200 and good.json()["company"] == "Alpha Pte Ltd" and gateway.state_["logins"][-1]["code"] == CODE
    # a wrong password never reveals that a second factor exists
    assert gateway.post("/login/api", json={"email": EMAIL, "password": "nope", "code": CODE}).json() == {"detail": "wrong username or password"}


def test_the_login_page_has_the_code_field(gateway):
    page = gateway.get("/login").text
    assert 'id="code"' in page and "one-time-code" in page and "e.mfa" in page
