# ruff: noqa: E501
"""Demo-Only → Test log: a record of a test run, for Demo Day.

While a recording is on (a scenario name: "1 · via endpoints", "2 · UI only", ...), every change made to VENTHORA is
written down: each POST / PUT / PATCH / DELETE, whoever sent it (an API key, the dashboard, a vendor's public form, an
inbound email, the phone pay simulator), its answer and the numbers it produced (PR, PO, request, card, intake...).
Reads are not recorded. Each entry falls in a tab by what it touched (Setup, Funding, Procurement, Invoices & intake,
Payments, Advances & cards, Reports), so the progress of a run can be followed step by step and exported.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

KIND, SESSION = "testlog", "testlog_session"
TABS = [  # (tab, path prefixes), first match wins
    ("Setup", ("/v1/org", "/v1/settings", "/v1/airwallex/connection", "/v1/airwallex/accounts", "/v1/airwallex/acting", "/v1/mandate", "/v1/doa",
               "/v1/priority-sop", "/v1/edition", "/v1/webhooks/register", "/v1/developer", "/v1/sources", "/v1/beneficiaries", "/v1/payees",
               "/v1/cards/issuing-account", "/v1/cards/cardholder", "/v1/card-authorization", "/v1/outbox/smtp", "/v1/budget", "/v1/reporting/config",
               "/v1/visa", "/v1/metal")),
    ("Funding", ("/api/sandbox/deposits", "/v1/airwallex/global-accounts", "/v1/treasury", "/v1/cash-placement")),
    ("Procurement", ("/v1/procurement", "/v1/purchase-requests", "/rfq/", "/vendor-details/")),
    ("Continuity", ("/v1/continuity",)),
    ("Invoices & intake", ("/v1/intake", "/webhooks/email", "/v1/exceptions")),
    ("Payments", ("/v1/agent-plan", "/v1/executions", "/v1/plans", "/v1/payment", "/v1/fund-requests/{n}/pay")),
    ("Advances & cards", ("/v1/fund-requests", "/v1/cards", "/pay-sim/", "/card-link/")),
    ("Reports", ("/v1/reports", "/v1/reporting", "/v1/metal/anchor")),
]
# never recorded: the log itself, the live stream, and the mail provider's delivery notices (one per email sent: noise, not a step of the run)
SKIP = ("/v1/demo/testlog", "/api/stream", "/webhooks/ses")
SUMMARY_KEYS = ("pr_number", "po_id", "request_number", "intake_id", "invoice_no", "status", "decision", "verdict", "kind", "masked", "card_masked",
                "vendor", "vendor_id", "cand_id", "account_id", "cardholder_id", "id", "action", "sent", "created", "submitted", "settled_minor",
                "amount_minor", "currency", "flags", "detail", "message")


def session(org) -> dict:
    return org.get(SESSION, "session") or {"active": False}


def start(org, scenario: str, by: str) -> dict:
    rec = {"_id": "session", "active": True, "scenario": scenario.strip()[:80] or "test", "started_at": _now(), "started_by": by, "run": uuid.uuid4().hex[:8]}
    org._put(SESSION, rec, by)
    return rec


def stop(org, by: str) -> dict:
    rec = session(org) | {"active": False, "stopped_at": _now(), "stopped_by": by}
    org._put(SESSION, rec | {"_id": "session"}, by)
    return rec


def tab_of(method: str, path: str) -> str:
    if path.startswith("/v1/fund-requests/") and path.endswith("/pay"):
        return "Payments"
    if path.startswith("/v1/cards/") and path.endswith("/simulate"):
        return "Advances & cards"
    for tab, prefixes in TABS:
        if path.startswith(prefixes):
            return tab
    return "Other"


def channel_of(path: str, headers, role: str | None) -> str:
    if path.startswith(("/rfq/", "/vendor-details/")):
        return "vendor's form"
    if path.startswith("/card-link/"):
        return "card link"
    if path.startswith("/pay-sim/"):
        return "phone pay simulator"
    if path in ("/webhooks/email", "/v1/intake/emails"):
        return "email"
    if (headers.get("authorization") or "").lower().startswith("bearer") or headers.get("x-api-key"):
        return "API key"
    if headers.get("x-venthora-role"):
        return "dashboard"
    return "endpoint" if role else "endpoint (no role)"


def summary(body: bytes, ctype: str) -> str:
    if "json" not in (ctype or "") or not body:
        return ""
    try:
        d = json.loads(body[:200_000])
    except ValueError:
        return ""
    if isinstance(d, list):
        return f"{len(d)} item(s)"
    if not isinstance(d, dict):
        return str(d)[:160]
    parts = []
    for k in SUMMARY_KEYS:
        v = d.get(k)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            v = v.get("message") or json.dumps(v)[:80]
        if isinstance(v, list):
            v = ", ".join(map(str, v[:4])) + ("…" if len(v) > 4 else "")
        if k == "intake_id":
            v = str(v)[:8]
        parts.append(f"{k} {v}")
    for k in ("card", "chain", "po"):  # the nested results that matter on Demo Day
        x = d.get(k)
        if isinstance(x, dict):
            parts.append(f"{k} " + " ".join(f"{a}={x[a]}" for a in ("masked", "po_id", "sealed", "head", "status") if x.get(a) not in (None, "")) [:120])
    return " · ".join(parts)[:400]


def record(org, *, method: str, path: str, query: str, status: int, ms: int, role: str | None, by: str | None, channel: str, body: bytes, ctype: str,
           req_body: bytes = b"") -> None:
    from . import processlog

    s = session(org)
    if not s.get("active"):
        return
    if path.startswith(("/approve/", "/rfq/", "/vendor-details/", "/v1/auth/invite/", "/card-link/")):  # a link's token is its key: never written down
        path = path.rsplit("/", 1)[0] + "/••••" if path.count("/") >= 2 else path
    local, utc = processlog.local_now(org)
    req = processlog.scrub(processlog.parse(req_body))
    resp = processlog.parse(body) if "json" in (ctype or "") else {}
    try:
        line = own_line(method, path, req, resp, status) or processlog.describe(method, path, req, resp, status)
    except Exception:  # noqa: BLE001 - a sentence that cannot be made never loses the entry
        line = f"{processlog.page_of(path)} · {method} {path}"
    entry = {"_id": f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:6]}", "run": s.get("run"), "scenario": s.get("scenario"), "at": utc,
             "local_at": local, "tab": tab_of(method, path), "method": method, "path": path, "query": _clean_query(query), "status": status, "ms": ms,
             "actor": f"{by} ({role})" if by and role else (role or by or "-"), "channel": channel, "result": summary(body, ctype), "line": line}
    org._put(KIND, entry, "test log")


def _filed(resp: dict) -> str | None:
    flag = next((x for x in resp.get("flags") or [] if str(x).startswith(("purchase_request", "fund_request"))), None)
    if not flag:
        return None
    what, _, rest = str(flag).partition(":")
    words = what.replace("_", " ")
    if what.endswith("rejected") or what.endswith("refused"):
        return f"Email · {words}: {rest.strip()[:140]}"
    return f"Email · became {words} {rest.strip()}"


def own_line(method: str, path: str, req: dict, resp: dict, status: int) -> str | None:
    """The sentence for the steps a general description says too little about: an email that became a request, the
    procurement decision, an advance's allocation and card, safe mode. None: the general description is used."""
    if status >= 400 or not isinstance(resp, dict):
        return None
    money = lambda m, c: f"{c} {m / 100:,.2f}"  # noqa: E731
    if path in ("/v1/intake/emails", "/webhooks/email"):
        return _filed(resp)
    if path.startswith("/v1/procurement/") and path.endswith("/demo-replies"):
        d = resp.get("decision") or {}
        n = path.split("/")[3]
        tail = {"award": f"awarded to {d.get('vendor')} · {d.get('po_id')}", "person": "needs a person: " + str(d.get("reason", ""))[:120],
                "wait": "waiting: " + str(d.get("reason", ""))[:100]}.get(d.get("action"), "")
        return f"Procurement · {n}: {len(resp.get('submitted') or [])} demo vendor(s) sent a quotation" + (f"; {tail}" if tail else "")
    if path.startswith("/v1/procurement/") and path.endswith("/award"):
        return f"Procurement · {path.split('/')[3]} awarded to {resp.get('vendor')} · {resp.get('po_id')} · {money(resp.get('total_minor', 0), resp.get('currency', ''))}"
    if path == "/v1/procurement/settings":
        return f"Procurement · PO threshold set to {money(resp.get('po_threshold_minor', 0), resp.get('po_threshold_currency', ''))}"
    if path.startswith("/vendor-details/"):
        inv = resp.get("invoice") or {}
        return f"Vendor's form · details received, bank check {resp.get('bank_check') or '-'}" + (f"; invoice {inv.get('status')}" if inv else "")
    if path.startswith("/v1/fund-requests/") and path.endswith("/cancel"):
        return f"Cash advance · {path.split('/')[3]} cancelled before it was paid"
    if path.startswith("/v1/fund-requests/") and path.endswith("/card"):
        card = resp.get("card") or {}
        return f"Cash advance · {path.split('/')[3]} paid on card {str(card.get('masked') or resp.get('card_masked') or '')[-4:]} · {money(resp.get('amount_minor', 0), resp.get('currency', ''))}"
    if path.startswith("/v1/fund-requests/") and path.endswith("/decision"):
        a = resp.get("allocation") or {}
        return (f"Cash advance · {path.split('/')[3]} {str(resp.get('status', '')).lower()}"
                + (f"; allocated, paid at {str(a.get('pay_at'))[:16].replace('T', ' ')} UTC" if a.get("status") == "ALLOCATED" else ""))
    if path == "/v1/continuity/safe-mode":
        return f"Continuity · safe mode {'ON: ' + str(resp.get('reason', '')) if resp.get('on') else 'lifted'}"
    if path == "/v1/continuity/takeover":
        return f"Continuity · {'took over as the writer' if resp.get('took_over') else 'not taken over: ' + str(resp.get('reason', ''))[:120]}"
    if path.startswith("/card-link/"):
        return "Card link · opened with the authenticator code"
    return None


def _clean_query(q: str) -> str:
    keep = [p for p in (q or "").split("&") if p and not p.lower().startswith(("key=", "token=", "secret=", "password=", "k="))]
    return "&".join(keep)[:200]


def entries(org, scenario: str | None = None, tab: str | None = None, limit: int = 2000) -> list[dict]:
    rows = sorted(org.list(KIND), key=lambda r: r["_id"])
    rows = [r for r in rows if (not scenario or r.get("scenario") == scenario) and (not tab or r.get("tab") == tab)]
    return rows[-limit:]


def clear(org, scenario: str | None = None) -> int:
    n = 0
    for r in org.list(KIND):
        if not scenario or r.get("scenario") == scenario:
            _delete(org, r["_id"])
            n += 1
    return n


def _delete(org, rid: str) -> None:
    with org._lock, org._connect() as c:
        c.execute("DELETE FROM records WHERE kind = ? AND record_id = ?", (KIND, rid))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
