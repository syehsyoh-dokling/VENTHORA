# ruff: noqa: E501
"""Sign-in with a second factor: the password alone stops working once a person turns it on."""
import time

import pytest
from fastapi.testclient import TestClient

from venthora import auth, totp

PASSWORD = "Treasury2026ok"


@pytest.fixture
def signed(monkeypatch):
    from venthora.api import app

    monkeypatch.setenv("VENTHORA_LOGIN", "1")
    monkeypatch.setattr(auth, "_fails", {})
    with TestClient(app) as c:
        user = next(u for u in app.state.org.list("user") if u.get("email") and u.get("active", True))
        auth.AuthStore().set_password(user["user_id"], PASSWORD)
        c.user = user
        yield c


def _login(c, **extra):
    return c.post("/v1/auth/login", json={"email": c.user["email"], "password": PASSWORD} | extra)


def _code(secret, ahead=0):
    return totp.code_at(secret, totp.counter(time.time()) + ahead)


def test_sign_in_works_with_the_password_until_a_second_factor_is_on(signed):
    assert signed.get("/v1/auth/mfa").status_code == 401  # nobody is signed in
    r = _login(signed)
    assert r.status_code == 200 and r.json()["role"] == signed.user["role"]
    assert signed.get("/v1/auth/mfa").json() == {"enabled": False}
    assert signed.get("/security").status_code == 200  # the page where a person turns it on


def test_turning_it_on_takes_a_code_from_the_app(signed):
    _login(signed)
    d = signed.post("/v1/auth/mfa/setup").json()
    assert len(d["secret"]) == 32 and d["uri"].startswith("otpauth://totp/VENTHORA:")
    assert signed.get("/v1/auth/mfa").json() == {"enabled": False}  # not in force until proven
    assert signed.post("/v1/auth/mfa/confirm", json={"code": "000000"}).status_code in (422,)
    assert signed.post("/v1/auth/mfa/confirm", json={"code": _code(d["secret"])}).json() == {"enabled": True}
    assert signed.post("/v1/auth/mfa/setup").status_code == 409  # already on: it is turned off first, with a code
    assert any(e["event_type"] == "second_factor_on" for e in signed.app.state.audit.list(20))


def test_once_on_the_password_alone_no_longer_signs_in(signed):
    _login(signed)
    secret = signed.post("/v1/auth/mfa/setup").json()["secret"]
    signed.post("/v1/auth/mfa/confirm", json={"code": _code(secret)})
    signed.post("/v1/auth/logout")
    signed.cookies.clear()
    r = _login(signed)
    assert r.status_code == 401 and r.json()["mfa_required"] is True and "code" in r.json()["detail"]
    assert signed.get("/v1/auth/mfa").status_code == 401  # still nobody
    bad = _login(signed, code="123456" if _code(secret, 1) != "123456" else "654321")
    assert bad.status_code == 401 and bad.json()["mfa_required"] is True
    assert _login(signed, code=_code(secret)).status_code == 401  # the code that turned it on was already used
    good = _login(signed, code=_code(secret, 1))  # the next code the app shows
    assert good.status_code == 200 and signed.get("/v1/auth/mfa").json() == {"enabled": True}
    assert _login(signed, code=_code(secret, 1)).status_code == 401  # and that one is spent too


def test_turning_it_off_takes_a_code_and_a_new_sign_in_link_recovers_a_lost_phone(signed):
    _login(signed)
    secret = signed.post("/v1/auth/mfa/setup").json()["secret"]
    signed.post("/v1/auth/mfa/confirm", json={"code": _code(secret)})
    assert signed.post("/v1/auth/mfa/off", json={"code": "000000"}).status_code == 422
    assert signed.post("/v1/auth/mfa/off", json={"code": _code(secret, 1)}).json() == {"enabled": False}
    signed.post("/v1/auth/logout")
    signed.cookies.clear()
    assert _login(signed).status_code == 200  # the password alone again
    # on again, then the phone is lost: an admin's new sign-in link lets the person choose a password and start over
    secret = signed.post("/v1/auth/mfa/setup").json()["secret"]
    signed.post("/v1/auth/mfa/confirm", json={"code": _code(secret)})
    token = auth.AuthStore().invite(signed.user["user_id"], "admin")
    signed.cookies.clear()
    assert signed.post("/v1/auth/accept", json={"token": token, "password": "Another2026pass"}).status_code == 200
    assert signed.get("/v1/auth/mfa").json() == {"enabled": False}


def test_wrong_codes_lock_the_email_like_wrong_passwords(signed):
    _login(signed)
    secret = signed.post("/v1/auth/mfa/setup").json()["secret"]
    signed.post("/v1/auth/mfa/confirm", json={"code": _code(secret)})
    signed.cookies.clear()
    wrong = "111111" if "111111" not in (_code(secret, -1), _code(secret), _code(secret, 1)) else "222222"
    for _ in range(auth.MAX_FAILS):
        assert _login(signed, code=wrong).status_code == 401
    assert _login(signed, code=_code(secret, 1)).status_code == 429  # locked for five minutes, right code or not
