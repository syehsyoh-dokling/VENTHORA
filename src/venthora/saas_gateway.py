# ruff: noqa: E501
"""The SaaS platform's front door: one address for every company, each company's browser sent to its own VENTHORA.

  /platform                  the platform home: the companies, each with its own dashboard (pick one to enter)
  /platform/console          the Developer's console (VENTHORA's operator): every company, its Airwallex account and
                             money totals, the platform account itself, and a form to add a company
  /c/<tenant>                enter that company's dashboard: a cookie remembers the company, then every request of this
                             browser goes to that company's own VENTHORA process and to nothing else
  /webhooks/airwallex        Airwallex events, sent to the company whose account the event is about (account_id)
  public channels            vendor forms, the pay simulator, API-key calls, card remote-auth, Xendit callbacks: to the
                             company that recognises the token, key or signature (find_owner)
  anything else              the company the cookie names; none yet -> /platform

The gateway starts and stops the companies' processes (saas.py). It adds no trust: each company's VENTHORA checks
roles and keys itself. It only decides which company a request reaches, and a request can reach one company only.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import contextlib
import json
import subprocess
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import saas
from .config import env, load_env

COOKIE = "venthora_company"
ROOT_COOKIE = "venthora_root"
PLATFORM_COOKIES = (COOKIE, ROOT_COOKIE)  # the platform's own: never sent on to a company
HOP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "content-length", "x-venthora-platform", "accept-encoding"}
PAGE = Path(__file__).with_name("dashboard") / "platform.html"
LOGIN_PAGE = Path(__file__).with_name("dashboard") / "login.html"
# reached from outside with no browser cookie, each owned by one company (see find_owner)
PUBLIC_PREFIXES = ("/rfq/", "/vendor-details/", "/pay-sim/", "/developers", "/.well-known/", "/approve/")
PUBLIC_EXACT = ("/webhooks/xendit", "/webhooks/airwallex/remote-auth", "/webhooks/ses")


def create_app(root: Path | None = None, *, spawn: bool = True) -> Starlette:
    load_env()
    reg = saas.Registry(root)
    procs: dict[str, subprocess.Popen] = {}
    clients: dict[str, httpx.AsyncClient] = {}
    accounts: dict[str, str] = {}  # Airwallex account id -> company, for webhooks

    def client(tid: str) -> httpx.AsyncClient:
        t = reg.get(tid)
        if tid not in clients:
            # a jar that keeps nothing: a session cookie set for one browser must never ride on another browser's request
            jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
            clients[tid] = httpx.AsyncClient(base_url=f"http://127.0.0.1:{t['port']}", timeout=httpx.Timeout(120.0, read=None), cookies=jar)
        return clients[tid]

    def start(t: dict) -> None:
        p = procs.get(t["tenant_id"])
        if p is not None and p.poll() is None:
            return
        log = reg.tenant_dir(t["tenant_id"]) / "server.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        procs[t["tenant_id"]] = subprocess.Popen(saas.command(t), env=saas.environ(reg, t), cwd=str(saas.ROOT),
                                                 stdout=log.open("ab"), stderr=subprocess.STDOUT)

    async def ensure(t: dict) -> None:
        """Start the company's VENTHORA, or keep the one already serving it (left from a platform that stopped hard)."""
        with contextlib.suppress(httpx.HTTPError, ValueError):
            r = await client(t["tenant_id"]).get("/v1/tenant", timeout=3)
            if r.status_code == 200 and r.json().get("tenant_id") == t["tenant_id"]:
                return
        start(t)

    async def ready(tid: str, wait_s: float = 90) -> bool:
        """Up, and the company's own: a process on that port serving another company is never taken for it."""
        for _ in range(int(wait_s * 2)):
            with contextlib.suppress(httpx.HTTPError, ValueError):
                r = await client(tid).get("/v1/tenant", timeout=3)
                if r.status_code == 200 and r.json().get("tenant_id") == tid:
                    return True
            p = procs.get(tid)
            if p is not None and p.poll() is not None:
                return False
            await asyncio.sleep(0.5)
        return False

    async def summary(t: dict) -> dict:
        tid = t["tenant_id"]
        row = {k: t[k] for k in ("tenant_id", "name", "fco_name", "fco_email", "country", "currency", "created_at")}
        p = procs.get(tid)
        row["running"] = p is None or p.poll() is None  # None: kept from before, its summary call tells
        try:
            r = await client(tid).get("/v1/tenant/summary", headers={"x-venthora-platform": reg.token()}, timeout=60)
            s = r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
        except httpx.HTTPError as exc:
            s = {"error": f"not reachable ({type(exc).__name__})"}
        for aid in ([((s.get("account") or {}).get("id"))] + list(s.get("opened_accounts") or [])):
            if aid:
                accounts[aid] = tid
        return row | {k: s.get(k) for k in ("mode", "account", "opened_accounts", "fco", "users", "balances_minor", "transfers", "deposits", "ai", "error")}

    async def platform_account() -> dict:
        """The operator's own Airwallex account (the platform keys): its balances and the connected accounts under it."""
        from .gateways.airwallex import Airwallex, AirwallexError

        cid, key = env("AIRWALLEX_CLIENT_ID"), env("AIRWALLEX_API_KEY")
        if not (cid and key):
            return {"configured": False}
        bank = Airwallex(credentials=(cid, key))
        try:
            bal = await bank.balances_minor()
            body = await bank._request("GET", "/api/v1/accounts", own=True)
            items = body.get("items", body) if isinstance(body, dict) else body
            conn = [{"id": a.get("id"), "status": a.get("status"),
                     "name": ((a.get("account_details") or {}).get("business_details") or {}).get("business_name") or a.get("nickname") or "",
                     "company": accounts.get(a.get("id"))} for a in items]
            return {"configured": True, "balances_minor": bal, "connected_accounts": conn}
        except AirwallexError as exc:
            return {"configured": True, "error": str(exc)[:200]}
        finally:
            await bank.close()

    # -- email sign-in apps (OAuth): Root keeps them; every company's VENTHORA is given the same ones ------------------
    def mail_redirect() -> str:
        from . import mail_oauth

        return reg.mail_oauth().get("redirect_uri") or (env("VENTHORA_PUBLIC_URL") or "https://venthora.muwahid.org").rstrip("/") + mail_oauth.PATH

    async def push_mail_oauth(t: dict) -> str:
        from . import mail_oauth

        apps = await asyncio.to_thread(reg.mail_oauth)
        body = {p: ({"client_id": apps[p]["client_id"], "client_secret": apps[p]["client_secret"]} if p in apps else {"client_id": ""})
                for p in mail_oauth.PROVIDERS} | {"redirect_uri": mail_redirect()}
        try:
            r = await client(t["tenant_id"]).put("/v1/platform/mail-oauth", json=body, headers={"x-venthora-platform": reg.token()}, timeout=20)
            return "given" if r.status_code == 200 else f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            return f"not reachable ({type(exc).__name__})"

    async def mail_oauth_root(request: Request) -> Response:
        """Root: VENTHORA's OAuth app per provider (client ID shown, secret write-only) and the callback to register."""
        from . import mail_oauth

        if not is_root(request):
            return need_root()
        pushed = None
        if request.method == "PUT":
            try:
                b = await request.json()
            except json.JSONDecodeError:
                b = {}
            if b.get("redirect_uri"):
                await asyncio.to_thread(reg.set_mail_oauth, "_redirect", str(b["redirect_uri"]).strip(), "-")
            for p in mail_oauth.PROVIDERS:
                if isinstance(b.get(p), dict):
                    await asyncio.to_thread(reg.set_mail_oauth, p, str(b[p].get("client_id") or "").strip(), str(b[p].get("client_secret") or "").strip() or None)
            ts = reg.list()
            pushed = dict(zip([t["name"] for t in ts], await asyncio.gather(*(push_mail_oauth(t) for t in ts)), strict=True))
        apps = await asyncio.to_thread(reg.mail_oauth)
        return JSONResponse({"redirect_uri": mail_redirect(), "pushed": pushed,
                             "providers": {p: {"label": v["label"], "register": v["register"], "links": v.get("links", []), "client_id": (apps.get(p) or {}).get("client_id", ""),
                                               "secret_set": bool((apps.get(p) or {}).get("client_secret")), "updated_at": (apps.get(p) or {}).get("updated_at")}
                                           for p, v in mail_oauth.PROVIDERS.items()}})

    async def mail_oauth_callback(request: Request) -> Response:
        """The provider's return: the state names the company that started it; the code goes to that company only."""
        tid = (request.query_params.get("state") or "").split(".", 1)[0]
        if reg.get(tid) is None:
            return HTMLResponse("<p>This sign-in link does not belong to a company here. Start again from Email Sync.</p>", status_code=400)
        try:
            r = await client(tid).get("/oauth/mail/callback", params=request.query_params.multi_items(), timeout=60)
        except httpx.HTTPError:
            return HTMLResponse("<p>The company's VENTHORA is not reachable; try again in a minute.</p>", status_code=502)
        keep = {k: v for k, v in r.headers.items() if k.lower() in ("location", "content-type")}
        return Response(r.content, status_code=r.status_code, headers=keep)

    # -- pages and the console's API ------------------------------------------------------------------------------
    async def page(request: Request) -> Response:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    # -- Root ------------------------------------------------------------------------------------------------------
    def is_root(request: Request) -> bool:
        return reg.root_ok(request.cookies.get(ROOT_COOKIE))

    def need_root() -> Response:
        return JSONResponse({"detail": "Root only: sign in to the Developer console"}, status_code=401)

    async def root_state(request: Request) -> Response:
        return JSONResponse({"set": await asyncio.to_thread(reg.root_set), "signed_in": is_root(request)})

    async def root_signin(request: Request) -> Response:
        """First visit: choose the Root password. Afterwards: sign in with it (five wrong in five minutes lock it)."""
        from . import auth

        try:
            b = await request.json()
        except json.JSONDecodeError:
            b = {}
        pw = str(b.get("password") or "")
        if request.url.path.endswith("/setup"):
            try:
                await asyncio.to_thread(reg.root_setup, pw)
            except ValueError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=422)
        else:
            if auth.locked("root"):
                return JSONResponse({"detail": "too many wrong passwords: wait five minutes"}, status_code=429)
            if not await asyncio.to_thread(reg.root_verify, pw):
                auth.failed("root")
                return JSONResponse({"detail": "wrong password"}, status_code=401)
            auth.cleared("root")
        res = JSONResponse({"signed_in": True})
        res.set_cookie(ROOT_COOKIE, reg.root_cookie(), httponly=True, samesite="strict", path="/platform")
        return res

    async def root_signout(request: Request) -> Response:
        res = JSONResponse({"signed_in": False})
        res.delete_cookie(ROOT_COOKIE, path="/platform")
        return res

    # -- /login: one door; who signs in decides where they land -------------------------------------------------------
    async def login_page(request: Request) -> Response:
        return HTMLResponse(LOGIN_PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})

    async def demo_accounts(request: Request) -> Response:
        """Staging-demo only (VENTHORA_DEMO_LOGIN=1): one sign-in per level from data/saas/demo_accounts.md, so a visitor
        can pick Root, the FCO or a staff member and sign in at once. Off by default: then it answers an empty list."""
        if env("VENTHORA_DEMO_LOGIN", "").strip().lower() not in ("1", "true", "yes"):
            return JSONResponse({"enabled": False, "accounts": []})
        from .config import ROOT

        path = Path(env("VENTHORA_DEMO_ACCOUNTS") or ROOT / "data" / "saas" / "demo_accounts.md")
        out = []
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
                if len(cells) < 5 or "@" not in cells[2] or cells[0].lower() == "level":
                    continue
                out.append({"level": cells[0], "name": cells[1], "email": cells[2], "password": cells[3], "role": cells[4]})
        return JSONResponse({"enabled": bool(out), "accounts": out}, headers={"Cache-Control": "no-store"})

    async def login_state(request: Request) -> Response:
        return JSONResponse({"root_set": await asyncio.to_thread(reg.root_set), "root": is_root(request)})

    async def login_root_setup(request: Request) -> Response:
        """The first visit only: Root chooses its email and password."""
        try:
            b = await request.json()
            await asyncio.to_thread(reg.root_setup, str(b.get("password") or ""), str(b.get("email") or ""))
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        res = JSONResponse({"redirect": "/platform/console", "who": "Root"})
        res.set_cookie(ROOT_COOKIE, reg.root_cookie(), httponly=True, samesite="strict", path="/platform")
        return res

    async def login(request: Request) -> Response:
        """Email and password. Root's email opens the console; anyone else is looked up in the companies (each says
        only whether it has an active person with that email), signed in by that company's own VENTHORA, and sent to
        its dashboard. Wrong email and wrong password get the same answer."""
        from . import auth

        try:
            b = await request.json()
        except json.JSONDecodeError:
            b = {}
        email, pw, pick = str(b.get("email") or "").strip().lower(), str(b.get("password") or ""), str(b.get("tenant_id") or "")
        if not email or not pw:
            return JSONResponse({"detail": "enter your username and password"}, status_code=422)
        if auth.locked(email):
            return JSONResponse({"detail": "too many wrong passwords: wait five minutes"}, status_code=429)
        if email == (await asyncio.to_thread(reg.root_email) or "\x00"):
            if await asyncio.to_thread(reg.root_verify, pw):
                auth.cleared(email)
                res = JSONResponse({"redirect": "/platform/console", "who": "Root"})
                res.set_cookie(ROOT_COOKIE, reg.root_cookie(), httponly=True, samesite="strict", path="/platform")
                return res
            auth.failed(email)
            return JSONResponse({"detail": "wrong username or password"}, status_code=401)

        async def has(t: dict) -> bool:
            with contextlib.suppress(httpx.HTTPError, ValueError):
                r = await client(t["tenant_id"]).post("/v1/auth/platform-lookup", json={"email": email},
                                                      headers={"x-venthora-platform": reg.token()}, timeout=20)
                return r.status_code == 200 and r.json().get("exists") is True
            return False

        ts = reg.list()
        found = [t for t, yes in zip(ts, await asyncio.gather(*(has(t) for t in ts)), strict=True) if yes]
        if pick:
            found = [t for t in found if t["tenant_id"] == pick]
        hits: list[tuple[dict, httpx.Response]] = []
        for t in found:
            with contextlib.suppress(httpx.HTTPError):
                r = await client(t["tenant_id"]).post("/v1/auth/login", json={"email": email, "password": pw}, timeout=60)
                if r.status_code == 200:
                    hits.append((t, r))
        if not hits:
            auth.failed(email)
            return JSONResponse({"detail": "wrong username or password"}, status_code=401)
        auth.cleared(email)
        if len(hits) > 1:  # the same person works for several companies on this platform: they choose
            return JSONResponse({"choose": [{"tenant_id": t["tenant_id"], "name": t["name"], "role": r.json().get("role")} for t, r in hits]})
        t, r = hits[0]
        me = r.json()
        res = JSONResponse({"redirect": "/#/overview", "company": t["name"], "who": me.get("name"), "role": me.get("role")})
        res.set_cookie(COOKIE, t["tenant_id"], httponly=True, samesite="lax")
        for k, v in r.headers.multi_items():  # the company's own session cookie, as its VENTHORA set it
            if k.lower() == "set-cookie":
                res.raw_headers.append((b"set-cookie", v.encode("latin-1")))
        return res

    async def to_login(request: Request) -> Response:
        return RedirectResponse("/login", status_code=303)

    async def fco_link(t: dict) -> str | None:
        """The FCO's one-time sign-in link, made by the company's own VENTHORA on the platform's token."""
        with contextlib.suppress(httpx.HTTPError, ValueError, KeyError):
            r = await client(t["tenant_id"]).post("/v1/auth/platform-invite", json={"user_id": "u-admin"},
                                                  headers={"x-venthora-platform": reg.token()}, timeout=30)
            if r.status_code == 200:
                return r.json()["link"]
        return None

    async def tenant_link(request: Request) -> Response:
        if not is_root(request):
            return need_root()
        t = reg.get(request.path_params["tenant"])
        if t is None:
            return JSONResponse({"detail": "no such company"}, status_code=404)
        link = await fco_link(t)
        return JSONResponse({"name": t["name"], "fco_name": t["fco_name"], "fco_email": t["fco_email"], "link": link}, status_code=200 if link else 502)

    async def tenants(request: Request) -> Response:
        if not is_root(request):
            return need_root()
        if request.method == "POST":
            from . import airwallex_onboarding

            try:
                b = await request.json()
                kyb = b.get("kyb") or {}
                if kyb:  # the full registration: checked before anything is created
                    kyb = airwallex_onboarding.validate(kyb | {"contact_email": kyb.get("contact_email") or b.get("fco_email", "")})
                t = await asyncio.to_thread(reg.create, name=kyb.get("business_name") or str(b.get("name", "")), fco_name=str(b.get("fco_name", "")),
                                            fco_email=str(b.get("fco_email", "")), country=kyb.get("country_code") or str(b.get("country") or "SG"),
                                            currency=str(b.get("currency") or "USD"), by="Root (VENTHORA platform)")
            except (ValueError, json.JSONDecodeError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=422)
            if spawn:
                start(t)
                if not await ready(t["tenant_id"]):
                    return JSONResponse({"detail": f"{t['name']} was registered but its VENTHORA did not start: see its server.log"}, status_code=502)
                await push_mail_oauth(t)
            profile = None
            if kyb:  # kept in the company's VENTHORA; its Airwallex account is opened once its FCO agrees to Airwallex's terms
                try:
                    r = await client(t["tenant_id"]).post("/v1/platform/company-profile", json=kyb, headers={"x-venthora-platform": reg.token()}, timeout=60)
                    profile = "kept" if r.status_code == 200 else r.text[:200]
                except httpx.HTTPError as exc:
                    profile = f"not kept: {type(exc).__name__}"
            return JSONResponse(await summary(t) | {"fco_link": await fco_link(t), "profile": profile})
        rows = await asyncio.gather(*(summary(t) for t in reg.list()))
        return JSONResponse({"items": rows})

    async def kyb_form(request: Request) -> Response:
        """The registration form Root fills for a new company: every field Airwallex needs to open and verify its
        account, with the choices and Airwallex's industry list (read with the platform's key)."""
        from . import airwallex_onboarding
        from .gateways.airwallex import Airwallex, AirwallexError

        if not is_root(request):
            return need_root()
        inds: list[dict] = []
        cid, key = env("AIRWALLEX_CLIENT_ID"), env("AIRWALLEX_API_KEY")
        if cid and key:
            bank = Airwallex(credentials=(cid, key))
            with contextlib.suppress(AirwallexError):
                inds = [{"code": v.get("value"), "name": f"{g.get('name')} · {v.get('name')}"}
                        for g in await bank.industry_categories() for v in g.get("values", [])]
            await bank.close()
        return JSONResponse({"fields": [{"key": k, "label": lbl, "section": s, "required": req, "choices": airwallex_onboarding.CHOICES.get(k)}
                                        for k, lbl, s, req in airwallex_onboarding.FIELDS], "industries": [i for i in inds if i["code"]]})

    async def tenants_public(request: Request) -> Response:
        """Names only, for Root: since /login, nobody else needs (or sees) which companies the platform serves."""
        if not is_root(request):
            return need_root()
        return JSONResponse({"items": [{"tenant_id": t["tenant_id"], "name": t["name"], "fco_name": t["fco_name"]} for t in reg.list()]})

    async def platform(request: Request) -> Response:
        if not is_root(request):
            return need_root()
        return JSONResponse(await platform_account())

    async def enter(request: Request) -> Response:
        tid = request.path_params["tenant"]
        if reg.get(tid) is None:
            return RedirectResponse("/login", status_code=303)
        invite = request.query_params.get("invite") or ""
        ok = invite.replace("-", "").replace("_", "").isalnum() and 20 <= len(invite) <= 100
        res = RedirectResponse(f"/?invite={invite}#/overview" if ok else "/#/overview", status_code=303)
        res.set_cookie(COOKIE, tid, httponly=True, samesite="lax")
        return res

    async def leave(request: Request) -> Response:
        res = RedirectResponse("/login", status_code=303)
        res.delete_cookie(COOKIE)
        return res

    # -- webhooks: to the company the event is about ---------------------------------------------------------------
    async def webhook(request: Request) -> Response:
        body = await request.body()
        if len(body) > 256 * 1024:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        try:
            aid = (json.loads(body) or {}).get("account_id")
        except ValueError:
            aid = None
        if aid and aid not in accounts:
            await asyncio.gather(*(summary(t) for t in reg.list()))  # a company opened an account since the last look
        tid = accounts.get(aid or "")
        if tid is None:
            return JSONResponse({"detail": "no company on this platform works on that account"}, status_code=202)
        headers = {k: request.headers[k] for k in ("content-type", "x-timestamp", "x-signature") if k in request.headers}
        try:
            res = await client(tid).post(request.url.path, content=body, headers=headers)
        except httpx.HTTPError:
            return JSONResponse({"detail": "that company's VENTHORA is not reachable"}, status_code=502)
        return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))

    # -- public channels with no browser cookie: the company that recognises the token, key or signature ---------
    async def find_owner(request: Request) -> Response:
        """A vendor's form token, an API key, a card remote-auth signature or a Xendit callback token belongs to one
        company: ask each in turn and answer with the first that accepts it (a company that does not know it refuses
        with 401/403/404 and changes nothing)."""
        body = await request.body()
        if len(body) > 32 * 1024 * 1024:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "cookie"]
        last: httpx.Response | None = None
        for t in reg.list():
            try:
                res = await client(t["tenant_id"]).request(request.method, request.url.path, params=request.query_params.multi_items(),
                                                           content=body or None, headers=headers, timeout=60)
            except httpx.HTTPError:
                continue
            last = res
            if res.status_code not in (401, 403, 404):
                break
        if last is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        keep = {k: v for k, v in last.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}
        return Response(last.content, status_code=last.status_code, headers=keep)

    def public(request: Request) -> bool:
        p = request.url.path
        keyed = (request.headers.get("authorization") or "").lower().startswith("bearer vth_") or bool(request.headers.get("x-api-key"))
        return p.startswith(PUBLIC_PREFIXES) or p in PUBLIC_EXACT or (keyed and (p.startswith(("/v1/", "/api/", "/docs")) or p in ("/openapi.json", "/health")))

    # -- inbound email: to the company whose mailbox it was sent to ------------------------------------------------
    async def route_email(request: Request) -> Response:
        """The mail service posts every email here (raw RFC 822, Postmark JSON, or a form with the raw message). The
        platform reads only its recipient addresses, asks each company whether one is its mailbox (Settings → Email
        sources), and hands the email, unchanged and with the service's credentials, to that company's VENTHORA, which
        authenticates it and reads it. No company has the address: 202, nothing kept."""
        from email import policy
        from email.parser import BytesParser
        from email.utils import getaddresses

        body = await request.body()
        if len(body) > 64 * 1024 * 1024:
            return JSONResponse({"detail": "email too large"}, status_code=413)
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        addrs: set[str] = {a.strip().lower() for a in request.query_params.getlist("to") if a}
        with contextlib.suppress(Exception):
            if ctype == "application/json":
                d = json.loads(body)
                vals = [d.get("OriginalRecipient", ""), d.get("To", ""), d.get("Cc", "")] + [x.get("Email", "") for x in d.get("ToFull") or []]
                addrs |= {a.lower() for _, a in getaddresses([v for v in vals if v])}
            else:
                raw = body
                if ctype == "multipart/form-data":  # the raw message inside the form (SendGrid "email", Mailgun "body-mime")
                    msg = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + body)
                    raw = next((p.get_payload(decode=True) or b"" for p in msg.iter_parts()
                                if p.get_param("name", header="content-disposition") in ("email", "body-mime")), b"")
                h = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
                names = ("To", "Cc", "Delivered-To", "X-Original-To", "X-Forwarded-To", "Envelope-To")
                addrs |= {a.lower() for _, a in getaddresses([v for n in names for v in h.get_all(n, [])])}
        addrs.discard("")

        async def owns(t: dict) -> bool:
            with contextlib.suppress(httpx.HTTPError, ValueError):
                r = await client(t["tenant_id"]).post("/v1/platform/mailbox-owner", json={"addresses": sorted(addrs)},
                                                      headers={"x-venthora-platform": reg.token()}, timeout=20)
                return r.status_code == 200 and r.json().get("owns") is True
            return False

        ts = reg.list()
        owners = [t for t, yes in zip(ts, await asyncio.gather(*(owns(t) for t in ts)), strict=True) if yes] if addrs else []
        if not owners:
            return JSONResponse({"detail": "no company on this platform has a mailbox for this address", "to": sorted(addrs)[:5]}, status_code=202)
        t = owners[0]
        headers = {k: request.headers[k] for k in ("content-type", "authorization", "x-venthora-secret") if k in request.headers}
        try:
            res = await client(t["tenant_id"]).post(request.url.path, content=body, headers=headers, timeout=180)
        except httpx.HTTPError:
            return JSONResponse({"detail": f"{t['name']}'s VENTHORA is not reachable"}, status_code=502)
        return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))

    # -- everything else: the company this browser entered -------------------------------------------------------
    async def proxy(request: Request) -> Response:
        tid = request.cookies.get(COOKIE)
        if (not tid or reg.get(tid) is None) and request.url.path in ("/webhooks/email", "/webhooks/email/mime"):
            return await route_email(request)
        if (not tid or reg.get(tid) is None) and public(request):
            return await find_owner(request)
        if not tid or reg.get(tid) is None:
            if request.url.path in ("/", "/index.html"):
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "choose a company first at /platform"}, status_code=401)
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "cookie"]
        mine = "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k not in PLATFORM_COOKIES)  # the company's session
        if mine:
            headers.append(("cookie", mine))
        body = await request.body()
        try:
            req = client(tid).build_request(request.method, request.url.path, params=request.query_params.multi_items(),
                                            content=body or None, headers=headers)
            res = await client(tid).send(req, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"detail": f"{reg.get(tid)['name']}'s VENTHORA is not reachable"}, status_code=502)
        out = StreamingResponse(res.aiter_raw(), status_code=res.status_code, background=BackgroundTask(res.aclose))
        for k, v in res.headers.multi_items():  # every Set-Cookie kept (a dict would keep one)
            if k.lower() not in HOP and k.lower() != "content-encoding":
                out.raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
        return out

    # -- staging-demo data reset (demo_reset.py): Developer Console → Data reset --------------------------------------
    reset_lock = asyncio.Lock()

    async def reset_actor(request: Request) -> str | None:
        """Who may switch or run the reset: Root, or a company's Super admin / Developer signed in on this browser."""
        if is_root(request):
            return "Root (VENTHORA platform)"
        tid = request.cookies.get(COOKIE)
        if not tid or reg.get(tid) is None:
            return None
        mine = "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k not in PLATFORM_COOKIES)
        with contextlib.suppress(httpx.HTTPError, ValueError):
            r = await client(tid).get("/v1/auth/me", headers={"cookie": mine} if mine else {}, timeout=20)
            if r.status_code == 200:
                me = r.json()
                if me.get("role") in ("super_admin", "developer"):
                    return f"{me.get('name') or me.get('email')} ({me.get('role')}, {reg.get(tid)['name']})"
        return None

    async def stop_tenant(tid: str) -> bool:
        p = procs.get(tid)
        if p is None or p.poll() is not None:
            return p is not None
        p.terminate()
        try:
            await asyncio.to_thread(p.wait, 20)
        except subprocess.TimeoutExpired:
            p.kill()
            await asyncio.to_thread(p.wait, 10)
        return True

    async def run_reset(by: str, categories: list[str]) -> dict:
        """The chosen categories back to the baseline in every company: stop its VENTHORA, replace those rows, start it
        again and wait until it answers. "companies" removes companies registered after the baseline."""
        from . import demo_reset

        cats = [c for c in categories if c in demo_reset.CAT]
        if not cats:
            raise ValueError("choose at least one category to reset")
        async with reset_lock:
            base = set(demo_reset.baseline_tenants(reg.root))
            if not base:
                raise ValueError("no baseline saved yet")
            data_cats = [c for c in cats if c != "companies"]
            done, removed, problems = [], [], []
            for t in list(reg.list()):
                tid = t["tenant_id"]
                extra = tid not in base
                if extra and "companies" not in cats:
                    continue
                if not extra and not data_cats:
                    continue
                stopped = await stop_tenant(tid)
                clients.pop(tid, None)
                if extra:
                    await asyncio.to_thread(demo_reset.remove_extra, reg.root, tid, reg.tenant_dir, reg.root / "platform.db")
                    procs.pop(tid, None)
                    removed.append(t["name"])
                    continue
                rows = await asyncio.to_thread(demo_reset.restore, reg.root, tid, reg.tenant_dir, data_cats)
                if spawn:
                    start(t)
                    if not await ready(tid):
                        problems.append(f"{t['name']} did not start again (see its server.log)")
                    else:
                        with contextlib.suppress(Exception):
                            await push_mail_oauth(t)
                done.append({"company": t["name"], "rows": rows, "was_running": stopped})
            result = {"by": by, "reset": done, "removed": removed, "problems": problems}
            return await asyncio.to_thread(demo_reset.record_run, reg.root, cats, result)

    async def demo_reset_api(request: Request) -> Response:
        from . import demo_reset

        who = await reset_actor(request)
        if who is None:
            return JSONResponse({"detail": "only the Super admin, a Developer or Root may manage the data reset"}, status_code=403)
        action = request.path_params.get("action", "")
        try:
            if request.method == "GET":
                return JSONResponse(await asyncio.to_thread(demo_reset.settings, reg.root) | {"now_utc": datetime.now(UTC).isoformat(timespec="seconds")})
            b = {}
            with contextlib.suppress(json.JSONDecodeError):
                b = await request.json()
            if request.method == "PUT" and not action:
                out = await asyncio.to_thread(demo_reset.configure, reg.root, bool(b.get("enabled")), list(b.get("categories") or []), who)
            elif request.method == "POST" and action == "baseline":
                out = await asyncio.to_thread(demo_reset.save_baseline, reg.root, reg.list(), reg.tenant_dir, who)
            elif request.method == "POST" and action == "run":  # now: the categories named, else every category set to reset
                cats = list(b.get("categories") or []) or await asyncio.to_thread(demo_reset.selected, reg.root)
                out = await run_reset(who, cats)
            else:
                return JSONResponse({"detail": "not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return JSONResponse(out | {"now_utc": datetime.now(UTC).isoformat(timespec="seconds")})

    async def reset_scheduler() -> None:
        from . import demo_reset

        while True:
            await asyncio.sleep(30)
            with contextlib.suppress(Exception):
                cats = await asyncio.to_thread(demo_reset.due_categories, reg.root)
                if cats:
                    await run_reset("daily schedule", cats)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if spawn:
            await asyncio.gather(*(ensure(t) for t in reg.list()))
            await asyncio.gather(*(ready(t["tenant_id"]) for t in reg.list()))
            await asyncio.gather(*(push_mail_oauth(t) for t in reg.list()))
            with contextlib.suppress(Exception):
                await asyncio.gather(*(summary(t) for t in reg.list()))
        resetter = asyncio.create_task(reset_scheduler())
        yield
        resetter.cancel()
        for c in clients.values():
            await c.aclose()
        for p in procs.values():
            with contextlib.suppress(Exception):
                p.terminate()
                p.wait(timeout=10)

    every = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[
        Route("/platform", to_login), Route("/platform/console", page), Route("/login", login_page),
        Route("/login/api", login, methods=["POST"]), Route("/login/api/state", login_state), Route("/login/api/root-setup", login_root_setup, methods=["POST"]),
        Route("/login/api/demo-accounts", demo_accounts),
        Route("/platform/demo-reset", demo_reset_api, methods=["GET", "PUT"]),
        Route("/platform/demo-reset/{action}", demo_reset_api, methods=["POST"]),
        Route("/platform/api/companies", tenants_public), Route("/platform/api/tenants", tenants, methods=["GET", "POST"]),
        Route("/platform/api/account", platform), Route("/c/{tenant}", enter), Route("/platform/leave", leave),
        Route("/platform/api/root", root_state), Route("/platform/api/root/setup", root_signin, methods=["POST"]),
        Route("/platform/api/root/login", root_signin, methods=["POST"]), Route("/platform/api/root/logout", root_signout, methods=["POST"]),
        Route("/platform/api/tenants/{tenant}/fco-link", tenant_link, methods=["POST"]), Route("/platform/api/kyb", kyb_form),
        Route("/webhooks/airwallex", webhook, methods=["POST"]),
        Route("/platform/api/mail-oauth", mail_oauth_root, methods=["GET", "PUT"]), Route("/oauth/mail/callback", mail_oauth_callback),
        Route("/{rest:path}", proxy, methods=every)], lifespan=lifespan)
