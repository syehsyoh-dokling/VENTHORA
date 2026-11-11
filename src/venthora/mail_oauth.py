# ruff: noqa: E501
"""Sign in to a company mailbox with the provider's own consent screen (OAuth 2.0), instead of an app password.

The OAuth app (client ID and secret) is VENTHORA's, registered once per provider and the same for every company: on
the SaaS platform Root keeps it and the platform hands it to each company's process; a self-hosted VENTHORA reads it
from the environment (MAIL_OAUTH_<PROVIDER>_CLIENT_ID / _CLIENT_SECRET, MAIL_OAUTH_REDIRECT_URI). What is per mailbox
is the consent: the FCO presses "Connect with Microsoft", signs in on Microsoft's page and allows; VENTHORA receives a
refresh token for that one mailbox, keeps it write-only, and turns it into a short-lived access token whenever it reads
(IMAP) or sends (SMTP) with XOAUTH2. No password is typed into VENTHORA.

The start makes a one-time state (10 minutes); the callback accepts only that state, once. The provider's endpoints can
be pointed elsewhere for a test (MAIL_OAUTH_<PROVIDER>_AUTHORIZE_URL / _TOKEN_URL).
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from . import secret_values
from .config import env

PROVIDERS: dict[str, dict] = {
    "outlook": {"label": "Microsoft", "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                "scope": "openid email offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send",
                "extra": {"prompt": "select_account"},
                "links": [["New app registration", "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/CreateApplicationBlade"],
                          ["App registrations", "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"]],
                "register": "https://entra.microsoft.com → App registrations → New registration (any organisational directory and personal accounts); "
                            "API permissions: IMAP.AccessAsUser.All, SMTP.Send, offline_access; Certificates & secrets → New client secret"},
    "gmail": {"label": "Google", "authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
              "scope": "openid email https://mail.google.com/", "extra": {"access_type": "offline", "prompt": "consent"},
              "links": [["New project", "https://console.cloud.google.com/projectcreate"], ["Consent screen", "https://console.cloud.google.com/auth/branding"],
                        ["Test users", "https://console.cloud.google.com/auth/audience"], ["Scopes", "https://console.cloud.google.com/auth/scopes"],
                        ["New OAuth client", "https://console.cloud.google.com/auth/clients/create"]],
              "register": "https://console.cloud.google.com → APIs & Services → OAuth consent screen (scope https://mail.google.com/; in testing, add the "
                          "mailboxes as test users) → Credentials → OAuth client ID (Web application)"},
    "yahoo": {"label": "Yahoo", "authorize": "https://api.login.yahoo.com/oauth2/request_auth", "token": "https://api.login.yahoo.com/oauth2/get_token",
              "scope": "openid mail-w", "extra": {},
              "links": [["Create an app", "https://developer.yahoo.com/apps/create/"], ["My apps", "https://developer.yahoo.com/apps/"]],
              "register": "https://developer.yahoo.com/apps → Create an App (Mail read/write needs Yahoo's approval)"},
}
# Microsoft's error codes, said as what to do about them
HINTS = {"AADSTS7000215": "the client secret is wrong: paste the secret's Value (not its Secret ID), or make a new client secret",
         "AADSTS7000222": "the client secret has expired: make a new client secret and paste its Value",
         "AADSTS700016": "the app was not found for this account: for a personal Outlook.com / Hotmail mailbox, set the app's Supported accounts "
                         "to include personal Microsoft accounts and leave the tenant ID blank",
         "AADSTS50020": "this account is not in the app's directory: for a personal Outlook.com / Hotmail mailbox, allow personal Microsoft "
                        "accounts and leave the tenant ID blank",
         "AADSTS50011": "the redirect URI does not match: add the redirect URI shown in VENTHORA to the app (platform Web)",
         "AADSTS65001": "the permissions were not allowed: sign in again and press Accept, or ask the admin to grant consent",
         "AADSTS53003": "your organisation's Conditional Access policy blocked the sign-in; ask the Microsoft admin"}
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")  # a Microsoft Secret ID, not its Value
STATE_KIND = "mail_oauth_state"
STATE_TTL = timedelta(minutes=10)
PATH = "/oauth/mail/callback"


class OAuthError(Exception):
    pass


def _name(provider: str, part: str) -> str:
    return f"mail_oauth.{provider}.{part}"


def _envname(provider: str, part: str) -> str:
    return f"MAIL_OAUTH_{provider.upper()}_{part.upper()}"


def endpoint(provider: str, which: str) -> str:
    """The provider's page; a Microsoft app registered for one directory only ("Single tenant") signs in through that
    directory, not through /common/."""
    url = env(_envname(provider, f"{which}_url")) or PROVIDERS[provider][which]
    tenant = secret_values.get(_own(provider, "tenant")) if provider == "outlook" and app(provider)["own"] else ""
    return url.replace("/common/", f"/{tenant}/") if tenant else url


def redirect_uri() -> str:
    """The one callback address registered with every provider (the platform's public address)."""
    v = env("MAIL_OAUTH_REDIRECT_URI") or secret_values.get("mail_oauth.redirect_uri")
    return v or (env("VENTHORA_PUBLIC_URL") or "https://venthora.muwahid.org").rstrip("/") + PATH


def _own(provider: str, part: str) -> str:
    return f"mail_oauth.own.{provider}.{part}"


def app(provider: str) -> dict:
    """The company's own app (registered in its own Microsoft / Google account) when it has one, else VENTHORA's."""
    own_id, own_sec = secret_values.get(_own(provider, "client_id")), secret_values.get(_own(provider, "client_secret"))
    if own_id and own_sec:
        return {"client_id": own_id, "client_secret": own_sec, "ready": True, "own": True}
    cid = secret_values.get(_name(provider, "client_id"), _envname(provider, "client_id"))
    sec = secret_values.get(_name(provider, "client_secret"), _envname(provider, "client_secret"))
    return {"client_id": cid, "client_secret": sec, "ready": bool(cid and sec), "own": False}


def set_own(provider: str, client_id: str, client_secret: str | None, by: str, tenant: str | None = None) -> None:
    """The company's own app; a blank secret keeps the saved one (same client ID); a blank client ID removes it.
    `tenant`: the Directory (tenant) ID of a single-tenant Microsoft app; blank = any directory (/common/)."""


    if provider not in PROVIDERS:
        raise OAuthError(f"no sign-in for {provider}")
    client_id = client_id.strip()
    if not client_id:
        for part in ("client_id", "client_secret", "tenant"):
            secret_values.delete(_own(provider, part))
        return
    if tenant is not None:
        tenant = tenant.strip()
        if tenant and not re.fullmatch(r"[0-9a-fA-F-]{36}|[a-z0-9.-]+\.[a-z]{2,}", tenant):
            raise OAuthError("the Directory (tenant) ID looks like 8-4-4-4-12 hex digits, or your domain (contoso.onmicrosoft.com)")
        if tenant:
            secret_values.put(_own(provider, "tenant"), tenant, by)
        else:
            secret_values.delete(_own(provider, "tenant"))
    if not client_secret and secret_values.get(_own(provider, "client_id")) != client_id:
        raise OAuthError("paste the client secret too")
    if client_secret and provider == "outlook" and re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", client_secret.strip()):
        raise OAuthError("that is the Secret ID: in Certificates & secrets copy the Value column of the secret instead "
                         "(a new secret shows it once; if it is hidden, make a new client secret)")
    secret_values.put(_own(provider, "client_id"), client_id, by)
    if client_secret:
        secret_values.put(_own(provider, "client_secret"), client_secret.strip(), by)


def apps_public() -> dict:
    """What a page may know: which providers can be connected with a sign-in, whose app it is, and the callback address."""
    out = {}
    for p, v in PROVIDERS.items():
        a = app(p)
        out[p] = {"label": v["label"], "ready": a["ready"], "own": a["own"], "own_client_id": a["client_id"] if a["own"] else "",
                  "own_tenant": secret_values.get(_own(p, "tenant")) if a["own"] else "",
                  "own_secret_is_id": bool(a["own"] and p == "outlook" and _GUID.fullmatch(a["client_secret"].strip())),
                  "links": v.get("links", [])}
    return {"redirect_uri": redirect_uri(), "providers": out}


def set_apps(body: dict, by: str) -> dict:
    """Keep the OAuth apps (from the platform): {provider: {client_id, client_secret}}, redirect_uri. A blank secret keeps the saved one."""
    if body.get("redirect_uri"):
        secret_values.put("mail_oauth.redirect_uri", str(body["redirect_uri"]).strip(), by)
    for p in PROVIDERS:
        a = body.get(p) or {}
        if "client_id" in a:
            if a["client_id"]:
                secret_values.put(_name(p, "client_id"), str(a["client_id"]).strip(), by)
            else:
                secret_values.delete(_name(p, "client_id"))
                secret_values.delete(_name(p, "client_secret"))
        if a.get("client_secret"):
            secret_values.put(_name(p, "client_secret"), str(a["client_secret"]).strip(), by)
    return apps_public()


def start(org, provider: str, *, origin: str, by: str, tenant: str | None, send_too: bool = True, login_hint: str = "",
          since_days: int | None = None) -> str:
    """The provider's consent page for this company; the state names the company (for the platform) and is used once."""
    if provider not in PROVIDERS:
        raise OAuthError(f"no sign-in for {provider}")
    a = app(provider)
    if not a["ready"]:
        raise OAuthError(f"paste the Client ID and secret of your own {PROVIDERS[provider]['label']} app first")
    nonce = secrets.token_urlsafe(24)
    org._put(STATE_KIND, {"_id": nonce, "provider": provider, "origin": origin.rstrip("/"), "by": by, "send_too": bool(send_too), "since_days": since_days,
                          "expires": (datetime.now(UTC) + STATE_TTL).isoformat()}, by)
    q = {"client_id": a["client_id"], "redirect_uri": redirect_uri(), "response_type": "code", "scope": PROVIDERS[provider]["scope"],
         "state": f"{tenant or 'local'}.{nonce}"} | PROVIDERS[provider]["extra"]
    if login_hint:
        q["login_hint"] = login_hint
    return endpoint(provider, "authorize") + "?" + urlencode(q)


def take_state(org, state: str) -> dict:
    """The start this callback answers: known, not expired, and used only once."""
    nonce = state.split(".", 1)[-1]
    rec = org.get(STATE_KIND, nonce) if nonce else None
    if rec is None:
        raise OAuthError("this sign-in link is unknown or was already used: start again from Email Sync")
    org.delete(STATE_KIND, nonce, "VENTHORA (mail sign-in)")
    if datetime.fromisoformat(rec["expires"]) < datetime.now(UTC):
        raise OAuthError("the sign-in took longer than 10 minutes: start again from Email Sync")
    return rec


def _claims(id_token: str) -> dict:
    """The id token's claims. It came straight from the provider's token endpoint over TLS, so its payload is read
    as the provider's statement of who signed in."""
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _token_call(provider: str, form: dict) -> dict:
    a = app(provider)
    data = form | {"client_id": a["client_id"], "client_secret": a["client_secret"]}
    try:
        r = httpx.post(endpoint(provider, "token"), data=data, timeout=30)
    except httpx.HTTPError as exc:
        raise OAuthError(f"{PROVIDERS[provider]['label']} did not answer: {type(exc).__name__}") from exc
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in body:
        said = str(body.get("error_description") or body.get("error") or f"HTTP {r.status_code}")
        hint = next((h for code, h in HINTS.items() if code in said), "")
        raise OAuthError(f"{PROVIDERS[provider]['label']} refused: {hint or said.splitlines()[0][:220]}")
    return body


def _record(provider: str, body: dict, old: dict | None = None) -> dict:
    return {"kind": "oauth", "provider": provider, "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or (old or {}).get("refresh_token", ""),
            "expires_at": int(time.time()) + int(body.get("expires_in") or 3600)}


def finish(provider: str, code: str) -> tuple[str, str]:
    """Code -> tokens; (the mailbox address that signed in, the secret to keep for it)."""
    body = _token_call(provider, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()})
    c = _claims(body.get("id_token", ""))
    address = str(c.get("email") or c.get("preferred_username") or c.get("upn") or "").strip().lower()
    if "@" not in address:
        raise OAuthError("the provider did not say which mailbox signed in (no email in the sign-in): allow the email permission")
    who = _claims(body["access_token"]) if body["access_token"].count(".") == 2 else {}
    if "#EXT#" in str(who.get("upn") or who.get("unique_name") or ""):  # signed in as a guest of the app's directory: no mailbox there
        raise OAuthError(f"{address} signed in as a guest of the app's directory, which has no mailbox for it: leave the Directory (tenant) ID "
                         "blank in VENTHORA (and let the app accept personal Microsoft accounts), then Test connect again")
    rec = _record(provider, body)
    if not rec["refresh_token"]:
        raise OAuthError("the provider gave no refresh token: remove VENTHORA's access in the account settings and connect again")
    return address, json.dumps(rec)


def is_oauth(secret: str) -> bool:
    return secret.startswith('{"kind": "oauth"') or secret.startswith('{"kind":"oauth"')


def fresh(secret: str, save=None) -> str:
    """The access token for a kept sign-in, renewed with the refresh token when it is (nearly) expired; `save` keeps the
    renewed record (the provider may also rotate the refresh token)."""
    rec = json.loads(secret)
    if rec.get("expires_at", 0) > time.time() + 60:
        return rec["access_token"]
    body = _token_call(rec["provider"], {"grant_type": "refresh_token", "refresh_token": rec["refresh_token"]})
    new = _record(rec["provider"], body, rec)
    if save:
        save(json.dumps(new))
    return new["access_token"]


def xoauth2(user: str, token: str) -> str:
    return f"user={user}\x01auth=Bearer {token}\x01\x01"
