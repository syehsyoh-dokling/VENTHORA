# ruff: noqa: E501
"""The only door the internet gets: a gateway that forwards the push channels and nothing else.

A tunnel (ngrok, Cloudflare) exposes whatever it points at. VENTHORA's API has no login, so the tunnel points
here instead. Only these routes are passed to the API, each with its raw body and only the headers it needs:

* ``POST /webhooks/airwallex``  Airwallex status events (signature headers; body up to 256 KB)
* ``POST /webhooks/email``      invoices and claims from the inbound mail service (Basic auth; up to 32 MB)
* ``POST /webhooks/airwallex/remote-auth``  Airwallex asks whether a card charge may go (x-signature, x-nonce)
* ``GET /.well-known/venthora-agent.json``  the agent's public signing keys (Visa Trusted Agent Protocol)
* ``GET|POST /rfq/{token}``, ``GET /rfq/{token}/data``  a vendor's quotation form (procurement; the token is the key)
* ``GET /developers`` (+ catalog.json, openapi.json, postman.json)  the public API Catalog: read only, no key, no data

Every other method or path gets 404 without reaching the API. The API still authenticates, dedupes and checks
everything itself, so the gateway adds no trust, only removes reach.

With ``--api`` (``create_app(api=True)``) the gateway also forwards the API itself (``/v1/*``, ``/api/*``,
``/openapi.json``, ``/health``) for other applications, but only requests that carry an API key
(``Authorization: Bearer vth_...`` or ``X-API-Key``); anything else gets 401 here. The API checks the key, its
scopes and its role (Platform Setup → App Integration).

    python scripts/webhook_gateway.py [--port 8002] [--upstream http://127.0.0.1:8001] [--api]
"""
from __future__ import annotations

import contextlib
import re

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

PATH = "/webhooks/airwallex"
EMAIL_PATH = "/webhooks/email"
REMOTE_AUTH_PATH = "/webhooks/airwallex/remote-auth"
AGENT_KEYS_PATH = "/.well-known/venthora-agent.json"
AGENT_KEYS_UPSTREAM = "/v1/visa/tap/keys.json"
MAX_BODY = 256 * 1024
MAX_EMAIL_BODY = 32 * 1024 * 1024
FORWARDED = ("content-type", "x-timestamp", "x-signature")
ROUTES = {PATH: (FORWARDED, MAX_BODY), EMAIL_PATH: (("content-type", "authorization"), MAX_EMAIL_BODY),
          REMOTE_AUTH_PATH: (("content-type", "x-signature", "x-nonce"), 64 * 1024)}
API_PREFIXES = ("/v1/", "/api/")
API_EXACT = ("/openapi.json", "/health")
API_HEADERS = ("content-type", "authorization", "x-api-key", "accept")
MAX_API_BODY = 16 * 1024 * 1024
RFQ_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,40}$")  # a vendor's quotation form link (sourcing.invite)
MAX_RFQ_BODY = 12 * 1024 * 1024  # the form posts the quotation file as base64 (8 MB file)
DEVELOPER_PATHS = ("/developers", "/developers/catalog.json", "/developers/openapi.json", "/developers/postman.json")  # the API Catalog


def has_key(headers) -> bool:
    auth = headers.get("authorization") or ""
    return auth.lower().startswith("bearer vth_") or bool((headers.get("x-api-key") or "").strip())


def create_app(upstream: str = "http://127.0.0.1:8001", client: httpx.AsyncClient | None = None, api: bool = False,
               email_upstream: str | None = None) -> Starlette:
    """`email_upstream`: inbound email (/webhooks/email) goes to another VENTHORA than the rest (one company's mail
    moved to a new installation while the Airwallex webhooks still reach the old one)."""
    http = client or httpx.AsyncClient(base_url=upstream, timeout=60.0)
    mail_http = httpx.AsyncClient(base_url=email_upstream, timeout=60.0) if email_upstream else http

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
            res = await (mail_http if path == EMAIL_PATH else http).post(path, content=body, headers=headers)
        except httpx.HTTPError:
            # both senders retry anything but 2xx, so an API that is down loses nothing
            return JSONResponse({"detail": "VENTHORA is not reachable"}, status_code=502)
        return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))

    async def agent_keys(request: Request) -> Response:
        try:
            res = await http.get(AGENT_KEYS_UPSTREAM)
        except httpx.HTTPError:
            return JSONResponse({"detail": "VENTHORA is not reachable"}, status_code=502)
        return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))

    async def nothing(request: Request) -> Response:
        path = request.url.path
        if not (api and (path.startswith(API_PREFIXES) or path in API_EXACT)):
            return JSONResponse({"detail": "not found"}, status_code=404)
        if not has_key(request.headers):
            return JSONResponse({"detail": "an API key is required (Platform Setup → App Integration)"}, status_code=401,
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

    async def rfq(request: Request) -> Response:
        """A vendor's quotation form: the page, its data and the submission, keyed by the token in the path."""
        token = request.path_params["token"]
        if not RFQ_TOKEN.match(token):
            return JSONResponse({"detail": "not found"}, status_code=404)
        body = await request.body() if request.method == "POST" else b""
        if len(body) > MAX_RFQ_BODY:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        try:
            res = await http.request(request.method, request.url.path, content=body or None,
                                     headers={"content-type": request.headers.get("content-type", "application/json")} if body else None)
        except httpx.HTTPError:
            return JSONResponse({"detail": "VENTHORA is not reachable"}, status_code=502)
        return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))

    async def developers(request: Request) -> Response:
        """The public API Catalog (read only, no key, no data): the page and its three files."""
        try:
            res = await http.get(request.url.path)
        except httpx.HTTPError:
            return JSONResponse({"detail": "VENTHORA is not reachable"}, status_code=502)
        keep = {k: v for k, v in res.headers.items() if k.lower() in ("content-disposition", "cache-control")}
        return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"), headers=keep)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await http.aclose()

    every = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[Route(PATH, forward, methods=["POST"]), Route(EMAIL_PATH, forward, methods=["POST"]),
                             Route(REMOTE_AUTH_PATH, forward, methods=["POST"]), Route(AGENT_KEYS_PATH, agent_keys, methods=["GET"]),
                             Route("/rfq/{token}", rfq, methods=["GET", "POST"]), Route("/rfq/{token}/data", rfq, methods=["GET"]),
                             *[Route(p, developers, methods=["GET"]) for p in DEVELOPER_PATHS],
                             Route("/{rest:path}", nothing, methods=every)], lifespan=lifespan)
