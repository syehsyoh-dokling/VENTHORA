"""The only door the internet gets: a gateway that forwards the two push channels and nothing else.

A tunnel (ngrok, Cloudflare) exposes whatever it points at. VENTHORA's API has no login, so the tunnel points
here instead. Exactly two routes are passed to the API, each with its raw body and only the headers it needs:

* ``POST /webhooks/airwallex``  Airwallex status events (signature headers; body up to 256 KB)
* ``POST /webhooks/email``      invoices and claims from the inbound mail service (Basic auth; up to 32 MB)

Every other method or path gets 404 without reaching the API. The API still authenticates, dedupes and checks
everything itself, so the gateway adds no trust, only removes reach.

    python scripts/webhook_gateway.py [--port 8002] [--upstream http://127.0.0.1:8001]
"""
from __future__ import annotations

import contextlib

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

PATH = "/webhooks/airwallex"
EMAIL_PATH = "/webhooks/email"
MAX_BODY = 256 * 1024
MAX_EMAIL_BODY = 32 * 1024 * 1024
FORWARDED = ("content-type", "x-timestamp", "x-signature")
ROUTES = {PATH: (FORWARDED, MAX_BODY), EMAIL_PATH: (("content-type", "authorization"), MAX_EMAIL_BODY)}


def create_app(upstream: str = "http://127.0.0.1:8001", client: httpx.AsyncClient | None = None) -> Starlette:
    http = client or httpx.AsyncClient(base_url=upstream, timeout=60.0)

    async def forward(request: Request) -> Response:
        path = request.url.path
        headers_allowed, limit = ROUTES[path]
        size = int(request.headers.get("content-length") or 0)
        if size > limit:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        body = await request.body()
        if len(body) > limit:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        headers = {k: request.headers[k] for k in headers_allowed if k in request.headers}
        try:
            res = await http.post(path, content=body, headers=headers)
        except httpx.HTTPError:
            # both senders retry anything but 2xx, so an API that is down loses nothing
            return JSONResponse({"detail": "VENTHORA is not reachable"}, status_code=502)
        return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))

    async def nothing(request: Request) -> Response:
        return JSONResponse({"detail": "not found"}, status_code=404)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await http.aclose()

    every = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[Route(PATH, forward, methods=["POST"]), Route(EMAIL_PATH, forward, methods=["POST"]),
                             Route("/{rest:path}", nothing, methods=every)], lifespan=lifespan)
