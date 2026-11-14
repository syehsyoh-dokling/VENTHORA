# ruff: noqa: E501
"""The process log: one plain sentence per action, for the report that goes with a recorded run.

The test log (testlog.py) records every change while a recording is on; this module turns each one into a line a
reader understands, e.g. "Provider selected: Anthropic; model: claude-opus-5-5; key: ••••a1B2", with the actual
local date and time of the company. Secrets never appear: any field whose name says key, secret, token, password or
value is shown as its last four characters only. Things that happen without a click (a payment run, a posting to
T:0) are added with note().
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

SECRET = re.compile(r"(key|secret|token|password|passwd|value|private|credential)", re.I)
PAGES = [("/v1/settings/ai", "Agent (AI & Model)"), ("/v1/settings/email", "Email Sync"), ("/v1/outbox/smtp", "Email Sync · sender"),
         ("/v1/funding-source", "Airwallex Global Account"), ("/v1/airwallex", "Airwallex Global Account"), ("/v1/sources/connectors", "App Integration · Sync API"),
         ("/v1/sources/uploads", "Dataset upload"), ("/v1/developer/keys", "App Integration · API access"), ("/v1/mandate", "Delegation of Authority · treasury limits"),
         ("/v1/doa", "Delegation of Authority"), ("/v1/priority-sop", "Standard Payment Priority Policy"), ("/v1/standards", "Finance standards"),
         ("/v1/finance-standards", "Finance standards"), ("/v1/cash-placement", "Placement providers"), ("/v1/placement", "Placement providers"),
         ("/v1/budget", "Period, cut-off & sync"), ("/v1/reporting/config", "Period, cut-off & sync"), ("/v1/doc-rules", "Document validation"),
         ("/v1/org/prices", "Unit prices"), ("/v1/org/users", "People & roles"), ("/v1/org/units", "Branches & units"), ("/v1/org", "Organisation"),
         ("/v1/payees", "Payee lifecycle"), ("/v1/beneficiaries", "Vendors"), ("/v1/intake", "Intake"), ("/v1/agent-plan", "Agent plan"),
         ("/api/sandbox/deposits", "Sandbox deposit"), ("/v1/t0", "Books & ledger · T:0"), ("/v1/metal", "Alta (blockchain)"), ("/v1/cards", "Cards"),
         ("/v1/visa", "Visa"), ("/v1/treasury", "Treasury"), ("/v1/demo/simulate", "Agent plan · simulation"), ("/v1/demo", "Demo"), ("/v1/auth", "Sign-in"), ("/login", "Sign-in"), ("/webhooks/airwallex", "Airwallex webhook"), ("/approve/", "FCO email approval"), ("/v1/fco-notify", "Notices to the FCO")]


def mask(v) -> str:
    s = str(v or "")
    return "" if not s else ("••••" + s[-4:] if len(s) >= 8 else "••••")


PASSWORD = re.compile(r"(password|passwd|pass_?code|pin|otp)", re.I)


def scrub(d, depth: int = 0):
    """The request with every secret masked (keys named like secrets), long lists shortened. A password shows nothing
    of itself at all; a key or token shows its last four characters so a person can tell which one was used."""
    if isinstance(d, dict):
        return {k: ("••••" if PASSWORD.search(str(k)) and v not in ("", None) else
                    mask(v) if SECRET.search(str(k)) and isinstance(v, (str, int)) and v not in ("", None) else scrub(v, depth + 1)) for k, v in d.items()}
    if isinstance(d, list):
        return [scrub(x, depth + 1) for x in d[:6]] + (["…"] if len(d) > 6 else [])
    return d


def page_of(path: str) -> str:
    return next((name for prefix, name in PAGES if path.startswith(prefix)), path)


def _money(minor, ccy="") -> str:
    try:
        return f"{ccy + ' ' if ccy else ''}{int(minor) / 100:,.2f}"
    except (TypeError, ValueError):
        return str(minor)


def _kv(d: dict, limit: int = 6) -> str:
    out = []
    for k, v in d.items():
        if v in (None, "", [], {}) or isinstance(v, (dict, list)) and len(json.dumps(v)) > 80:
            continue
        out.append(f"{k}: {v if not isinstance(v, (dict, list)) else json.dumps(v)}")
        if len(out) >= limit:
            break
    return "; ".join(out)


def describe(method: str, path: str, req: dict, resp: dict, status: int) -> str:
    """One sentence for one action; the request is already scrubbed."""
    p, r = req or {}, resp if isinstance(resp, dict) else {}
    line = None
    if path in ("/v1/auth/login", "/login/api"):
        line = f"Signed in: {p.get('email') or p.get('username') or '-'}" if status < 300 else f"Sign-in refused for {p.get('email') or '-'}"
    elif path == "/v1/auth/platform-lookup":
        line = f"Company looked up for sign-in: {p.get('email') or '-'}"
    elif path == "/v1/fco-notify" and method == "PUT":
        line = (f"Notices to the FCO saved: {'on' if p.get('enabled', True) else 'off'} to {p.get('to') or '-'}; needs a person: {'yes' if p.get('needs_review', True) else 'no'}; "
                f"first payment with approve link: {'yes' if p.get('first_payment', True) else 'no'}; at most {p.get('max_per_hour', 20)} per hour")
    elif path.startswith("/v1/fco-notify/send/"):
        line = f"Email to the FCO sent on request: {r.get('subject') or '-'} · {r.get('status') or r.get('detail')}"
    elif path.startswith("/approve/"):
        line = "Approve link from the FCO's email " + ("opened" if method == "GET" else "used")
    elif path == "/v1/platform/mail-oauth":
        line = "Platform pushed the mailbox sign-in settings to this company (" + ", ".join(k for k in ("outlook", "gmail", "yahoo") if k in p) + ")"
    elif path.startswith("/v1/auth/"):
        line = f"Sign-in step {path.rsplit('/', 1)[-1]}" + (f": {p.get('email')}" if p.get("email") else "")
    elif path.startswith("/webhooks/airwallex"):
        data = p.get("data") if isinstance(p.get("data"), dict) else {}
        obj = data.get("object") if isinstance(data.get("object"), dict) else data
        amt = obj.get("amount")
        line = (f"Airwallex event {p.get('name') or p.get('type') or '-'}"
                + (f": {obj.get('currency', '')} {amt:,.2f}" if isinstance(amt, (int, float)) else "")
                + (f" · {obj.get('status')}" if obj.get("status") else "") + (f" · id {str(obj.get('id') or p.get('id'))[:12]}" if obj.get("id") or p.get("id") else ""))
    elif path == "/v1/settings/ai" and method == "PUT":
        prov, sh = p.get("provider") or {}, p.get("self_hosted") or {}
        line = (f"Provider selected: {prov.get('name') or '-'}; model: {prov.get('model') or '-'}; key: {prov.get('api_key') or 'saved key (••••)'}" if p.get("mode") == "provider"
                else f"Self-hosted model selected: {sh.get('model') or '-'} at {sh.get('endpoint') or '-'}" if p.get("mode") == "self_hosted" else "AI switched off: rules only")
    elif path == "/v1/settings/ai/test":
        tk = r.get("tokens") or {}
        line = (f"AI connection test: {r.get('model')} answered “{r.get('reply')}” in {(r.get('latency_ms') or 0) / 1000:.1f} s"
                f" ({tk.get('input', 0)} tokens in / {tk.get('output', 0)} out; answered by {r.get('answered_by') or r.get('model')})" if r.get("ok")
                else f"AI connection test failed: {r.get('error')}")
    elif path.startswith("/v1/settings/ai/keys/"):
        line = f"API key saved for {path.rsplit('/', 1)[-1]}: {p.get('api_key') or '••••'}"
    elif path == "/v1/funding-source/check":
        line = f"Airwallex account check: account no. {mask(p.get('account_number'))}, {p.get('account_name') or ''}: " + ("accepted" if status < 300 and r.get("ok", True) else f"refused: {r.get('detail') or r.get('message') or r.get('error')}")
    elif path == "/v1/funding-source" and method == "PUT":
        line = f"Funding account saved and new Airwallex account requested: {p.get('account_name') or ''} ({p.get('currency') or ''})"
    elif re.fullmatch(r"/v1/settings/email/inboxes/[^/]+/check", path):
        line = f"Mailbox test connect: {p.get('address')} ({p.get('provider')}): " + (f"signed in, {r.get('unread')} unread" if status < 300 else f"refused: {r.get('detail')}")
    elif re.fullmatch(r"/v1/settings/email/inboxes/[^/]+/(test|fetch)", path):
        line = f"Mailbox {path.rsplit('/', 1)[-1]}: " + (f"{r.get('fetched', r.get('unread'))} new / unread" if status < 300 else f"failed: {r.get('detail')}")
    elif re.fullmatch(r"/v1/settings/email/inboxes/[^/]+", path) and method == "PUT":
        line = f"Mailbox saved: {p.get('address')} ({p.get('provider')}, sign-in {p.get('auth')}, reads {p.get('since_days', 7) or 'all'} days back)"
    elif path == "/v1/settings/email/oauth/start":
        line = f"Mailbox sign-in started with {p.get('provider')}: client ID {mask(p.get('client_id'))}; tenant {p.get('tenant_id') or 'any'}"
    elif path in ("/v1/outbox/smtp", "/v1/outbox/smtp/try"):
        line = f"Sender {'tested' if path.endswith('try') else 'saved'}: {p.get('sender')} via {p.get('provider')}" + (f": {r.get('detail')}" if path.endswith("try") else "")
    elif path == "/v1/settings/email/policy":
        line = f"Instructions by email: {'on' if p.get('orders_by_email') else 'off'}; whole domains: {', '.join(p.get('order_domains') or []) or 'none'}; sender authentication {'required' if p.get('require_authentication') else 'not required'}"
    elif path == "/v1/sources/connectors/template/erpnext":
        line = f"ERPNext connected: {p.get('site_url')}; key {p.get('api_key') or 'saved'}: {(r.get('check') or {}).get('detail', '')}"
    elif re.fullmatch(r"/v1/sources/connectors/[^/]+/sync", path):
        reps = r.get("reports") or {}
        line = f"Sync from {path.split('/')[4]}{' (dry run)' if r.get('dry_run') else ''}: " + "; ".join(
            f"{k} +{len(v.get('created') or [])}/~{len(v.get('updated') or [])}/x{len(v.get('rejected') or [])}" for k, v in reps.items() if isinstance(v, dict))
    elif path == "/api/sandbox/deposits":
        line = f"Sandbox deposit: {_money(p.get('amount_minor'), 'USD')} from {p.get('payer_name')} ({r.get('status')})"
    elif path == "/v1/t0/books/post":
        po, ve = r.get("post") or {}, r.get("verify") or {}
        line = f"Journal posted to T:0 ({p.get('scope')}): {len(po.get('posted') or [])} posted, {len(po.get('refused') or [])} refused; verified {ve.get('passed', 0)}/{ve.get('checked', 0)}"
    elif path == "/v1/t0/books/verify":
        line = f"T:0 journal verification: {r.get('passed')} of {r.get('checked')} entries match"
    elif re.fullmatch(r"/v1/intake/requests/[^/]+/pay", path):
        line = f"Approved and paid from Pending payment: {'paid' if r.get('paid') else 'not paid'}: {r.get('message')}"
    elif re.fullmatch(r"/v1/payees/[^/]+/callback", path) and method == "POST":
        fixed = ", ".join(sorted((p.get("corrections") or {}).keys()))
        line = (f"Call-back to {path.split('/')[3]} on {p.get('called_number')} by {p.get('by')}: payee said {p.get('outcome')}"
                + (f" (corrected: {fixed})" if fixed else "") + (f"; {'; '.join(r.get('steps') or [])}" if status < 300 else "")
                + (f"; status now {(r.get('payee') or {}).get('status')}" if status < 300 else ""))
    elif re.fullmatch(r"/v1/payees/[^/]+/verify", path):
        line = f"Payee call-back on {p.get('called_number')} by {p.get('by')}: {path.split('/')[3]} {r.get('status') or r.get('detail')}"
    elif path == "/v1/demo/simulate-invoices":
        line = (f"Invoice simulation {r.get('run')} started: {r.get('count')} supplier emails from {', '.join(r.get('vendors') or [])}; "
                f"re-plan every {r.get('batch')} emails; the first {p.get('ai_sample', 2)} read by the AI model") if status < 300 else "Invoice simulation not started"
    elif path == "/v1/metal/anchor-payments":
        line = f"Payments anchored on Alta now: {len(r.get('anchored') or [])} anchored, {len(r.get('failed') or [])} not yet (from USD {(r.get('threshold_minor') or 0) / 100:,.2f})"
    elif path == "/v1/priority-sop" and method == "PUT":
        pol = p.get("policy") or {}
        times = pol.get("run_times") or []
        line = (f"Policy saved: {'high priority first' if pol.get('mode') == 'priority' else 'first in, first out (real time)'}"
                f"{', plan ' + str(pol.get('horizon_hours')) + ' h' if pol.get('mode') == 'priority' else ''}; approved payments run "
                + (f"at {', '.join(times)} ({'Monday to Friday' if pol.get('run_days') == 'weekdays' else 'every day'})" if times else "every five minutes")
                + "; rules: " + (", ".join(k.replace("_", " ") for k in ("deemed_approval", "pay_what_fits", "notify_fco", "cascade", "fx_at_plan") if pol.get(k)) or "none")
                + f"; late cost moves up: {'yes' if p.get('late_cost', True) else 'no'}")
    elif path == "/v1/mandate" and method == "PUT":
        bk = p.get("book") or {}
        first = "; ".join(f"{lab}: {'USD ' + format(bk[k] / 100, ',.2f') if bk[k] else 'off'}" for k, lab in
                          (("card_max_minor", "virtual card up to"), ("anchor_from_minor", "anchor each payment on Alta from")) if k in bk)
        vals = p.get("values") or {}
        line = "Delegation of Authority saved: " + "; ".join(x for x in (
            f"agent pays alone up to USD {vals['pay_single'] / 100:,.2f} a payment" if isinstance(vals.get("pay_single"), int) else "", first,
            _kv({k: v for k, v in {**vals, **bk}.items() if k not in ("pay_single", "card_max_minor", "anchor_from_minor")}, 6)) if x)
    if line is None:
        verb = {"POST": "action", "PUT": "saved", "PATCH": "changed", "DELETE": "deleted"}.get(method, method)
        detail = _kv(p if isinstance(p, dict) else {})
        line = f"{verb}{': ' + detail if detail else ''}"
    if status >= 400 and "refused" not in line and "failed" not in line:
        why = r.get("detail") if isinstance(r.get("detail"), str) else json.dumps(r.get("detail"))[:200] if r.get("detail") else ""
        line += f" (refused: {why})" if why else f" (HTTP {status})"
    return f"{page_of(path)} · {line}"[:600]


def company_tz(org=None) -> str:
    """The company's reporting time zone; when that is left at UTC, the time zone of the head office."""
    tz = "UTC"
    try:
        from .reporting import load_config

        tz = load_config().timezone or "UTC"
        if tz == "UTC" and org is not None:
            units = org.list("unit")
            hq = next((u for u in units if u.get("kind") == "HQ"), units[0] if units else {})
            tz = hq.get("timezone") or "UTC"
    except Exception:  # noqa: BLE001
        pass
    return tz


def local_now(org=None) -> tuple[str, str]:
    """(local date-time with the zone's name, UTC) of this moment, in the company's time zone."""
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    tz = company_tz(org)
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        local = now
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {local.tzname() or tz}", now.isoformat(timespec="seconds")


def parse(body: bytes) -> dict:
    try:
        d = json.loads(body[:400_000]) if body else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {"items": len(d)} if isinstance(d, list) else {}


def note(org, tab: str, line: str, actor: str = "VENTHORA (agent)") -> None:
    """A step nobody clicked (a payment run, a posting): written to the log while a recording is on."""
    from . import testlog

    s = testlog.session(org)
    if not s.get("active"):
        return
    import time
    import uuid

    local, utc = local_now(org)
    org._put(testlog.KIND, {"_id": f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:6]}", "run": s.get("run"), "scenario": s.get("scenario"), "at": utc,
                            "local_at": local, "tab": tab, "method": "AUTO", "path": "", "query": "", "status": 200, "ms": 0, "actor": actor,
                            "channel": "background", "result": "", "line": line[:600]}, "process log")
