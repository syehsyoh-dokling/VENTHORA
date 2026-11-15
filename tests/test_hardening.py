# ruff: noqa: E501
"""Every answer carries the security headers; over HTTPS a cookie is Secure and HSTS is set; one address is limited."""
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from venthora import hardening


async def page(request):
    out = PlainTextResponse("ok")
    out.set_cookie("venthora_session", "t", httponly=True, samesite="lax")
    return out


async def framed(request):
    return JSONResponse({}, headers={"X-Frame-Options": "DENY"})


def make():
    return Starlette(routes=[Route("/", page), Route("/own", framed), Route("/docs", page), Route("/v1/auth/login", page, methods=["POST"]),
                             Route("/webhooks/airwallex", page, methods=["POST"])], middleware=[Middleware(hardening.Hardening)])


def test_headers_on_every_answer():
    r = TestClient(make()).get("/")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"] and "object-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] and r.headers["permissions-policy"]
    assert "strict-transport-security" not in r.headers  # plain HTTP: a browser would ignore it, and it is not claimed
    assert "secure" not in r.headers["set-cookie"].lower()
    assert TestClient(make()).get("/own").headers["x-frame-options"] == "DENY"  # a handler's own header is kept
    assert "content-security-policy" not in TestClient(make()).get("/docs").headers  # the API reference loads its viewer


def test_https_gets_hsts_and_a_secure_cookie():
    r = TestClient(make()).get("/", headers={"x-forwarded-proto": "https"})
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert r.headers["set-cookie"].rstrip().endswith("; Secure") and "httponly" in r.headers["set-cookie"].lower()
    assert hardening.secure_cookie(b"a=b; Secure; HttpOnly") == b"a=b; Secure; HttpOnly"  # never added twice
    assert hardening.secure_cookie(b"secure=1") == b"secure=1; Secure"  # a cookie named secure is not the attribute


def test_sign_in_is_limited_per_address(monkeypatch):
    monkeypatch.setenv("VENTHORA_RATE_LIMIT", "1")
    c = TestClient(make())
    a = {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}
    codes = [c.post("/v1/auth/login", headers=a).status_code for _ in range(hardening.AUTH_PER_MINUTE + 1)]
    assert codes[:-1] == [200] * hardening.AUTH_PER_MINUTE and codes[-1] == 429
    r = c.post("/v1/auth/login", headers=a)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.headers["x-content-type-options"] == "nosniff"
    assert c.post("/v1/auth/login", headers={"x-forwarded-for": "203.0.113.8"}).status_code == 200  # another address
    assert c.get("/", headers=a).status_code == 200  # reading pages is a separate, wider limit
    assert [c.post("/webhooks/airwallex", headers=a).status_code for _ in range(30)] == [200] * 30  # signed, not limited
    assert [c.post("/v1/auth/login").status_code for _ in range(30)] == [200] * 30  # the gateway's own loopback calls


def test_the_window_slides():
    lim = hardening.Limiter()
    assert [lim.allow("ip", "auth", 2, now=t) for t in (0.0, 1.0)] == [0.0, 0.0]
    assert lim.allow("ip", "auth", 2, now=2.0) >= 1.0
    assert lim.allow("ip", "auth", 2, now=60.5) == 0.0  # the first hit is a minute old
    assert lim.allow("ip", "all", 2, now=60.6) == 0.0  # another bucket


def test_the_real_apps_carry_it():
    from venthora.api import app

    with TestClient(app) as c:
        r = c.get("/health")
        assert r.headers["x-content-type-options"] == "nosniff" and "content-security-policy" in r.headers
