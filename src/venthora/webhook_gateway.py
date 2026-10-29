"""The only door the internet gets: a gateway that forwards the two push channels and nothing else.

A tunnel (ngrok, Cloudflare) exposes whatever it points at. VENTHORA's API has no login, so the tunnel points
here instead. Exactly two routes are passed to the API, each with its raw body and only the headers it needs:

* ``POST /webhooks/airwallex``  Airwallex status events (signature headers; body up to 256 KB)
* ``POST /webhooks/email``      invoices and claims from the inbound mail service (Basic auth; up to 32 MB)

Every other method or path gets 404 without reaching the API. The API still authenticates, dedupes and checks
everything itself, so the gateway adds no trust, only removes reach.

With ``--api`` (``create_app(api=True)``) the gateway also forwards the API itself (``/v1/*``, ``/api/*``,
``/openapi.json``, ``/health``) for other applications, but only requests that carry an API key
(``Authorization: Bearer vth_...`` or ``X-API-Key``); anything else gets 401 here. The API checks the key, its
scopes and its role (Settings → API access).

    python scripts/webhook_gateway.py [--port 8002] [--upstream http://127.0.0.1:8001] [--api]
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
API_PREFIXES = ("/v1/", "/api/")
API_EXACT = ("/openapi.json", "/health")
API_HEADERS = ("content-type", "authorization", "x-api-key", "accept")
MAX_API_BODY = 16 * 1024 * 1024


def has_key(headers) -> bool:
    auth = headers.get("authorization") or ""
    return auth.lower().startswith("bearer vth_") or bool((headers.get("x-api-key") or "").strip())


def create_app(upstream: str = "http://127.0.0.1:8001", client: httpx.AsyncClient | None = None, api: bool = False) -> Starlette:
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
        path = request.url.path
        if not (api and (path.startswith(API_PREFIXES) or path in API_EXACT)):
            return JSONResponse({"detail": "not found"}, status_code=404)
        if not has_key(request.headers):
            return JSONResponse({"detail": "an API key is required (Settings → API access)"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        if int(request.headers.get("content-length") or 0) > MAX_API_BODY:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        body = await request.body()
        if len(body) > MAX_API_BODY:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        headers = {k: request.headers[k] for k in API_HEADERS if k in request.headers}
        try:
            params = request.query_params.multi_items()
            res = await http.request(request.method, path, params=params, content=body, headers=headers)
        except httpx.HTTPError:
            return JSONResponse({"detail": "VENTHORA is not reachable"}, status_code=502)
        return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await http.aclose()

    every = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[Route(PATH, forward, methods=["POST"]), Route(EMAIL_PATH, forward, methods=["POST"]),
                             Route("/{rest:path}", nothing, methods=every)], lifespan=lifespan)
