# ruff: noqa: E501
"""What every answer to a browser carries, and how fast one address may ask.

One ASGI middleware, the same on the company's VENTHORA (api.py) and on the public gateway (saas_gateway.py):

  headers     X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy and a Content-Security-Policy
              on every answer; Strict-Transport-Security when the request came over HTTPS. A header the handler set
              itself is kept.
  cookies     over HTTPS every Set-Cookie gets `Secure`, so a session is never sent over plain HTTP.
  rate limit  per client address, in a sliding minute: sign-in and password routes 20, everything else 1800. Webhooks
              carry their own signature and are not limited; neither is the gateway's own loopback traffic. Over the
              limit the answer is 429 with Retry-After. VENTHORA_RATE_LIMIT=0 turns it off (the test suite does).

HTTPS is read from the request (its scheme, or X-Forwarded-Proto set by the proxy in front); VENTHORA_HTTPS=1 says so
for a proxy that sets nothing.

The policy allows inline script and style because the dashboard is one page with its own inline code; it still
forbids every other origin except the one library the pages load, plug-ins, foreign frames and foreign form targets.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'self' https://www.youtube-nocookie.com; frame-ancestors 'self'; "
       "base-uri 'self'; form-action 'self'; object-src 'none'")
HEADERS = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"SAMEORIGIN"),
           (b"referrer-policy", b"strict-origin-when-cross-origin"),
           (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()")]
HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
NO_CSP = ("/docs", "/redoc", "/openapi.json")  # the API reference pages load their own viewer from a CDN

AUTH_PATHS = {"/v1/auth/login", "/v1/auth/accept", "/login/api", "/login/api/root-setup", "/platform/api/root/login",
              "/platform/api/root/setup"}
AUTH_PER_MINUTE, ALL_PER_MINUTE, WINDOW_S = 20, 1800, 60.0
LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def _header(scope, name: bytes) -> str:
    return next((v.decode("latin-1") for k, v in scope.get("headers") or [] if k == name), "")


def is_https(scope) -> bool:
    return (scope.get("scheme") == "https" or _header(scope, b"x-forwarded-proto").split(",")[0].strip().lower() == "https"
            or os.environ.get("VENTHORA_HTTPS") == "1")


def client_ip(scope) -> str | None:
    """The address that asked. A caller that connects directly is its own socket address, whatever headers it sends:
    X-Forwarded-For is the caller's to write, so it is believed only from a proxy on this machine (the tunnel, the
    gateway), and then the LAST hop is taken, the one the nearest proxy added (the first ones are the caller's own
    claim). None for the gateway's own loopback calls, which are never limited."""
    host = (scope.get("client") or ("",))[0]
    if host and host not in LOCAL:
        return host
    hops = [h.strip() for h in _header(scope, b"x-forwarded-for").split(",") if h.strip()]
    return hops[-1] if hops else None


class Limiter:
    def __init__(self):
        self.hits: dict[tuple[str, str], deque] = {}

    def allow(self, ip: str, bucket: str, limit: int, now: float | None = None) -> float:
        """0 when the request may pass; else the seconds to wait."""
        now = time.monotonic() if now is None else now
        q = self.hits.setdefault((ip, bucket), deque())
        while q and now - q[0] >= WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            return max(1.0, WINDOW_S - (now - q[0]))
        q.append(now)
        if len(self.hits) > 20_000:  # addresses that stopped asking are forgotten
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] >= WINDOW_S]:
                del self.hits[k]
        return 0.0


def secure_cookie(value: bytes) -> bytes:
    attrs = [a.strip().lower() for a in value.split(b";")[1:]]
    return value if b"secure" in attrs else value + b"; Secure"


class Hardening:
    def __init__(self, app):
        self.app = app
        self.limiter = Limiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, https = scope.get("path") or "", is_https(scope)
        if os.environ.get("VENTHORA_RATE_LIMIT", "1") != "0" and not path.startswith("/webhooks/"):
            ip = client_ip(scope)
            if ip is not None:
                auth = scope.get("method") == "POST" and path in AUTH_PATHS
                wait = self.limiter.allow(ip, "auth" if auth else "all", AUTH_PER_MINUTE if auth else ALL_PER_MINUTE)
                if wait:
                    logging.getLogger("venthora").warning("rate limit: %s over the %s limit on %s", ip, "sign-in" if auth else "request", path)
                    body =json.dumps({"detail": "too many requests from this address: wait a minute and try again"}).encode()
                    await send({"type": "http.response.start", "status": 429,
                                "headers": [(b"content-type", b"application/json"), (b"retry-after", str(int(wait)).encode()),
                                            (b"content-length", str(len(body)).encode()), *HEADERS]})
                    return await send({"type": "http.response.body", "body": body})

        async def out(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {k.lower() for k, _ in headers}
                add = list(HEADERS) + ([HSTS] if https else [])
                if not path.startswith(NO_CSP):
                    add.append((b"content-security-policy", CSP.encode()))
                headers += [(k, v) for k, v in add if k not in have]
                if https:
                    headers = [(k, secure_cookie(v)) if k.lower() == b"set-cookie" else (k, v) for k, v in headers]
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, out)
