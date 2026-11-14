# ruff: noqa: E501
"""HTTP API. Run: uvicorn venthora.api:app --reload"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import __version__
from .audit import AuditStore
from .config import ROOT, env, load_env

load_env()

from pydantic import BaseModel, Field  # noqa: E402

from . import ai_settings, apiaccess, apidocs, bankcheck, webhooks  # noqa: E402
from . import live as live_session  # noqa: E402
from .aging import evaluate as aging_evaluate  # noqa: E402
from .ai_settings import active_model  # noqa: E402
from .domain.models import PlanResult, TreasuryState  # noqa: E402
from .engines.llm_planner import llm_proposal  # noqa: E402
from .execution.executor import Executor  # noqa: E402
from .execution.facts import apply_operational_facts  # noqa: E402
from .execution.incidents import IncidentCommander, IncidentRequest, IncidentResult  # noqa: E402
from .execution.live import with_live_balances_and_rates  # noqa: E402
from .execution.models import ExecutionRequest, ExecutionResult  # noqa: E402
from .execution.store import ExecutionStore  # noqa: E402
from .gateways.airwallex import AirwallexError  # noqa: E402
from .intake.models import IntakeRequest, ReviewRequest  # noqa: E402
from .intake.pipeline import Intake  # noqa: E402
from .ledger import adjust as ledger_adjust_entry  # noqa: E402
from .ledger import evaluate as ledger_evaluate  # noqa: E402
from .orchestrator import Orchestrator  # noqa: E402
from .organisation import (  # noqa: E402
    ID_FIELD,
    WRITE_ROLES,
    Account,
    Allocation,
    Engagement,
    FundRequest,
    Mailbox,
    OrgError,
    OrgStore,
    Price,
    Project,
    PurchaseOrder,
    PurchaseRequest,
    Receipt,
    Standards,
    TravelAuthorisation,
    TravelRate,
    Unit,
    User,
)
from .payees import PayeeError  # noqa: E402
from .payees import evaluate as payees_evaluate  # noqa: E402
from .proofcore.approvals import evaluate as approvals_evaluate  # noqa: E402
from .proofcore.budget import attach_budget, cutoff_report, load_budget  # noqa: E402
from .reporting import (  # noqa: E402
    ReportingConfig,
    period_report,
    reporting_period,  # noqa: E402
)
from .reporting import evaluate as reporting_evaluate  # noqa: E402
from .reporting import load_config as load_reporting_config  # noqa: E402
from .reporting import save_config as save_reporting_config  # noqa: E402
from .tenancy import evaluate as tenancy_evaluate  # noqa: E402
from .tenancy import load_registry as load_tenant_registry  # noqa: E402
from .tenancy import resolve as resolve_tenancy  # noqa: E402
from .testing.groundtruth import book_fingerprints, compare, load_cases, run_case, view_of  # noqa: E402

SCENARIOS = ROOT / "data" / "scenarios"
DASHBOARD = ROOT / "src" / "venthora" / "dashboard" / "index.html"
RUNS = ROOT / "data" / "runs"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.agent = Orchestrator()
    app.state.audit = AuditStore()
    app.state.executor = Executor(app.state.agent, ExecutionStore(), app.state.audit)
    app.state.executor.bank_gate = lambda pid, payload: bankcheck.passed(app.state.org, pid, payload)
    app.state.org = OrgStore()
    app.state.executor.org = app.state.org  # payments with a charge code are held to their budget allocation
    from . import prompt_system
    from .evidence import llm as _llm

    _llm.FRAMER = lambda messages, org=app.state.org: prompt_system.frame(org, messages)  # every model call carries the system prompt
    app.state.executor.on_result = _execution_done
    app.state.keys = apiaccess.KeyStore()
    app.state.intake = Intake(org=app.state.org, payments=app.state.executor.store)  # staff claims and POs use the org settings
    app.state.webhooks = webhooks.WebhookStore()
    app.state.hub = webhooks.Hub()
    await asyncio.to_thread(_warm_horizon)
    pending = asyncio.create_task(webhooks.confirm_pending(app.state.webhooks, app.state.agent.proof.airwallex, app.state.hub.publish))
    scheduler = asyncio.create_task(_budget_scheduler()) if env("VENTHORA_BUDGET_SCHEDULER", "1") != "0" else None
    syncer = asyncio.create_task(_sync_scheduler()) if env("VENTHORA_SYNC_SCHEDULER", "1") != "0" else None
    doa_pilot = asyncio.create_task(_doa_scheduler()) if env("VENTHORA_DOA_SCHEDULER", "1") != "0" else None
    mailer = asyncio.create_task(_outbox_worker()) if env("VENTHORA_OUTBOX_WORKER", "1") != "0" else None
    books = asyncio.create_task(_t0_worker()) if env("VENTHORA_T0_AUTOPOST", "1") != "0" else None
    windows = asyncio.create_task(_card_window_worker()) if env("VENTHORA_CARD_WINDOWS", "1") != "0" else None
    yield
    _llm.FRAMER = None  # the organisation's prompt leaves with the app
    for task in (pending, scheduler, syncer, doa_pilot, mailer, books, windows):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await app.state.agent.close()


async def _outbox_worker(every_s: int = 60) -> None:
    """The outbox's worker: every minute, once outgoing email is set up, send what waits in the queue and retry what
    failed (at most 5 tries); the audit log records each message sent."""
    from . import outbox

    while True:
        await asyncio.sleep(every_s)
        with contextlib.suppress(Exception):  # a mail server that is down only leaves the messages queued
            for rec in await asyncio.to_thread(outbox.flush, app.state.org):
                await asyncio.to_thread(app.state.audit.append, "outbox_" + rec["status"], {"outbox_id": rec["_id"], "ref": rec.get("ref"),
                                                                                           "to": rec.get("to"), "attempts": rec.get("attempts")})


async def _t0_worker(every_s: int = 300) -> None:
    """The books' worker: every five minutes, once T:0 is connected (T0_API_KEY), post the money that moved and is
    not in T:0 yet as journal entries (once each: T:0's idempotency key), then read them back and verify. A T:0 that is
    down only leaves the entries pending; the next round posts them."""
    from . import t0_books

    while True:
        await asyncio.sleep(every_s)
        if not env("T0_API_KEY"):
            continue
        with contextlib.suppress(Exception):
            entries = await asyncio.to_thread(app.state.executor.store.journal, None)
            rep = await t0_books.post(app.state.org, entries, kinds=t0_books.PAID_KINDS)
            if rep["posted"] or rep["refused"]:
                ver = await t0_books.verify(app.state.org, only_unverified=True)
                await asyncio.to_thread(app.state.audit.append, "t0_journal_posted", {"by": "VENTHORA (books worker)", "scope": "paid",
                                                                                      "posted": len(rep["posted"]), "refused": len(rep["refused"]),
                                                                                      "verified": ver.get("passed")})


def _warm_horizon() -> None:
    """Compute Horizon's daily FX stress once at startup so requests stay in milliseconds."""
    from .data.ecb import ecb_path
    from .engines.horizon_m0 import fx_stress_ppm

    if ecb_path().is_file():
        for ccy in ("EUR", "GBP"):
            with contextlib.suppress(Exception):  # best effort; the request path fails closed on its own
                fx_stress_ppm(ccy, "USD")


def _bank():
    bank = app.state.agent.proof.airwallex
    if not bank.configured:
        raise HTTPException(503, "Airwallex sandbox credentials are not configured")
    return bank


# a month in a URL path (2026-11); anything else is refused with 422 instead of failing inside the handler
PeriodPath = Annotated[str, PathParam(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


app = FastAPI(title="VENTHORA", version=__version__, lifespan=lifespan,
              description="Evidence-gated autonomous treasury agent on Airwallex. "
                          "AI proposes, independent engines verify, Airwallex executes.")


@app.middleware("http")
async def demo_only(request: Request, call_next):
    """Demo endpoints (demo data, sandbox simulations, the hackathon evidence, the Autopilot page) answer only while
    demo mode is on (Platform Setup → Setup Checklist); in the real version they are gone (404)."""
    from . import edition

    if edition.is_demo_path(request.url.path) and not await asyncio.to_thread(edition.demo_on, request.app.state.org):
        return JSONResponse({"detail": "not available: demo mode is off (Platform Setup → Setup Checklist)"}, status_code=404)
    return await call_next(request)


@app.middleware("http")
async def idempotency(request: Request, call_next):
    """A write that carries an Idempotency-Key is answered once: a retry with the same key and body gets the stored
    response (Idempotent-Replayed: true), the same key with another body is refused (422). Declared before
    api_keys, so it runs inside it: the key is already verified and the caller's role already replaced."""
    idem = (request.headers.get("idempotency-key") or "").strip()
    if not idem or request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return await call_next(request)
    if len(idem) > 255:
        return JSONResponse({"detail": "Idempotency-Key is longer than 255 characters"}, status_code=400)
    body = await request.body()
    owner = getattr(request.state, "api_key", None) or "local"
    fingerprint = hashlib.sha256(b"\n".join([request.method.encode(), request.url.path.encode(),
                                              request.scope.get("query_string", b""), body])).hexdigest()
    keys = request.app.state.keys
    state, stored = await asyncio.to_thread(keys.idem_begin, owner, idem, fingerprint)
    if state == "conflict":
        return JSONResponse({"detail": "this Idempotency-Key was already used with a different request"}, status_code=422)
    if state == "busy":
        return JSONResponse({"detail": "a request with this Idempotency-Key is still being processed"}, status_code=409)
    if state == "replay":
        return Response(stored["body"], status_code=stored["status"], media_type=stored["content_type"],
                        headers={"Idempotent-Replayed": "true"})
    try:
        response = await call_next(request)
        raw = b"".join([chunk async for chunk in response.body_iterator])
    except Exception:
        await asyncio.to_thread(keys.idem_drop, owner, idem)
        raise
    if response.status_code >= 500:
        await asyncio.to_thread(keys.idem_drop, owner, idem)
    else:
        await asyncio.to_thread(keys.idem_finish, owner, idem, response.status_code, raw,
                                response.headers.get("content-type", "application/json"))
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(raw, status_code=response.status_code, headers=headers)


@app.middleware("http")
async def menu_access(request: Request, call_next):
    """FCO Authorize, Platform Setup and the Developer Console by role (access.py). Declared before api_keys, so it
    runs inside it: an API key's role is already the request's role."""
    from . import access

    role = request.query_params.get("role") or request.headers.get("x-venthora-role") or None
    refused = access.check(role, request.method, request.url.path)
    if refused:
        return JSONResponse({"detail": refused[1]}, status_code=refused[0])
    return await call_next(request)


@app.middleware("http")
async def api_keys(request: Request, call_next):
    """Requests carrying an API key (Platform Setup → App Integration) are held to its scopes and act with its role.

    Requests without a key are the local dashboard; the gateway forwards the API to the internet only for
    requests that carry one (scripts/webhook_gateway.py --api)."""
    token = apiaccess.token_of(request.headers)
    if token is None:
        return await call_next(request)
    path, method = request.url.path, request.method
    if path.startswith(apiaccess.NO_KEY):
        return await call_next(request)  # the webhooks authenticate themselves
    keys = request.app.state.keys
    key = await asyncio.to_thread(keys.verify, token)
    if key is None:
        return JSONResponse({"detail": "invalid, expired or revoked API key"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
    started = time.perf_counter()

    async def logged(response):
        await asyncio.to_thread(keys.log, key["key_id"], method, path, response.status_code,
                                int((time.perf_counter() - started) * 1000), request.client.host if request.client else "")
        return response

    if path.startswith(apiaccess.NEVER_BY_KEY):
        return await logged(JSONResponse({"detail": "this endpoint is managed only from VENTHORA itself"}, status_code=403))
    if not path.startswith(apiaccess.OPEN_WITH_KEY):
        need = apiaccess.scope_for(method, path)
        if need is None:
            return await logged(JSONResponse({"detail": "this endpoint is not available to API keys"}, status_code=403))
        if need not in key["scopes"]:
            return await logged(JSONResponse({"detail": f"the key lacks the scope {need}"}, status_code=403))
    # the key's role and name replace whatever the caller sent
    q = [(k, v) for k, v in parse_qsl(request.scope.get("query_string", b"").decode()) if k not in ("role", "by")]
    q += [("role", key["role"]), ("by", f"api:{key['name']}")]
    request.scope["query_string"] = urlencode(q).encode()
    request.state.api_key = key["key_id"]
    return await logged(await call_next(request))


def _execution_done(result) -> None:
    """Every execution result reaches the dashboards at once: paid, refused or waiting for a person."""
    app.state.position_cache = None
    paid = sum(1 for st in result.steps if st.kind == "transfer" and (st.detail or {}).get("status") == "PAID")
    app.state.hub.publish("execution", {"execution_id": result.execution_id, "status": result.status, "message": result.message[:300],
                                        "paid": paid})


@app.middleware("http")
async def test_log(request: Request, call_next):
    """Demo-Only → Test log: while a recording is on, every change (POST/PUT/PATCH/DELETE) is written down with who sent
    it, its answer and the numbers it produced (testlog.py). Declared last, so it is the outermost: it sees each request
    as it came in, API keys and public forms included."""
    from . import testlog

    path, method = request.url.path, request.method
    if method not in ("POST", "PUT", "PATCH", "DELETE") or path.startswith(testlog.SKIP):
        return await call_next(request)
    org = request.app.state.org
    try:
        active = (await asyncio.to_thread(testlog.session, org)).get("active")
    except Exception:  # noqa: BLE001 - the log never stops a request
        active = False
    if not active:
        return await call_next(request)
    started = time.perf_counter()
    try:  # what was sent (secrets are masked before anything is kept): the process log's sentence needs it
        req_body = await request.body() if "json" in request.headers.get("content-type", "") else b""
    except Exception:  # noqa: BLE001
        req_body = b""
    response = await call_next(request)
    raw = b"".join([chunk async for chunk in response.body_iterator])
    role = request.query_params.get("role") or request.headers.get("x-venthora-role")
    with contextlib.suppress(Exception):
        await asyncio.to_thread(testlog.record, org, method=method, path=path, query=request.url.query, status=response.status_code,
                                ms=int((time.perf_counter() - started) * 1000), role=role, by=request.query_params.get("by"),
                                channel=testlog.channel_of(path, request.headers, role), body=raw, ctype=response.headers.get("content-type", ""),
                                req_body=req_body)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(raw, status_code=response.status_code, headers=headers)


# ---- Demo-Only → Test log ----
class TestLogStartIn(BaseModel):
    scenario: str = Field(min_length=1, max_length=80, description="e.g. '1 · via endpoints' or '2 · UI only'")


@app.get("/v1/demo/testlog")
async def demo_testlog(scenario: str | None = None, tab: str | None = None, limit: int = Query(2000, ge=1, le=10000)):
    from . import testlog

    org = app.state.org
    rows = await asyncio.to_thread(testlog.entries, org, scenario, None, limit)
    tabs = {t: 0 for t, _ in testlog.TABS} | {"Other": 0}
    for r in rows:
        tabs[r["tab"]] = tabs.get(r["tab"], 0) + 1
    scenarios = sorted({r.get("scenario") for r in await asyncio.to_thread(testlog.entries, org, None, None, 10000) if r.get("scenario")})
    return {"session": await asyncio.to_thread(testlog.session, org), "scenarios": scenarios, "tabs": tabs,
            "entries": [r for r in rows if not tab or r["tab"] == tab][::-1]}


@app.post("/v1/demo/testlog/start")
async def demo_testlog_start(body: TestLogStartIn, role: str = "super_admin", by: str = "dashboard"):
    from . import testlog

    rec = await asyncio.to_thread(testlog.start, app.state.org, body.scenario, f"{by} ({role})")
    await asyncio.to_thread(app.state.audit.append, "testlog_started", {"scenario": rec["scenario"], "by": f"{by} ({role})"})
    return rec


@app.post("/v1/demo/testlog/stop")
async def demo_testlog_stop(role: str = "super_admin", by: str = "dashboard"):
    from . import testlog

    rec = await asyncio.to_thread(testlog.stop, app.state.org, f"{by} ({role})")
    await asyncio.to_thread(app.state.audit.append, "testlog_stopped", {"scenario": rec.get("scenario"), "by": f"{by} ({role})"})
    return rec


@app.delete("/v1/demo/testlog")
async def demo_testlog_clear(scenario: str | None = None, role: str = "super_admin", by: str = "dashboard"):
    from . import testlog

    if role != "super_admin":
        raise HTTPException(403, "only the FCO (Super admin) clears the test log")
    return {"deleted": await asyncio.to_thread(testlog.clear, app.state.org, scenario)}


@app.get("/v1/demo/testlog.html", include_in_schema=False)
async def demo_testlog_html(scenario: str | None = None):
    """The process log as a page to read, print or save as PDF: one sentence per step, local time, who, the result."""
    import html as _h

    from . import testlog

    rows = await asyncio.to_thread(testlog.entries, app.state.org, scenario, None, 10000)
    s = await asyncio.to_thread(testlog.session, app.state.org)
    ok = sum(1 for r in rows if r["status"] < 400)
    body = "".join(
        f"<tr><td>{i}</td><td class=t>{_h.escape(r.get('local_at') or r['at'])}</td><td>{_h.escape(r['tab'])}</td>"
        f"<td>{_h.escape(r.get('line') or r['method'] + ' ' + r['path'])}</td><td>{_h.escape(r['actor'])}</td>"
        f"<td class={'ok' if r['status'] < 400 else 'bad'}>{r['status']}</td></tr>" for i, r in enumerate(rows, 1))
    page = f"""<!doctype html><meta charset=utf-8><title>VENTHORA process log</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;margin:28px;color:#16213a}}h1{{font-size:20px;margin:0 0 4px}}.m{{color:#667;margin-bottom:14px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #e3e7ef;padding:6px 8px;text-align:left;vertical-align:top}}th{{background:#f3f6fb}}
.t{{white-space:nowrap;font-variant-numeric:tabular-nums}}.ok{{color:#11823b}}.bad{{color:#c0312b;font-weight:600}}</style>
<h1>VENTHORA · process log{' · ' + _h.escape(scenario) if scenario else ''}</h1>
<div class=m>{len(rows)} steps · {ok} succeeded · {len(rows) - ok} refused or failed · recording {'on' if s.get('active') else 'off'}
{(' · started ' + _h.escape(str(s.get('started_at')))) if s.get('started_at') else ''}{(' · stopped ' + _h.escape(str(s.get('stopped_at')))) if s.get('stopped_at') else ''}
· keys and secrets show only their last four characters</div>
<table><tr><th>#</th><th>Local time</th><th>Stage</th><th>What happened</th><th>Who</th><th>Result</th></tr>{body}</table>"""
    return HTMLResponse(page)


@app.get("/v1/demo/testlog.csv", include_in_schema=False)
async def demo_testlog_csv(scenario: str | None = None):
    import csv
    import io

    from . import testlog

    rows = await asyncio.to_thread(testlog.entries, app.state.org, scenario, None, 10000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["local time", "utc", "scenario", "stage", "what happened", "actor", "channel", "method", "path", "status", "ms", "result"])
    for r in rows:
        w.writerow([r.get("local_at") or "", r["at"], r.get("scenario"), r["tab"], r.get("line") or "", r["actor"], r["channel"], r["method"],
                    r["path"] + (f"?{r['query']}" if r.get("query") else ""), r["status"], r["ms"], r["result"]])
    name = f"venthora-test-log-{(scenario or 'all').replace(' ', '_').replace('·', '')}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _scenario_file(scenario_id: str):
    path = SCENARIOS / f"{scenario_id}.json"
    if not scenario_id.replace("_", "").replace("-", "").isalnum() or not path.is_file():
        raise HTTPException(404, "unknown scenario")
    return path


@app.get("/", include_in_schema=False)
async def root():
    # no-cache: the browser asks again each load (a 304 when unchanged), so a new menu or page shows on a plain refresh
    return FileResponse(DASHBOARD, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return {"status": "ok", "version": __version__, "utc": datetime.now(UTC).isoformat()}


@app.get("/api/status")
async def status():
    proof = app.state.agent.proof
    try:
        import ortools  # noqa: F401
        solver = "ortools-cp-sat"
    except ImportError:
        solver = "exact-enumeration"
    return {"version": __version__, "solver": solver,
            "authorities": {"opa": proof.opa.configured, "tigerbeetle": proof.ledger.configured,
                            "airwallex": proof.airwallex.configured},
            "audit": await asyncio.to_thread(app.state.audit.verify)}


@app.get("/api/scenarios")
async def list_scenarios():
    out = []
    for path in sorted(SCENARIOS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({"id": path.stem, "title": data.get("title", path.stem), "story": data.get("story", "")})
    return out


@app.get("/api/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str):
    return json.loads(_scenario_file(scenario_id).read_text(encoding="utf-8"))


def _live_base() -> TreasuryState:
    """The live book's base (balances, rates, policy) from data/scenarios/demo_live.json. A blank company has no
    obligation and no budget line there yet: the base is read all the same, its obligations empty; what is due comes
    from Intake and the requests of the day."""
    raw = json.loads(_scenario_file("demo_live").read_text(encoding="utf-8"))["state"]
    if raw.get("budget") and not raw["budget"].get("lines"):
        raw = raw | {"budget": None}
    if raw.get("obligations"):
        return TreasuryState.model_validate(raw)
    hold = {"obligation_id": "BASE-PLACEHOLDER", "amount_minor": 1, "currency": raw.get("base_currency", "USD"), "priority": 1,
            "due_in_hours": 720, "beneficiary_id": "base-placeholder", "evidence_ref": "base-placeholder"}
    ev = {"ref": "base-placeholder", "subject_id": "BASE-PLACEHOLDER", "source": "base", "status": "verified", "summary": "placeholder"}
    st = TreasuryState.model_validate(raw | {"obligations": [hold], "evidence": [*raw.get("evidence", []), ev]})
    return st.model_copy(update={"obligations": [], "evidence": [e for e in st.evidence if e.ref != "base-placeholder"]})


def _with_budget(state: TreasuryState, period: str | None = None) -> TreasuryState:
    """The period's plan from data/budgets.json plus what the execution store knows (realized spend, grants, closure)."""
    budget = state.budget or load_budget(period)
    if budget is None or not (state.budget or any(o.budget_line for o in state.obligations)):
        return state
    store = app.state.executor.store
    state = attach_budget(state, budget.period, realized=store.realized(budget.period), grants=store.grants(budget.period))
    if state.budget and store.closed_period(state.budget.period):
        state = state.model_copy(update={"budget": state.budget.model_copy(update={"closed": True})})
    return state


@app.post("/api/scenarios/{scenario_id}/plan", response_model=PlanResult)
async def plan_scenario(scenario_id: str, period: str | None = None):
    data = json.loads(_scenario_file(scenario_id).read_text(encoding="utf-8"))
    return await plan(_with_budget(TreasuryState.model_validate(data["state"]), period))


# -- Budget · mandate -------------------------------------------------------------------------

@app.get("/api/live/latest")
async def live_latest():
    """whether an Autopilot Console run exists in this process, so the dashboard can offer it without probing for a 404."""
    sessions = getattr(app.state, "live", {})
    if not sessions:
        return {"sid": None}
    s = list(sessions.values())[-1]
    return {"sid": s.sid, "book_name": s.book_name}


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#1d3f8f"/>'
           '<path d="M16 18l16 28 16-28" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/></svg>')


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/budget")
async def budget_pipeline(scenario_id: str = "kit01_initial", period: str | None = None):
    """The pipeline for a book: plan / granted / realized / paying now / committed / available / minus per line,
    alerts and budget requests, computed from the current plan of that book. scenario_id=live: the book of the latest
    Autopilot Console run, with what the store has realized since (paid obligations are settled, not paid again)."""
    if scenario_id == "live":
        sessions = getattr(app.state, "live", {})
        if not sessions:
            raise HTTPException(404, "no Autopilot Console run yet")
        s = list(sessions.values())[-1]
        state, _notes = await asyncio.to_thread(apply_operational_facts, s.state, app.state.executor.store)
        result = await app.state.agent.decide(state)
        shown = json.loads(json.dumps(result.budget).replace(f"-{s.tag}", ""))  # obligation names as the finance team knows them
        return {"scenario_id": "live", "book_name": s.book_name, "decision": result.decision, "plan_hash": result.plan_hash, **shown,
                "grants": app.state.executor.store.list_grants(state.budget.period) if state.budget else [],
                "open_recon_cases": app.state.executor.store.open_recon_cases(state.book_id),
                "closed_period": bool(state.budget and app.state.executor.store.closed_period(state.budget.period)),
                "requires_approval": next((x.details.get("local", {}).get("requires_approval", [])
                                           for x in result.stages if x.engine == "Policy"), [])}
    data = json.loads(_scenario_file(scenario_id).read_text(encoding="utf-8"))
    state = _with_budget(TreasuryState.model_validate(data["state"]), period)
    if state.budget is None:
        raise HTTPException(404, "no budget for this book/period")
    result = await app.state.agent.decide(state)
    return {"scenario_id": scenario_id, "decision": result.decision, "plan_hash": result.plan_hash, **result.budget,
            "grants": app.state.executor.store.list_grants(state.budget.period),
            "open_recon_cases": app.state.executor.store.open_recon_cases(state.book_id),
            "closed_period": bool(app.state.executor.store.closed_period(state.budget.period)),
            "requires_approval": next((s.details.get("local", {}).get("requires_approval", [])
                                       for s in result.stages if s.engine == "Policy"), [])}


class ReviewerAction(BaseModel):
    reviewer: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=500)


@app.get("/v1/recon-cases")
async def list_recon_cases():
    """Reconciliations that did not add up. An OPEN case locks its book: no autonomous execution until closed."""
    return app.state.executor.store.list_recon_cases()


@app.post("/v1/recon-cases/{case_id}/close")
async def close_recon_case(case_id: str, act: ReviewerAction):
    if act.role != "finance_manager":
        raise HTTPException(403, f"role {act.role} may not close a reconciliation case")
    if not act.note.strip():
        raise HTTPException(422, "a closing note (what explained the difference) is required")
    case = app.state.executor.store.close_recon_case(case_id, f"{act.reviewer} ({act.role})", act.note)
    if case is None:
        raise HTTPException(404, "unknown case")
    await asyncio.to_thread(app.state.audit.append, "recon_case_closed", {"case_id": case_id, **act.model_dump()})
    return case


class PeriodCloseRequest(BaseModel):
    scenario_id: str = "kit01_initial"
    as_of: str = Field(min_length=10, max_length=10, description="cut-off date, ISO")
    reviewer: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=500)


@app.post("/v1/budgets/{period}/close")
async def close_period(period: PeriodPath, req: PeriodCloseRequest):
    """Period cut-off: issue the period's statement (plan, realized to the cut-off, open commitments, carry-forward,
    late-posted) and close it; later spend against it needs a person. Role-checked and audited."""
    if req.role != "finance_manager":
        raise HTTPException(403, f"role {req.role} may not close a period")
    data = json.loads(_scenario_file(req.scenario_id).read_text(encoding="utf-8"))
    state = _with_budget(TreasuryState.model_validate(data["state"] | {"as_of": req.as_of}), period)
    if state.budget is None or state.budget.period != period:
        raise HTTPException(404, "no such budget period for this book")
    plan = await app.state.agent.decide(state)
    report = cutoff_report(state, plan.budget, app.state.executor.store.realized(period))
    report |= {"closed_by": f"{req.reviewer} ({req.role})", "note": req.note}
    app.state.executor.store.close_period(period, report["closed_by"], report)
    await asyncio.to_thread(app.state.audit.append, "period_closed", {
        "period": period, "closed_by": report["closed_by"], "carry_forward_total_minor": report["carry_forward_total_minor"]})
    return report


@app.get("/v1/budgets/{period}/cutoff")
async def period_cutoff(period: PeriodPath):
    closed = app.state.executor.store.closed_period(period)
    if closed is None:
        raise HTTPException(404, "period not closed")
    return closed


# -- the book ledger ----------------------------------------------------------------------------

@app.get("/v1/ledger")
async def ledger_books():
    """Every book with its balances per currency (cash, committed, expense, fees, fx, funding)."""
    store = app.state.executor.store
    return [{"book_id": b, "balances": store.book_balances(b), "entries": len(store.journal(b))} for b in store.books()]


@app.get("/v1/t0/books")
async def t0_books_get():
    """Books & ledger → T:0: what the ledger holds, what is posted to T:0, pending, refused, and each entry's verification."""
    from . import t0_books

    entries = await asyncio.to_thread(app.state.executor.store.journal, None)
    return await asyncio.to_thread(t0_books.summary, app.state.org, entries)


class T0PostIn(BaseModel):
    scope: Literal["paid", "all"] = Field("paid", description="paid: payments sent, settled or refunded; all: every ledger entry (funding and FX too)")
    dry_run: bool = False
    limit: int = Field(200, ge=1, le=1000)


@app.post("/v1/t0/books/post")
async def t0_books_post(body: T0PostIn, role: str = "finance_manager", by: str = "dashboard"):
    """Post the ledger entries T:0 does not have yet as manual journal entries (once each: VENTHORA's note and T:0's
    idempotency key), then read them back and verify."""
    import httpx

    from . import t0_books

    who = _writer(role, by)
    entries = await asyncio.to_thread(app.state.executor.store.journal, None)
    kinds = t0_books.PAID_KINDS if body.scope == "paid" else t0_books.ALL_KINDS
    try:
        rep = await t0_books.post(app.state.org, entries, kinds=kinds, limit=body.limit, dry_run=body.dry_run)
        ver = None if body.dry_run else await t0_books.verify(app.state.org, only_unverified=True)
    except (t0_books.T0Error, httpx.HTTPError) as exc:
        raise HTTPException(502, f"T:0: {exc}") from exc
    if not body.dry_run:
        await asyncio.to_thread(app.state.audit.append, "t0_journal_posted", {"by": who, "scope": body.scope, "posted": len(rep["posted"]),
                                                                              "refused": len(rep["refused"]), "verified": (ver or {}).get("passed")})
    return {"post": rep, "verify": ver}


@app.post("/v1/t0/books/verify")
async def t0_books_verify(role: str = "finance_manager", by: str = "dashboard"):
    """Read every posted entry back from T:0 and compare it with what VENTHORA sent."""
    import httpx

    from . import t0_books

    who = _writer(role, by)
    try:
        out = await t0_books.verify(app.state.org)
    except (t0_books.T0Error, httpx.HTTPError) as exc:
        raise HTTPException(502, f"T:0: {exc}") from exc
    await asyncio.to_thread(app.state.audit.append, "t0_journal_verified", {"by": who, "checked": out["checked"], "passed": out["passed"]})
    return out


JOURNAL_ACCOUNTS = {"cash": ("1010", "Cash at bank (Airwallex wallet)"), "committed": ("1050", "Payments in transit"),
                    "expense": ("6000", "Payments to payees"), "fees": ("6950", "Bank and transfer fees"),
                    "fx": ("7900", "Foreign exchange"), "funding": ("3000", "Treasury funding")}


@app.get("/v1/ledger/export.csv")
async def ledger_export(book: str | None = Query(None, max_length=80), date_from: str | None = Query(None, alias="from", max_length=10),
                        date_to: str | None = Query(None, alias="to", max_length=10)):
    """Books & ledger -> the journal for an accounting system: one row per journal line, debit or credit, in the
    line's own currency, with the account code and name, the entry type, its reference and the execution."""
    import csv
    import io

    from fastapi.responses import Response

    rows = await asyncio.to_thread(app.state.executor.store.journal, book or None)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Journal entry", "Book", "Entry type", "Reference", "Execution", "Account code", "Account name", "Currency", "Debit", "Credit"])
    for e in rows:
        day = str(e["at"])[:10]
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        for ln in e["lines"]:
            code, name = JOURNAL_ACCOUNTS.get(ln["account"], ("", ln["account"]))
            amt = int(ln["amount"])
            w.writerow([day, e["entry_id"], e["book_id"], e["kind"], e["ref"], e.get("execution_id") or "", code, name, ln["currency"],
                        f"{amt / 100:.2f}" if amt > 0 else "", f"{-amt / 100:.2f}" if amt < 0 else ""])
    name = f"venthora-journal{'-' + book if book else ''}{'-' + date_from if date_from else ''}{'-' + date_to if date_to else ''}.csv"
    return Response(out.getvalue(), media_type="text/csv", headers={"content-disposition": f'attachment; filename="{name}"'})


@app.get("/v1/ledger/{book_id}")
async def ledger_book(book_id: str):
    store = app.state.executor.store
    if not store.has_book(book_id):
        raise HTTPException(404, "unknown book")
    return {"book_id": book_id, "balances": store.book_balances(book_id), "journal": store.journal(book_id)}


@app.post("/v1/ledger/eval")
async def ledger_eval(inp: Annotated[dict, Body()]):
    """Pure evaluation of postings (ground truth / the form): balances, refusals, every entry balanced."""
    return ledger_evaluate(inp)


class AdjustRequest(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    amount_minor: int = Field(ne=0, ge=-(10**11), le=10**11)
    reviewer: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=60)
    note: str = Field(min_length=1, max_length=500)


@app.post("/v1/ledger/{book_id}/adjust")
async def ledger_adjust(book_id: str, req: AdjustRequest):
    """A person's correction to a book (an unreported bank charge, a manual top-up), against funding; audited."""
    if req.role != "finance_manager":
        raise HTTPException(403, f"role {req.role} may not adjust a book")
    store = app.state.executor.store
    if not store.has_book(book_id):
        raise HTTPException(404, "unknown book")
    entry = ledger_adjust_entry(req.currency, req.amount_minor, f"{req.reviewer}: {req.note}")
    posted = store.post(book_id, entry)
    await asyncio.to_thread(app.state.audit.append, "ledger_adjust", {"book_id": book_id, **req.model_dump()})
    return posted


# -- payee lifecycle: NEW -> VERIFIED -> ACTIVE, cooling after a bank change, call-back mandatory -----------------

@app.get("/v1/payees")
async def list_payees():
    """Every payee's live record: status, bank-details fingerprint, cooling period, who verified it, with its name and
    currency from the registry."""
    reg = app.state.intake.registry.payees
    return [r | {"name": (reg.get(r["beneficiary_id"]) or {}).get("name"), "currency": (reg.get(r["beneficiary_id"]) or {}).get("currency")}
            for r in app.state.executor.store.list_payees()]


@app.get("/v1/payees/{beneficiary_id}")
async def get_payee(beneficiary_id: str):
    store = app.state.executor.store
    rec = store.payee(beneficiary_id)
    if rec is None:
        raise HTTPException(404, "unregistered payee")
    return rec | {"events": store.payee_events(beneficiary_id)}


# -- the live demo: the builder guide's five elements, step by step, on the real sandbox ----------------------------------

class LiveStart(BaseModel):
    scenario: str = Field(default="demo_live", min_length=1, max_length=80)
    config: dict | None = Field(default=None, description="the mandate form as submitted (GET /api/live/setup returns its shape)")


class LiveStep(BaseModel):
    approver: str | None = Field(default=None, max_length=120, description="the person approving a plan that needs one")
    role: str = Field(default="finance_manager", max_length=60)


@app.get("/live", include_in_schema=False)
async def live_page():
    return FileResponse(DASHBOARD.parent / "live.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/flow", include_in_schema=False)
async def flow_page():
    return FileResponse(DASHBOARD.parent / "flow.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/api/live/setup")
async def live_setup(scenario: str = "demo_live"):
    """The mandate form, filled with the platform's current settings: allocation, reserve, every permission and safeguard,
    budget lines, approvers and the approval matrix, payees, the reporting cut-off, and the requests received."""
    from . import mandate

    _scenario_file(scenario)
    d = live_session.defaults(scenario, app.state.executor, mandate.standing(app.state.org))
    return _owned_by_menus(d)


def _owned_by_menus(d: dict) -> dict:
    """The console takes these from the menus that own them (nothing is set twice): the allocation and the reserve
    floor from Delegation of Authority -> Treasury limits, the horizon from the Standard Payment Priority Policy once
    it is saved, the budget period from Budget period, cut-off & sync."""
    from . import budget_allocation as ba
    from . import priority_sop

    org = app.state.org
    src = {"allocation": "System Setting → Delegation of Authority → Treasury limits and controls",
           "horizon": "System Setting → Delegation of Authority → Treasury limits and controls"}
    if (priority_sop.saved(org) or {}).get("policy"):
        d["horizon_hours"] = priority_sop.policy(org)["horizon_hours"]
        src["horizon"] = "System Setting → Standard Payment Priority Policy"
    try:
        kind = org.budget_policy()["period_type"]
    except Exception:  # noqa: BLE001 - no budget policy saved: monthly
        kind = "monthly"
    d["period_type"] = kind if kind in ("monthly", "quarterly", "annual") else ("quarterly" if kind == "half_year" else "annual")
    d["budget_period_label"] = f"{ba.PERIOD_LABEL.get(kind, kind)} · {d['periods'][d['period_type']]['label']}"
    src["period"] = "Payment → Budget allocation → Budget period, cut-off & sync"
    from .masterdata import MasterStore, budget_for

    try:  # the budget lines of that same period, as set in Payment -> Budget allocation
        mine = budget_for(MasterStore(), d["periods"][d["period_type"]]["period"])
    except Exception:  # noqa: BLE001 - no master data store
        mine = None
    if mine and mine.get("lines"):
        d["budget"] = [{"line": ln["line"], "amount_minor": ln["amount_minor"], "auto_share_bp": ln.get("auto_share_bp", 10_000),
                        "alert_share_bp": ln.get("alert_share_bp", 8_000), "require_approval_on_alert": ln.get("require_approval_on_alert", False),
                        "bypass": ln.get("bypass", False), "bypass_approved_by": ln.get("bypass_approved_by")} for ln in mine["lines"]]
        src["budget"] = f"Payment → Budget allocation, {d['periods'][d['period_type']]['label']}"
    elif d["period_type"] != "monthly":
        src["budget"] = (f"Payment → Budget allocation has no lines for {d['periods'][d['period_type']]['label']}; "
                         f"the lines of {d['periods']['monthly']['label']} are used")
    d["sources"] = src
    return d


@app.get("/v1/mandate")
async def mandate_get(scenario: str = "demo_live", role: str | None = None):
    """The standing mandate by category: the policy each category serves, the document checks run on every request
    with their mode, every permission and safeguard with its value, all limits in one list, the approval policy, and
    which role holds the mandate authority (with `role`, whether that role may change it)."""
    from . import mandate

    _scenario_file(scenario)
    setup = live_session.defaults(scenario, app.state.executor, mandate.standing(app.state.org))
    rules = (await doc_rules_get())["rules"]
    return mandate.view(setup, app.state.org, bool(rules.get("enabled")), role)


class MandateIn(BaseModel):
    values: dict[str, int] = {}
    enabled: dict[str, bool] = {}
    book: dict[str, int] = {}
    checks: dict[str, str] = Field(default={}, description="check key -> hold, warn or off")
    authority_role: str | None = Field(default=None, description="hand the mandate authority to another role")
    acknowledged: list[str] = Field(default=[], description="the warning keys the person has read and accepts")


@app.put("/v1/mandate")
async def mandate_put(req: MandateIn, role: str = "super_admin", by: str = "dashboard", scenario: str = "demo_live"):
    """Save the standing mandate; every Autopilot Console run starts from it. Only the role holding the mandate authority may
    save it. A change that weakens a critical control answers 409 with the warnings until each is acknowledged."""
    from . import mandate

    who = f"{by} ({role})"
    setup = live_session.defaults(scenario, app.state.executor, mandate.standing(app.state.org))
    try:
        out = await asyncio.to_thread(mandate.save, app.state.org, req.model_dump(), who, role=role, setup=setup)
    except mandate.AuthorityError as exc:
        raise HTTPException(403, str(exc)) from exc
    except mandate.NeedsAcknowledgement as exc:
        raise HTTPException(409, {"message": str(exc), "warnings": exc.warnings}) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "standing_mandate", {
        "by": who, "values": out["values"], "enabled": out["enabled"], "book": out["book"], "checks": out["checks"],
        "authority_role": out["authority_role"], "warnings_acknowledged": out["warnings_acknowledged"]})
    return out


# ---- Delegation of Authority: the general policy per category; individuals live on the source documents ----
def _payee_status(bid: str) -> str | None:
    rec = app.state.executor.store.payee(bid)
    if rec is not None:
        return rec.get("status")
    return ((app.state.intake.registry.payees.get(bid) or {}).get("lifecycle") or {}).get("status")


def _doa_authority(role: str) -> None:
    from . import mandate

    holder = mandate.standing(app.state.org)["authority_role"]
    if role != holder:
        raise HTTPException(403, f"only the {mandate.AUTHORITY_ROLES[holder]} holds the delegation of authority; role '{role}' may read it")


@app.get("/v1/doa")
async def doa_get(role: str | None = None):
    """The delegation of authority: every internal category with its policy (schedule, pay day, payout currencies) and this period's source document judged against it; the external categories with the limits
    that govern them; and whether the autopilot pays due lines on its own."""
    from . import doa, mandate

    org = app.state.org
    await asyncio.to_thread(doa.ensure_accounts, org)
    payees = app.state.intake.registry.payees
    today = datetime.now(UTC).date()
    rows = {r["key"]: r for r in doa.evaluate(org, _payee_status, payees, today)}
    setup = live_session.defaults("demo_live", app.state.executor, mandate.standing(org))
    vals = {m["key"]: m for m in setup["mandates"]}
    st = mandate.standing(org)
    return {"internal": [c | {"policy": doa.policy(org, c["key"]), "now": rows[c["key"]]} for c in doa.INTERNAL],
            "external": [c | {"limits": [{"label": vals[k]["label"], "value": vals[k].get("value"), "unit": vals[k]["unit"], "enabled": vals[k].get("enabled", True)}
                                         for k in (("pay_single", "pay_first", "three_way", "tolerance_floor") if c["key"] == "vendors" else ("pay_single", "pay_first", "pay_per_payee"))]}
                         for c in doa.EXTERNAL],
            "runs": sorted(org.list("doa_run"), key=lambda r: r["at"], reverse=True)[:30],
            "autopilot": doa.autopilot(org), "currencies": list(doa.CURRENCIES), "schedules": list(doa.SCHEDULES), "pay_days": list(doa.PAY_DAYS),
            "benefit_catalog": {"benefits": doa.BENEFITS, "groups": doa.BENEFIT_GROUPS, "frequencies": list(doa.FREQUENCIES), "paid_to": doa.PAID_TO},
            "consultant_catalog": {"senders": doa.CONSULTANT_SENDERS, "documents": doa.CONSULTANT_DOCUMENTS},
            "office_catalog": {"senders": doa.OFFICE_SENDERS, "requirements": doa.OFFICE_REQUIREMENTS},
            "custom": doa.custom_categories(org), "custom_requirements": doa.CUSTOM_REQUIREMENTS,
            "pending": doa.pending(org, _payee_status, payees, today),
            "travel_catalog": {"senders": doa.TRAVEL_SENDERS, "documents": doa.TRAVEL_DOCUMENTS, "channels": doa.TRAVEL_CHANNELS, "places": doa.travel_places(org)},
            "engine_currencies": list(doa.ENGINE_CURRENCIES),
            "people": [{"user_id": u["user_id"], "name": u["name"], "role": u["role"]} for u in org.list("user") if u.get("active", True)],
            "authority": {"role": st["authority_role"], "label": mandate.AUTHORITY_ROLES[st["authority_role"]], "you_may_change": role == st["authority_role"] if role else None},
            "today": today.isoformat()}


@app.put("/v1/doa/policies/{key}")
async def doa_policy_put(key: str, body: Annotated[dict, Body()], role: str = "super_admin", by: str = "dashboard"):
    """Set one internal category's policy. Only the holder of the delegation may."""
    from . import doa

    _doa_authority(role)
    try:
        rec = await asyncio.to_thread(doa.save_policy, app.state.org, key, body, f"{by} ({role})")
    except doa.DoAError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "doa_policy", rec | {"by": f"{by} ({role})"})
    return rec


@app.post("/v1/doa/custom")
async def doa_custom_post(body: Annotated[dict, Body()], role: str = "super_admin", by: str = "dashboard"):
    """Add (or, with its key, change) a category of the FCO's own: item, charge code, who may ask for it by email or
    on the dashboard, and what the request must carry. Only the holder of the delegation may."""
    from . import doa

    _doa_authority(role)
    try:
        rec = await asyncio.to_thread(doa.save_custom, app.state.org, body, f"{by} ({role})")
    except (doa.DoAError, OrgError) as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "doa_custom", rec | {"by": f"{by} ({role})"})
    return rec


@app.delete("/v1/doa/custom/{key}")
async def doa_custom_delete(key: str, role: str = "super_admin", by: str = "dashboard"):
    _doa_authority(role)
    try:
        await asyncio.to_thread(app.state.org.delete, "doa_custom", key, f"{by} ({role})")
    except KeyError as exc:
        raise HTTPException(404, "no such category") from exc
    await asyncio.to_thread(app.state.audit.append, "doa_custom_deleted", {"key": key, "by": f"{by} ({role})"})
    return {"deleted": key}


@app.put("/v1/doa/settings")
async def doa_settings_put(body: Annotated[dict, Body()], role: str = "super_admin", by: str = "dashboard"):
    """Switch the autopilot: on, the agent pays every due line the DoA lets it pay on its own, without a click."""
    from . import doa

    _doa_authority(role)
    rec = {"_id": "doa_settings", "autopilot": bool(body.get("autopilot"))}
    await asyncio.to_thread(app.state.org._put, "doa_settings", rec, f"{by} ({role})")
    await asyncio.to_thread(app.state.audit.append, "doa_autopilot", rec | {"by": f"{by} ({role})"})
    return {"autopilot": doa.autopilot(app.state.org)}


class RegisterIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    lines: list[dict] = Field(max_length=2000, description="the lines adjusted for this period only (employee, payee_id, currency, amount_minor, charge_code); none = paid from People and roles")


@app.post("/v1/doa/registers")
async def doa_register_post(req: RegisterIn, role: str = "finance_manager", by: str = "dashboard"):
    """Prepare (or replace) the payroll register of a period; it needs HR's and then the FCO's approval."""
    from . import doa

    who = _writer(role, by)
    try:
        return await asyncio.to_thread(doa.save_register, app.state.org, req.period, req.lines, who)
    except (doa.DoAError, OrgError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/v1/doa/registers")
async def doa_registers():
    """Every payroll register, newest period first, with the people of the organisation and the registered accounts
    a line may pay into (name and currency), for the register page."""
    org = app.state.org
    reg = app.state.intake.registry.payees
    return {"registers": sorted(org.list("payroll_register"), key=lambda r: r["period"], reverse=True),
            "people": [{"user_id": u["user_id"], "name": u["name"], "role": u["role"], "payee_id": u.get("payee_id"), "salary_minor": u.get("salary_minor"),
                        "salary_currency": u.get("salary_currency"), "charge_code": u.get("charge_code"), "contract_end": u.get("contract_end")}
                       for u in org.list("user") if u.get("active", True)],
            "accounts": [{"id": k, "name": v["name"], "currency": v.get("currency"), "status": _payee_status(k)} for k, v in reg.items()]}


@app.get("/v1/doa/registers/{period}")
async def doa_register_get(period: PeriodPath):
    from . import doa

    rec = doa.register(app.state.org, period)
    if rec is None:
        raise HTTPException(404, "no payroll register for that period")
    return rec


@app.post("/v1/doa/registers/{period}/approve")
async def doa_register_approve(period: PeriodPath, step: str = Query(pattern="^(hr|fco)$"), role: str = "super_admin", by: str = "dashboard"):
    from . import doa

    try:
        rec = await asyncio.to_thread(doa.approve_register, app.state.org, period, step, f"{by} ({role})", role)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except doa.DoAError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "payroll_register_approved", {"period": period, "step": step, "by": f"{by} ({role})"})
    return rec


class DoARunIn(BaseModel):
    refs: list[str] = Field(default_factory=list, description="lines prepared for a person that the approver approves now")
    approver: str | None = Field(default=None, max_length=120)
    role: str = "finance_manager"


async def _doa_run(refs: list[str], approver: str | None, role: str, by: str) -> dict:
    """Pay every due line the DoA lets the agent pay, plus the prepared lines a person approves now. The book still
    passes the treasury proof (reserve floor, FX, outflow limits) and the split pays only what is inside it."""
    from . import autonomy, doa
    from .execution.models import Approval

    if approver and role not in ("finance_manager", "cfo", "super_admin"):
        raise HTTPException(403, f"role {role} may not approve a payment")
    org = app.state.org
    today = datetime.now(UTC).date()
    payees = app.state.intake.registry.payees
    done = {r["_id"] for r in org.list("doa_run") if r["status"] in ("PAID", "IN_FLIGHT", "IN_REVIEW")}
    out = {"paid": [], "held": [], "skipped": [], "executions": []}
    base = _live_base()
    rows = doa.evaluate(org, _payee_status, payees, today)
    mine = (approver or "").split(" (")[0].strip().lower()
    own = [ln for row in rows for ln in row["lines"] if ln["verdict"] == "approve" and approver and ln.get("ref") in refs and mine
           and mine == str(ln.get("changed_by") or "").split(" (")[0].strip().lower()]
    if own:  # a change is never approved by the person who made it
        raise HTTPException(403, f"{own[0]['employee']}: the person who made the change cannot approve it")
    for row in rows:
        if not row["due"] or not row["lines"]:
            if row["due"]:
                out["skipped"].append({"key": row["key"], "reasons": row["reasons"]})
            continue
        pre = {a["_id"]: a for a in org.list("payroll_approval")}
        ok_before = {ln["ref"] for ln in row["lines"] if ln.get("ref") in pre and all(pre[ln["ref"]].get(k) == ln.get(k)
                                                                                 for k in ("amount_minor", "currency", "payee_id", "charge_code"))}
        go = [ln for ln in row["lines"] if ln.get("ref") not in done and (ln["verdict"] == "auto" or (ln["verdict"] == "approve" and (
            (approver and ln["ref"] in refs) or ln["ref"] in ok_before)))]
        out["skipped"] += [{"key": row["key"], "employee": ln["employee"], "verdict": ln["verdict"], "reasons": ln["reasons"]} for ln in row["lines"] if ln not in go and ln.get("ref") not in done]
        if not go:
            continue
        from . import priority_sop

        state = priority_sop.apply(doa.book(base, row, go, payees, today), org, payees)
        try:
            state = await with_live_balances_and_rates(state, _bank(), ring_fence=True)
        except AirwallexError as exc:
            raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
        state, _ = apply_operational_facts(state, app.state.executor.store)
        s = await autonomy.split(app.state.agent, state)
        execution, paid = None, set()
        if s.auto_plan is not None:
            res = await app.state.executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash, requested_by="VENTHORA (DoA)"))
            execution, paid = res.model_dump(), ({d.obligation_id for d in s.auto_plan.obligations if d.action in autonomy.PAYING} if res.status in ("COMPLETED", "IN_FLIGHT") else set())
        elif approver and s.full.decision == "REQUIRES_APPROVAL":
            from .execution.executor import terms_digest

            res = await app.state.executor.execute(ExecutionRequest(state=state, plan_hash=s.full.plan_hash, requested_by="VENTHORA (DoA)",
                                                                    approval=Approval(approver=approver, role=role, terms_digest=terms_digest(s.full))))
            execution, paid = res.model_dump(), ({d.obligation_id for d in s.full.obligations if d.action in autonomy.PAYING} if res.status in ("COMPLETED", "IN_FLIGHT") else set())
        held = {h["obligation_id"]: h["reasons"] for h in s.held}
        if execution:
            out["executions"].append({"status": execution["status"], "message": execution["message"], "execution_id": execution["execution_id"]})
        for ln in go:
            oid = f"DOA-{row['key'].upper()}-{row['period']}-{ln['line'] + 1:03d}"
            status = ("PAID" if execution["status"] == "COMPLETED" else "IN_FLIGHT") if oid in paid else "HELD"
            msg = execution["message"] if oid in paid else "; ".join(held.get(oid, ["not paid by the plan"]))
            rec = await asyncio.to_thread(doa.record_run, org, row, ln, status, by, (execution or {}).get("execution_id"),
                                          approver if ln["verdict"] == "approve" else None, msg)
            (out["paid"] if status != "HELD" else out["held"]).append(rec)
    await asyncio.to_thread(app.state.audit.append, "doa_run", {"by": by, "paid": [r["_id"] for r in out["paid"]], "held": [r["_id"] for r in out["held"]]})
    if out["paid"] or out["held"]:
        _note("Payments", f"DoA payment run by {by}: paid {', '.join(r['_id'] for r in out['paid']) or 'none'}; held {', '.join(r['_id'] for r in out['held']) or 'none'}")
    return out


async def _anchor_large_payments(limit: int = 5) -> list[dict]:
    """Every payment made of the FCO's anchoring amount or more (FCO Authorize -> Delegation of Authority) gets its own
    proof on Alta: the payment's facts and the audit head at that moment, in one transaction. A payment that could
    not be anchored (no testnet key, no gas) is tried again after 30 minutes."""
    from . import mandate, metal

    org = app.state.org
    th = (mandate.standing(org).get("book") or {}).get("anchor_from_minor", mandate.BOOK_DEFAULTS["anchor_from_minor"])
    if not th:
        return []
    now = datetime.now(UTC)
    seen = {r["_id"]: r for r in org.list("metal_payment")}
    due = [r for r in await asyncio.to_thread(app.state.executor.store.paid_rows) if r["base_cost_minor"] >= th and (
        r["transfer_id"] not in seen or (seen[r["transfer_id"]].get("status") == "failed"
                                         and now - datetime.fromisoformat(seen[r["transfer_id"]]["at"]) > timedelta(minutes=30)))]
    out = []
    for r in due[:limit]:
        pay = {k: r.get(k) for k in ("transfer_id", "obligation_id", "beneficiary_id", "currency", "amount_minor", "base_cost_minor", "paid_at",
                                     "po_number", "invoice_fingerprint", "account_code")}
        head = (await asyncio.to_thread(app.state.audit.verify)).get("head")
        rec = {"_id": r["transfer_id"], "at": now.isoformat(timespec="seconds"), "base_minor": r["base_cost_minor"], "threshold_minor": th}
        try:
            a = await metal.anchor(org, {"payment": pay, "audit_head": head}, "VENTHORA (anchoring rule)")
            rec |= {"status": "anchored", "tx_hash": a["tx_hash"], "digest": a["digest"], "explorer": a.get("explorer")}
            _note("Reports", f"Payment {r['obligation_id']} ({r['currency']} {r['amount_minor'] / 100:,.2f}, USD {r['base_cost_minor'] / 100:,.2f}) "
                             f"anchored on Alta testnet: tx {a['tx_hash'][:18]}…, at or above the FCO's USD {th / 100:,.2f}")
        except Exception as exc:  # noqa: BLE001 - the payment is made; its proof waits for the next try
            rec |= {"status": "failed", "error": str(exc)[:300]}
            _note("Reports", f"Payment {r['obligation_id']} not anchored yet on Alta: {str(exc)[:200]}")
        await asyncio.to_thread(org._put, "metal_payment", rec, "VENTHORA (anchoring rule)")
        await asyncio.to_thread(app.state.audit.append, "metal_payment_anchor", {k: rec.get(k) for k in ("_id", "status", "tx_hash", "error")})
        out.append(rec)
    return out


@app.post("/v1/metal/anchor-payments")
async def metal_anchor_payments(role: str = "finance_manager", by: str = "dashboard"):
    """Anchor now the payments at or above the FCO's anchoring amount that have no proof yet (the scheduler does it
    every minute on its own)."""
    _writer(role, by)
    done = await _anchor_large_payments(20)
    from . import mandate

    return {"anchored": [r for r in done if r["status"] == "anchored"], "failed": [r for r in done if r["status"] != "anchored"],
            "threshold_minor": (mandate.standing(app.state.org).get("book") or {}).get("anchor_from_minor", 0),
            "proofs": sorted(app.state.org.list("metal_payment"), key=lambda r: r["at"], reverse=True)[:50]}


def _note(tab: str, line: str) -> None:
    """A step nobody clicked, written to the process log while a recording is on; never fails the step itself."""
    from . import processlog

    with contextlib.suppress(Exception):
        processlog.note(app.state.org, tab, line)


@app.post("/v1/doa/run")
async def doa_run(req: DoARunIn, by: str = "dashboard"):
    """Run the due lines now (the autopilot does the same on its own when it is on): every line ready is paid; a line
    that is blocked, or waits for a person, stays in Executions & incidents and stops nothing else."""
    from . import doa

    p = doa.pending(app.state.org, _payee_status, app.state.intake.registry.payees, datetime.now(UTC).date())
    if not p["ready"] and not req.refs:  # what is blocked waits in Executions & incidents; the rest is paid
        raise HTTPException(409, {"message": "nothing is ready to pay: what stops the due payments is in Executions & incidents", "blocked": p["blocked"]})
    return await _doa_run(req.refs, req.approver, req.role, f"{by} ({req.role})" if req.approver else "VENTHORA (DoA)")


class TravelFile(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content_type: str = Field(default="application/pdf", max_length=100)
    content_b64: str = Field(min_length=1, max_length=14_000_000)


class TravelRequestIn(BaseModel):
    traveller: str = Field(description="user_id of the person travelling")
    origin: str = Field(description="Country/City, from the travel rates")
    destination: str = Field(description="Country/City, from the travel rates")
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    purpose: str = Field(default="", max_length=500)
    amount_minor: int | None = Field(default=None, gt=0, le=10**10, description="the amount claimed; default: what the rates allow")
    documents: dict[str, list[TravelFile]] = Field(default_factory=dict, description="travel_request, receipts, travel_report")


@app.post("/v1/doa/travel-requests")
async def doa_travel_request(req: TravelRequestIn, role: str = "finance_manager", by: str = "dashboard"):
    """A travel claim made on the dashboard (the channel 'Dashboard request only' accepts). It is written as the
    traveller's own request and goes through the same intake and the same checks as an emailed one: the places, the
    rates, the checklist; complete and inside the delegation, the agent pays it to the traveller's registered account."""
    import base64
    import secrets
    from email.message import EmailMessage
    from email.utils import format_datetime

    from . import doa

    org = app.state.org
    user = org.get("user", req.traveller)
    if user is None or not user.get("active", True):
        raise HTTPException(422, "the traveller is not an active person in People and roles")
    places = {f"{c}/{r['city']}": (c, r) for c, rows in doa.travel_places(org).items() for r in rows}
    if req.destination not in places or req.origin not in places:
        raise HTTPException(422, "choose the origin and the destination from the travel rates on file")
    start, end = datetime.fromisoformat(req.start).date(), datetime.fromisoformat(req.end).date()
    if end < start:
        raise HTTPException(422, "the trip ends before it starts")
    dc, rate = places[req.destination]
    oc, orate = places[req.origin]
    nights, days = (end - start).days, (end - start).days + 1
    allowed = rate["lodging_minor"] * nights + rate["per_diem_minor"] * days
    amount = req.amount_minor or allowed
    msg = EmailMessage()
    msg["From"] = f"{user['name']} <{user['email']}>"
    msg["To"] = "travel@venthora.local"
    msg["Subject"] = f"Travel claim: {orate['city']} to {rate['city']}, {req.start} to {req.end}"
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = f"<travel-{secrets.token_hex(8)}@venthora.local>"
    msg["Authentication-Results"] = "venthora.local; dashboard=pass"
    msg.set_content(f"Travel claim of {user['name']}, made on the VENTHORA dashboard by {by} ({role}).\n"
                    f"Trip from {orate['city']} to {rate['city']}, {start:%d %b %Y} to {end:%d %b %Y}: {nights} nights, {days} days.\n"
                    f"Purpose: {req.purpose or '-'}\nAmount claimed: {rate['currency']} {amount / 100:,.2f}\n")
    words = {"travel_request": "Travel request, approved by the supervisor", "receipts": "Receipt", "travel_report": "Travel report"}
    for kind, files in req.documents.items():
        if kind not in words:
            raise HTTPException(422, f"{kind}: not a travel document")
        for f in files:
            try:
                data = base64.b64decode(f.content_b64, validate=True)
            except ValueError as exc:
                raise HTTPException(422, f"{f.name}: the file could not be read") from exc
            main, _, sub = f.content_type.partition("/")
            name = f.name if words[kind].split(",")[0].lower() in f.name.lower() else f"{words[kind].split(',')[0]} - {f.name}"
            msg.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream", filename=name)
    return await _ingest(msg.as_bytes(), None, False, channel="dashboard")


PAYABLE_KINDS = ("supplier_invoice", "contractor_claim", "expense_claim")


async def _pay_on_arrival(intake_id: str, by: str = "VENTHORA (on arrival)") -> dict:
    """A verified request paid at once when the standing mandate says so: planned alone on the live balance with the
    Standard Payment Priority Policy, proved, and executed only for what the plan approves on the agent's own authority. Never
    raises: what is not paid stays in Pending payment, with the reason on the audit chain."""
    from . import autonomy, mandate

    try:
        if not mandate.pay_on_arrival(app.state.org) or not app.state.agent.proof.airwallex.configured:
            return {"paid": False, "reason": "pay on arrival is off or the bank is not connected"}
        req = app.state.intake.store.get(intake_id)
        if req is None or req.status != "VERIFIED" or req.payment or req.kind not in PAYABLE_KINDS:
            return {"paid": False, "reason": "not a verified payable request"}
        base = _live_base()
        base = base.model_copy(update={"obligations": [], "receivables": []})
        state = app.state.intake.to_state(base, as_of=datetime.now(UTC).date(), requests=[req])
        if not state.obligations:
            return {"paid": False, "reason": "nothing payable in it yet"}
        state = await with_live_balances_and_rates(state, _bank(), ring_fence=True)
        state, _ = apply_operational_facts(state, app.state.executor.store)
        s = await autonomy.split(app.state.agent, state)
        if s.auto_plan is None or not any(d.action in autonomy.PAYING for d in s.auto_plan.obligations):
            held = "; ".join(r for h in s.held for r in h["reasons"]) or "the plan does not pay it now"
            await asyncio.to_thread(app.state.audit.append, "held_on_arrival", {"intake_id": intake_id, "reason": held[:300]})
            _note("Payments", f"Pay on arrival · {req.invoice_no or intake_id[:8]} held, not paid: {held[:300]}")
            return {"paid": False, "reason": held}
        res = await app.state.executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash, requested_by=by))
        app.state.position_cache = None
        await asyncio.to_thread(app.state.audit.append, "paid_on_arrival", {"intake_id": intake_id, "execution_id": res.execution_id, "status": res.status})
        _note("Payments", f"Pay on arrival · {req.invoice_no or intake_id[:8]} paid by the agent alone ({by}): execution {res.execution_id} {res.status}")
        return {"paid": res.status in ("COMPLETED", "IN_FLIGHT"), "execution_id": res.execution_id, "status": res.status}
    except Exception as exc:  # noqa: BLE001 - the request stays in Pending payment; the next sweep tries again
        with contextlib.suppress(Exception):
            await asyncio.to_thread(app.state.audit.append, "pay_on_arrival_failed", {"intake_id": intake_id, "error": str(getattr(exc, "detail", exc))[:300]})
        return {"paid": False, "reason": str(getattr(exc, "detail", exc))[:300]}


def _after_intake(req) -> None:
    """Run pay-on-arrival for a request that just became verified, without making the sender wait."""
    from . import mandate

    if req is not None and req.status == "VERIFIED" and mandate.pay_on_arrival(app.state.org):
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(_pay_on_arrival(req.intake_id))


async def _doa_scheduler(every_s: float = 60.0) -> None:
    """The autopilot: every minute, when the holder of the delegation switched it on and the bank is connected, pay
    the due instructions the DoA lets the agent pay on its own."""
    from . import doa

    tick = 0
    app.state.policy_clock = (datetime.now(UTC), every_s * 5)
    while True:
        await asyncio.sleep(every_s)
        tick += 1
        with contextlib.suppress(Exception):
            await _scheduled_policy_run(tick)
        with contextlib.suppress(Exception):
            await _anchor_large_payments()
        try:
            from . import priority_sop as _sop

            # scheduled lines (payroll, benefits) are paid on their pay date from 00:30 UTC, never in its first half hour
            if doa.autopilot(app.state.org) and app.state.agent.proof.airwallex.configured and _sop.scheduled_pay_open(datetime.now(UTC)):
                await _doa_run([], None, "finance_manager", "VENTHORA (autopilot)")
            from . import mandate

            if mandate.pay_on_arrival(app.state.org) and app.state.agent.proof.airwallex.configured:  # the sweep
                waiting = [q for q in app.state.intake.store.list(500) if q.status == "VERIFIED" and not q.payment and q.kind in PAYABLE_KINDS]
                for q in sorted(waiting, key=lambda q: q.received_at)[:20]:  # first in, first out
                    await _pay_on_arrival(q.intake_id, "VENTHORA (sweep)")
        except Exception as exc:  # noqa: BLE001  the next minute tries again; the failure is on the audit chain
            with contextlib.suppress(Exception):
                await asyncio.to_thread(app.state.audit.append, "doa_autopilot_failed", {"error": str(getattr(exc, "detail", exc))[:300]})


async def _scheduled_policy_run(tick: int) -> dict | None:
    """The policy's payment run: at the run times the FCO set (company time), once each; with no run time set, every
    five minutes as before."""
    from . import priority_sop, processlog

    org = app.state.org
    pol = priority_sop.policy(org)
    if not pol.get("run_times"):
        return await _policy_run() if tick % 5 == 0 else None
    tz = priority_sop.run_timezone(pol, processlog.company_tz(org))
    done = {r["_id"] for r in org.list("policy_slot")}
    slot = priority_sop.due_slot(pol, datetime.now(UTC), tz, done)
    if slot is None:
        return None
    await asyncio.to_thread(org._put, "policy_slot", {"_id": slot, "at": datetime.now(UTC).isoformat(timespec="seconds")}, "VENTHORA (payment run)")
    out = await _policy_run()
    _note("Payments", f"Scheduled payment run {slot} {tz}: paid {', '.join(out.get('paid') or []) or 'nothing'}"
                      f"{'; deemed approved ' + ', '.join(out['deemed']) if out.get('deemed') else ''}"
                      f"{'; waiting ' + str(len(out['waiting'])) if out.get('waiting') else ''}{'; ' + out['skipped'] if out.get('skipped') else ''}")
    await asyncio.to_thread(app.state.audit.append, "policy_scheduled_run", {"slot": slot, "timezone": tz, "paid": out.get("paid"), "skipped": out.get("skipped")})
    return out


async def _policy_run(now: datetime | None = None) -> dict:
    """Standard Payment Priority Policy, high priority first: plan the horizon, and pay what the policy lets the agent
    pay now. A payment a person holds is never paid by it. Never raises; what it did and why is on the audit chain."""
    from . import autonomy, priority_sop

    org = app.state.org
    pol = priority_sop.policy(org)
    out: dict = {"paid": [], "deemed": [], "waiting": [], "notices": []}
    if not (priority_sop.saved(org) or {}).get("policy") or pol["mode"] != "priority" or not app.state.agent.proof.airwallex.configured:
        return out | {"skipped": "the policy is not high priority first, or the bank is not connected"}
    now = now or datetime.now(UTC)
    state, source = await _todays_state()
    if state is None:
        return out | {"skipped": "nothing waits for payment"}
    holds = priority_sop.holds(org)
    if holds:
        left = [o for o in state.obligations if o.obligation_id not in holds]
        if not left:
            return out | {"skipped": "every payment is held by a person"}
        state = state.model_copy(update={"obligations": left})
    s = await autonomy.split(app.state.agent, state)
    by_id = {o.obligation_id: o for o in state.obligations}
    auto = [d for d in (s.auto_plan.obligations if s.auto_plan else []) if d.action in autonomy.PAYING]
    if not pol["cascade"]:  # only High priority is paid by the policy; Medium and Low wait for a person
        out["waiting"] += [{"obligation_id": d.obligation_id, "why": "the policy pays High priority only"} for d in auto if by_id[d.obligation_id].priority < 8]
        auto = [d for d in auto if by_id[d.obligation_id].priority >= 8]
    short = [d.obligation_id for d in s.full.obligations if d.action == "DEFER"] + ([s.stopped_for_cash] if s.stopped_for_cash else [])
    if short and not pol["pay_what_fits"]:
        out["waiting"] += [{"obligation_id": d.obligation_id, "why": "the balance does not cover every payment: the policy pays none"} for d in auto]
        auto = []
    if short and pol["notify_fco"]:
        out["notices"].append(priority_sop.notify(org, f"short-{now.date()}-{'-'.join(sorted(short))[:120]}", "The balance does not cover every payment",
                                                  "These payments wait in Pending payment: " + ", ".join(sorted(short))
                                                  + ("; the payments that fit are paid in priority order." if pol["pay_what_fits"] else "; the policy pays none until all fit."),
                                                  sorted(short)))
    # when: a currency payment is bought FX_BEFORE_H before its deadline, at the planned rate or better; the rest now
    due, rates = [], {}
    for d in auto:
        o = by_id[d.obligation_id]
        if pol["fx_at_plan"] and o.currency != state.base_currency:
            planned = priority_sop.planned_rate(org, o.obligation_id, o.currency, state.fx_to_base.get(o.currency))
            if priority_sop.hours_left(o.model_dump(), now) > priority_sop.FX_BEFORE_H:
                out["waiting"].append({"obligation_id": o.obligation_id, "why": f"bought {priority_sop.FX_BEFORE_H} h before its deadline, at the planned rate {planned}"})
                continue
            if planned:
                rates[o.currency] = min(rates.get(o.currency, planned), planned, key=lambda r: float(r))
        due.append(d.obligation_id)

    async def pay(ids: list[str], approval=None, label="policy") -> dict | None:
        paying_all = {d.obligation_id for d in s.full.obligations if d.action in autonomy.PAYING}
        book = state.model_copy(update={"obligations": [o for o in state.obligations if o.obligation_id not in (paying_all - set(ids))]})
        if rates:
            book = book.model_copy(update={"fx_to_base": book.fx_to_base | {c: r for c, r in rates.items() if c in {by_id[i].currency for i in ids}}})
        plan = await app.state.agent.decide(book)
        pays = {d.obligation_id for d in plan.obligations if d.action in autonomy.PAYING}
        if not set(ids) <= pays or plan.decision == "BLOCKED" or (approval is None and plan.decision != "APPROVED"):
            return None
        if approval is not None and plan.decision == "REQUIRES_APPROVAL":
            req = plan.approval_requirement or {}
            if req.get("approvals_required", 1) > 1 or getattr(getattr(state.tenancy, "config", None), "approvers", None):
                priority_sop.notify(org, f"deemed-{ids[0]}", "Deemed approval does not cover this payment",
                                    f"{ids[0]} needs {req.get('approvals_required', 1)} approvals or a named approver; a person must approve it before its deadline.", ids)
                return None
        res = await app.state.executor.execute(ExecutionRequest(
            state=book, plan_hash=plan.plan_hash, requested_by=f"VENTHORA ({label})", approval=approval,
            max_quote_slippage_bp=0 if rates else 100))
        await asyncio.to_thread(app.state.audit.append, f"policy_{label}", {"obligations": ids, "execution_id": res.execution_id, "status": res.status,
                                                                            "message": res.message[:300]})
        _note("Payments", f"Payment run · Standard Payment Priority Policy ({label.replace('_', ' ')}) paid {', '.join(ids)}: "
                          f"execution {res.execution_id} {res.status}; {res.message[:200]}")
        if res.status not in ("COMPLETED", "IN_FLIGHT") and pol["notify_fco"]:
            priority_sop.notify(org, f"failed-{'-'.join(ids)[:150]}", "A planned payment was not made", f"{', '.join(ids)}: {res.message[:400]}", ids)
        return {"execution_id": res.execution_id, "status": res.status}

    if due:
        r = await pay(due)
        if r:
            out["execution"] = r
            out["paid" if r["status"] in ("COMPLETED", "IN_FLIGHT") else "failed"] = due
    if pol["deemed_approval"]:
        from .execution.models import Approval

        for h in s.held:
            o = by_id.get(h["obligation_id"])
            if o is None or autonomy.CASH_REASONS & set(h["reasons"]) or priority_sop.hours_left(o.model_dump(), now) > priority_sop.DEEMED_BEFORE_H:
                continue
            if priority_sop.hours_left(o.model_dump(), now) < 0:
                continue  # past its deadline: a person decides
            role = (state.policy.approver_roles or ["finance_manager"])[0]
            r = await pay([o.obligation_id], Approval(approver="Deemed approval (Standard Payment Priority Policy)", role=role,
                                                     note=f"no intervention {priority_sop.DEEMED_BEFORE_H} h before the deadline"), "deemed_approval")
            if r and r["status"] in ("COMPLETED", "IN_FLIGHT"):
                out["deemed"].append(o.obligation_id)
    app.state.position_cache = None
    return out


@app.post("/api/live/start")
async def live_start(req: LiveStart):
    _scenario_file(req.scenario)
    if not app.state.executor.bank.configured:
        raise HTTPException(409, "Airwallex sandbox credentials are not configured")
    try:
        from . import priority_sop

        s = await live_session.start(req.scenario, app.state.executor, app.state.audit, req.config,
                                     adjust=lambda st: priority_sop.apply(st, app.state.org, app.state.intake.registry.payees))
    except live_session.LiveError as exc:
        raise HTTPException(422, str(exc)) from exc
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex: {exc}") from exc
    sessions = getattr(app.state, "live", {})
    sessions[s.sid] = s
    app.state.live = sessions
    return live_session.view(s)


def _live(sid: str):
    s = getattr(app.state, "live", {}).get(sid)
    if s is None:
        raise HTTPException(404, "unknown live session")
    return s


@app.get("/api/live/{sid}")
async def live_get(sid: str):
    return live_session.view(_live(sid))


@app.post("/api/live/{sid}/step")
async def live_step(sid: str, req: LiveStep):
    s = _live(sid)
    try:
        return await live_session.step(s, app.state.executor, app.state.audit, approver=req.approver, role=req.role)
    except live_session.LiveError as exc:
        raise HTTPException(409, str(exc)) from exc
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex: {exc}") from exc


@app.get("/api/live/{sid}/wallet")
async def live_wallet(sid: str):
    """Polled by the page: the live Airwallex wallet, the book's managed budget, and its ledger once it has one."""
    s = _live(sid)
    store = app.state.executor.store
    book = s.state.book_id if s.state else None
    return {"at": datetime.now(UTC).isoformat(timespec="seconds"), "wallet": await live_session.wallet(app.state.executor.bank),
            "start": s.wallet_start, "managed": dict(s.state.balances) if s.state else {},
            "ledger": ({c: {k: v for k, v in a.items() if v} for c, a in store.book_balances(book).items()} if book and store.has_book(book) else {})}


@app.get("/v1/tenants")
async def tenants():
    """The tenant view: every tenant, its books (exact or pattern), allowed payees, approvers, policy overrides, and for each
    book id the store has seen, its ledger cash and execution count."""
    reg = load_tenant_registry()
    store = app.state.executor.store
    seen: dict[str, int] = {}
    for e in store.list_executions(500):
        row = store.get_execution(e["execution_id"])
        book = (((row or {}).get("request") or {}).get("request") or {}).get("state", {}).get("book_id") if row else None
        if book:
            seen[book] = seen.get(book, 0) + 1
    books = set(store.books())
    out = []
    for tenant, t in (reg.get("tenants") or {}).items():
        for pattern, raw in (t.get("books") or {}).items():
            matching = sorted(b for b in books | set(seen) if resolve_tenancy(reg, b).config is not None
                              and resolve_tenancy(reg, b).config.tenant == tenant and resolve_tenancy(reg, b).config.book_id == pattern)
            out.append({"tenant": tenant, "tenant_name": t.get("name"), "book": pattern, "pattern": any(ch in pattern for ch in "*?["),
                        "allowed_payees": raw.get("allowed_payees") or ["*"], "approvers": raw.get("approvers") or [],
                        "policy_overrides": raw.get("policy_overrides") or {}, "internal": bool(raw.get("internal")),
                        "books_seen": [{"book_id": b, "executions": seen.get(b, 0),
                                        "cash": {c: a.get("cash", 0) for c, a in store.book_balances(b).items()} if b in books else {}}
                                       for b in matching]})
    return {"strict": reg.get("strict", False), "default_tenant": reg.get("default_tenant"), "rows": out}


@app.post("/v1/tenants/eval")
async def tenants_eval(inp: Annotated[dict, Body()]):
    """Pure resolution of a book id against a registry (ground truth / the form)."""
    missing = [k for k in ("registry", "book_id") if k not in inp]
    if missing:
        raise HTTPException(422, f"missing field(s): {', '.join(missing)}")
    if not isinstance(inp["registry"], dict) or not isinstance(inp["book_id"], str):
        raise HTTPException(422, "registry must be an object and book_id a string")
    return tenancy_evaluate(inp)


@app.post("/v1/aging/eval")
async def aging_eval(inp: Annotated[dict, Body()]):
    """Pure evaluation of invoice aging (ground truth / the form): buckets per payee, days overdue, late interest, reasons."""
    return aging_evaluate(inp)


@app.get("/v1/aging")
async def aging_report(scenario_id: str = "kit01_initial", as_of: str | None = None):
    """The aging table of a book as of a date: every payable obligation with its bucket, dunning level and late interest,
    per payee, with the plan's own actions (what this plan pays, what it leaves overdue)."""
    path = _scenario_file(scenario_id)
    if path is None:
        raise HTTPException(404, "unknown scenario")
    state = TreasuryState.model_validate(json.loads(path.read_text(encoding="utf-8"))["state"])
    if as_of:
        state = state.model_copy(update={"as_of": as_of})
    plan = await app.state.agent.decide(state)
    return {"scenario_id": scenario_id, "decision": plan.decision, "aging": plan.aging}


@app.post("/v1/approvals/eval")
async def approvals_eval(inp: Annotated[dict, Body()]):
    """Pure evaluation of the approval matrix (ground truth / the form): the tier that applies and whether the approvals
    given satisfy it (roles, terms, different people, never the requester)."""
    return approvals_evaluate(inp)


@app.post("/v1/payees/eval")
async def payees_eval(inp: Annotated[dict, Body()]):
    """Pure evaluation of lifecycle events (ground truth / the form): the record after the events, refusals named."""
    return payees_evaluate(inp)


class CallbackRequest(BaseModel):
    by: str = Field(min_length=1, max_length=120, description="the person who made the call")
    role: str = Field(min_length=1, max_length=60)
    called_number: str = Field(min_length=3, max_length=40, description="must be the registered number, not one from the request")
    note: str = Field(default="", max_length=500)


class BankChangeRequest(BaseModel):
    bank_fingerprint: str = Field(min_length=1, max_length=200, description="fingerprint of the new details")
    source: str = Field(min_length=1, max_length=200, description="where the change came from (email intake id, portal, letter)")
    note: str = Field(default="", max_length=500)


class PayeeAction(BaseModel):
    by: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=60)
    reason: str = Field(default="", max_length=500)


def _payee_event(beneficiary_id: str, event: dict, cooling_hours: int):
    store = app.state.executor.store
    if store.payee(beneficiary_id) is None and event.get("kind") != "register":
        raise HTTPException(404, "unregistered payee")
    try:
        return store.payee_event(beneficiary_id, event, cooling_hours=cooling_hours)
    except PayeeError as exc:
        raise HTTPException(409 if "role" not in str(exc) else 403, str(exc)) from exc


@app.post("/v1/payees/{beneficiary_id}/verify")
async def verify_payee(beneficiary_id: str, req: CallbackRequest, cooling_hours: int = Query(72, ge=0, le=2160)):
    """The mandatory call-back: a finance manager confirms the bank details on the REGISTERED number. Refused on any other
    number, by any other role, or when nothing is pending. From CHANGED the cooling period stays."""
    rec = _payee_event(beneficiary_id, {"kind": "verify", "by": req.by, "role": req.role, "called_number": req.called_number,
                                        "note": req.note}, cooling_hours)
    await asyncio.to_thread(app.state.audit.append, "payee_verified",
                            {"beneficiary_id": beneficiary_id, **req.model_dump(), "status": rec["status"]})
    return rec


class CallbackRecordIn(BaseModel):
    called_number: str = Field(min_length=3, max_length=40, description="the number dialled; must be the registered one")
    by: str = Field(min_length=2, max_length=80, description="who made the call")
    role: str = "finance_manager"
    outcome: Literal["confirmed", "corrected", "denied", "unreachable"]
    corrections: dict[str, str] = Field(default_factory=dict, description="what the payee said is right: account_number, account_name, "
                                        "bank_name, swift_code, registered_phone")
    note: str = Field(default="", max_length=500)


CALLBACK_FIELDS = {"account_number": "bank.account_number", "account_name": "bank.account_name", "bank_name": "bank.bank_name",
                   "swift_code": "bank.swift_code", "registered_phone": "registered_phone"}


def _vendor_view(vid: str) -> dict | None:
    from .masterdata import MasterStore

    v = (_ingest_ctx().get("master") or MasterStore()).get("vendor", vid)
    if v is None:
        return None
    b = v.get("bank") or {}
    acct = str(b.get("account_number") or "")
    return {"name": v.get("name"), "currency": v.get("currency"), "registered_phone": v.get("registered_phone"), "contacts": v.get("contacts") or [],
            "bank_name": b.get("bank_name"), "swift_code": b.get("swift_code"), "account_name": b.get("account_name"),
            "account_number_masked": ("••••" + acct[-4:]) if acct else None, "bank_country_code": b.get("bank_country_code")}


@app.get("/v1/payees/{beneficiary_id}/callback")
async def payee_callback_view(beneficiary_id: str):
    """Payee lifecycle -> Call-back: what is on file to read out on the phone (the account masked), and every call-back so far."""
    rec = app.state.executor.store.payee(beneficiary_id)
    hist = sorted((r for r in app.state.org.list("callback_log") if r.get("beneficiary_id") == beneficiary_id), key=lambda r: r["_id"], reverse=True)
    return {"payee": rec, "vendor": _vendor_view(beneficiary_id), "history": hist[:20]}


@app.post("/v1/payees/{beneficiary_id}/callback")
async def payee_callback(beneficiary_id: str, req: CallbackRecordIn, cooling_hours: int = Query(72, ge=0, le=2160)):
    """Record a call-back on the registered number and what it found. Confirmed: the payee is verified. Corrected: what the
    payee said is right replaces the details on file (a new account starts the cooling period; a new phone number must
    itself be called back before any payment), then the account is verified on this call. Denied: the payee is suspended.
    Unreachable: nothing changes; the attempt is logged. Every call is kept, with who called, the number and the outcome."""
    from . import ingestion

    store = app.state.executor.store
    rec = store.payee(beneficiary_id)
    if rec is None:
        raise HTTPException(404, "not a registered payee")
    if req.role not in ("finance_manager", "cfo", "super_admin"):
        raise HTTPException(403, f"role {req.role} may not record a call-back")
    from .payees import _digits

    if _digits(req.called_number) != _digits(rec.get("registered_phone")):
        raise HTTPException(409, "the call-back must be made to the registered number on file, not to a number taken from an email or invoice")
    role = "finance_manager" if req.role in ("cfo", "super_admin") else req.role
    bad = [k for k in req.corrections if k not in CALLBACK_FIELDS]
    if bad:
        raise HTTPException(422, f"cannot correct: {', '.join(bad)}")
    fixes = {k: v.strip() for k, v in req.corrections.items() if v and v.strip()}
    if req.outcome == "corrected" and not fixes:
        raise HTTPException(422, "say what the payee corrected")
    steps: list[str] = []
    if req.outcome == "corrected":
        phone = fixes.pop("registered_phone", None)
        if fixes:
            master = _ingest_ctx()["master"]
            v = master.get("vendor", beneficiary_id)
            if v is None:
                raise HTTPException(404, "the vendor is not in the master data: correct it in Vendors")
            v = json.loads(json.dumps(v))
            for k, val in fixes.items():
                v.setdefault("bank", {})[CALLBACK_FIELDS[k].split(".", 1)[1]] = val
            rep = await asyncio.to_thread(ingestion.apply, "vendor", [v], [None], source=f"call-back:{req.by}", **_ingest_ctx())
            if rep.get("rejected"):
                raise HTTPException(422, f"the corrected details are not valid: {rep['rejected'][0].get('error')}")
            steps += [f"corrected {', '.join(fixes)}"] + list(rep.get("followed") or [])
        if phone:
            _payee_event(beneficiary_id, {"kind": "phone_change", "registered_phone": phone, "role": role, "by": req.by}, cooling_hours)
            steps.append(f"call-back number changed to {phone}: call that number back before any payment")
    status = (store.payee(beneficiary_id) or {}).get("status")
    if req.outcome == "confirmed" or (req.outcome == "corrected" and not any("call-back number changed" in s for s in steps)):
        if status in ("NEW", "CHANGED"):
            _payee_event(beneficiary_id, {"kind": "verify", "by": req.by, "role": role, "called_number": req.called_number, "note": req.note}, cooling_hours)
            steps.append("account confirmed by the payee on the registered number: verified")
        else:
            steps.append(f"already {status}: nothing to verify")
    elif req.outcome == "denied":
        _payee_event(beneficiary_id, {"kind": "suspend", "role": role, "by": req.by, "note": req.note or "the payee denied the details on file"}, cooling_hours)
        steps.append("the payee denied the details on file: suspended; no payment goes to it")
    elif req.outcome == "unreachable":
        steps.append("no answer on the registered number: nothing changes; call again")
    after = store.payee(beneficiary_id)
    log = {"_id": f"{datetime.now(UTC):%Y%m%d%H%M%S%f}", "beneficiary_id": beneficiary_id, "at": datetime.now(UTC).isoformat(timespec="seconds"),
           "called_number": req.called_number, "by": req.by, "role": req.role, "outcome": req.outcome, "corrected": sorted(req.corrections),
           "note": req.note, "steps": steps, "status_before": rec.get("status"), "status_after": (after or {}).get("status")}
    await asyncio.to_thread(app.state.org._put, "callback_log", log, req.by)
    await asyncio.to_thread(app.state.audit.append, "payee_callback", {k: v for k, v in log.items() if k != "_id"})
    return {"payee": after, "steps": steps, "log": log, "vendor": _vendor_view(beneficiary_id)}


@app.post("/v1/payees/{beneficiary_id}/bank-change")
async def payee_bank_change(beneficiary_id: str, req: BankChangeRequest, cooling_hours: int = Query(72, ge=0, le=2160)):
    """Record that the payee's bank details changed: the payee becomes CHANGED (no payment until a call-back) and the
    cooling period starts now. The same fingerprint as on file is not a change."""
    rec = _payee_event(beneficiary_id, {"kind": "bank_change", "bank_fingerprint": req.bank_fingerprint, "source": req.source,
                                        "note": req.note}, cooling_hours)
    await asyncio.to_thread(app.state.audit.append, "payee_bank_change", {"beneficiary_id": beneficiary_id, **req.model_dump()})
    return rec


@app.post("/v1/payees/{beneficiary_id}/suspend")
async def suspend_payee(beneficiary_id: str, req: PayeeAction):
    rec = _payee_event(beneficiary_id, {"kind": "suspend", **req.model_dump()}, 72)
    await asyncio.to_thread(app.state.audit.append, "payee_suspended", {"beneficiary_id": beneficiary_id, **req.model_dump()})
    return rec


@app.post("/v1/payees/{beneficiary_id}/reinstate")
async def reinstate_payee(beneficiary_id: str, req: PayeeAction):
    """Back to NEW: another call-back is required before any payment."""
    rec = _payee_event(beneficiary_id, {"kind": "reinstate", **req.model_dump()}, 72)
    await asyncio.to_thread(app.state.audit.append, "payee_reinstated", {"beneficiary_id": beneficiary_id, **req.model_dump()})
    return rec


# -- reporting periods ---------------------------------------------------------------------------

@app.get("/v1/reporting/config")
async def reporting_config():
    return load_reporting_config().model_dump()


@app.put("/v1/reporting/config")
async def set_reporting_config(cfg: ReportingConfig, role: str = "admin"):
    """The admin's cut-off (day, time, timezone), reporting currency and fallback closing rates."""
    if role != "admin":
        raise HTTPException(403, "only an admin may change the reporting configuration")
    save_reporting_config(cfg)
    await asyncio.to_thread(app.state.audit.append, "reporting_config", cfg.model_dump())
    return cfg.model_dump()


@app.post("/v1/reporting/eval")
async def reporting_eval(inp: Annotated[dict, Body()]):
    """Pure evaluation (ground truth / the form): which report a posting belongs to, a period's report from a ledger."""
    try:
        return reporting_evaluate(inp)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/v1/reporting/periods/{period}")
async def reporting_period_report(period: PeriodPath):
    """Layer 1 (monthly spend by payment instant), layer 2 (invoice statements as of the period's end), layer 3
    (consolidation in the reporting currency at the period's closing rates)."""
    cfg = load_reporting_config()
    store = app.state.executor.store
    rates = store.closing_rates(period) or cfg.closing_rates.get(period, {})
    source = "snapshot" if store.closing_rates(period) else "admin fallback"
    return period_report(period, store.ledger(), cfg, rates) | {"rates_source": source}


@app.post("/v1/reporting/periods/{period}/rates")
async def snapshot_closing_rates(period: PeriodPath, rates: Annotated[dict[str, str] | None, Body()] = None, role: str = "admin"):
    """Record the period's closing rates: from Airwallex's indicative rates now, or the admin's own figures."""
    if role not in ("admin", "super_admin"):
        raise HTTPException(403, "only an admin or the FCO (Super admin) may set closing rates")
    cfg = load_reporting_config()
    store = app.state.executor.store
    if rates:
        store.set_closing_rates(period, rates, f"admin ({len(rates)} rates)")
        return {"period": period, "rates": rates, "source": "admin"}
    # Per currency: Airwallex's indicative rate when the sandbox quotes the pair; otherwise the admin's fallback for
    # that currency, with the source recorded (observed 23 Oct 2026: the sandbox answers unsupported_currency for IDR).
    bank = _bank()
    out: dict[str, str] = {}
    sources: dict[str, str] = {}
    fallback = cfg.closing_rates.get(period, {})
    for ccy in ("USD", "EUR", "GBP"):
        try:
            out[ccy] = format(await bank.rate_to_base(ccy, cfg.reporting_currency), "f")
            sources[ccy] = "airwallex"
        except AirwallexError as exc:
            if ccy not in fallback:
                raise HTTPException(502, f"Airwallex rate {ccy}/{cfg.reporting_currency}: {exc}; no admin fallback") from exc
            out[ccy], sources[ccy] = fallback[ccy], f"admin fallback ({exc.code or 'airwallex error'})"
    label = "; ".join(f"{c}: {s}" for c, s in sources.items()) + f" @ {datetime.now(UTC).isoformat()}"
    store.set_closing_rates(period, out, label)
    await asyncio.to_thread(app.state.audit.append, "closing_rates", {"period": period, "rates": out, "sources": sources})
    overall = "airwallex" if all(s == "airwallex" for s in sources.values()) else "mixed"
    return {"period": period, "rates": out, "sources": sources, "source": overall}


class GrantRequest(BaseModel):
    extra_minor: int = Field(gt=0, le=10**11)
    approver: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=500)


@app.post("/v1/budgets/{period}/{line}/grant")
async def grant_budget(period: PeriodPath, line: str, req: GrantRequest):
    """A person answers a budget request by raising a line's mandate. Role-checked, audited; the agent never grants."""
    budget = load_budget(period)
    if budget is None or budget.line(line) is None:
        raise HTTPException(404, "unknown period or line")
    if req.role not in ("finance_manager",):
        raise HTTPException(403, f"role {req.role} may not grant budget")
    grant = app.state.executor.store.grant(period, line, req.extra_minor, req.approver, req.role, req.note)
    await asyncio.to_thread(app.state.audit.append, "budget_grant", grant)
    return grant


@app.post("/v1/plans", response_model=PlanResult)
async def plan(state: TreasuryState):
    result = await app.state.agent.decide(state)
    await asyncio.to_thread(app.state.audit.append, "plan", {"state": state.model_dump(), "result": result.model_dump()})
    return result


class ReplanRequest(BaseModel):
    state: TreasuryState
    previous_state: TreasuryState
    previous_plan: PlanResult


@app.post("/v1/plans/replan", response_model=PlanResult)
async def replan(req: ReplanRequest):
    """New information arrived: recalculate only the decisions it affects, with a decision diff."""
    result = await app.state.agent.decide(req.state, previous=(req.previous_state, req.previous_plan))
    await asyncio.to_thread(app.state.audit.append, "replan", {
        "state": req.state.model_dump(), "previous_plan_hash": req.previous_plan.plan_hash,
        "result": result.model_dump()})
    return result


class ChallengeRequest(BaseModel):
    state: TreasuryState
    proposal: list[str] | None = Field(default=None, description="obligation ids to pay; omit to ask the LLM")
    source: str = Field(default="external recommendation", max_length=120)


@app.post("/v1/plans/challenge", response_model=PlanResult)
async def challenge(req: ChallengeRequest):
    """Run an outside recommendation (yours, or the LLM's) through Verity, Horizon and ProofCore."""
    if req.proposal is None:
        proposal, source = await llm_proposal(req.state, active_model())
    else:
        proposal, source = req.proposal, req.source
    result = await app.state.agent.challenge(req.state, proposal, source)
    await asyncio.to_thread(app.state.audit.append, "challenge", {
        "source": source, "proposal": proposal, "decision": result.decision})
    return result


@app.get("/api/audit")
async def audit(limit: int = Query(20, ge=1, le=200)):
    store = app.state.audit
    return {"events": await asyncio.to_thread(store.list, limit), "integrity": await asyncio.to_thread(store.verify)}


@app.post("/api/scenarios/{scenario_id}/live-state", response_model=TreasuryState)
async def live_state(scenario_id: str):
    """Scenario obligations grounded in live sandbox balances and indicative rates.
    Plan it with POST /v1/plans, then execute that exact state with POST /v1/executions."""
    data = json.loads(_scenario_file(scenario_id).read_text(encoding="utf-8"))
    try:
        return await with_live_balances_and_rates(TreasuryState.model_validate(data["state"]), _bank())
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc


@app.post("/v1/executions", response_model=ExecutionResult)
async def execute(req: ExecutionRequest):
    """Execute an approved plan: quote -> conversion -> transfers -> settlement -> reconciliation."""
    return await app.state.executor.execute(req)


class AutonomousIn(BaseModel):
    state: TreasuryState
    requested_by: str | None = Field(default=None, max_length=120)
    simulate_settlement: bool = True


@app.post("/v1/plans/split")
async def plan_split(state: TreasuryState):
    """What the agent executes on its own now and what waits for a person: the largest part of the plan that is
    APPROVED by itself, most urgent first, and every held obligation with the policy's reasons. Nothing is executed."""
    from . import autonomy

    s = await autonomy.split(app.state.agent, state)
    return s.summary() | {"remaining_state": s.remaining_state.model_dump() if s.remaining_state else None}


@app.post("/v1/executions/autonomous")
async def execute_autonomous(req: AutonomousIn):
    """Execute the part of the plan inside the mandate, without an approval, and return what is held: the held
    obligations stay in `remaining_state`, which a person approves through POST /v1/executions as usual."""
    from . import autonomy

    s = await autonomy.split(app.state.agent, req.state)
    out = s.summary() | {"remaining_state": s.remaining_state.model_dump() if s.remaining_state else None, "execution": None}
    if s.auto_plan is not None and any(d.action in autonomy.PAYING for d in s.auto_plan.obligations):
        result = await app.state.executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash,
                                                                   requested_by=req.requested_by or "agent (autonomous)",
                                                                   simulate_settlement=req.simulate_settlement))
        out["execution"] = result.model_dump()
    await asyncio.to_thread(app.state.audit.append, "autonomous_execution", {k: v for k, v in out.items() if k != "remaining_state"})
    return out


@app.post("/v1/executions/{execution_id}/resume", response_model=ExecutionResult)
async def resume_execution(execution_id: str):
    try:
        return await app.state.executor.resume(execution_id)
    except KeyError as exc:
        raise HTTPException(404, "unknown execution") from exc


@app.get("/v1/executions/{execution_id}")
async def get_execution(execution_id: str):
    record = app.state.executor.store.get_execution(execution_id)
    if record is None:
        raise HTTPException(404, "unknown execution")
    return {"execution_id": execution_id, "status": record["status"], "result": record["result"],
            "operations": app.state.executor.store.operations(execution_id)}


@app.post("/v1/incidents", response_model=IncidentResult)
async def handle_incident(req: IncidentRequest):
    """Kit 03: a sent transfer did not arrive. Decide WAIT / REPLACE / ESCALATE under finality,
    idempotency and the duplicate lock; a replacement needs a new request_id and its own fee."""
    try:
        return await IncidentCommander(app.state.executor).handle(req)
    except (KeyError, LookupError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc


class IncidentDecisionRequest(BaseModel):
    """The Kit 03 decision table as a pure function: what the rail reported, how long it has been in flight."""
    status: str = Field(min_length=1, max_length=40)
    failure_type: str | None = Field(default=None, max_length=64)
    hours_in_flight: int = Field(default=0, ge=0, le=100_000)
    replacements_so_far: int = Field(default=0, ge=0, le=10)
    incident_wait_hours: int = Field(default=48, ge=0, le=720)
    max_replacements: int = Field(default=1, ge=0, le=3)


@app.post("/v1/incidents/decide")
async def decide_incident(req: IncidentDecisionRequest):
    """WAIT / REPLACE / ESCALATE / RESOLVE from the typed failure and finality, with the reason. No money moves."""
    from types import SimpleNamespace

    from .execution.incidents import decide
    from .gateways.airwallex import TransferResult

    transfer = TransferResult(transfer_id="t", status=req.status.upper(), source_currency="", transfer_currency="",
                              transfer_amount_minor=0, amount_payer_pays_minor=0, fee_minor=0, failure_reason=None, raw={},
                              failure_type=req.failure_type)
    policy = SimpleNamespace(incident_wait_hours=req.incident_wait_hours, max_replacements=req.max_replacements)
    d = decide(transfer, req.hours_in_flight, policy, req.replacements_so_far)
    return {"action": d.action, "reason": d.reason}


@app.get("/v1/incidents")
async def list_incidents():
    return app.state.executor.store.list_incidents()


@app.get("/v1/incidents/{incident_id}")
async def get_incident(incident_id: str):
    record = app.state.executor.store.get_incident(incident_id)
    if record is None:
        raise HTTPException(404, "unknown incident")
    return record


# -- intake: payment requests from external users ---------------------------------------------

@app.get("/v1/intake", response_model=list[IntakeRequest])
async def list_intake():
    """The queue, each request with the money that settled it (PAID transfers booked against its invoice)."""
    def build():
        paid: dict[str, dict] = {}
        for r in app.state.executor.store.paid_rows():
            if not r.get("invoice_fingerprint"):
                continue
            p = paid.setdefault(r["invoice_fingerprint"], {"amount_minor": 0, "currency": r["currency"], "transfers": [], "paid_at": r["paid_at"], "base_cost_minor": 0})
            p["amount_minor"] += r["amount_minor"]
            p["base_cost_minor"] += r.get("base_cost_minor") or 0
            p["transfers"].append(r["transfer_id"])
            p["paid_at"] = max(p["paid_at"], r["paid_at"])
        return [q.model_copy(update={"payment": paid.get(q.invoice_fingerprint)}) if q.invoice_fingerprint in paid else q
                for q in app.state.intake.store.list()]
    return await asyncio.to_thread(build)


@app.post("/v1/intake/emails", response_model=IntakeRequest)
async def ingest_email(raw: bytes = Body(..., media_type="message/rfc822"), dry_run: bool = False,
                       book: str | None = None):
    """Submit one raw email (RFC 822). The sender is verified against the counterparty registry, every
    field is tied to a verbatim quote, and the request is queued as VERIFIED / NEEDS_REVIEW / QUARANTINED
    / DUPLICATE. Nothing in the email text can change that outcome. dry_run=true triages without queueing;
    book=<scenario id> checks duplicates against that book's invoices."""
    if len(raw) > MAX_EMAIL_BYTES:
        raise HTTPException(413, "email too large")
    fps = book_fingerprints(book) if book else None
    if book and not fps and not _scenario_file(book):
        raise HTTPException(404, "unknown scenario")
    return await _ingest(raw, fps, dry_run, channel="api")


MAX_EMAIL_BYTES = 12 * 1024 * 1024  # an invoice PDF or two; Postmark caps inbound mail at 35 MB


async def _ingest(raw: bytes, fps: dict | None, dry_run: bool, *, channel: str, simulate: bool = False, use_model: bool = True) -> IntakeRequest:
    """One email through the model reader (when configured) and the rules; queued, audited and pushed to the dashboards."""
    from .intake import reader
    from .intake.pipeline import parse_eml

    parsed = await asyncio.to_thread(parse_eml, raw)
    model = active_model()  # Settings → AI model, else .env; rules only when neither names a model
    proposals = await reader.read(parsed["text"], parsed["subject"], datetime.fromisoformat(parsed["received_at"]).date(),
                                  model) if model.configured and use_model else {}
    req = await asyncio.to_thread(app.state.intake.ingest, raw, book_fingerprints=fps, save=not dry_run, proposals=proposals,
                                  channel="dashboard" if channel == "dashboard" else "email")
    # a request forwarded by someone the delegation of authority names is governed by the delegation, not by the
    # payment-order-by-email rule (the FCO's naming is the authorisation)
    if req.trust == "staff" and not dry_run:
        from . import fund_email, pr_email

        if fund_email.looks_like(req.subject or "", req.text or ""):
            req = await _fund_from_email(req, model)
        elif pr_email.looks_like(req.subject or "", req.text or ""):
            req = await _pr_from_email(req, model)
    if req.trust == "staff" and not dry_run and "doa_forwarded" not in req.flags and not any(f.startswith(("purchase_request", "fund_request")) for f in req.flags):
        order, req = await _email_order(req, raw)
        if order is not None:
            return order
    if "po_unknown" in req.flags and req.po_number and not dry_run:
        req = await _po_from_sources(req, raw, fps, proposals)
    req = await _validate_documents(req, raw, model, save=not dry_run)
    if not dry_run:
        app.state.position_cache = None  # a new bill or remittance changes the position
        app.state.hub.publish("intake", {"channel": channel} | req.model_dump(include={
            "intake_id", "kind", "status", "trust", "sender", "counterparty_name", "amount_minor", "currency", "invoice_no",
            "reader", "flags"}))
        await asyncio.to_thread(app.state.audit.append, "intake", req.model_dump(exclude={"text"}))
        # Payee lifecycle: a registered counterparty announcing new bank details moves to CHANGED at once (no payment
        # until a call-back on the registered number), whatever the email says about urgency.
        if "payee_bank_details_changed" in req.flags and req.counterparty_id and not simulate:  # a simulation never changes a real payee
            quoted = " | ".join(q.quote for q in req.quotes if q.field == "bank_change") or req.subject
            fp = "email:" + hashlib.sha256(quoted.encode("utf-8")).hexdigest()[:16]
            store = app.state.executor.store
            if store.payee(req.counterparty_id) is not None:
                with contextlib.suppress(PayeeError):
                    store.payee_event(req.counterparty_id, {"kind": "bank_change", "bank_fingerprint": fp,
                                                            "source": f"intake:{req.intake_id}", "quote": quoted[:300]})
        if not simulate:  # a simulation plans; it never pays on arrival
            _after_intake(req)  # verified and inside the mandate: paid now, when the standing mandate says so
        with contextlib.suppress(Exception):  # the FCO's notice: one email per request that needs a person or a first payment
            await _fco_notice(req, simulated=simulate)
    return req


def _public_url() -> str:
    return (env("VENTHORA_PUBLIC_URL") or "https://venthora.muwahid.org").rstrip("/")


def _notice_kind(req) -> str | None:
    """needs_review | first_payment | None, for one request as it stands now."""
    if req is None or req.payment:
        return None
    if req.status in ("NEEDS_REVIEW", "QUARANTINED"):
        return "needs_review"
    if req.status == "VERIFIED" and req.kind in PAYABLE_KINDS and req.counterparty_id:
        p = app.state.executor.store.payee(req.counterparty_id) or {}
        if p.get("status") == "VERIFIED" and not p.get("paid_count"):
            return "first_payment"
    return None


async def _fco_notice(req, *, simulated: bool = False, force: bool = False) -> dict | None:
    """Queue and send one notice to the FCO for this request (never twice; capped per hour). force: sent on request."""
    from . import fco_notify, outbox

    org = app.state.org
    s = fco_notify.settings(org)
    kind = _notice_kind(req)
    if kind is None or not s["to"] or (not force and (not s["enabled"] or not s[kind])):
        return None
    if not force and ((simulated or "[SIMULATION]" in (req.subject or "")) and not s["include_simulations"]):
        return None
    if not force and (fco_notify.already(org, req.intake_id, kind) or fco_notify.sent_last_hour(org) >= s["max_per_hour"]):
        return None
    token = fco_notify.make_link(org, req.intake_id, kind) if kind == "first_payment" else None
    payee = app.state.executor.store.payee(req.counterparty_id or "") or {}
    subject, body = fco_notify.compose(req, kind, _public_url(), token, payee | {"fco_name": fco_notify.fco_name(org)})
    rec = await asyncio.to_thread(outbox.queue, org, to=[s["to"]], subject=subject, body=body, ref=f"{kind}:{req.intake_id}",
                                  by="VENTHORA (FCO notice)", kind="fco_notice")
    rec = await asyncio.to_thread(outbox.deliver, org, rec["_id"])
    _note("Invoices & intake", f"Notice to the FCO ({s['to']}): {'first payment, approve link' if kind == 'first_payment' else 'needs a person'} · "
                               f"{req.counterparty_name or req.sender} {req.invoice_no or ''} · email {rec.get('status')}")
    return rec


class FcoNotifyIn(BaseModel):
    enabled: bool | None = None
    to: str | None = Field(None, max_length=200)
    needs_review: bool | None = None
    first_payment: bool | None = None
    include_simulations: bool | None = None
    max_per_hour: int | None = Field(None, ge=1, le=200)


@app.get("/v1/fco-notify")
async def fco_notify_get():
    from . import fco_notify, outbox

    sent = sorted((r for r in app.state.org.list("outbox") if r.get("kind") == "fco_notice"), key=lambda r: r.get("created_at", ""), reverse=True)
    return {"settings": fco_notify.settings(app.state.org), "sender": outbox.smtp(app.state.org).get("sender"),
            "sent_last_hour": fco_notify.sent_last_hour(app.state.org), "recent": [{k: r.get(k) for k in ("created_at", "subject", "to", "status", "error", "ref")} for r in sent[:15]]}


@app.put("/v1/fco-notify")
async def fco_notify_put(body: FcoNotifyIn, role: str = "super_admin", by: str = "dashboard"):
    from . import fco_notify

    if role != "super_admin":
        raise HTTPException(403, "only the FCO sets the notices")
    try:
        return {"settings": await asyncio.to_thread(fco_notify.save, app.state.org, body.model_dump(exclude_none=True), f"{by} ({role})")}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/v1/fco-notify/send/{intake_id}")
async def fco_notify_send(intake_id: str, role: str = "finance_manager", by: str = "dashboard"):
    """Email the FCO about this one request now (needs a person, or a first payment with its approve link)."""
    _writer(role, by)
    req = app.state.intake.store.get(intake_id)
    if req is None:
        raise HTTPException(404, "no such request")
    if _notice_kind(req) is None:
        raise HTTPException(409, "nothing to ask the FCO: it is not held for a person and it is not a first payment")
    rec = await _fco_notice(req, force=True)
    if rec is None:
        raise HTTPException(409, "set the FCO's email address first (FCO Authorize → Payment Priority Policy → Notices)")
    return {"status": rec.get("status"), "to": rec.get("to"), "error": rec.get("error"), "subject": rec.get("subject")}


def _approve_page(title: str, body: str, status: int = 200) -> HTMLResponse:
    import html as _h

    return HTMLResponse(f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>VENTHORA · {_h.escape(title)}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#0f1a3a;margin:0;padding:24px;color:#16213a}}.c{{max-width:520px;margin:30px auto;background:#fff;border-radius:14px;padding:22px 24px}}
h1{{font-size:19px;margin:0 0 10px}}table{{width:100%;border-collapse:collapse;margin:10px 0}}td{{padding:5px 0;border-bottom:1px solid #eef}}td:first-child{{color:#667;width:38%}}
button{{background:#4f46e5;color:#fff;border:0;border-radius:9px;padding:11px 18px;font-size:15px;cursor:pointer}}.m{{color:#667;font-size:13px}}.ok{{color:#11823b}}.bad{{color:#c0312b}}</style>
<div class=c><div class=m>VENTHORA · payment approval</div><h1>{_h.escape(title)}</h1>{body}</div>""", status_code=status)


@app.get("/approve/{token}", include_in_schema=False)
async def approve_link_page(token: str):
    """The page an approve link opens: the payment again and one button. Opening it approves nothing."""
    import html as _h

    from . import fco_notify

    try:
        link = await asyncio.to_thread(fco_notify.check_link, app.state.org, token)
    except LookupError:
        return _approve_page("Link not recognised", "<p>This link does not belong to this company.</p>", 404)
    except ValueError as exc:
        return _approve_page("Link no longer valid", f"<p class=bad>{_h.escape(str(exc))}</p>", 410)
    req = app.state.intake.store.get(link["intake_id"])
    if req is None or req.payment:
        return _approve_page("Nothing to approve", "<p>This payment was already paid or no longer exists.</p>", 410)
    rows = [("Payee", req.counterparty_name or req.display_name), ("Invoice", req.invoice_no or "-"), ("Amount", f"{req.currency} {(req.amount_minor or 0) / 100:,.2f}"),
            ("Due", req.due_date or "-"), ("Purchase order", req.po_number or "-"), ("Status", req.status)]
    table = "".join(f"<tr><td>{_h.escape(k)}</td><td><b>{_h.escape(str(v))}</b></td></tr>" for k, v in rows)
    return _approve_page("Approve this first payment?", f"""<table>{table}</table>
<form method=post><button type=submit>Approve &amp; pay</button></form>
<p class=m>Approving as {_h.escape(fco_notify.fco_name(app.state.org))} (FCO). VENTHORA still checks the balance, the reserve floor and the limits before Airwallex is called. The link works once.</p>""")


@app.post("/approve/{token}", include_in_schema=False)
async def approve_link_do(token: str):
    import html as _h

    from . import fco_notify, mandate

    org = app.state.org
    try:
        link = await asyncio.to_thread(fco_notify.check_link, org, token)
    except LookupError:
        return _approve_page("Link not recognised", "<p>This link does not belong to this company.</p>", 404)
    except ValueError as exc:
        return _approve_page("Link no longer valid", f"<p class=bad>{_h.escape(str(exc))}</p>", 410)
    who, role = fco_notify.fco_name(org), mandate.standing(org)["authority_role"]
    await asyncio.to_thread(fco_notify.use_link, org, token, {"status": "approving"})  # once: a second click finds it used
    try:
        out = await intake_pay(link["intake_id"], CorrectionIn(note="approved from the FCO's email"), role=role, by=who)
    except HTTPException as exc:
        msg = exc.detail if isinstance(exc.detail, str) else (exc.detail or {}).get("message", str(exc.detail))
        await asyncio.to_thread(fco_notify.use_link, org, token, {"status": "refused", "detail": msg})
        _note("Payments", f"Approve link from email refused: {msg}")
        return _approve_page("Not approved", f"<p class=bad>{_h.escape(msg)}</p><p class=m>Open Pending payment in VENTHORA to see why.</p>", 409)
    ex = out.get("execution") or {}
    await asyncio.to_thread(fco_notify.use_link, org, token, {"status": "paid" if out.get("paid") else "approved", "execution_id": ex.get("execution_id")})
    await asyncio.to_thread(app.state.audit.append, "approved_from_email", {"intake_id": link["intake_id"], "by": who, "paid": out.get("paid"), "execution_id": ex.get("execution_id")})
    _note("Payments", f"First payment approved from the FCO's email by {who}: {'paid, ' + str(ex.get('status')) if out.get('paid') else out.get('message')}")
    if out.get("paid"):
        return _approve_page("Approved and paid", f"<p class=ok>Paid: {_h.escape(str(ex.get('status')))} · execution {_h.escape(str(ex.get('execution_id')))}</p><p class=m>The payee is now active; its next payments inside the mandate go on their own.</p>")
    return _approve_page("Approved, not paid yet", f"<p>{_h.escape(out.get('message') or '')}</p>")


async def _po_from_sources(req: IntakeRequest, raw: bytes, fps: dict | None, proposals: dict) -> IntakeRequest:
    """An invoice quotes a PO VENTHORA does not have: ask every connected source (Settings → Sync API) for it. Found:
    the PO is written through the usual checks and the invoice is read again against it. Not found: it waits for a
    person, with the sources that were asked in the reason."""
    po_id = req.po_number
    got, asked = await _pull_po(po_id, req.intake_id)
    if got:
        fresh = await asyncio.to_thread(app.state.intake.ingest, raw, book_fingerprints=fps, save=False, proposals=proposals,
                                        retriage_of=req.intake_id)
        out = fresh.model_copy(update={"intake_id": req.intake_id,
                                       "reasons": [f"PO {po_id} was not in VENTHORA; pulled from {got} and matched again"] + fresh.reasons})
    else:
        out = req.model_copy(update={"reasons": req.reasons + [_po_missing_reason(po_id, asked)]})
    await asyncio.to_thread(app.state.intake.store.save, out)
    return out


def _po_missing_reason(po_id: str, asked: list[str]) -> str:
    return (f"PO {po_id} is not in VENTHORA and none of the connected sources has it (asked: {', '.join(asked)})" if asked
            else f"PO {po_id} is not in VENTHORA and no source is connected to pull it from (Settings → Sync API)")


async def _pull_po(po_id: str, for_id: str) -> tuple[str | None, list[str]]:
    """Ask every connected source for one PO and write it through the usual checks. (source name or None, asked)."""
    from . import ingestion
    from .connectors import ConnectorStore, fetch

    asked, got = [], None
    store = ConnectorStore()
    for c in store.list():
        raw_c = store.get_raw(c["connector_id"])
        em = (raw_c or {}).get("entities", {}).get("po")
        if not em or not em.get("enabled", True):
            continue
        asked.append(c["name"])
        try:
            rows, ids = await fetch(raw_c, "po")
        except Exception:  # noqa: BLE001 - a source that does not answer is reported in the reason
            continue
        hit = [(r, i) for r, i in zip(rows, ids, strict=False) if str(r.get("po_id", "")).upper() == po_id]
        if hit:
            rep = await asyncio.to_thread(ingestion.apply, "po", [r for r, _ in hit], [i for _, i in hit], source=f"sync:{c['connector_id']}",
                                          **_ingest_ctx())
            if app.state.org.get("po", po_id) is not None:
                got = c["name"]
                await asyncio.to_thread(app.state.audit.append, "po_pulled", {"po": po_id, "connector": c["connector_id"], "for": for_id,
                                                                            "report": {k: v for k, v in rep.items() if k in ("created", "updated", "rejected")}})
                break
    return got, asked


class OrderAttachment(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content_type: str = Field("application/pdf", max_length=100)
    base64: str = Field(max_length=MAX_EMAIL_BYTES * 2)


class PaymentOrderIn(BaseModel):
    """A payment someone inside the company asks for: an invoice in hand, a contractor, a supplier deposit."""
    vendor_id: str = Field(min_length=1, max_length=80, description="a registered payee")
    invoice_no: str = Field(min_length=2, max_length=40, description="the invoice or reference being paid")
    amount_minor: int = Field(gt=0, le=10**13)
    currency: str = Field(min_length=3, max_length=3)
    due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    po_number: str | None = Field(None, max_length=40)
    description: str = Field("", max_length=300)
    attachments: list[OrderAttachment] = Field(default_factory=list, max_length=10)
    category: str | None = Field(None, max_length=60, description="a category of the FCO's own (Delegation of Authority)")
    pr_number: str | None = Field(None, max_length=40, description="the purchase request number")


@app.post("/v1/intake/payment-orders", response_model=IntakeRequest)
async def payment_order(body: PaymentOrderIn, role: str = "finance_manager", by: str = "dashboard"):
    """An internal payment order into the same queue as emailed invoices, through the same checks: the payee must be
    registered, the PO must match (pulled from a connected source when VENTHORA does not have it), duplicates are
    refused, attached documents are validated. It always waits for a person other than the one who asked."""
    import base64
    from email.message import EmailMessage

    person = next((u for u in app.state.org.list("user") if u.get("active", True)
                   and by.strip().lower() in (u["name"].lower(), u["email"].lower())), None)
    if person is None:
        raise HTTPException(403, f"{by} is not an active person in User management; only people of the organisation may ask for a payment")
    msg = EmailMessage()  # the attachments as one message, so documents are read and validated like an email's
    msg["From"] = person["email"]
    msg["Subject"] = f"Payment order {body.invoice_no.upper()}"
    msg.set_content(f"Payment order {body.invoice_no.upper()} for {body.vendor_id}. {body.description}")
    for a in body.attachments:
        main, _, sub = a.content_type.partition("/")
        msg.add_attachment(base64.b64decode(a.base64), maintype=main or "application", subtype=sub or "octet-stream", filename=a.name)
    return await _payment_order_core(body, person, msg, channel="internal", role=role)


async def _payment_order_core(body: PaymentOrderIn, person: dict, msg, *, channel: str, role: str,
                              note: str | None = None) -> IntakeRequest:
    """One payment order into the queue: registered payee, matching PO (pulled from a source when missing), no
    duplicate, validated documents, and always a different person to approve it."""
    import uuid

    from . import doa
    from .intake.documents import attachments_of
    from .intake.models import Attachment

    po_id = (body.po_number or "").strip().upper() or None
    pulled = None
    if po_id and app.state.org.get("po", po_id) is None:
        pulled = await _pull_po(po_id, f"order:{body.invoice_no}")
    bill = {"bill_id": f"ORDER-{uuid.uuid4().hex[:10]}", "vendor_id": body.vendor_id, "invoice_no": body.invoice_no.upper(),
            "amount_minor": body.amount_minor, "currency": body.currency.upper(), "due_date": body.due_date,
            "po_number": po_id, "description": body.description}
    req = await asyncio.to_thread(app.state.intake.ingest_record, bill, source=f"internal:{person['user_id']}")
    reasons = [note or f"internal payment order by {person['name']} ({person['role'].replace('_', ' ')}); a different person approves it"]
    if pulled and pulled[0]:
        reasons.append(f"PO {po_id} was not in VENTHORA; pulled from {pulled[0]}")
    elif pulled:
        reasons.append(_po_missing_reason(po_id, pulled[1]))
    reasons += [r for r in req.reasons if not r.startswith("from the company's own system")]
    files = await asyncio.to_thread(attachments_of, msg)
    status = "NEEDS_REVIEW" if req.status == "VERIFIED" else req.status
    cat = next((c for c in doa.custom_categories(app.state.org) if c["key"] == body.category), None) if body.category else None
    if cat is not None and channel == "internal":
        # the delegation lets the people it names ask for this item on the dashboard and be paid at once: the vendor's
        # name, account, bank and country are its registered record; the PR number and the amount are on the order
        missing = [doa.CUSTOM_REQUIREMENTS["pr_number"]] if "pr_number" in cat["requirements"] and not body.pr_number else []
        if body.currency.upper() not in cat["currencies"]:
            missing.append(f"a payout currency the category allows ({', '.join(cat['currencies'])})")
        if not cat["enabled"] or person["user_id"] not in cat["login_people"]:
            reasons.append(f"{cat['title']}: {person['name']} is not named to ask for it on the dashboard; a different person approves it")
        elif missing:
            reasons.append(f"{cat['title']}: not paid on the agent's own authority: missing " + "; ".join(m.lower() for m in missing))
        elif req.status in ("VERIFIED", "NEEDS_REVIEW") and not (set(req.flags) - {"po_missing"}):
            status = "VERIFIED"
            reasons[0] = (f"{cat['title']} ({cat['charge_code']}) asked for on the dashboard by {person['name']}, whom the delegation of authority names; "
                          f"PR {body.pr_number or '-'}; paid at once to the vendor's registered account")
            reasons = [r for r in reasons if "quotes no purchase order" not in r]
            req = req.model_copy(update={"flags": [f for f in req.flags if f != "po_missing"], "account_code": cat["charge_code"]})
    req = req.model_copy(update={"display_name": person["name"], "sender": person["email"], "flags": [*req.flags, "internal_order"],
                                 "reasons": reasons, "status": status,
                                 "attachments": [Attachment(**{k: v for k, v in f.items() if k != "text"}) for f in files]})
    await asyncio.to_thread(app.state.intake.store.save, req)
    req = await _validate_documents(req, msg.as_bytes(), active_model(), save=True)
    app.state.position_cache = None
    app.state.hub.publish("intake", {"channel": channel} | req.model_dump(include={
        "intake_id", "kind", "status", "trust", "sender", "counterparty_name", "amount_minor", "currency", "invoice_no", "reader", "flags"}))
    await asyncio.to_thread(app.state.audit.append, "payment_order", req.model_dump(exclude={"text"}) | {"by": f"{person['name']} ({role})",
                                                                                                        "channel": channel})
    _after_intake(req)
    return req


async def _pr_from_email(req: IntakeRequest, model) -> IntakeRequest:
    """A staff email asking to buy something (pr_email.py): the PR, its RFQ draft, the candidate vendors and the RFQ
    queued to them. The email's own intake record says which PR it became."""
    from . import pr_email, sourcing
    from .intake.claims import employee

    org = app.state.org
    person = employee(org, req.sender)
    if person is None:
        return req
    fields, basis = await pr_email.read(model, req.subject or "", req.text or "")
    try:
        saved = await asyncio.to_thread(org.save, "pr", PurchaseRequest.model_validate(pr_email.to_pr(fields, person, org)), f"email:{req.sender}")
    except (OrgError, ValueError) as exc:
        note = f"purchase_request_refused: {str(exc)[:160]}"
        await asyncio.to_thread(app.state.audit.append, "pr_from_email_refused", {"sender": req.sender, "reason": str(exc)[:300]})
        return _flagged(req, note)
    num, steps = saved["pr_number"], {"read_by": basis}
    who = f"VENTHORA agent (email from {person['name']})"
    await asyncio.to_thread(sourcing.draft_letter, org, num, who)
    if saved["status"] == "SUBMITTED":
        try:
            rec = await sourcing.find_vendors(org, num, payees=app.state.intake.registry.payees, model=model, by=who)
            cands = [c["cand_id"] for c in rec["candidates"] if c.get("email")][:10]
            steps["candidates"] = len(rec["candidates"])
            if cands:
                sent = await asyncio.to_thread(sourcing.invite, org, num, cands, by=who)
                steps["rfq_queued"] = len(sent["sent"])
        except (sourcing.SourcingError, OrgError) as exc:
            steps["sourcing"] = str(exc)[:200]
    await asyncio.to_thread(app.state.audit.append, "pr_from_email", {"pr": num, "sender": req.sender, "status": saved["status"]} | steps)
    app.state.hub.publish("intake", {"channel": "email", "kind": "purchase_request", "pr_number": num})
    return _flagged(req, f"purchase_request:{num}")


async def _fund_from_email(req: IntakeRequest, model) -> IntakeRequest:
    """A staff email asking for a cash advance, petty cash or an ODC (fund_email.py): the fund request, approved when the
    FCO sent it for someone else, and then paid as a virtual card on the card-issuing account."""
    from . import fund_email
    from .intake.claims import employee

    org = app.state.org
    sender = employee(org, req.sender)
    if sender is None:
        return req
    fields, basis = await fund_email.read(model, req.subject or "", req.text or "")
    person = fund_email.person_of(org, fields.get("person"), sender)
    if person is None:
        await asyncio.to_thread(app.state.audit.append, "fund_from_email_refused", {"sender": req.sender, "reason": f"no person {fields.get('person')!r}"})
        return _flagged(req, f"fund_request_refused: no person named {str(fields.get('person'))[:60]!r} in People & roles")
    try:
        saved = await asyncio.to_thread(org.save, "fund_request", FundRequest.model_validate(fund_email.to_request(fields, person, sender)), f"email:{req.sender}")
    except (OrgError, ValueError) as exc:
        await asyncio.to_thread(app.state.audit.append, "fund_from_email_refused", {"sender": req.sender, "reason": str(exc)[:300]})
        return _flagged(req, f"fund_request_refused: {str(exc)[:160]}")
    num, steps = saved["request_number"], {"read_by": basis, "status": saved["status"]}
    if saved["status"] == "APPROVED":
        try:
            paid = await _issue_advance_card(num, f"VENTHORA agent (email from {sender['name']})")
            steps["card"] = (paid.get("card") or {}).get("masked")
        except HTTPException as exc:
            steps["card_error"] = str(exc.detail)[:200]
    await asyncio.to_thread(app.state.audit.append, "fund_from_email", {"request": num, "sender": req.sender, "for": person["user_id"]} | steps)
    app.state.hub.publish("intake", {"channel": "email", "kind": "fund_request", "request_number": num})
    return _flagged(req, f"fund_request:{num}")


def _flagged(req: IntakeRequest, flag: str) -> IntakeRequest:
    req = req.model_copy(update={"flags": [*req.flags, flag]})
    with contextlib.suppress(Exception):
        app.state.intake.store.save(req)
    return req


async def _email_order(req: IntakeRequest, raw: bytes) -> tuple[IntakeRequest | None, IntakeRequest]:
    """An email from a person of the organisation that asks to pay a registered vendor (it names the vendor, an
    invoice and an amount) becomes a payment order, when Settings → Email allows it and the person is ticked
    "may order vendor payments by email". It never pays by itself: a different person approves it. Returns the
    (order, the email's own request); order is None when the email is not an order or may not be one, and the
    request then says why."""
    from email import policy as _policy
    from email.parser import BytesParser

    from .email_settings import EmailStore
    from .intake.claims import employee
    from .procurement import find_po

    claimed = app.state.intake.registry.claimed(req.subject, req.text or "")
    if not claimed or claimed[2] == "payer" or not (req.amount_minor and req.currency):
        return None, req  # not an order to pay a vendor
    cid, rec, _ = claimed
    person = employee(app.state.org, req.sender)
    pol = await asyncio.to_thread(EmailStore().policy)
    domain = req.sender.rsplit("@", 1)[-1].lower()
    passes = [k for k, v in (req.authentication or {}).items() if str(v).lower() == "pass"]
    refuse = None
    if not pol["orders_by_email"]:
        refuse = "payment orders by email are turned off (Settings → Email)"
    elif person is None or not (person.get("may_order_by_email") or domain in pol.get("order_domains", [])):
        refuse = f"{req.sender} may not order vendor payments by email (User management → may order vendor payments by email)"
    elif pol["company_domains"] and domain not in pol["company_domains"]:
        refuse = f"{domain} is not a company domain ({', '.join(pol['company_domains'])})"
    elif pol["require_authentication"] and not passes:
        refuse = "the receiving server did not authenticate the sender (no SPF, DKIM or DMARC pass)"
    elif pol["order_tag"] and pol["order_tag"].lower() not in (req.subject or "").lower():
        refuse = f"the subject does not carry {pol['order_tag']}"
    elif not req.invoice_no:
        refuse = "an order must name the invoice or reference to pay"
    if refuse:
        out = req.model_copy(update={"flags": [*req.flags, "email_order_refused"], "status": "NEEDS_REVIEW" if req.status == "VERIFIED" else req.status,
                                     "reasons": [*req.reasons, f"asks to pay {rec['name']}, but {refuse}"]})
        await asyncio.to_thread(app.state.intake.store.save, out)
        return None, out
    po = find_po(req.text or "", req.subject or "")  # the same reading as an emailed invoice
    terms = int(app.state.org.standards().get("default_payment_terms_days", 30))
    due = req.due_date or (datetime.now(UTC).date() + timedelta(days=terms)).isoformat()
    body = PaymentOrderIn(vendor_id=cid, invoice_no=req.invoice_no, amount_minor=req.amount_minor, currency=req.currency, due_date=due,
                          po_number=po[0] if po else None, description=(req.subject or "")[:300])
    msg = BytesParser(policy=_policy.default).parsebytes(raw)
    order = await _payment_order_core(body, person, msg, channel="email", role=person["role"],
                                      note=f"payment order by email from {person['name']} ({person['role'].replace('_', ' ')}); a different person approves it")
    original = req.model_copy(update={"status": "DUPLICATE", "duplicate_of": order.intake_id, "flags": [*req.flags, "became_payment_order"],
                                      "reasons": [*req.reasons, f"became payment order {order.invoice_no} ({order.intake_id})"]})
    await asyncio.to_thread(app.state.intake.store.save, original)
    return order, original


@app.post("/webhooks/email")
async def inbound_email(request: Request):
    """Invoices and claims emailed to the company's inbound address, pushed by the mail service the moment they
    arrive: Postmark's inbound JSON or a raw RFC 822 message. The service authenticates with HTTP Basic auth whose
    password is VENTHORA_INBOUND_EMAIL_SECRET; the email itself then goes through the same intake as any other."""
    import base64
    import hmac

    secret = env("VENTHORA_INBOUND_EMAIL_SECRET")
    if not secret:
        raise HTTPException(503, "inbound email secret not configured")
    auth = request.headers.get("authorization", "")
    try:
        password = base64.b64decode(auth[6:]).decode().split(":", 1)[1] if auth.lower().startswith("basic ") else ""
    except (ValueError, IndexError):
        password = ""
    password = password or request.headers.get("x-venthora-secret", "")  # a Worker or script that sets a header
    if not hmac.compare_digest(password.encode(), secret.encode()):
        await asyncio.to_thread(app.state.audit.append, "inbound_email_refused", {"reason": "bad credentials"})
        raise HTTPException(401, "bad credentials", headers={"WWW-Authenticate": "Basic"})
    body = await request.body()
    if len(body) > MAX_EMAIL_BYTES * 2:  # base64 inside JSON is a third larger than the message
        raise HTTPException(413, "email too large")
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        if ctype == "application/json":
            from .intake.documents import postmark_to_eml

            raw = postmark_to_eml(json.loads(body))
        elif ctype == "multipart/form-data":  # SendGrid Inbound Parse (raw) sends "email"; Mailgun /mime sends "body-mime"
            raw = _form_field(request.headers.get("content-type", ""), body, ("email", "body-mime"))
        else:
            raw = body
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, f"not an email: {type(exc).__name__}") from exc
    req = await _ingest(raw, None, False, channel="inbound-email")
    req = await asyncio.to_thread(_route_mailbox, req, raw)
    return {"intake_id": req.intake_id, "kind": req.kind, "status": req.status}


# Mailgun sends the full message (body-mime) only to a URL that ends in "mime"
app.add_api_route("/webhooks/email/mime", inbound_email, methods=["POST"], name="inbound_email_mime")


def _form_field(content_type: str, body: bytes, names: tuple[str, ...]) -> bytes:
    """One field of a multipart/form-data body, read with the standard email parser (no extra library)."""
    from email import policy
    from email.parser import BytesParser

    form = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields = {p.get_param("name", header="content-disposition"): p for p in form.iter_parts()} if form.is_multipart() else {}
    for n in names:
        if n in fields:
            got = fields[n].get_payload(decode=True)
            return got if got is not None else str(fields[n].get_payload()).encode()
    raise ValueError(f"no {' or '.join(names)} field in the form")


def _route_mailbox(req: IntakeRequest, raw: bytes, inboxes: list[dict] | None = None) -> IntakeRequest:
    """Settings → Email sources: which inbound address the email was sent to, and whether it belongs there
    (an invoice sent to the claims address waits for a person). No configured mailboxes: nothing changes."""
    from email import policy
    from email.parser import BytesParser
    from email.utils import getaddresses

    from .organisation import PURPOSE_KINDS

    inboxes = inboxes or []  # an inbox VENTHORA read: the mail is there, whatever its To says

    boxes = [m for m in app.state.org.list("mailbox") if m.get("active", True)] + inboxes
    if not boxes:
        return req
    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    to = {a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Delivered-To", []))}
    box = next((m for m in boxes if m["address"].lower() in to), None) or (inboxes[0] if inboxes else None)
    flags, reasons, status = list(req.flags), list(req.reasons), req.status
    if box is None:
        from .email_settings import EmailStore

        if not EmailStore().policy()["flag_unknown_mailbox"]:  # Settings → Email: the person chose not to flag it
            return req
        flags.append("unknown_mailbox")
        reasons.append("sent to an address that is not one of the configured mailboxes: " + ", ".join(sorted(to)[:3]))
    elif box["purpose"] != "any" and req.kind != "unknown" and req.kind not in PURPOSE_KINDS[box["purpose"]]:
        flags.append("wrong_mailbox")
        reasons.append(f"a {req.kind.replace('_', ' ')} sent to {box['address']}, which takes {box['purpose']}")
        if status == "VERIFIED":
            status = "NEEDS_REVIEW"
    out = req.model_copy(update={"mailbox": box["address"] if box else None, "flags": flags, "reasons": reasons, "status": status})
    app.state.intake.store.save(out)
    return out


# ---- Settings → Email: how mail reaches VENTHORA, who may send what, the inboxes VENTHORA reads ----
@app.get("/v1/settings/email")
async def email_settings_get():
    """The policy, the inboxes VENTHORA reads (secrets never returned), the providers to pick from, and the push address."""
    from .email_settings import PRESETS, PUSH_PROVIDERS, EmailStore

    store = EmailStore()
    users = [{"user_id": u["user_id"], "name": u["name"], "email": u["email"], "role": u["role"], "may_order_by_email": bool(u.get("may_order_by_email"))}
             for u in app.state.org.list("user") if u.get("active", True)]
    vendors = sum(1 for _, r in app.state.intake.registry.payees.items() if r.get("contacts"))
    return {"policy": await asyncio.to_thread(store.policy), "inboxes": await asyncio.to_thread(store.inboxes),
            "presets": [{"key": k} | v for k, v in PRESETS.items()], "push_providers": [{"key": k} | v for k, v in PUSH_PROVIDERS.items()],
            "push": {"path": "/webhooks/email", "secret_configured": bool(env("VENTHORA_INBOUND_EMAIL_SECRET"))},
            "senders": {"staff": users, "vendors_with_mailboxes": vendors},
            "addresses": app.state.org.get("email_addresses", "addresses") or {"invoices": None, "payments": None, "same_for_both": False},
            "oauth": await asyncio.to_thread(_mail_oauth_public), "oauth_last_error": app.state.org.get("mail_oauth_status", "last")}


def _mail_oauth_public() -> dict:
    from . import mail_oauth

    return mail_oauth.apps_public()


class MailAddressIn(BaseModel):
    address: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    receive: str = Field(pattern="^(imap|forwarded)$", description="imap: VENTHORA signs in to the mailbox; forwarded: the mail service posts each email to /webhooks/email")
    provider: str = Field("gmail", max_length=20)


class MailAddressesIn(BaseModel):
    """Settings → Email → Company email addresses: the invoices address and the payments address (one generic address
    may serve both; it is still entered for both roles)."""
    invoices: MailAddressIn
    payments: MailAddressIn
    same_for_both: bool = False
    send_from_payments: bool = Field(True, description="VENTHORA's own emails (RFQ, PO, notices) are sent from the payments address")


@app.put("/v1/settings/email/addresses")
async def email_addresses_put(body: MailAddressesIn, role: str = "super_admin", by: str = "dashboard"):
    """Keep the two company addresses (the inbox sign-in and the outgoing email are saved by their own calls)."""
    from .email_settings import PRESETS

    who = _setup_writer(role, by)
    for slot in (body.invoices, body.payments):
        if slot.provider not in PRESETS:
            raise HTTPException(422, f"unknown provider {slot.provider}")
    if body.same_for_both and body.invoices.address.lower() != body.payments.address.lower():
        raise HTTPException(422, "one generic address for both: the invoices and payments addresses must be the same")
    rec = {"_id": "addresses", **body.model_dump(), "updated_by": who, "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}
    rec["invoices"]["address"], rec["payments"]["address"] = body.invoices.address.lower(), body.payments.address.lower()
    await asyncio.to_thread(app.state.org._put, "email_addresses", rec, who)
    await asyncio.to_thread(app.state.audit.append, "email_addresses", {"invoices": rec["invoices"]["address"], "payments": rec["payments"]["address"], "by": who})
    return rec


@app.put("/v1/settings/email/policy")
async def email_policy_put(body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    from .email_settings import EmailPolicy, EmailStore

    who = _setup_writer(role, by)
    try:
        pol = EmailPolicy.model_validate(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    out = await asyncio.to_thread(EmailStore().save_policy, pol, who)
    await asyncio.to_thread(app.state.audit.append, "email_policy_saved", {"by": who} | out)
    return out


def _inbox_in(inbox_id: str, body: dict):
    """The inbox a form sends; `secret_from_sender`: the same mailbox already sends, so its saved app password."""
    from .email_settings import Inbox

    body = dict(body)
    if body.pop("secret_from_sender", False) and not body.get("secret"):
        from . import outbox, secret_values

        smtp = outbox.smtp(app.state.org)
        if smtp["sender"].lower() != str(body.get("address", "")).lower() or not smtp["password"]["set"]:
            raise HTTPException(422, "no saved app password for this address yet: type it once")
        body["secret"] = secret_values.get(outbox.PASSWORD)
    try:
        return Inbox.model_validate(body | {"inbox_id": inbox_id})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/v1/settings/email/inboxes/{inbox_id}/check")
async def email_inbox_check(inbox_id: str, body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    """Test connect before anything is kept: sign in with what the form holds (a blank password: the saved one) and
    open the folder. Nothing is saved, read or changed."""
    from .email_settings import EmailStore, MailboxError, test_inbox

    _setup_writer(role, by)
    ib = _inbox_in(inbox_id, body)
    old = await asyncio.to_thread(EmailStore().raw, inbox_id)
    secret = ib.secret or ((old or {}).get("_secret") if old and old["address"] == ib.address else "") or ""
    if not secret:
        raise HTTPException(422, "type the app password first")
    try:
        return await asyncio.to_thread(test_inbox, ib.model_dump(exclude={"secret"}) | {"_secret": secret})
    except MailboxError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.put("/v1/settings/email/inboxes/{inbox_id}")
async def email_inbox_put(inbox_id: str, body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    """Connect or change an inbox VENTHORA reads; the password or token is write-only (blank keeps the saved one)."""
    from .email_settings import EmailStore

    who = _setup_writer(role, by)
    ib = _inbox_in(inbox_id, body)
    out = await asyncio.to_thread(EmailStore().save_inbox, ib)
    await asyncio.to_thread(app.state.audit.append, "email_inbox_saved", {"by": who, "inbox": inbox_id, "address": ib.address,
                                                                          "provider": ib.provider, "host": out["resolved"]["host"]})
    return out


@app.delete("/v1/settings/email/inboxes/{inbox_id}")
async def email_inbox_delete(inbox_id: str, role: str = "finance_manager", by: str = "dashboard"):
    from .email_settings import EmailStore

    who = _setup_writer(role, by)
    if not await asyncio.to_thread(EmailStore().delete_inbox, inbox_id):
        raise HTTPException(404, "no such inbox")
    await asyncio.to_thread(app.state.audit.append, "email_inbox_deleted", {"by": who, "inbox": inbox_id})
    return {"deleted": inbox_id}


@app.post("/v1/settings/email/inboxes/{inbox_id}/test")
async def email_inbox_test(inbox_id: str):
    """Sign in and open the folder: how many messages and how many unread. Nothing is read or changed."""
    from .email_settings import EmailStore, MailboxError, test_inbox

    raw = await asyncio.to_thread(EmailStore().raw, inbox_id)
    if raw is None:
        raise HTTPException(404, "no such inbox")
    try:
        return await asyncio.to_thread(test_inbox, raw)
    except MailboxError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/v1/settings/email/inboxes/{inbox_id}/fetch")
async def email_inbox_fetch(inbox_id: str, role: str = "finance_manager", by: str = "dashboard"):
    """Read the new mail of an inbox now, instead of waiting for its schedule."""
    from .email_settings import EmailStore

    who = _writer(role, by)
    if await asyncio.to_thread(EmailStore().raw, inbox_id) is None:
        raise HTTPException(404, "no such inbox")
    return await _poll_inbox(inbox_id, who)


class MailOAuthStartIn(BaseModel):
    provider: str = Field(pattern="^(outlook|gmail|yahoo)$")
    send_too: bool = Field(True, description="also send VENTHORA's emails from the mailbox that signs in")
    login_hint: str = Field("", max_length=200)
    client_id: str | None = Field(None, max_length=200, description="the company's own app, registered in its own account (kept, then used)")
    client_secret: str | None = Field(None, max_length=500, description="write-only; blank keeps the saved one")
    tenant_id: str | None = Field(None, max_length=100, description="Directory (tenant) ID of a single-tenant Microsoft app; blank = any")
    since_days: int | None = Field(None, ge=0, le=365, description="how far back the mailbox is read; 0 = all mail")


@app.post("/v1/settings/email/oauth/start")
async def email_oauth_start(body: MailOAuthStartIn, request: Request, role: str = "finance_manager", by: str = "dashboard"):
    """The provider's consent page for this company's mailbox (Connect with Microsoft / Google / Yahoo)."""
    from urllib.parse import urlsplit

    from . import airwallex_settings, mail_oauth

    who = _setup_writer(role, by)
    src = request.headers.get("origin") or request.headers.get("referer") or ""
    u = urlsplit(src)
    origin = f"{u.scheme}://{u.netloc}" if u.scheme in ("http", "https") and u.netloc else ""
    try:
        if body.client_id is not None:  # the company's own app, from its own Microsoft / Google account
            await asyncio.to_thread(mail_oauth.set_own, body.provider, body.client_id, body.client_secret, who, body.tenant_id)
        url = await asyncio.to_thread(mail_oauth.start, app.state.org, body.provider, origin=origin, by=who, tenant=(airwallex_settings.tenant() or {}).get("tenant_id"),
                                      send_too=body.send_too, login_hint=body.login_hint.strip(), since_days=body.since_days)
    except mail_oauth.OAuthError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"url": url}


@app.get("/oauth/mail/callback", include_in_schema=False)
async def email_oauth_callback(state: str = "", code: str = "", error: str = "", error_description: str = ""):
    """The provider sends the person back here with a one-time code: the mailbox is connected (read, and send when
    asked), its sign-in kept write-only, and the person returned to Email Sync."""
    import html as _html
    import re as _re
    from urllib.parse import quote

    from . import mail_oauth, outbox
    from .email_settings import EmailStore, Inbox

    try:
        rec = await asyncio.to_thread(mail_oauth.take_state, app.state.org, state)
    except mail_oauth.OAuthError as exc:
        return HTMLResponse(f"<p>{_html.escape(str(exc))}</p><p><a href='/#/sources-email'>Back to Email Sync</a></p>", status_code=400)
    back = (rec.get("origin") or "") + "/#/sources-email"

    async def failed(why: str) -> RedirectResponse:  # kept, so Email Sync shows why until the next try succeeds
        await asyncio.to_thread(app.state.org._put, "mail_oauth_status", {"_id": "last", "provider": rec["provider"], "error": why[:400],
                                                                          "at": datetime.now(UTC).isoformat(timespec="seconds")}, "VENTHORA (mail sign-in)")
        await asyncio.to_thread(app.state.audit.append, "email_oauth_failed", {"by": rec.get("by") or "dashboard", "provider": rec["provider"], "error": why[:400]})
        return RedirectResponse(f"{back}?oauth_error={quote(why[:200])}", status_code=303)

    if error or not code:
        said = error_description or error or "no code"
        return await failed(next((h for c, h in mail_oauth.HINTS.items() if c in said), said.splitlines()[0][:220]))
    try:
        address, secret = await asyncio.to_thread(mail_oauth.finish, rec["provider"], code)
    except mail_oauth.OAuthError as exc:
        return await failed(str(exc))
    with contextlib.suppress(KeyError):
        await asyncio.to_thread(app.state.org.delete, "mail_oauth_status", "last", "VENTHORA (mail sign-in)")
    who = rec.get("by") or "dashboard"
    store = EmailStore()
    old = next((i for i in await asyncio.to_thread(store.inboxes) if i["address"] == address), None)
    iid = old["inbox_id"] if old else _re.sub(r"[^a-z0-9]+", "-", address).strip("-")[:40]
    keep = {k: old[k] for k in ("purpose", "folder", "only_unread", "after", "move_to", "since_days", "schedule_minutes")} if old else {}
    keep |= {"since_days": rec["since_days"]} if rec.get("since_days") is not None else {}
    ib = Inbox.model_validate({"purpose": "any", "schedule_minutes": 1} | keep | {"inbox_id": iid, "address": address, "provider": rec["provider"],
                                                                                 "auth": "oauth2", "secret": secret, "host": "", "username": "", "active": True})
    await asyncio.to_thread(store.save_inbox, ib)
    addrs = app.state.org.get("email_addresses", "addresses") or {}
    slot = {"address": address, "receive": "imap", "provider": rec["provider"]}
    rec_addr = {"_id": "addresses", "invoices": slot, "payments": slot if rec.get("send_too") else (addrs.get("payments") or slot),
                "send_from_payments": True, "updated_by": who, "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}
    rec_addr["same_for_both"] = rec_addr["payments"]["address"] == address
    await asyncio.to_thread(app.state.org._put, "email_addresses", rec_addr, who)
    if rec.get("send_too"):
        await asyncio.to_thread(outbox.save_smtp, app.state.org, {"provider": rec["provider"], "sender": address, "password": secret}, who)
    await asyncio.to_thread(app.state.audit.append, "email_oauth_connected", {"by": who, "provider": rec["provider"], "address": address,
                                                                               "sends": bool(rec.get("send_too"))})
    return RedirectResponse(f"{back}?connected={quote(address)}", status_code=303)


@app.put("/v1/platform/mail-oauth", include_in_schema=False)
async def platform_mail_oauth(body: Annotated[dict, Body()], x_venthora_platform: str | None = Header(default=None)):
    """For the platform only: VENTHORA's OAuth apps (one per provider, the same for every company)."""
    import hmac

    from . import mail_oauth

    token = env("VENTHORA_PLATFORM_TOKEN")
    if not token or not x_venthora_platform or not hmac.compare_digest(token, x_venthora_platform):
        raise HTTPException(404, "Not Found")
    return await asyncio.to_thread(mail_oauth.set_apps, body, "VENTHORA platform")


async def _poll_inbox(inbox_id: str, who: str) -> dict:
    """New mail of one inbox through the intake, then marked read, moved or left, as the inbox says."""
    from .email_settings import EmailStore, MailboxError, fetch_new, finish

    store = EmailStore()
    raw = await asyncio.to_thread(store.raw, inbox_id)
    report: dict = {"fetched": 0, "queued": [], "error": None}
    try:
        msgs = await asyncio.to_thread(fetch_new, raw, await asyncio.to_thread(store.seen, inbox_id))
        report["fetched"] = len(msgs)
        done = []
        for uid, mid, body in msgs:
            try:
                req = await _ingest(body, None, False, channel="inbox")
                req = await asyncio.to_thread(_route_mailbox, req, body, [{"address": raw["address"], "purpose": raw["purpose"]}])
                report["queued"].append({"intake_id": req.intake_id, "kind": req.kind, "status": req.status, "sender": req.sender,
                                         "subject": (req.subject or "")[:80]})
                await asyncio.to_thread(store.mark_seen, inbox_id, mid, req.intake_id)
                done.append(uid)
            except HTTPException as exc:  # one bad message does not stop the rest; it is read again next time
                report.setdefault("skipped", []).append(f"{mid}: {exc.detail}")
        await asyncio.to_thread(finish, raw, done)
    except MailboxError as exc:
        report["error"] = str(exc)
    await asyncio.to_thread(store.record_poll, inbox_id, report)
    if report["queued"]:
        await asyncio.to_thread(app.state.audit.append, "email_inbox_read", {"by": who, "inbox": inbox_id, "queued": len(report["queued"])})
    return report


# -- ground truth ------------------------------------------------------------------------------

@app.get("/api/groundtruth")
async def groundtruth_cases(suite: str = "cases"):
    """suite = cases (the rules, one by one) or anomalies (the organizers' failure modes, smallest to largest)."""
    try:
        return load_cases(suite)
    except FileNotFoundError as exc:
        raise HTTPException(404, "unknown suite") from exc


def _case(case_id: str) -> dict:
    for suite in ("cases", "anomalies", "budget", "controls", "reporting", "ledger", "limits", "payees", "approvals", "fx",
                  "aging", "tenancy"):
        with contextlib.suppress(FileNotFoundError):
            for c in load_cases(suite):
                if c["id"] == case_id:
                    return c
    raise HTTPException(404, "unknown case")


@app.post("/api/groundtruth/{case_id}/check")
async def groundtruth_check(case_id: str):
    """Run one hand-derived case through the live service and judge it with the shared comparator."""
    case = _case(case_id)
    if case["kind"] == "execution":
        raise HTTPException(409, "execution cases run in-process against the fake sandbox (tests), not through the service")
    return await run_case(case, app.state.agent, app.state.intake)


class JudgeRequest(BaseModel):
    case_id: str
    result: dict


@app.post("/api/groundtruth/judge")
async def groundtruth_judge(req: JudgeRequest):
    """Judge a result the client obtained itself (the dashboard form) against a case's expectation."""
    case = _case(req.case_id)
    view = view_of(case["kind"], req.result)
    return compare(case["expect"], view) | {"view": view}


class CorrectionIn(BaseModel):
    counterparty_id: str | None = Field(None, max_length=80, description="the registered payee to pay")
    invoice_no: str | None = Field(None, max_length=40)
    amount_minor: int | None = Field(None, gt=0, le=10**13)
    currency: str | None = Field(None, min_length=3, max_length=3)
    due_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    po_number: str | None = Field(None, max_length=40, description="empty to take the PO off")
    account_code: str | None = Field(None, max_length=60)
    category: str | None = Field(None, max_length=60)
    note: str = Field("", max_length=300)


@app.get("/v1/intake/requests/{intake_id}")
async def intake_get(intake_id: str):
    """One request in full, for the Pending payment form: what was read, the checks, the documents and the history."""
    from . import procurement

    req = app.state.intake.store.get(intake_id)
    if req is None:
        raise HTTPException(404, "unknown intake request")
    po = procurement.status(app.state.org, app.state.executor.store, req.po_number) if req.po_number else None
    payees = app.state.intake.registry.payees
    return req.model_dump() | {"po": po and {"po_id": po["po"]["po_id"], "total_minor": po["po"]["total_minor"], "currency": po["po"]["currency"],
                                             "paid_minor": po["paid_minor"], "remaining_minor": po["remaining_minor"], "supplier_id": po["po"]["supplier_id"]},
                               "payees": [{"id": k, "name": v["name"], "currency": v.get("currency")} for k, v in sorted(payees.items(), key=lambda x: x[1]["name"])],
                               "open_pos": [{"po_id": p["po_id"], "supplier_id": p["supplier_id"], "currency": p["currency"]} for p in app.state.org.list("po")
                                            if p.get("status", "OPEN") == "OPEN"]}


@app.get("/v1/intake/requests/{intake_id}/correspondence")
async def intake_correspondence(intake_id: str):
    """What was said about this request: the messages sent to the vendor (date, text, delivery), where the vendor's
    answer stands, the vendor's other submissions of the same invoice with what was decided, and the notices sent to
    the FCO's mailbox with their full text."""
    from . import correspondence, outbox

    req = app.state.intake.store.get(intake_id)
    if req is None:
        raise HTTPException(404, "unknown intake request")
    every = await asyncio.to_thread(app.state.intake.store.list, 5000)
    out = await asyncio.to_thread(correspondence.build, req, every, outbox.items(app.state.org))
    ids = {intake_id} | {s["intake_id"] for s in out["submissions"]}
    out["callbacks"] = sorted((c for c in app.state.org.list("callback_log") if c.get("intake_id") in ids), key=lambda c: c.get("at", ""))
    return out


class CallbackIn(BaseModel):
    pic_name: str = Field(min_length=2, max_length=120, description="the person spoken to")
    company: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=5, max_length=40, description="the number that was called")
    revision: str = Field(min_length=3, max_length=1000, description="what the vendor must correct")


@app.post("/v1/intake/requests/{intake_id}/callbacks")
async def intake_callback(intake_id: str, body: CallbackIn, role: str = "finance_manager", by: str = "dashboard"):
    """Record a call-back made by a person about this request: who was spoken to, at which company and number, and what
    must be corrected. It is kept on the request's log with the time it was recorded; it changes no status by itself."""
    import uuid

    who = _writer(role, by)
    if app.state.intake.store.get(intake_id) is None:
        raise HTTPException(404, "unknown intake request")
    rec = {"_id": str(uuid.uuid4()), "intake_id": intake_id, "at": datetime.now(UTC).isoformat(timespec="seconds"), "by": who} | body.model_dump()
    await asyncio.to_thread(app.state.org._put, "callback_log", rec, who)
    await asyncio.to_thread(app.state.audit.append, "manual_callback", rec)
    return rec


@app.put("/v1/intake/requests/{intake_id}")
async def intake_correct(intake_id: str, body: CorrectionIn, role: str = "finance_manager", by: str = "dashboard"):
    """Save a person's corrections to a request in Pending payment and run its checks again (payee, currency, PO,
    duplicate). It is not approved: it waits for an approval, or for Pay."""
    who = _writer(role, by)
    try:
        req, problems = await asyncio.to_thread(app.state.intake.recheck, intake_id, body.model_dump(exclude_none=True, exclude={"note"})
                                                | ({"po_number": None} if body.po_number == "" else {}), by=who)
    except KeyError as exc:
        raise HTTPException(404, "unknown intake request") from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "intake_corrected", {"intake_id": intake_id, "by": who, "problems": problems, "note": body.note})
    return {"request": req.model_dump(), "checks_pass": not problems, "problems": problems}


@app.post("/v1/intake/requests/{intake_id}/pay")
async def intake_pay(intake_id: str, body: CorrectionIn, role: str = "finance_manager", by: str = "dashboard"):
    """Save the corrections, run the checks again and, when they pass, approve the request as this person and pay it
    now: the book still passes the treasury proof (reserve floor, FX, outflow limits, the balance) before Airwallex is
    called. When a check fails nothing is approved and the reasons come back."""
    from . import autonomy
    from .execution.executor import terms_digest
    from .execution.models import Approval

    if app.state.intake.store.get(intake_id) is None and len(intake_id) >= 8:  # the plan names a request by its first characters
        hits = [q.intake_id for q in await asyncio.to_thread(app.state.intake.store.list, 5000) if q.intake_id.startswith(intake_id)]
        if len(hits) == 1:
            intake_id = hits[0]
    out = await intake_correct(intake_id, body, role=role, by=by)
    if not out["checks_pass"]:
        raise HTTPException(409, {"message": "the checks do not pass yet: correct the request first", "problems": out["problems"]})
    req = app.state.intake.store.get(intake_id)
    if req.status != "VERIFIED":
        await review_intake(intake_id, ReviewRequest(decision="verify", reviewer=by, role=role,
                                                     note=body.note or "corrected and approved in Pending payment"))
        req = app.state.intake.store.get(intake_id)
    if req.status != "VERIFIED":
        raise HTTPException(409, f"it could not be approved: {'; '.join(req.reasons[:2])}")
    base = _live_base()
    state = _with_mandate(app.state.intake.to_state(base, as_of=datetime.now(UTC).date(), requests=[req]))  # same book rules as Agent plan
    try:
        state = await with_live_balances_and_rates(state, _bank(), ring_fence=True)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
    state, _ = apply_operational_facts(state, app.state.executor.store)
    s = await autonomy.split(app.state.agent, state)
    if s.auto_plan is not None and any(d.action in autonomy.PAYING for d in s.auto_plan.obligations):
        res = await app.state.executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash, requested_by=f"{by} (Pending payment)"))
    elif s.full.decision == "REQUIRES_APPROVAL" and any(d.action in autonomy.PAYING for d in s.full.obligations):
        res = await app.state.executor.execute(ExecutionRequest(state=state, plan_hash=s.full.plan_hash, requested_by=f"{by} (Pending payment)",
                                                                approval=Approval(approver=by, role=role, terms_digest=terms_digest(s.full))))
    else:
        held = "; ".join(r for h in s.held for r in h["reasons"]) or "the plan does not pay it now"
        return {"paid": False, "request": req.model_dump(), "message": f"approved, but not paid now: {held}"}
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "intake_paid_from_pending", {"intake_id": intake_id, "by": by, "execution_id": res.execution_id,
                                                                                 "status": res.status})
    return {"paid": res.status in ("COMPLETED", "IN_FLIGHT"), "execution": {"execution_id": res.execution_id, "status": res.status, "message": res.message},
            "request": app.state.intake.store.get(intake_id).model_dump(), "message": res.message}


@app.post("/v1/intake/{intake_id}/review", response_model=IntakeRequest)
async def review_intake(intake_id: str, review: ReviewRequest):
    if review.decision == "verify":
        # a request that failed a check when it arrived (above what was left on its PO, an unknown PO, a duplicate...)
        # has the checks run again now: verifying never makes a refused draw on a PO count, and never lets two
        # invoices together pass the PO total
        cur = app.state.intake.store.get(intake_id)
        if cur is not None and set(cur.flags) & app.state.intake.RECHECKED:
            _, problems = await asyncio.to_thread(app.state.intake.recheck, intake_id, {}, by=f"{review.reviewer} ({review.role})")
            if problems:
                raise HTTPException(409, "not verified: the checks run again still fail: " + "; ".join(problems))
        await _balance_covers(intake_id)
    try:
        req = await asyncio.to_thread(app.state.intake.review, intake_id, review)
    except KeyError as exc:
        raise HTTPException(404, "unknown intake request") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "intake_review", {"intake_id": intake_id, **review.model_dump()})
    return req


async def _balance_covers(intake_id: str) -> None:
    """A payment is approved only while the Airwallex balance covers it and every bill already approved and unpaid
    (at indicative rates). Without a connected wallet nothing is refused here; the executor's reserve check still is."""
    req = app.state.intake.store.get(intake_id)
    if req is None or not req.amount_minor or req.kind not in ("supplier_invoice", "contractor_claim", "expense_claim"):
        return
    try:
        pos = await treasury_position()
    except HTTPException:
        return
    base, rates = pos["base_currency"], pos["rates"]
    if req.currency != base and req.currency not in rates:
        return
    need = req.amount_minor if req.currency == base else int(req.amount_minor * float(rates[req.currency]))
    approved = sum(x["amount_minor"] if x["currency"] == base else int(x["amount_minor"] * float(rates.get(x["currency"], 0)))
                   for x in pos["bills_unpaid"]["approved"] if x["intake_id"] != intake_id)
    free = pos["available"]["base_minor"] - approved
    if need > free:
        raise HTTPException(409, f"the balance does not cover it: {base} {need / 100:,.2f} asked, {base} {max(free, 0) / 100:,.2f} free "
                                 f"(available {pos['available']['base_minor'] / 100:,.2f} less {approved / 100:,.2f} already approved); "
                                 "fund the account or reject it with a note")


class IntakePlanRequest(BaseModel):
    scenario_id: str = "kit01_initial"
    as_of: str = Field(default_factory=lambda: datetime.now(UTC).date().isoformat())


@app.post("/v1/intake/plan", response_model=PlanResult)
async def plan_with_intake(req: IntakePlanRequest):
    """Add the intake queue to a book and run the full loop. Verified requests are payable; anything
    unverified appears in the plan as REJECT (evidence pending) and can never be paid."""
    data = json.loads(_scenario_file(req.scenario_id).read_text(encoding="utf-8"))
    base = TreasuryState.model_validate(data["state"])
    try:
        state = app.state.intake.to_state(base, as_of=datetime.fromisoformat(req.as_of).date())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await plan(_with_budget(state))


@app.get("/v1/intake/state/{scenario_id}", response_model=TreasuryState)
async def intake_state(scenario_id: str, as_of: str | None = None, live: bool = False):
    """The book with the intake queue added. live=true grounds it in the sandbox's live balances (ring-fenced to the
    book's allocation), today's indicative rates and what earlier runs taught (fees), as the CLI does before it pays:
    a plan on a scenario's stored rates is refused at execution by the slippage check (seen in the demo cases)."""
    data = json.loads(_scenario_file(scenario_id).read_text(encoding="utf-8"))
    base = TreasuryState.model_validate(data["state"])
    day = datetime.fromisoformat(as_of).date() if as_of else datetime.now(UTC).date()
    state = app.state.intake.to_state(base, as_of=day)
    if not live:
        return state
    try:
        state = await with_live_balances_and_rates(state, _bank(), ring_fence=True)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
    state, _ = apply_operational_facts(state, app.state.executor.store)
    return state


# -- dashboard data ----------------------------------------------------------------------------

@app.get("/api/runs")
async def list_runs(limit: int = Query(30, ge=1, le=200)):
    """Run reports written by the CLIs (sandbox cycles, intake runs), newest first."""
    out = []
    for path in sorted(RUNS.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        plans = data.get("plans") or ([data["plan"]] if isinstance(data.get("plan"), dict) else [])
        last = plans[-1] if plans else {}
        out.append({"file": path.name, "scenario": data.get("scenario") or ("intake" if "intake" in path.name else None),
                    "fake": path.name.endswith("-fake.json"), "started_at": data.get("started_at") or data.get("as_of"),
                    "decision": last.get("decision"), "execution": (data.get("execution") or {}).get("status"),
                    "incident": (data.get("incident") or {}).get("status"),
                    "intake": [r.get("status") for r in data.get("intake", [])] if "intake" in data else None})
    return out


@app.get("/api/matrix")
async def matrix_summary():
    path = ROOT / "docs" / "scenario_matrix_log.json"
    if not path.is_file():
        raise HTTPException(404, "scenario matrix has not been run; python scripts/scenario_matrix.py")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [{k: r.get(k) for k in ("id", "family", "severity", "title", "ok", "total_ms")}
            | {"plan": (r.get("plan") or {}).get("decision"), "execution": (r.get("execution") or {}).get("status"),
               "incident": (r.get("incident") or {}).get("status"), "breach_bp": (r.get("plan") or {}).get("breach_bp"),
               "failed": [k for k, ok in (r.get("invariants") or {}).items() if not ok]}
            for r in data["records"]]
    return {"generated_at": data["generated_at"], "summary": data["summary"], "findings": data.get("findings", []), "rows": rows}


@app.get("/v1/executions")
async def list_executions(limit: int = Query(30, ge=1, le=200)):
    return app.state.executor.store.list_executions(limit)


POSITION_TTL_S = 20.0


@app.get("/v1/treasury/position")
async def treasury_position(fresh: bool = False):
    """The wallet now and what is about to change it: available (with what is processing out), bills not yet paid,
    pending deposits, and incoming payments not yet cleared, per currency and in the base currency. Read from
    Airwallex at most every 20 s (a webhook clears the cache, so a status change shows at once)."""
    import time

    from . import position

    cache = getattr(app.state, "position_cache", None)
    if cache and not fresh and time.monotonic() - cache["at"] < POSITION_TTL_S:
        return cache["value"]
    reg = app.state.intake.registry
    customers = {n for p in reg.payers.values() for n in [p["name"], *p.get("aliases", [])]}
    try:
        value = await position.fetch(_bank(), intake=app.state.intake, store=app.state.executor.store, customers=customers,
                                     base=app.state.org.standards()["base_currency"],
                                     rate_cache=app.state.__dict__.setdefault("rate_cache", {}),
                                     extra=(load_reporting_config().reporting_currency,))
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
    value["as_of"] = datetime.now(UTC).isoformat(timespec="seconds")
    app.state.position_cache = {"at": time.monotonic(), "value": value}
    return value


@app.get("/api/airwallex/balances")
async def airwallex_balances():
    try:
        return {"environment": "sandbox", "balances_minor": await _bank().balances_minor()}
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc


@app.get("/api/airwallex/global-accounts")
async def global_accounts():
    try:
        return await _bank().list_global_accounts()
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc


class DepositRequest(BaseModel):
    amount_minor: int = Field(gt=0, le=10**11)
    payer_name: str = Field(default="Sandbox payer", min_length=1, max_length=120)
    global_account_id: str | None = None


@app.post("/api/sandbox/deposits")
async def simulate_deposit(req: DepositRequest):
    """Sandbox: simulate an incoming customer payment (Kit 01 'simulate the deposit')."""
    from .cli import usd_global_account

    try:
        # The same choice as the demo: a configured id is used only when it is a primary-USD Global Account
        # (observed: a stale AWX_GLOBAL_ACCOUNT_ID made every simulated deposit fail with "could not be processed").
        account = req.global_account_id or await usd_global_account(_bank(), fake=False)
        result = await _bank().simulate_deposit(global_account_id=account, amount_minor=req.amount_minor,
                                                payer_name=req.payer_name)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
    await asyncio.to_thread(app.state.audit.append, "sandbox_deposit", {"amount_minor": req.amount_minor,
                                                                         "payer_name": req.payer_name})
    return result


# ---- Airwallex webhooks: authenticated, once only, re-read from the API before anything is shown ----------
@app.post("/webhooks/airwallex", include_in_schema=True)
async def airwallex_webhook(request: Request, background: BackgroundTasks):
    """Airwallex pushes deposit, transfer and conversion status changes here. The event is a hint: it is stored
    once, acknowledged at once (Airwallex retries anything but 200), then re-read from the Airwallex API."""
    body = await request.body()
    reason = webhooks.verify(webhooks.secret(), request.headers.get("x-timestamp"), body, request.headers.get("x-signature"))
    if reason == "webhook secret not configured":
        raise HTTPException(503, reason)
    if reason:
        await asyncio.to_thread(app.state.audit.append, "webhook_refused", {"reason": reason})
        raise HTTPException(400, reason)
    try:
        ev = webhooks.parse(body)
    except ValueError as exc:
        raise HTTPException(400, f"not an Airwallex event: {exc}") from exc
    if not await asyncio.to_thread(app.state.webhooks.record, ev):
        return {"received": True, "duplicate": True}
    await asyncio.to_thread(app.state.audit.append, "webhook_received", {k: ev[k] for k in ("event_id", "name", "object_id")})
    app.state.hub.publish("webhook", app.state.webhooks.get(ev["event_id"]) or ev)
    background.add_task(_confirm_webhook, ev)
    return {"received": True, "duplicate": False}


async def _confirm_webhook(ev: dict) -> None:
    row = await webhooks.confirm_event(app.state.webhooks, app.state.agent.proof.airwallex, ev)
    app.state.position_cache = None  # the wallet may have changed: the next read goes to Airwallex
    app.state.hub.publish("webhook", row)


@app.get("/v1/webhooks/status")
async def webhook_status():
    rows = app.state.webhooks.list(1)
    bank = app.state.agent.proof.airwallex
    subscriptions = None  # what Airwallex will call; API-created webhooks are not shown in its web app
    if bank.configured:
        with contextlib.suppress(Exception):  # best effort: the page still shows the local state
            subscriptions = [{"url": h.get("url"), "events": len(h.get("events") or []), "version": h.get("version")}
                             for h in await bank.list_webhooks() if str(h.get("url", "")).endswith("/webhooks/airwallex")]
    return {"endpoint": "/webhooks/airwallex", "secret_configured": bool(webhooks.secret()),
            "airwallex_configured": bank.configured, "replay_window_s": webhooks.TOLERANCE_S,
            "counts": app.state.webhooks.counts(), "last_received_at": rows[0]["received_at"] if rows else None,
            "listeners": app.state.hub.listeners, "subscriptions": subscriptions,
            "inbound_email": {"endpoint": "/webhooks/email", "secret_configured": bool(env("VENTHORA_INBOUND_EMAIL_SECRET")),
                              "model_reader": active_model().configured, "reader": ai_settings.describe(active_model())}}


WEBHOOK_EVENTS = ["deposit.pending", "deposit.settled", "deposit.rejected", "deposit.reversed",
                  "payout.transfer.processing", "payout.transfer.sent", "payout.transfer.paid", "payout.transfer.failed",
                  "conversion.settled", "conversion.cancelled"]


class WebhookRegisterIn(BaseModel):
    public_url: str = Field(pattern=r"^https://[^\s/]+(/[^\s]*)?$", max_length=300,
                            description="the https address Airwallex can reach (the tunnel or host in front of VENTHORA)")


@app.post("/v1/webhooks/register")
async def webhook_register(body: WebhookRegisterIn, role: str = "super_admin", by: str = "dashboard"):
    """Platform Setup → Airwallex webhooks → Create webhooks: subscribe VENTHORA on Airwallex (POST /webhooks/create), or
    move the existing subscription to a new address. The signing secret Airwallex returns is kept write-only; it is
    never shown."""
    import uuid

    from . import secret_values
    from .webhook_gateway import PATH

    who = _setup_writer(role, by)
    url = body.public_url.rstrip("/") + PATH
    try:
        bank = _bank()
        ours = [h for h in await bank.list_webhooks() if str(h.get("url", "")).endswith(PATH)]
        if ours and ours[0].get("url") == url:
            hook, action = ours[0], "already subscribed"
        elif ours:
            hook, action = await bank.update_webhook(ours[0]["id"], url), "moved to the new address"
        else:
            hook, action = await bank.create_webhook(request_id=str(uuid.uuid4()), url=url, events=WEBHOOK_EVENTS, version="2025-02-14"), "created"
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex: {exc}") from exc
    if hook.get("secret"):
        await asyncio.to_thread(secret_values.put, webhooks.SECRET_NAME, hook["secret"], who)
    await asyncio.to_thread(app.state.audit.append, "webhook_registered", {"by": who, "url": url, "action": action})
    return {"action": action, "url": hook.get("url") or url, "events": hook.get("events") or WEBHOOK_EVENTS,
            "secret_kept": bool(hook.get("secret")) or bool(webhooks.secret())}


@app.get("/v1/webhooks/events")
async def webhook_events(limit: int = Query(30, ge=1, le=200)):
    return app.state.webhooks.list(limit)


@app.get("/api/stream", include_in_schema=False)
async def event_stream(request: Request):
    """Server-sent events for the dashboards: each webhook as it arrives and again once confirmed."""
    q = app.state.hub.subscribe()

    async def gen():
        try:
            yield "retry: 5000\n\n"
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {msg['kind']}\ndata: {json.dumps(msg['data'], default=str)}\n\n"
        finally:
            app.state.hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"cache-control": "no-cache"})


# ---- organisation: units, people, budget allocations, unit prices and finance standards ---------------------
ORG_KINDS = {"units": ("unit", Unit), "users": ("user", User), "allocations": ("allocation", Allocation), "prices": ("price", Price), "travel-rates": ("travel_rate", TravelRate), "mailboxes": ("mailbox", Mailbox),
             "projects": ("project", Project), "accounts": ("account", Account), "purchase-orders": ("po", PurchaseOrder),
             "engagements": ("engagement", Engagement), "purchase-requests": ("pr", PurchaseRequest), "receipts": ("receipt", Receipt),
             "travel-authorisations": ("ta", TravelAuthorisation), "fund-requests": ("fund_request", FundRequest)}


def _org_kind(collection: str):
    if collection not in ORG_KINDS:
        raise HTTPException(404, "unknown collection")
    return ORG_KINDS[collection]


def _setup_writer(role: str, by: str) -> str:
    """Platform Setup (technical part) and the Developer Console: a Developer (IT) may change them as well as the FCO;
    access.py narrows each route to its roles."""
    if role in ("super_admin", "developer"):
        return f"{by} ({role})"
    return _writer(role, by)


def _writer(role: str, by: str) -> str:
    if role not in WRITE_ROLES:
        raise HTTPException(403, f"role '{role}' may read the organisation but not change it")
    return f"{by} ({role})"


# ---- Settings → AI model: rules only, a self-hosted model (any URL), or a provider with the user's own key ----
@app.get("/v1/settings/ai/usage")
async def ai_usage_get(days: int = Query(30, ge=1, le=366)):
    """Every model call of the last `days` days: tokens in and out by day, feature and model, the latest calls, and the
    company's own prices (USD per million tokens) for the cost estimate. Prompts and answers are never kept."""
    from . import ai_usage

    return await asyncio.to_thread(ai_usage.summary, days, 30)


class AIPriceIn(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    input_per_m: float | None = Field(None, ge=0, le=10_000, description="USD per million input tokens; both empty removes the price")
    output_per_m: float | None = Field(None, ge=0, le=10_000)


@app.put("/v1/settings/ai/prices")
async def ai_price_put(body: AIPriceIn, role: str = "finance_manager", by: str = "dashboard"):
    from . import ai_usage

    who = _setup_writer(role, by)
    try:
        out = await asyncio.to_thread(ai_usage.set_price, body.model, body.input_per_m, body.output_per_m, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "ai_price_saved", {"by": who} | body.model_dump())
    return out


class AIBudgetIn(BaseModel):
    cap_usd: float | None = Field(None, description="USD for paid model calls (Anthropic, Bedrock) over the period; null removes the cap")
    since: str = Field(..., max_length=40, description="ISO time the period starts (counts calls from then)")
    until: str = Field(..., max_length=40, description="ISO time the period ends")


@app.get("/v1/settings/ai/budget")
async def ai_budget_get():
    """The AI budget: cap, spend since it started (from the entered prices), what is left, and whether calls are stopped."""
    from . import ai_usage

    return await asyncio.to_thread(ai_usage.budget)


@app.put("/v1/settings/ai/budget")
async def ai_budget_put(body: AIBudgetIn, role: str = "finance_manager", by: str = "dashboard"):
    """Set the AI budget. Once paid calls since `since` cost the cap, every paid call is refused until `until` and
    VENTHORA continues with its deterministic rules; a self-hosted model is never counted."""
    from . import ai_usage

    who = _setup_writer(role, by)
    try:
        out = await asyncio.to_thread(ai_usage.set_budget, body.cap_usd, body.since, body.until, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "ai_budget_saved", {"by": who} | body.model_dump())
    return out


@app.get("/v1/settings/ai")
async def ai_settings_get():
    """The current choice; secrets are never returned, only whether one is set and its last four characters."""
    out = ai_settings.public(ai_settings.AIStore())
    return out | {"active": ai_settings.describe(active_model())}


@app.put("/v1/settings/ai")
async def ai_settings_put(s: ai_settings.AISettings, role: str = "finance_manager", by: str = "dashboard"):
    who = _setup_writer(role, by)
    try:
        out = await asyncio.to_thread(ai_settings.AIStore().save, s, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "ai_settings_saved", {  # never the key itself
        "by": who, "mode": out["mode"], "provider": out["provider"]["name"] if out["mode"] == "provider" else None,
        "model": out["provider"]["model"] if out["mode"] == "provider" else out["self_hosted"]["model"]})
    return out | {"active": ai_settings.describe(active_model())}


def _draft_model(s: ai_settings.AISettings | None):
    """The client for a draft from the form (its blank secrets filled from what is saved), or the active one."""
    if s is None:
        return active_model()
    store = ai_settings.AIStore()
    rec = store.merged(s)
    ai_settings.check(rec | {"consent_external": True})  # testing a key is not yet sending mail out
    return ai_settings.model_for(rec)


@app.post("/v1/settings/ai/test")
async def ai_settings_test(s: ai_settings.AISettings | None = None, role: str = "finance_manager"):
    """One short request to the chosen model, to prove the address, key and model name work before mail goes to it."""
    _writer(role, "test")
    from time import perf_counter

    from .evidence.llm import LLMUnavailable

    try:
        model = _draft_model(s)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not model.configured:
        return {"ok": False, "error": "rules only: no model to test"}
    mode = s.mode if s is not None else ai_settings.public(ai_settings.AIStore()).get("mode")
    if mode == "self_hosted" and getattr(model, "endpoint", None) and getattr(model, "model", None):
        # a self-hosted server must actually offer the model named: a lenient server answering any name is not "connected"
        with contextlib.suppress(Exception):  # a server that cannot list its models is judged by the reply alone
            offered = await ai_settings.list_models(model)
            if offered and model.model not in offered:
                return {"ok": False, "error": f"The server at {model.endpoint} does not have the model “{model.model}”. "
                                              f"It offers: {', '.join(offered[:8])}{' …' if len(offered) > 8 else ''}.",
                        "model": ai_settings.describe(model), "offered": offered[:50]}
    t0 = perf_counter()
    try:
        reply = await model.complete([{"role": "system", "content": "Reply with the single word OK.", "frame": False},
                                      {"role": "user", "content": "Connection test from VENTHORA."}], max_tokens=20)
    except LLMUnavailable as exc:
        return {"ok": False, "error": str(exc), "model": ai_settings.describe(model)}
    from . import ai_usage

    last = await asyncio.to_thread(ai_usage.latest) or {}  # the call just made, from the usage log: who answered, the tokens
    return {"ok": True, "latency_ms": round((perf_counter() - t0) * 1000), "reply": reply.strip()[:80],
            "model": ai_settings.describe(model), "answered_by": last.get("model"),
            "tokens": {"input": last.get("input_tokens", 0), "output": last.get("output_tokens", 0)}}


@app.post("/v1/settings/ai/models")
async def ai_settings_models(s: ai_settings.AISettings | None = None, role: str = "finance_manager"):
    """The model ids the server or provider offers, to pick from instead of typing."""
    _writer(role, "list")
    if s is not None and s.mode == "self_hosted" and s.self_hosted.endpoint and not s.self_hosted.model:
        s = s.model_copy(update={"self_hosted": s.self_hosted.model_copy(update={"model": "(listing)"})})  # listing is how the name is found
    if s is not None and s.mode == "provider" and not s.provider.model:
        s = s.model_copy(update={"provider": s.provider.model_copy(update={"model": "(listing)"})})
    try:
        model = _draft_model(s)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not getattr(model, "endpoint", None) and not getattr(model, "api_key", None):
        raise HTTPException(422, "enter the server address or the API key first")
    try:
        ids, labels = await ai_settings.list_models_labeled(model)
        return {"models": ids[:300], "labels": {k: v for k, v in labels.items() if k in set(ids[:300])}}
    except Exception as exc:  # noqa: BLE001 - the provider's reason, shown on the form
        raise HTTPException(502, f"could not list models: {type(exc).__name__}: {str(exc)[:200]}") from exc


class AIKeyIn(BaseModel):
    api_key: str | None = Field(None, max_length=500)
    model: str = Field("", max_length=200)
    base_url: str = Field("", max_length=300)
    workspace_id: str = Field("", max_length=80)


@app.put("/v1/settings/ai/keys/{provider}")
async def ai_key_save(provider: str, body: AIKeyIn, role: str = "finance_manager", by: str = "dashboard"):
    """Keep a provider's API key (and preferred model) in the vault, without switching the model in use."""
    who = _setup_writer(role, by)
    if provider not in ai_settings.PROVIDERS:
        raise HTTPException(404, "unknown provider")
    body.api_key = ai_settings._unmask(body.api_key)  # ******** is the saved key shown back: keep it
    try:
        out = await asyncio.to_thread(ai_settings.AIStore().save_key, provider, body.api_key, body.model, body.base_url, who, body.workspace_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "ai_key_saved", {"by": who, "provider": provider, "model": body.model})  # never the key
    return out | {"active": ai_settings.describe(active_model())}


@app.delete("/v1/settings/ai/keys/{provider}")
async def ai_key_delete(provider: str, role: str = "finance_manager", by: str = "dashboard"):
    who = _setup_writer(role, by)
    try:
        out = await asyncio.to_thread(ai_settings.AIStore().delete_key, provider, who)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "ai_key_deleted", {"by": who, "provider": provider})
    return out | {"active": ai_settings.describe(active_model())}


@app.get("/v1/org/standards")
async def org_standards():
    return app.state.org.standards()


@app.get("/v1/org/standards/catalog")
async def org_standards_catalog():
    """Every finance standard VENTHORA knows: sections, fields, options, which organisations each concerns, and
    whether VENTHORA enforces it or records it."""
    from .finance_standards import catalog

    return catalog()


@app.put("/v1/org/standards")
async def set_org_standards(s: Standards, role: str = "finance_manager", by: str = "dashboard"):
    who = _writer(role, by)
    out = await asyncio.to_thread(app.state.org.save_standards, s, who)
    await asyncio.to_thread(app.state.audit.append, "org_standards", {"by": who})
    return out


# ---- Settings → Data sources: master data from the old application (Sync API) or from files (Upload) -----------------
def _ingest_ctx() -> dict:
    from .masterdata import MasterStore

    return {"master": MasterStore(), "org": app.state.org, "intake": app.state.intake, "payees": app.state.executor.store}


@app.get("/demo/legacy-erp/openapi.json", include_in_schema=False)
async def demo_legacy_erp_openapi():
    """The stand-in ERP describes its own API, as many finance applications do, so discovery can read it."""
    from .sync_catalog import PUSH

    data = json.loads((ROOT / "data" / "demo_legacy_erp.json").read_text(encoding="utf-8"))
    paths = {f"/{k}": {"get": {"summary": k.replace("_", " ")}} for k in data if not k.startswith("_")}
    paths |= {f"/inbox/{k}": {"post": {"summary": f"receive {k}"}} for k in PUSH}
    return {"openapi": "3.0.3", "info": {"title": "Legacy ERP (demo)", "version": "1"}, "paths": paths}


@app.get("/demo/legacy-erp/{entity}", include_in_schema=False)
async def demo_legacy_erp(entity: str, request: Request):
    """A stand-in for the company's old ERP (fictional data in data/demo_legacy_erp.json), behind a fake API key."""
    from .connectors import DEMO_KEY

    if request.headers.get("x-legacy-key") != DEMO_KEY:
        raise HTTPException(401, "bad API key")
    data = json.loads((ROOT / "data" / "demo_legacy_erp.json").read_text(encoding="utf-8"))
    if entity not in data or entity.startswith("_"):
        raise HTTPException(404, "unknown entity")
    items = data[entity]
    if entity == "budget":  # the demo budget is always for the current month, so the sync replaces a live placeholder
        period = datetime.now(UTC).strftime("%Y-%m")
        items = [r | {"Period": period} if r.get("Period") == "CURRENT" else r for r in items]
    return {"items": items, "count": len(items)}


@app.get("/v1/sources/connectors")
async def connectors_list(request: Request):
    from .connectors import Connector, ConnectorStore, demo_connector

    store = ConnectorStore()
    demo = demo_connector(str(request.base_url).rstrip("/"))
    if not store.list():  # a fresh install shows the demo connector to the stand-in ERP, ready to sync
        store.save(demo)
    raw = store.get_raw(demo.connector_id)
    new_fields = raw is not None and any(set(em.mapping) - set((raw["entities"].get(k) or {}).get("mapping", {}))
                                         for k, em in demo.entities.items() if k in raw["entities"])
    if raw is not None and (set(demo.entities) - set(raw["entities"]) or set(demo.push) - set(raw.get("push") or {}) or new_fields):
        add = {k: v for k, v in demo.entities.items() if k not in raw["entities"]}  # entities and push items added since
        push = {k: v for k, v in demo.push.items() if k not in (raw.get("push") or {})}
        ents = {k: em | {"mapping": demo.entities[k].mapping | em.get("mapping", {})} if k in demo.entities else em
                for k, em in raw["entities"].items()}  # fields mapped since are added; what a person mapped stays
        store.save(Connector.model_validate(raw | {"entities": ents | add, "push": (raw.get("push") or {}) | push}))
    return store.list()


class ConnectIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="the application, e.g. our accounting system")
    connection: str = Field(min_length=8, max_length=800, description="one line: the API's base URL, then its key")
    pull: list[str] | None = Field(None, description="entities to read; default every one")
    push: list[str] | None = Field(None, description="items to send back; default every one")
    schedule_minutes: int = Field(1, ge=0, le=1440, description="1 = every minute (the default); 0 = only on demand")


@app.get("/v1/sources/catalog")
async def sources_catalog():
    """What VENTHORA reads from a finance application and what it sends back, each with why and its fields."""
    from .sync_catalog import PULL, PUSH

    return {"pull": [{"key": k, "label": lbl, "why": why, "path": path, "fields": list(f)} for k, (lbl, why, path, f) in PULL.items()],
            "push": [{"key": k, "label": lbl, "why": why, "path": path, "fields": list(f)} for k, (lbl, why, path, f) in PUSH.items()],
            "connection": {"format": "<base URL> <credential>", "credential": ["Bearer <token>", "<Header-Name>: <value>", "<token>",
                                                                                "https://user:password@host/... (basic)"]}}


@app.post("/v1/sources/connectors")
async def connectors_create(body: ConnectIn, role: str = "finance_manager", by: str = "dashboard"):
    """Connect a finance application with one line: base URL and key. Every item is read and sent back unless unticked;
    the other application's field names default to VENTHORA's and can be mapped afterwards."""
    import re as _re

    from .connectors import Auth, Connector, ConnectorStore, EntityMap, PushMap
    from .sync_catalog import PULL, PUSH, default_mapping, parse_connection

    who = _setup_writer(role, by)
    try:
        base_url, auth = parse_connection(body.connection)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    unknown = [k for k in (body.pull or []) if k not in PULL] + [k for k in (body.push or []) if k not in PUSH]
    if unknown:
        raise HTTPException(422, f"unknown item {unknown[0]}")
    pull = set(PULL if body.pull is None else body.pull)
    push = set(PUSH if body.push is None else body.push)
    store = ConnectorStore()
    slug = _re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:30] or "app"
    cid, n = slug if len(slug) >= 2 else f"app-{slug}", 2
    while store.get_raw(cid) is not None:
        cid, n = f"{slug}-{n}", n + 1
    c = Connector(connector_id=cid, name=body.name, base_url=base_url, auth=Auth(**auth), schedule_minutes=body.schedule_minutes,
                  entities={k: EntityMap(enabled=k in pull, path=PULL[k][2], items_path="", mapping=default_mapping(k)) for k in PULL},
                  push={k: PushMap(enabled=k in push, path=PUSH[k][2]) for k in PUSH})
    out = await asyncio.to_thread(store.save, c)
    await asyncio.to_thread(app.state.audit.append, "connector_saved", {"id": cid, "by": who, "base_url": base_url,
                                                                        "pull": sorted(pull), "push": sorted(push)})
    return out


@app.put("/v1/sources/connectors/{connector_id}")
async def connectors_save(connector_id: str, body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    from .connectors import Connector, ConnectorStore

    who = _setup_writer(role, by)
    try:
        c = Connector.model_validate(body | {"connector_id": connector_id})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    out = ConnectorStore().save(c)
    await asyncio.to_thread(app.state.audit.append, "connector_saved", {"id": connector_id, "by": who, "base_url": c.base_url})
    return out


class ERPNextIn(BaseModel):
    site_url: str = Field(min_length=8, max_length=300, description="https://<your-site>.frappe.cloud")
    api_key: str = Field("", max_length=200, description="ERPNext: User -> API Access -> Generate Keys; blank keeps the saved one")
    api_secret: str = Field("", max_length=200, description="shown once by ERPNext; write-only here")
    default_charge_code: str = Field("", max_length=40, description="the charge code POs from ERPNext get (ERPNext keeps none); blank = POs wait for one")


@app.post("/v1/sources/connectors/template/erpnext")
async def connectors_erpnext(body: ERPNextIn, role: str = "finance_manager", by: str = "dashboard"):
    """Connect ERPNext from its template (every item VENTHORA reads, mapped from ERPNext's own field names), then
    read one supplier to prove the site and the key work. Nothing is written until a person presses Sync."""
    import httpx

    from .connectors import ConnectorStore, erpnext_connector, headers_for

    who = _setup_writer(role, by)
    if bool(body.api_key.strip()) != bool(body.api_secret.strip()):
        raise HTTPException(422, "paste both the API key and the API secret")
    try:
        c = erpnext_connector(body.site_url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.api_key.strip():
        c.auth.secret = f"token {body.api_key.strip()}:{body.api_secret.strip()}"
    store = ConnectorStore()
    if c.auth.secret is None and store.get_raw("erpnext") is None:
        raise HTTPException(422, "paste the API key and API secret from ERPNext (User → API Access → Generate Keys)")
    out = store.save(c)
    await asyncio.to_thread(app.state.audit.append, "connector_saved", {"id": "erpnext", "by": who, "base_url": c.base_url, "template": "erpnext"})
    raw = store.get_raw("erpnext")
    check = {"ok": False, "detail": ""}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{raw['base_url']}/api/method/frappe.auth.get_logged_user", headers=headers_for(raw))
            check = {"ok": r.status_code == 200, "detail": f"signed in as {r.json().get('message')}" if r.status_code == 200 else
                     "ERPNext refused the key (check the API key and secret)" if r.status_code in (401, 403) else f"HTTP {r.status_code}"}
            if check["ok"]:  # the company's currency (a customer without one uses it), and the office new staff land in
                comp = await client.get(f"{raw['base_url']}/api/resource/Company", params={"fields": '["name","default_currency"]', "limit_page_length": 1},
                                        headers=headers_for(raw))
                ccy = ((comp.json().get("data") or [{}])[0].get("default_currency") or "") if comp.status_code == 200 else ""
                hq = next((u["unit_id"] for u in app.state.org.list("unit") if u.get("kind") == "HQ"), "")
                out = store.save(erpnext_connector(body.site_url, ccy, hq, body.default_charge_code.strip().upper()))
                check["defaults"] = {"customer_currency": ccy or None, "staff_office": hq or None, "po_charge_code": body.default_charge_code.strip().upper() or None}
    except (httpx.HTTPError, ValueError) as exc:
        check["detail"] = f"cannot reach {raw['base_url']}: {type(exc).__name__}"
    return {"connector": out, "check": check}


@app.post("/v1/sources/connectors/{connector_id}/disconnect")
async def connectors_disconnect(connector_id: str, role: str = "finance_manager", by: str = "dashboard"):
    """Disconnect: nothing is read from or written to the application any more, but the connection and its key are kept,
    so Connect works again without typing the key (the ERPNext template's Connect re-enables it)."""
    from .connectors import Connector, ConnectorStore

    who = _setup_writer(role, by)
    store = ConnectorStore()
    raw = store.get_raw(connector_id)
    if raw is None:
        raise HTTPException(404, "no such connector")
    rec = {k: v for k, v in raw.items() if not k.startswith("_")}
    rec["auth"] = {k: v for k, v in (rec.get("auth") or {}).items() if k not in ("secret_set", "secret_hint")}
    out = store.save(Connector.model_validate(rec | {"enabled": False}))  # no secret given: the saved key is kept
    await asyncio.to_thread(app.state.audit.append, "connector_disconnected", {"id": connector_id, "by": who})
    return out


@app.delete("/v1/sources/connectors/{connector_id}")
async def connectors_delete(connector_id: str, role: str = "finance_manager", by: str = "dashboard"):
    from .connectors import ConnectorStore

    _setup_writer(role, by)
    if not ConnectorStore().delete(connector_id):
        raise HTTPException(404, "no such connector")
    return {"deleted": connector_id}


@app.post("/v1/sources/connectors/{connector_id}/sync")
async def connectors_sync(connector_id: str, dry_run: bool = False, entity: str | None = None, role: str = "finance_manager",
                          by: str = "dashboard"):
    """Read every enabled entity from the old system and write it (dry_run=true only reports what would change).
    Entities run in dependency order: vendors before their POs and bills, customers before receivables."""
    from .connectors import ConnectorStore

    who = _writer(role, by)
    raw = ConnectorStore().get_raw(connector_id)
    if raw is None:
        raise HTTPException(404, "no such connector")
    if raw.get("enabled", True) is False:
        raise HTTPException(409, f"{raw['name']} is disconnected: press Connect first")
    reports = await _sync_connector(connector_id, dry_run, entity, who)
    return {"connector_id": connector_id, "dry_run": dry_run, "reports": reports}


async def _sync_connector(connector_id: str, dry_run: bool, entity: str | None, who: str) -> dict:
    from . import ingestion
    from .connectors import ORDER, ConnectorStore, fetch

    store = ConnectorStore()
    raw = store.get_raw(connector_id)
    ctx, reports = _ingest_ctx(), {}
    for ent in ORDER:
        em = raw["entities"].get(ent)
        if not em or not em.get("enabled", True) or (entity and ent != entity):
            continue
        try:
            rows, ids = await fetch(raw, ent)
        except Exception as exc:  # noqa: BLE001 - the old system's answer, shown on the page
            reports[ent] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            continue
        reports[ent] = await asyncio.to_thread(ingestion.apply, ent, rows, ids, source=f"sync:{connector_id}", dry_run=dry_run, **ctx)
    if not dry_run:
        store.record_sync(connector_id, reports)
        app.state.position_cache = None
        await asyncio.to_thread(app.state.audit.append, "connector_synced", {"id": connector_id, "by": who, "summary": {
            e: {k: len(v) for k, v in r.items() if isinstance(v, list)} if "error" not in r else r for e, r in reports.items()}})
    return reports


@app.post("/v1/sources/connectors/{connector_id}/discover")
async def connectors_discover(connector_id: str, ai: bool = True, role: str = "finance_manager", by: str = "dashboard"):
    """Find where the application keeps each item (its OpenAPI document, else the usual path names, GET only) and
    propose how its fields map to VENTHORA's: by name first, then the AI model for what is left. Nothing is saved:
    a person reviews the proposal and applies it with PUT."""
    from . import sync_discover
    from .connectors import ConnectorStore

    who = _setup_writer(role, by)
    raw = ConnectorStore().get_raw(connector_id)
    if raw is None:
        raise HTTPException(404, "no such connector")
    model = active_model() if ai else None
    out = await sync_discover.discover(raw, model=model)
    out["model"] = ai_settings.describe(model) if model is not None else "names only"
    await asyncio.to_thread(app.state.audit.append, "connector_discovered", {"id": connector_id, "by": who, "openapi": out["openapi"],
                                                                             "found": [e for e, p in out["entities"].items() if p.get("found")]})
    return out


@app.post("/v1/sources/connectors/{connector_id}/push")
async def connectors_push(connector_id: str, dry_run: bool = False, item: str | None = None, role: str = "finance_manager",
                          by: str = "dashboard"):
    """Send back every ticked item that is new or changed since the last successful send (dry_run=true only counts)."""
    from .connectors import ConnectorStore

    who = _writer(role, by)
    if ConnectorStore().get_raw(connector_id) is None:
        raise HTTPException(404, "no such connector")
    return {"connector_id": connector_id, "dry_run": dry_run, "reports": await _push_connector(connector_id, dry_run, item, who)}


def _pick(row: dict, fields) -> dict:
    return {f: row.get(f) for f in fields}


async def _push_records(item: str) -> list[tuple[str, dict]]:
    """The records of one item, each with its stable id, from VENTHORA's own books."""
    from .sync_catalog import PUSH

    fields = PUSH[item][3]
    if item in ("payment", "journal"):
        rows, _, _ = await asyncio.to_thread(_report_rows)
        if item == "payment":
            return [(r["transfer_id"], _pick(r, fields)) for r in rows if r.get("transfer_id")]
        return [(r["transfer_id"], {"entry_id": f"je-{r['transfer_id']}", "date": r.get("paid_on"), "currency": r["currency"],
                                    "memo": f"Payment to {r.get('payee') or r.get('payee_id')} · {r.get('obligation_id') or ''}".strip(" ·"),
                                    "lines": [{"account": r.get("account_code") or "unallocated", "debit_minor": r["amount_minor"]},
                                              {"account": f"bank:{r['currency']}", "credit_minor": r["amount_minor"]}]})
                for r in rows if r.get("transfer_id")]
    if item == "po_balance":
        return [(p["po_id"], _pick(p, fields)) for p in await po_reconciliation_report()]
    if item == "payee_status":
        return [(p["beneficiary_id"], _pick(p | {"payee_id": p["beneficiary_id"]}, fields)) for p in await list_payees()]
    if item == "request_status":
        out = []
        for r in await list_intake():
            d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
            out.append((d["intake_id"], _pick(d | {"request_id": d["intake_id"]}, fields)))
        return out
    if item == "budget_usage":
        st = await budget_allocation_status()
        return [(a["alloc_id"], _pick(a | {"allocation_id": a["alloc_id"]}, fields)) for a in st.get("allocations", [])]
    raise ValueError(f"unknown item {item}")


async def _push_connector(connector_id: str, dry_run: bool, item: str | None, who: str) -> dict:
    from .connectors import ConnectorStore, send
    from .sync_catalog import PUSH

    store = ConnectorStore()
    raw = store.get_raw(connector_id)
    reports = {}
    for it in PUSH:
        pm = (raw.get("push") or {}).get(it)
        if not pm or not pm.get("enabled", True) or (item and it != item):
            continue
        try:
            reports[it] = await send(raw, it, await _push_records(it), store, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - shown on the page; the next run tries again
            reports[it] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if not dry_run:
        await asyncio.to_thread(store.record_push_report, connector_id, reports | {"_at": datetime.now(UTC).isoformat(timespec="seconds")})
        if any(r.get("sent") for r in reports.values()):
            await asyncio.to_thread(app.state.audit.append, "connector_pushed", {"id": connector_id, "by": who, "sent": {
                k: r.get("sent", 0) for k, r in reports.items()}})
    return reports


async def _sync_scheduler(every_s: float = 60.0) -> None:
    """Every connector with a schedule is read and written back on time (schedule_minutes=1 is every minute), and
    every inbox VENTHORA reads is read on its own schedule."""
    from .connectors import ConnectorStore

    while True:
        await asyncio.sleep(every_s)
        with contextlib.suppress(Exception):  # a failed run is retried on the next tick; its report is on the page
            now = datetime.now(UTC)
            for c in await asyncio.to_thread(ConnectorStore().list):
                minutes = (c.get("schedule_minutes") or 0) if c.get("enabled", True) else 0  # a disconnected one is never read
                last =datetime.fromisoformat(c["last_sync_at"]) if c.get("last_sync_at") else None
                if minutes and (last is None or (now - last).total_seconds() >= minutes * 60 - 5):
                    await _sync_connector(c["connector_id"], False, None, "scheduler (sync)")
                    await _push_connector(c["connector_id"], False, None, "scheduler (sync)")
        with contextlib.suppress(Exception):
            from .email_settings import EmailStore

            now = datetime.now(UTC)
            for ib in await asyncio.to_thread(EmailStore().inboxes):
                minutes = ib.get("schedule_minutes") or 0
                last = datetime.fromisoformat(ib["last_poll_at"]) if ib.get("last_poll_at") else None
                if ib.get("active", True) and minutes and (last is None or (now - last).total_seconds() >= minutes * 60 - 5):
                    await _poll_inbox(ib["inbox_id"], "scheduler (email)")


@app.post("/demo/legacy-erp/inbox/{item}", include_in_schema=False)
async def demo_legacy_erp_inbox(item: str, request: Request):
    """The stand-in ERP receives what VENTHORA sends back, so the demo shows both directions."""
    from .connectors import DEMO_KEY

    if request.headers.get("x-legacy-key") != DEMO_KEY:
        raise HTTPException(401, "bad API key")
    body = await request.json()
    key = request.headers.get("idempotency-key")
    inbox = _erp_inbox()
    if key and any(x.get("idempotency_key") == key for x in inbox):  # the finance app counts a resend once, as a real one would
        return {"received": body.get("id"), "duplicate": True}
    inbox.append({"item": item, "at": datetime.now(UTC).isoformat(timespec="seconds"), "idempotency_key": key, "body": body})
    _erp_inbox_save(inbox[-500:])
    return {"received": body.get("id")}


def _erp_inbox_path() -> Path:
    """The stand-in finance app keeps what it received beside the org store (data/demo/ for the demo server, a temp
    folder in tests), so it survives a restart and the demo can show it."""
    return Path(app.state.org.path).with_name("legacy_erp_inbox.json")


def _erp_inbox() -> list[dict]:
    try:
        return json.loads(_erp_inbox_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _erp_inbox_save(items: list[dict]) -> None:
    _erp_inbox_path().write_text(json.dumps(items, separators=(",", ":")), encoding="utf-8")


@app.get("/demo/legacy-erp-inbox", include_in_schema=False)
async def demo_legacy_erp_inbox_list(limit: int = Query(50, ge=1, le=500)):
    return list(reversed(_erp_inbox()[-limit:]))


@app.get("/demo/finance-app", include_in_schema=False)
async def demo_finance_app_page():
    """The stand-in finance application's own screen: the records VENTHORA reads, and what VENTHORA sent back."""
    return FileResponse(DASHBOARD.parent / "finance_app.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/demo/finance-app/data", include_in_schema=False)
async def demo_finance_app_data():
    data = json.loads((ROOT / "data" / "demo_legacy_erp.json").read_text(encoding="utf-8"))
    ents = {k: v for k, v in data.items() if not k.startswith("_")}
    return {"name": "Acme Finance ERP (demo)", "note": data.get("_note", ""), "entities": ents,
            "counts": {k: len(v) for k, v in ents.items()}, "inbox": list(reversed(_erp_inbox()[-200:]))}


# ---- Users & Beneficiaries: vendors, consultants and customers, every field a payment or a receipt reads ----------
def _synced_from(source: str | None) -> dict | None:
    """A record last written by a connector sync: {connector_id, name}. It is read only on the dashboard, since the next
    sync would overwrite a change made here; it is changed in the source system and pulled again."""
    if not source or not str(source).startswith("sync:"):
        return None
    from .connectors import ConnectorStore

    cid = str(source)[5:]
    try:
        raw = ConnectorStore().get_raw(cid) or {}
    except Exception:  # noqa: BLE001 - the name is only a label
        raw = {}
    return {"connector_id": cid, "name": raw.get("name") or cid}


def _refuse_synced(what: str, source: str | None) -> None:
    s = _synced_from(source)
    if s:
        raise HTTPException(409, f"{what} is synced from {s['name']} through Auto Connect (API) and cannot be changed here: the next "
                                 "sync would overwrite it. Change it in the source system, then refresh the API connection.")


def _masked(b: dict | None) -> dict | None:
    if not b:
        return None
    return b | {k: ("•••• " + b[k][-4:]) for k in ("account_number", "iban") if b.get(k)}


@app.get("/v1/beneficiaries")
async def beneficiaries_list(kind: str = Query("vendor", pattern="^(vendor|consultant|employee|customer)$")):
    """Every vendor or consultant VENTHORA may pay (or every customer it receives from), with what a payment reads:
    the registered mailboxes, the call-back number, currency, payout method, priority, budget line, the bank details
    (masked), the address, the lifecycle status, and the purchase orders or contracts held against it."""
    from .masterdata import MasterStore, bank_from_payload, vendor_kind

    reg = app.state.intake.registry
    if kind == "customer":
        master = {c["customer_id"]: c for c in MasterStore().list("customer")}
        return [{"id": k, "name": v["name"], "currency": v["currency"], "contacts": v.get("contacts", []), "domains": v.get("domains", []),
                 "aliases": v.get("aliases", []), "history": v.get("history"), "source": (master.get(k) or {}).get("_source") or "data/payees.json",
                 "synced_from": _synced_from((master.get(k) or {}).get("_source"))}
                for k, v in sorted(reg.payers.items(), key=lambda x: x[1]["name"])]
    master = {v["vendor_id"]: v for v in MasterStore().list("vendor")}
    bens = app.state.executor.beneficiaries
    pos = app.state.org.list("po")
    out = []
    for k, v in sorted(reg.payees.items(), key=lambda x: x[1]["name"]):
        m = master.get(k) or {}
        vk = vendor_kind({"kind": m.get("kind") or v.get("kind"), "budget_line": m.get("budget_line") or v.get("budget_line"), "default_priority": m.get("default_priority", v.get("default_priority"))})
        if vk != kind:
            continue
        ben = bens.get(k) or {}
        b = ((ben.get("payload") or {}).get("beneficiary") or {})
        life = app.state.executor.store.payee(k) or {}
        out.append({"id": k, "name": v["name"], "kind": vk, "engagement": m.get("engagement") or (("regular" if v.get("default_priority") == 7 else "short_term") if vk == "consultant" else None),
                    "entity_type": m.get("entity_type") or b.get("entity_type") or "COMPANY", "tax_id": m.get("tax_id", ""),
                    "currency": v.get("currency"), "payout_method": v.get("payout_method", "LOCAL"), "contacts": v.get("contacts", []), "domains": v.get("domains", []),
                    "invoice_prefix": v.get("invoice_prefix", ""), "budget_line": v.get("budget_line"), "default_priority": v.get("default_priority", 5),
                    "registered_phone": life.get("registered_phone") or (v.get("lifecycle") or {}).get("registered_phone"),
                    "transfer_reason": ben.get("transfer_reason", "business_expenses"), "address": b.get("address") or m.get("address") or {},
                    "bank": _masked(bank_from_payload(ben) or m.get("bank")), "status": life.get("status") or (v.get("lifecycle") or {}).get("status"),
                    "verified_by": life.get("verified_by"), "cooling_until": life.get("cooling_until"),
                    "bank_check": _bank_check_row(k, ben),
                    "source": m.get("_source") or "data/payees.json", "synced_from": _synced_from(m.get("_source")),
                    "purchase_orders": [{"po_id": p["po_id"], "total_minor": p["total_minor"], "currency": p["currency"], "status": p.get("status", "OPEN"),
                                         "description": p.get("description", "")} for p in pos if p.get("supplier_id") == k]})
    return out


def _bank_check_row(payee_id: str, ben: dict | None) -> dict:
    """The payee's bank check as a list shows it: the verdict, whether a payment may go, and what the bank said."""
    rec = app.state.org.get(bankcheck.KIND, payee_id) or {}
    ok, why = bankcheck.passed(app.state.org, payee_id, ben)
    return {"verdict": rec.get("verdict") if rec.get("fingerprint") == bankcheck.fingerprint(ben) else ("STALE" if rec else "NOT_CHECKED"),
            "payable": ok, "detail": rec.get("detail") if ok else why, "at": rec.get("at"),
            "resolved_account_name": rec.get("resolved_account_name"), "resolved_bank_name": rec.get("resolved_bank_name")}


class BeneficiaryIn(BaseModel):
    record: dict = Field(description="the vendor, consultant or customer, in the fields of GET /v1/beneficiaries")
    keep_bank: bool = Field(default=False, description="keep the bank details on file (they are never sent back unmasked)")


@app.post("/v1/beneficiaries")
async def beneficiaries_save(body: BeneficiaryIn, kind: str = Query("vendor", pattern="^(vendor|consultant|employee|customer)$"),
                             role: str = "finance_manager", by: str = "dashboard"):
    """Add or change a vendor, consultant or customer. It goes through the same pipeline as a sync or an upload: new
    or changed bank details make the payee NEW or CHANGED, and nothing is paid to it until a call-back on the
    registered number."""
    from . import ingestion
    from .masterdata import bank_from_payload

    who = _writer(role, by)
    rec = dict(body.record)
    entity = "customer" if kind == "customer" else "vendor"
    rid = rec.get(f"{entity}_id") or rec.get("id")
    if rid:
        from .masterdata import MasterStore

        _refuse_synced(rec.get("name") or rid, MasterStore().source(entity, rid))
    if entity == "vendor":
        rec["kind"] = kind
        rec.setdefault("vendor_id", rec.pop("id", None))
        if body.keep_bank and rec.get("vendor_id"):
            from .masterdata import MasterStore

            old = MasterStore().get("vendor", rec["vendor_id"]) or {}
            rec["bank"] = old.get("bank") or bank_from_payload(app.state.executor.beneficiaries.get(rec["vendor_id"]))
        if not rec.get("vendor_id"):
            rec.pop("vendor_id", None)
    else:
        rec.setdefault("customer_id", rec.pop("id", None))
        if not rec.get("customer_id"):
            rec.pop("customer_id", None)
    pre = None
    if entity == "vendor" and rec.get("bank"):
        # wrong bank details are refused here, before VENTHORA keeps them (bankcheck.py): Airwallex is never asked to pay them
        pre = await _bank_check_payload(rec)
        if pre and pre["verdict"] in bankcheck.REFUSED:
            await asyncio.to_thread(app.state.audit.append, "beneficiary_bank_refused", {"by": who, "name": rec.get("name"), "verdict": pre["verdict"]})
            raise HTTPException(422, {"message": f"bank details refused ({pre['verdict']}): {pre['detail']}", "bank_check": pre})
    report = await asyncio.to_thread(ingestion.apply, entity, [rec], [None], source=f"dashboard:{who}", **_ingest_ctx())
    if report["rejected"]:
        raise HTTPException(422, report["rejected"][0]["error"])
    await asyncio.to_thread(app.state.audit.append, f"beneficiary_{kind}", {"by": who, "report": {k: v for k, v in report.items() if k != "changes"}})
    if entity == "vendor":  # the check is kept with the payee, for exactly these bank details
        report["airwallex_check"] = await _airwallex_check([*report["created"], *report["updated"]], reuse=pre, by=who)
    return report


async def _bank_check_payload(rec: dict) -> dict | None:
    """Run the bank check on a vendor record before it is saved; None when the record is not complete enough to build
    a payment payload (the ingestion then names what is missing)."""
    from .masterdata import Vendor, beneficiary_payload

    try:  # a new vendor has no id yet: a placeholder lets the model fill its defaults for the check
        payload = beneficiary_payload(Vendor.model_validate(rec | {"vendor_id": rec.get("vendor_id") or "new-vendor"}).model_dump())
    except (ValueError, KeyError, TypeError):
        return None
    try:
        bank = _bank()
    except HTTPException:
        bank = None
    return await bankcheck.run(bank, payload, _xendit_or_none())


class AirwallexCheckIn(BaseModel):
    ids: list[str] | None = Field(None, description="the vendors to check; every vendor when empty")


@app.post("/v1/beneficiaries/bank-check")
async def beneficiaries_airwallex_check(body: AirwallexCheckIn | None = None):
    """Ask Airwallex whether it would accept each vendor's and consultant's bank details (validation only, nothing is
    created): every vendor in the master data, or the ids given. The answer names the fields to fix."""
    from .masterdata import MasterStore

    all_ids = [v["vendor_id"] for v in MasterStore().list("vendor")]  # vendors, consultants and staff paid by transfer
    ids = (body.ids if body else None) or []
    want = [i for i in all_ids if not ids or i in ids]
    res = await _airwallex_check(want)
    return {"checked": len(res), "accepted": sum(1 for r in res.values() if r.get("ok")), "results": res}


async def _airwallex_check(vendor_ids: list[str], reuse: dict | None = None, by: str = "dashboard") -> dict:
    """The bank check (bankcheck.py) for each saved vendor, kept with it for exactly its current bank details. Nothing
    is created on Airwallex. `reuse`: a check just made on the same details (the save), not asked twice."""
    from .masterdata import MasterStore, beneficiary_payload

    try:
        bank = _bank()
    except HTTPException:
        bank = None
    store, out = MasterStore(), {}
    for vid in vendor_ids:
        rec = store.get("vendor", vid)
        p = beneficiary_payload(rec) if rec else None
        res = reuse if reuse and p is not None and reuse.get("fingerprint") == bankcheck.fingerprint(p) else await bankcheck.run(bank, p, _xendit_or_none())
        await asyncio.to_thread(bankcheck.save, app.state.org, vid, res, by)
        out[vid] = res | {"ok": None if res["verdict"] == "UNCHECKED" else res["passed"]}
    return out


@app.get("/v1/sources/master/{entity}")
async def master_list(entity: str):
    from .ingestion import ORG_ENTITIES
    from .masterdata import ENTITIES, MasterStore

    if entity in ORG_ENTITIES:
        org = app.state.org
        with org._connect() as c:
            by = {r["record_id"]: r["updated_by"] for r in c.execute("SELECT record_id, updated_by FROM records WHERE kind = ?", (entity,))}
        return [r | {"_source": by.get(r.get(ID_FIELD[entity]), "-")} for r in org.list(entity)]
    if entity not in ENTITIES:
        raise HTTPException(404, "unknown entity")
    rows = MasterStore().list(entity)
    for r in rows:  # bank account numbers are shown masked
        b = r.get("bank") or {}
        for k in ("account_number", "iban"):
            if b.get(k):
                b[k] = "•••• " + b[k][-4:]
    return rows


MAX_UPLOAD_BYTES = 15 * 1024 * 1024


@app.get("/v1/sources/fields")
async def upload_fields():
    """The fields each entity takes, with the kind that decides how a cell is cleaned."""
    from .imports import FIELDS

    return {e: [{"field": f, "kind": k} for f, (k, _) in fs.items()] for e, fs in FIELDS.items()}


@app.get("/v1/sources/entities")
async def upload_entities():
    """Every kind of record a dataset can hold: its label, what VENTHORA uses it for, and the page it shows on."""
    from .imports import ENTITIES

    return [{"entity": e, "label": label, "why": why, "page": page} for e, (label, why, page) in ENTITIES.items()]


@app.post("/v1/sources/uploads")
async def upload_start(request: Request, filename: str, entity: str = "auto", role: str = "finance_manager"):
    """Upload any data file (the raw bytes as the body): CSV, TSV, Excel, JSON, Word, HTML, e-mail, PDF or text.
    entity=auto lets VENTHORA decide what the file holds (column names first, then the AI model). The reply proposes
    which column feeds which field; nothing is written until the preview is committed."""
    import uuid

    from . import imports

    _writer(role, "upload")
    if entity != "auto" and entity not in imports.FIELDS:
        raise HTTPException(404, "unknown entity")
    data = await request.body()
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "the file is empty or larger than 15 MB")
    try:
        parsed = await asyncio.to_thread(imports.parse, data, filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    model = active_model()
    detected = None
    if entity == "auto":
        scores = imports.detect(parsed["headers"])
        clear = bool(scores) and scores[0][1] >= 3 and (len(scores) == 1 or scores[0][1] - scores[1][1] >= 1)
        entity, by = (scores[0][0], "column names") if clear else (None, "")
        if entity is None:
            entity = await imports.detect_with_model(parsed["headers"], parsed["rows"][:3], parsed.get("text", ""), model)
            by = "AI model" if entity else ""
        if entity is None and scores:
            entity, by = scores[0][0], "column names (best guess)"
        if entity is None:
            raise HTTPException(422, "VENTHORA could not tell what this file holds" + ("" if model.configured else
                                " (connect an AI model to read documents without columns)") + ": choose the kind of data and upload again")
        detected = {"entity": entity, "by": by, "candidates": [{"entity": e, "score": round(sc, 2)} for e, sc in scores[:3]]}
    reader = "rules"
    if parsed["kind"] in ("pdf", "docx", "html", "eml", "text") and not parsed["rows"] and not model.configured:
        raise HTTPException(422, "this document has no columns; connect an AI model (Platform Setup > Agent) so it can be read")
    if parsed["kind"] in ("pdf", "docx", "html", "eml", "text") and model.configured and (parsed["kind"] == "pdf" or not parsed["rows"]):
        rows = await imports.pdf_rows_with_model(entity, parsed.get("text", ""), model)
        if rows:  # the model read the report; keys are already VENTHORA's fields
            parsed |= {"rows": rows, "headers": sorted({k for r in rows for k in r})}
            reader = "model"
    mapping = imports.suggest(entity, parsed["headers"])
    if reader == "model":
        mapping = {h: h for h in parsed["headers"]}
    elif model.configured:
        extra = await imports.suggest_with_model(entity, parsed["headers"], parsed["rows"], mapping, model)
        mapping |= extra
        reader = "rules+model" if extra else "rules"
    uid = uuid.uuid4().hex[:12]
    app.state.__dict__.setdefault("uploads", {})[uid] = {"filename": filename, "entity": entity, **parsed}
    label, _, page = imports.ENTITIES[entity]
    return {"upload_id": uid, "filename": filename, "entity": entity, "label": label, "page": page, "detected": detected,
            "kind": parsed["kind"], "headers": parsed["headers"],
            "rows": len(parsed["rows"]), "sample": parsed["rows"][:5], "mapping": mapping, "reader": reader,
            "unmapped_fields": [f for f in imports.FIELDS[entity] if f not in mapping]}


class UploadMapping(BaseModel):
    mapping: dict[str, str]


async def _upload_apply(upload_id: str, req: UploadMapping, dry_run: bool):
    from . import imports, ingestion

    up = getattr(app.state, "uploads", {}).get(upload_id)
    if up is None:
        raise HTTPException(404, "unknown upload; upload the file again")
    bad = [f for f in req.mapping if f not in imports.FIELDS[up["entity"]]]
    if bad:
        raise HTTPException(422, f"unknown field {bad[0]}")
    rows = imports.clean(up["entity"], up["rows"], req.mapping)
    report = await asyncio.to_thread(ingestion.apply, up["entity"], rows, [None] * len(rows), source=f"upload:{up['filename']}",
                                     dry_run=dry_run, **_ingest_ctx())
    return report | {"cleaned_sample": [r for r in rows[:10]]}


@app.post("/v1/sources/uploads/{upload_id}/preview")
async def upload_preview(upload_id: str, req: UploadMapping):
    """The full pipeline as a dry run: what would be created, updated or rejected, and the first rows as cleaned."""
    return await _upload_apply(upload_id, req, dry_run=True)


@app.post("/v1/sources/uploads/{upload_id}/commit")
async def upload_commit(upload_id: str, req: UploadMapping, role: str = "finance_manager", by: str = "dashboard"):
    who = _writer(role, by)
    report = await _upload_apply(upload_id, req, dry_run=False)
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "upload_committed", {"upload_id": upload_id, "by": who, "entity": report["entity"],
                                                                         "created": len(report["created"]), "updated": len(report["updated"]),
                                                                         "rejected": len(report["rejected"])})
    getattr(app.state, "uploads", {}).pop(upload_id, None)
    return report


@app.get("/v1/sources/readiness")
async def readiness():
    """Platform setup only: the connections, organisation, master data, intake channels and policies VENTHORA must
    have configured before it can run, and the ones that are optional. It does not judge day-to-day transactions
    (balances, bills, POs, payee verification): those live on their own pages. Each item says whether it is done,
    still required, or optional and open, what to do, and where; `status` also tells real data from a placeholder
    (the demo seed or the data/*.json files)."""
    from .connectors import ConnectorStore
    from .email_settings import EmailStore
    from .intake.claims import APPROVER_ROLES
    from .masterdata import MasterStore
    from .reporting import load_config

    master, org = MasterStore(), app.state.org
    counts, reg = master.counts(), app.state.intake.registry

    def real(entity: str) -> int:
        return sum(n for src, n in counts.get(entity, {}).items() if not src.startswith("seed"))

    def org_real(kind: str) -> tuple[int, int]:
        recs = org.list(kind)
        with org._connect() as c:  # who last wrote each record
            by = {r["record_id"]: r["updated_by"] for r in c.execute("SELECT record_id, updated_by FROM records WHERE kind = ?", (kind,))}
        return sum(1 for r in recs if by.get(r.get(ID_FIELD[kind])) != "seed"), len(recs)

    items = []

    def add(group, name, status, detail, fix, *, need="required", todo=""):
        """status: ok (real) / placeholder (demo data, the platform runs on it) / missing."""
        state = "done" if status == "ok" else ("required" if need == "required" and status == "missing" else "optional")
        items.append({"group": group, "name": name, "status": status, "need": need, "state": state, "detail": detail,
                      "todo": "" if state == "done" else todo, "fix": fix})

    # 1. connections: the bank, its confirmations, the AI reader, the company's finance application
    bank = app.state.agent.proof.airwallex
    add("Connections", "Airwallex sandbox credentials", "ok" if bank.configured else "missing",
        "AWX_CLIENT_ID / AWX_API_KEY in .env" if bank.configured else "not set", "#/airwallex",
        todo="Connect the Airwallex account (client ID and API key); without it nothing can be paid.")
    add("Connections", "Airwallex webhooks", "ok" if webhooks.secret() else "missing",
        "secret set; subscribe with scripts/register_webhook.py" if webhooks.secret() else "no webhook secret", "#/integrations",
        todo="Set the webhook secret and subscribe the tunnel URL, so a payment is confirmed PAID the moment Airwallex says so.")
    m = active_model()
    add("Connections", "AI model", "ok" if m.configured else "missing", ai_settings.describe(m) if m.configured else "rules only",
        "#/ai", need="optional", todo="Optional: a model reads invoices and PDFs better; the rules decide without one.")
    conns = [c for c in ConnectorStore().list() if c.get("active", True)]
    add("Connections", "Finance application (Sync API)", "ok" if conns else "missing",
        ", ".join(c["name"] for c in conns[:3]) if conns else "not connected", "#/sources-sync", need="optional",
        todo="Optional: connect the company's accounting or ERP application with one line, to sync data both ways.")

    # 2. organisation: who works where, and their limits
    for kind, label, need in (("unit", "Organisation units (offices, branches)", "required"), ("user", "People and approval limits", "required"),
                              ("project", "Projects", "optional"), ("account", "Chart of accounts", "optional"), ("price", "Unit prices", "optional")):
        r, a = org_real(kind)
        add("Organisation", label, "ok" if r else ("placeholder" if a else "missing"),
            f"{a} records ({r} synced, uploaded or entered by a person; the rest are the demo seed)", "#/users" if kind == "user" else "#/sources-sync",
            need=need, todo=(f"Add {label.lower()}: sync, upload or by hand." if not a else
                             f"The platform runs on the demo {label.lower()}; sync or upload your own to replace them.")
            if need == "required" else f"Optional: {label.lower()}, synced or uploaded.")

    # 3. approval: a second person who may approve
    people = [u for u in org.list("user") if u.get("active", True)]
    approvers = [u for u in people if u["role"] in APPROVER_ROLES]
    add("Approval", "An approver other than who asks", "ok" if len(people) >= 2 and approvers else "missing",
        f"{len(people)} active people, {len(approvers)} who may approve ({', '.join(u['name'] for u in approvers[:3])})" if people else "no person yet",
        "#/users", todo="Add at least two active people, one of them a finance manager, country finance lead or CFO: "
                        "nobody approves their own request.")

    # 4. master data the platform pays and matches against
    n_vendor_real, n_payees = real("vendor"), len(reg.payees)
    add("Master data", "Vendors and bank details", "ok" if n_vendor_real else ("placeholder" if n_payees else "missing"),
        f"{n_payees} vendors ({n_vendor_real} from the company's data, {n_payees - n_vendor_real} from data/payees.json)", "#/sources-sync",
        todo="Bring in the vendors with their bank details: Sync API, an upload, or by hand." if not n_payees else
        "The platform runs on the demo vendors; sync or upload your own to replace them.")
    add("Master data", "Customers", "ok" if real("customer") else "placeholder",
        f"{len(reg.payers)} customers ({real('customer')} from the company's data)", "#/sources-sync", need="optional",
        todo="Optional: customers, to match money that arrives to who sent it.")
    add("Master data", "Tenant books", "ok" if real("tenant_book") else "placeholder",
        f"{real('tenant_book')} books from the company's data; the rest from data/tenants.json", "#/mandate", need="optional",
        todo="Optional: which entity each book belongs to, its payees and approvers.")

    # 5. intake: a configured way in for payment requests
    boxes = [b for b in org.list("mailbox") if b.get("active", True)]
    inboxes = [i for i in await asyncio.to_thread(EmailStore().inboxes) if i.get("active", True) and i.get("secret_set")]
    push_ok = bool(env("VENTHORA_INBOUND_EMAIL_SECRET")) and bool(boxes)
    bills_by_api = [c["name"] for c in conns if (c.get("entities", {}).get("bill") or {}).get("enabled")]
    ways = (["email pushed to " + ", ".join(b["address"] for b in boxes[:2])] if push_ok else []) + \
           ([f"{len(inboxes)} inbox(es) VENTHORA reads"] if inboxes else []) + ([f"bills from {', '.join(bills_by_api[:2])}"] if bills_by_api else []) + \
           (["payment orders from the dashboard"] if people else [])
    add("Intake", "A channel for payment requests", "ok" if ways else "missing", "; ".join(ways) or "none",
        "#/sources-email", todo="Connect an inbox or a mail service, sync bills from the finance application, or add people "
                                "who can enter payment orders.")
    add("Intake", "Email intake", "ok" if push_ok or inboxes else "missing",
        "; ".join([f"{len(boxes)} pushed address(es)"] * bool(push_ok) + [f"{len(inboxes)} inbox(es)"] * bool(inboxes)) or "not connected",
        "#/sources-email", need="optional", todo="Optional: let suppliers email invoices (a mail service or an inbox VENTHORA reads).")

    # 6. policy: the rules the platform applies
    pol = org.budget_policy()
    limited = pol["enforcement"] != "no_limit"
    allocs = len(org.list("allocation"))
    add("Policy", "Budget policy", "ok" if (not limited or allocs) else "missing",
        "No limit: every unit pays from the balance" if not limited else f"Limited per allocation, {allocs} allocations", "#/budget-policy",
        need="required" if limited else "optional", todo="Budgets are enforced but none is allocated: allocate a budget, or choose No limit.")
    rules = (await doc_rules_get())["rules"]
    add("Policy", "Document validation", "ok" if rules.get("enabled") else "missing",
        "on: required documents, dates and stamps are checked" if rules.get("enabled") else "off", "#/doc-rules", need="optional",
        todo="Optional: check that every invoice comes with its required documents, sensible dates and a stamp.")
    rep = load_config()
    add("Policy", "Reporting cut-off", "ok" if getattr(rep, "set_by", None) not in (None, "", "seed") else "placeholder",
        f"day {rep.cutoff_day} {rep.cutoff_time} {rep.timezone}, reporting in {rep.reporting_currency}", "#/standards", need="optional",
        todo="Optional: the defaults work; set your own cut-off day, time and reporting currency.")

    summary = {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "placeholder", "missing")}
    states = {s: sum(1 for i in items if i["state"] == s) for s in ("done", "required", "optional")}
    return {"summary": summary, "states": states, "ready": states["required"] == 0, "items": items}


@app.get("/v1/sources/runs")
async def master_runs(limit: int = Query(30, ge=1, le=200)):
    from .masterdata import MasterStore

    return MasterStore().runs(limit)


@app.get("/v1/org/account-code")
async def org_account_code(code: str):
    """Check an account code (office cost centre . project . account) and name what it charges."""
    try:
        r = app.state.org.account_code(code)
    except OrgError as exc:
        raise HTTPException(422, str(exc)) from exc
    limit, at = app.state.org.payment_limit(r["unit"]["unit_id"])
    return {"code": r["code"], "unit": r["unit"]["name"], "unit_id": r["unit"]["unit_id"], "project": r["project"]["name"],
            "account": r["account"]["name"], "category": r["account"]["category"], "payment_limit_minor": limit,
            "limit_set_by": at["name"] if at else None}


@app.get("/v1/procurement/purchase-orders")
async def purchase_orders():
    """Every PO with what is paid (from transfers Airwallex reported PAID), pending in the queue, and still open."""
    from . import procurement

    org, pay, intake = app.state.org, app.state.executor.store, app.state.intake
    paid_fp = {r["invoice_fingerprint"] for r in pay.po_payments()}
    pending: dict[str, dict[str, int]] = {}
    for r in intake.store.list(1000):
        if r.po_number and procurement.holds(r, paid_fp):
            key = r.installment or r.intake_id
            pending.setdefault(r.po_number, {})[key] = pending.get(r.po_number, {}).get(key, 0) + r.amount_minor
    out = []
    for po in org.list("po"):
        st = procurement.status(org, pay, po["po_id"], pending=pending.get(po["po_id"]))
        out.append(st | {"po": po, "synced_from": _synced_from(org.source("po", po["po_id"]))})
    return out


class BudgetLinesIn(BaseModel):
    lines: list[dict] = Field(max_length=50, description="line, amount_minor, auto_share_bp, alert_share_bp, require_approval_on_alert, bypass, bypass_approved_by")


def _budget_file() -> list[dict]:
    from .proofcore.budget import BUDGETS_FILE

    try:
        return json.loads(BUDGETS_FILE.read_text(encoding="utf-8"))["budgets"]
    except (OSError, ValueError, KeyError):
        return []


@app.get("/v1/budget-lines")
async def budget_lines_get(period: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """The budget lines of a month: the amount per line, the share the agent pays on its own, the alert share, whether
    an alert needs an approver, and a bypass with its signatory. Lines set here (or uploaded or synced) replace
    data/budgets.json for that month; the rule for a payment outside every line is in the standing mandate."""
    import re

    from . import mandate
    from .masterdata import BUDGET_LINE_FIELDS, MasterStore

    period = period or datetime.now(UTC).strftime("%Y-%m")
    m = MasterStore()
    mine = sorted((ln for ln in m.list("budget_line") if ln["period"] == period), key=lambda x: x["line"])
    file = next((b for b in _budget_file() if b["period"] == period), None)
    if mine:
        lines = [{k: ln.get(k, False if k == "bypass" else None) for k in BUDGET_LINE_FIELDS} for ln in mine]
        src = mine[0]["_source"]
    else:
        lines = [{k: ln.get(k, False if k in ("bypass", "require_approval_on_alert") else 10_000 if k == "auto_share_bp" else 8_000 if k == "alert_share_bp" else None)
                  for k in BUDGET_LINE_FIELDS} for ln in (file or {}).get("lines", [])]
        src = "data/budgets.json" if file else None
    names = sorted({ln["line"] for ln in lines} | {p.get("budget_line") for p in app.state.intake.registry.payees.values() if p.get("budget_line")})
    periods = sorted({ln["period"] for ln in m.list("budget_line")} | {b["period"] for b in _budget_file() if re.match(r"^\d{4}-\d{2}$", b["period"])} | {period})
    synced = next((s for s in (_synced_from(ln["_source"]) for ln in mine) if s), None)
    return {"period": period, "periods": periods, "lines": lines, "source": src, "synced_from": synced, "line_names": names,
            "unbudgeted_requires_approval": mandate.unbudgeted_rule(app.state.org) if mandate.unbudgeted_rule(app.state.org) is not None
            else bool((file or {}).get("unbudgeted_requires_approval", True))}


@app.put("/v1/budget-lines/{period}")
async def budget_lines_put(period: PeriodPath, body: BudgetLinesIn, role: str = "finance_manager", by: str = "dashboard"):
    """Replace the budget lines of a month. A bypass (unlimited line) needs the person who signed it. Lines synced
    through Auto Connect (API) are changed in the source system instead."""
    import re

    from .masterdata import BudgetLineIn, MasterStore

    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", period):
        raise HTTPException(422, "the period is a month, YYYY-MM")
    who = _writer(role, by)
    m = MasterStore()
    old = [ln for ln in m.list("budget_line") if ln["period"] == period]
    for ln in old:
        _refuse_synced(f"The budget of {period}", ln["_source"])
    rows, seen = [], set()
    for raw in body.lines:
        row = {k: raw.get(k) for k in ("line", "amount_minor", "auto_share_bp", "alert_share_bp", "require_approval_on_alert", "bypass", "bypass_approved_by")
               if raw.get(k) is not None} | {"period": period}
        row["line"] = str(row.get("line", "")).strip().lower()
        if row["line"] in seen:
            raise HTTPException(422, f"the line {row['line']} is there twice")
        seen.add(row["line"])
        if row.get("bypass") and not str(row.get("bypass_approved_by") or "").strip():
            raise HTTPException(422, f"{row['line']}: a bypass is honoured only when signed; name the person who signed it")
        if not row.get("bypass"):
            row["bypass_approved_by"] = None
        try:
            rows.append(BudgetLineIn.model_validate(row).model_dump())
        except ValueError as exc:
            raise HTTPException(422, f"{row['line'] or 'a line'}: {exc}") from exc
    if not rows:
        raise HTTPException(422, "a budget needs at least one line")
    rep = await asyncio.to_thread(m.upsert, "budget_line", rows, source=f"dashboard:{who}")
    if rep["rejected"]:
        raise HTTPException(422, str(rep["rejected"][0]))
    for ln in old:
        if ln["line"] not in seen:
            await asyncio.to_thread(m.delete, "budget_line", f"{period}:{ln['line']}")
    await asyncio.to_thread(app.state.audit.append, "budget_lines_saved", {"period": period, "by": who, "lines": rows})
    return await budget_lines_get(period)


PR_DECIDERS = ("finance_manager", "cfo", "super_admin", "country_finance_lead", "hq_admin", "country_admin")


class PRDecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field("", max_length=300)


@app.get("/v1/purchase-requests")
async def purchase_requests_list():
    """Every purchase request, newest first, with its decision and the PO raised from it."""
    org = app.state.org
    pos = {p["po_id"]: p for p in org.list("po")}
    out = [r | {"po": pos.get(r.get("po_id") or "")} for r in org.list("pr")]
    return sorted(out, key=lambda r: r["pr_number"], reverse=True)


@app.post("/v1/purchase-requests/{pr_number}/decision")
async def purchase_request_decide(pr_number: str, body: PRDecisionIn, role: str = "finance_manager", by: str = "dashboard"):
    """Approve or reject a purchase request. The requester never decides their own; an approver decides only
    within their approval limit (People and roles)."""
    org = app.state.org
    pr = org.get("pr", pr_number)
    if pr is None:
        raise HTTPException(404, "no such purchase request")
    if pr["status"] != "SUBMITTED":
        raise HTTPException(409, f"{pr_number} is {pr['status'].lower()}; only a submitted request is decided")
    if role not in PR_DECIDERS:
        raise HTTPException(403, f"role {role} may not decide a purchase request")
    me = by.strip().lower()
    if me == pr["requested_by"].strip().lower():
        raise HTTPException(403, "the requester cannot decide their own purchase request")
    person = next((u for u in org.list("user") if me in (u["name"].lower(), u["email"].lower())), None)
    limit = (person or {}).get("approval_limit_minor")
    if body.decision == "approve" and limit is not None and pr["estimated_minor"] > limit:
        raise HTTPException(403, f"{by} may approve up to {limit / 100:,.2f}; this request is {pr['currency']} {pr['estimated_minor'] / 100:,.2f}")
    rec = pr | {"status": "APPROVED" if body.decision == "approve" else "REJECTED", "decided_by": f"{by} ({role})",
                "decided_at": datetime.now(UTC).isoformat(timespec="seconds"), "decision_note": body.note}
    await asyncio.to_thread(org._put, "pr", rec, by)
    await asyncio.to_thread(app.state.audit.append, "purchase_request_decided", {"pr": pr_number, "decision": body.decision, "by": by, "role": role})
    return rec


# ---- Procurement process: vendor sourcing, RFQ forms, quotations, recommendation, award → PO (sourcing.py) ----
class ProcVendorIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=120)
    website: str = Field("", max_length=200)


class ProcInviteIn(BaseModel):
    cand_ids: list[str] = Field(min_length=1, max_length=20)
    deadline: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ProcAwardIn(BaseModel):
    cand_id: str = Field(min_length=1, max_length=60)
    note: str = Field("", max_length=500)


def _proc(fn, *a, **k):
    from .sourcing import SourcingError

    try:
        return fn(*a, **k)
    except SourcingError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OrgError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/v1/procurement/board")
async def procurement_board():
    """Every purchase request in procurement: candidates, invitations, quotations (one row per vendor) and the
    recommendation. Purchase requests not yet sourced are listed under `requests`."""
    from . import sourcing

    org = app.state.org
    rows = await asyncio.to_thread(sourcing.board, org)
    inside = {r["pr"]["pr_number"] for r in rows}
    waiting = [p for p in org.list("pr") if p["pr_number"] not in inside and p["status"] in ("SUBMITTED", "APPROVED")]
    return {"board": rows, "requests": sorted(waiting, key=lambda p: p["pr_number"], reverse=True), "public_url": sourcing.public_url()}


@app.get("/v1/visa/supplier-match")
async def visa_supplier_match(name: str = Query(..., min_length=2, max_length=100), country: str = Query(..., min_length=2, max_length=3),
                              city: str = "", state: str = "", postal: str = ""):
    """Visa Supplier Matching Service: does a supplier accept Visa cards, with Visa's confidence and the merchant
    category code, and how VENTHORA would pay it (Visa virtual card or bank transfer)."""
    from . import visa_sms

    m = await visa_sms.search(name=name, country=country, city=city, state=state, postal=postal)
    return m | {"rail": visa_sms.rail(m)}


@app.get("/v1/visa/supplier-match/vendors")
async def visa_supplier_match_vendors(fresh: bool = False):
    """Every registered vendor and consultant checked with Visa Supplier Matching (kept 30 days; fresh=true asks again)."""
    from . import visa_sms

    rows = await visa_sms.check_all(app.state.org, app.state.intake.registry.payees, beneficiaries=app.state.executor.beneficiaries, fresh=fresh)
    return {"vendors": rows, "accept_visa": sum(1 for r in rows if r.get("accepts_visa")), "checked": sum(1 for r in rows if r.get("ok")),
            "environment": "sandbox", "keep_days": visa_sms.KEEP_DAYS}


class EditionIn(BaseModel):
    demo: bool


@app.get("/v1/edition")
async def edition_get():
    """Demo mode on (alpha-sandbox: demo menus, buttons and endpoints) or off (the real version), and whether the
    server locks it off (VENTHORA_EDITION=production)."""
    from . import edition

    return await asyncio.to_thread(edition.state, app.state.org)


@app.put("/v1/edition")
async def edition_put(body: EditionIn, role: str = "finance_manager", by: str = "dashboard"):
    """Switch demo mode: a Super admin only; recorded in the audit chain."""
    from . import edition

    try:
        out = await asyncio.to_thread(edition.set_demo, app.state.org, body.demo, role=role, by=by)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "edition_demo_mode", {"demo": body.demo, "by": f"{by} ({role})"})
    return out


@app.get("/v1/evidence")
async def hackathon_evidence():
    """System Setting → Hackathon evidence: every organiser item with its status, the screen that proves it and a live
    probe (data/hackathon_evidence.json; the line-by-line matrix is docs/HACKATHON_COMPLIANCE.md)."""
    from .config import ROOT

    data = json.loads((ROOT / "data" / "hackathon_evidence.json").read_text(encoding="utf-8"))
    items = [i for g in data["groups"] for i in g["items"]]
    data["counts"] = {s: sum(1 for i in items if i["status"] == s) for s in ("done", "partial", "blocked", "na")} | {"total": len(items)}
    return data


@app.post("/v1/procurement/demo-requests")
async def procurement_demo_requests(role: str = "finance_manager", by: str = "dashboard"):
    """For the alpha-sandbox demo: two purchase requests in Intake (toner with a locked brand, approved; chairs, waiting)."""
    from . import sourcing

    who = _writer(role, by)
    return {"created": await asyncio.to_thread(_proc, sourcing.demo_requests, app.state.org, who)}


@app.post("/v1/procurement/{pr_number}/source")
async def procurement_source(pr_number: str, role: str = "finance_manager", by: str = "dashboard"):
    """The agent lists candidate vendors for a PR: registered vendors that fit, and vendors found online by the AI model
    (or the demo directory). Nothing is sent."""
    from . import sourcing

    who = _writer(role, by)
    try:
        rec = await sourcing.find_vendors(app.state.org, pr_number, payees=app.state.intake.registry.payees, model=active_model(), by=who)
    except (sourcing.SourcingError, OrgError) as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "procurement_sourced", {"pr": pr_number, "by": who, "candidates": len(rec["candidates"])})
    return {k: v for k, v in rec.items() if k != "candidates"} | {"candidates": [{k: v for k, v in c.items() if k != "token"} for c in rec["candidates"]]}


@app.post("/v1/procurement/{pr_number}/vendors")
async def procurement_add_vendor(pr_number: str, body: ProcVendorIn, role: str = "finance_manager", by: str = "dashboard"):
    """Add a vendor procurement already knows to the PR's candidates."""
    from . import sourcing

    who = _writer(role, by)
    await asyncio.to_thread(_proc, sourcing._pr, app.state.org, pr_number)
    await asyncio.to_thread(_proc, sourcing.add_vendor, app.state.org, pr_number, name=body.name, email=body.email, website=body.website, by=who)
    return {"ok": True}


@app.post("/v1/procurement/{pr_number}/invite")
async def procurement_invite(pr_number: str, body: ProcInviteIn, role: str = "finance_manager", by: str = "dashboard"):
    """Queue the RFQ email to the chosen vendors, each with its own quotation form link."""
    from . import sourcing

    who = _writer(role, by)
    rec = await asyncio.to_thread(_proc, sourcing.invite, app.state.org, pr_number, body.cand_ids, by=who, deadline=body.deadline)
    await asyncio.to_thread(app.state.audit.append, "procurement_rfq_sent", {"pr": pr_number, "by": who, "to": rec["sent"]})
    return {"sent": rec["sent"], "deadline": rec["deadline"]}


@app.get("/v1/procurement/{pr_number}/form-link/{cand_id}")
async def procurement_form_link(pr_number: str, cand_id: str):
    """For the demo only: the form link of an invited demo-directory vendor (a real vendor's link is never shown)."""
    from . import sourcing

    r = sourcing.rfq(app.state.org, pr_number) or {}
    c = next((c for c in r.get("candidates", []) if c["cand_id"] == cand_id), None)
    if not c or not c.get("token"):
        raise HTTPException(404, "not invited")
    if not c.get("demo"):
        raise HTTPException(403, "only a demo-directory vendor's form is opened from here; a real vendor uses the link in its email")
    return {"url": f"/rfq/{c['token']}"}


@app.post("/v1/procurement/{pr_number}/demo-replies")
async def procurement_demo_replies(pr_number: str, role: str = "finance_manager", by: str = "dashboard"):
    """For the demo: the invited demo-directory vendors submit their forms (never a real vendor)."""
    from . import sourcing

    _writer(role, by)
    out = await asyncio.to_thread(_proc, sourcing.demo_replies, app.state.org, pr_number)
    return {"submitted": [q["cand_id"] for q in out]}


@app.post("/v1/procurement/{pr_number}/review")
async def procurement_review(pr_number: str):
    """The AI model reads every vendor's remarks against the specifications; a contradicted 'complies' is not met."""
    from . import sourcing

    try:
        return await sourcing.review(app.state.org, pr_number, active_model())
    except sourcing.SourcingError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/v1/procurement/{pr_number}/award")
async def procurement_award(pr_number: str, body: ProcAwardIn, role: str = "finance_manager", by: str = "dashboard"):
    """A person awards one quotation (the recommendation, or another with the reason); the PO is raised and sent."""
    from . import sourcing
    from .masterdata import MasterStore

    out = await asyncio.to_thread(_proc, sourcing.award, app.state.org, pr_number, body.cand_id, role=role, by=by, note=body.note,
                                  master=MasterStore(), payees_store=app.state.executor.store, registry_payees=app.state.intake.registry.payees)
    await asyncio.to_thread(app.state.audit.append, "procurement_awarded", {k: v for k, v in out.items() if k not in ("po", "vendor_followed")})
    out["chain"] = await _register_contract(out["po_id"], f"{by} ({role})")
    return out


async def _register_contract(po_id: str, who: str) -> dict:
    """A new PO goes on the contract blockchain at once: its block 0 (what was agreed) is hashed and sealed, the seal
    is written to the audit log, and the heads are anchored on Alta when Alta is set up (Developer Console)."""
    from . import metal

    rep = await reports_contract_chain()
    row = next((c for c in rep.get("rows", []) if c.get("contract") == po_id), None)
    seal = app.state.org.get("contract_seal", po_id) or {}
    out = {"contract": po_id, "sealed": bool(seal), "head": seal.get("head"), "blocks": len((row or {}).get("blocks") or []),
           "state": (row or {}).get("chain_state"), "verdict": (row or {}).get("verdict")}
    try:
        rec = await metal.anchor(app.state.org, await asyncio.to_thread(_anchor_heads), who)
        out["anchored"] = {"tx_hash": rec.get("tx_hash"), "chain_id": rec.get("chain_id")}
        await asyncio.to_thread(app.state.audit.append, "metal_anchor", {"tx_hash": rec.get("tx_hash"), "digest": rec.get("digest"), "by": who, "for": po_id})
    except metal.MetalError as exc:
        out["anchored"] = None
        out["anchor_note"] = f"not anchored on Alta: {str(exc)[:160]}"
    return out


@app.get("/vendor-details/{token}", include_in_schema=False)
async def vendor_details_form(token: str):
    """The awarded vendor's own form for the details a payment needs (public; the token is the only key)."""
    return FileResponse(DASHBOARD.parent / "vendor_details.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/vendor-details/{token}/data", include_in_schema=False)
async def vendor_details_data(token: str):
    from . import sourcing
    from .masterdata import MasterStore

    f = sourcing.details_form(app.state.org, token)
    if f is None:
        raise HTTPException(404, "this link is not valid")
    v = MasterStore().get("vendor", f["vendor_id"]) or {}
    return {"vendor": f["vendor"], "po_id": f["po_id"], "pr_number": f["pr_number"], "submitted_at": f.get("submitted_at"),
            "known": {"name": v.get("name"), "tax_id": v.get("tax_id", ""), "registered_phone": v.get("registered_phone", ""),
                      "address": v.get("address") or {}, "has_bank": bool(v.get("bank"))}}


class VendorDetailsIn(BaseModel):
    tax_id: str = Field("", max_length=40)
    registered_phone: str = Field(min_length=6, max_length=40, description="the number VENTHORA calls back to confirm the bank account")
    street_address: str = Field(min_length=3, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field("", max_length=100)
    postcode: str = Field("", max_length=20)
    country_code: str = Field(min_length=2, max_length=2)
    account_name: str = Field(min_length=2, max_length=140)
    bank_name: str = Field(min_length=2, max_length=140)
    bank_country_code: str = Field(min_length=2, max_length=2)
    account_number: str = Field("", max_length=40)
    iban: str = Field("", max_length=40)
    swift_code: str = Field("", max_length=11)
    routing_type: str = Field("", max_length=30)
    routing_value: str = Field("", max_length=30)
    bank_account_category: str = Field("", max_length=20)


@app.post("/vendor-details/{token}", include_in_schema=False)
async def vendor_details_submit(token: str, body: VendorDetailsIn):
    """The awarded vendor's details: saved through the same path as the dashboard, so the bank check refuses wrong
    bank details at once and the payee stays NEW (no payment) until the call-back on the registered number."""
    from . import sourcing
    from .masterdata import MasterStore

    f = sourcing.details_form(app.state.org, token)
    if f is None:
        raise HTTPException(404, "this link is not valid")
    if not (body.account_number or body.iban):
        raise HTTPException(422, "enter the account number or the IBAN")
    v = MasterStore().get("vendor", f["vendor_id"]) or {}
    po = app.state.org.get("po", f["po_id"]) or {}
    rec = {k: x for k, x in v.items() if not k.startswith("_")} | {
        "vendor_id": f["vendor_id"], "tax_id": body.tax_id or v.get("tax_id", ""), "registered_phone": body.registered_phone,
        "address": {"street_address": body.street_address, "city": body.city, "state": body.state, "postcode": body.postcode, "country_code": body.country_code.upper()},
        "bank": {"account_name": body.account_name, "account_currency": po.get("currency") or v.get("currency") or "USD", "bank_name": body.bank_name,
                 "bank_country_code": body.bank_country_code.upper(), "account_number": body.account_number, "iban": body.iban, "swift_code": body.swift_code.upper(),
                 "routing_type": body.routing_type, "routing_value": body.routing_value, "bank_account_category": body.bank_account_category}}
    rec["payout_method"] = "SWIFT" if body.iban or (body.swift_code and not body.routing_value) else "LOCAL"
    rep = await beneficiaries_save(BeneficiaryIn(record=rec), kind="vendor", role="finance_manager", by=f"vendor form ({f['vendor']})")
    app.state.org._put("vendor_details", f | {"submitted_at": datetime.now(UTC).isoformat(timespec="seconds")}, f"vendor form ({f['vendor']})")
    chk = next(iter((rep.get("airwallex_check") or {}).values()), {})
    return {"ok": True, "bank_check": chk.get("verdict"), "detail": chk.get("detail"),
            "next": "VENTHORA will call you back on the registered number to confirm the account before the first payment."}


@app.get("/v1/procurement/{pr_number}/file/{cand_id}")
async def procurement_file(pr_number: str, cand_id: str):
    """The quotation document a vendor uploaded."""
    from . import sourcing

    q = app.state.org.get("quote", f"{pr_number}:{cand_id}")
    p = sourcing.stored_file(pr_number, (q or {}).get("file", {}).get("path", "")) if q and q.get("file") else None
    if p is None:
        raise HTTPException(404, "no quotation file")
    return FileResponse(p, filename=q["file"]["name"])


@app.get("/rfq/{token}", include_in_schema=False)
async def rfq_form(token: str):
    """The vendor's quotation form (public, reached through the gateway; the token is the only key)."""
    return FileResponse(DASHBOARD.parent / "rfq.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/rfq/{token}/data", include_in_schema=False)
async def rfq_form_data(token: str):
    from . import sourcing

    found = sourcing.by_token(app.state.org, token)
    if found is None:
        raise HTTPException(404, "this link is not valid")
    r, c, pr = found
    q = app.state.org.get("quote", f"{r['pr_number']}:{c['cand_id']}")
    return {"pr": {k: pr.get(k) for k in ("pr_number", "title", "quantity", "unit", "brand", "specs", "currency", "needed_by")},
            "vendor": {"name": c["name"], "email": c.get("email")}, "deadline": r.get("deadline"), "closed": r["status"] == "AWARDED",
            "quote": {k: v for k, v in (q or {}).items() if k not in ("model_review",)} if q else None}


@app.post("/rfq/{token}", include_in_schema=False)
async def rfq_form_submit(token: str, body: Annotated[dict, Body()]):
    from . import sourcing

    q = await asyncio.to_thread(_proc, sourcing.submit, app.state.org, token, body)
    await asyncio.to_thread(app.state.audit.append, "procurement_quote_received", {"pr": q["pr_number"], "vendor": q["cand_id"], "total_minor": q["total_minor"], "revision": q["revision"]})
    return {"ok": True, "pr_number": q["pr_number"], "total_minor": q["total_minor"], "currency": q["currency"], "revision": q["revision"]}


def _ga_label(g: dict | None) -> str:
    """A receiving account as people name it: nickname, currencies, last four digits."""
    if not g:
        return ""
    return f"{g['nick_name'] or 'account'} · {', '.join(g['currencies'])} · {(g['iban'] or g['account_number'] or '')[-4:]}"


@app.get("/v1/fund-requests")
async def fund_requests_list(kind: str | None = Query(None, pattern="^(cash_advance|petty_cash|odc)$")):
    """Cash advances, petty cash (slush fund) floats and other direct costs, newest first, with the person, the payee,
    the decision, the payment order raised and the settlement."""
    org = app.state.org
    users = {u["user_id"]: u for u in org.list("user")}
    payees = app.state.intake.registry.payees
    rows = [r | {"requested_by_name": (users.get(r["requested_by"]) or {}).get("name", r["requested_by"]),
                 "payee_name": (payees.get(r.get("payee_id") or "") or {}).get("name"),
                 "synced_from": _synced_from(org.source("fund_request", r["request_number"]))}
            for r in org.list("fund_request") if not kind or r["kind"] == kind]
    return sorted(rows, key=lambda r: (r.get("request_date") or "", r["request_number"]), reverse=True)


@app.post("/v1/fund-requests/{number}/decision")
async def fund_request_decide(number: str, body: PRDecisionIn, role: str = "finance_manager", by: str = "dashboard"):
    """Approve or reject a cash advance, petty cash or ODC request: never by the person asking, and within the
    approver's limit."""
    org = app.state.org
    fr = org.get("fund_request", number)
    if fr is None:
        raise HTTPException(404, "no such request")
    if fr["status"] != "SUBMITTED":
        raise HTTPException(409, f"{number} is {fr['status'].lower()}; only a submitted request is decided")
    if role not in PR_DECIDERS:
        raise HTTPException(403, f"role {role} may not decide this request")
    me = by.strip().lower()
    asker = org.get("user", fr["requested_by"]) or {}
    if me in ((asker.get("name") or "").lower(), (asker.get("email") or "").lower(), fr["requested_by"].lower()):
        raise HTTPException(403, "the person asking cannot decide their own request")
    person = next((u for u in org.list("user") if me in (u["name"].lower(), u["email"].lower())), None)
    limit = (person or {}).get("approval_limit_minor")
    if body.decision == "approve" and limit is not None and fr["amount_minor"] > limit:
        raise HTTPException(403, f"{by} may approve up to {limit / 100:,.2f}; this request is {fr['currency']} {fr['amount_minor'] / 100:,.2f}")
    rec = fr | {"status": "APPROVED" if body.decision == "approve" else "REJECTED", "decided_by": f"{by} ({role})",
                "decided_at": datetime.now(UTC).isoformat(timespec="seconds"), "decision_note": body.note}
    await asyncio.to_thread(org._put, "fund_request", rec, by)
    await asyncio.to_thread(app.state.audit.append, "fund_request_decided", {"request": number, "decision": body.decision, "by": by, "role": role})
    return rec


@app.post("/v1/fund-requests/{number}/pay")
async def fund_request_pay(number: str, role: str = "finance_manager", by: str = "dashboard"):
    """Send an approved request to Pending payment: a payment order to its payee (the person's own account for an
    advance or a float), with the same checks as any other, and a person other than the one sending it approves it."""
    org = app.state.org
    fr = org.get("fund_request", number)
    if fr is None:
        raise HTTPException(404, "no such request")
    if fr["status"] != "APPROVED":
        raise HTTPException(409, f"{number} is {fr['status'].lower()}; only an approved request is sent for payment")
    if not fr.get("payee_id"):
        raise HTTPException(409, "no payee: register the person's own bank account (People and roles) or name the supplier")
    from .organisation import FUND_KINDS

    order = PaymentOrderIn(vendor_id=fr["payee_id"], invoice_no=number, amount_minor=fr["amount_minor"], currency=fr["currency"],
                           due_date=fr.get("needed_by") or datetime.now(UTC).strftime("%Y-%m-%d"),
                           description=f"{FUND_KINDS[fr['kind']][1]}: {fr['purpose']}"[:300])
    req = await payment_order(order, role=role, by=by)
    rec = fr | {"status": "IN_PAYMENT", "intake_id": req.intake_id}
    await asyncio.to_thread(org._put, "fund_request", rec, by)
    return rec | {"payment_order": {"intake_id": req.intake_id, "status": req.status}}


class SettleIn(BaseModel):
    settled_minor: int = Field(ge=0, le=10**12, description="the receipts accounted for")
    note: str = Field("", max_length=300)


@app.post("/v1/fund-requests/{number}/settle")
async def fund_request_settle(number: str, body: SettleIn, role: str = "finance_manager", by: str = "dashboard"):
    """Settle a cash advance or a petty cash float against its receipts: what is left is returned by the person, what
    is over is reimbursed to them."""
    _writer(role, by)
    org = app.state.org
    fr = org.get("fund_request", number)
    if fr is None:
        raise HTTPException(404, "no such request")
    if fr["kind"] == "odc":
        raise HTTPException(409, "an other direct cost is paid against its invoice; it is not settled")
    if fr["status"] != "IN_PAYMENT":
        raise HTTPException(409, f"{number} is {fr['status'].lower()}; an advance is settled once it was sent for payment")
    rec = fr | {"status": "SETTLED", "settled_minor": body.settled_minor, "settlement_note": body.note}
    await asyncio.to_thread(org._put, "fund_request", rec, by)
    await asyncio.to_thread(app.state.audit.append, "fund_request_settled", {"request": number, "settled_minor": body.settled_minor, "by": by})
    diff = fr["amount_minor"] - body.settled_minor
    return rec | {"to_return_minor": max(diff, 0), "to_reimburse_minor": max(-diff, 0)}


async def _rates_to_base(currencies) -> dict:
    """Rates into the organisation's main currency for a report: Airwallex's indicative rate when the bank is
    connected (cached with the treasury position), else the demo book's rate; a currency with neither is listed as missing."""
    from decimal import Decimal

    base = app.state.org.standards().get("base_currency") or "USD"
    cache = app.state.__dict__.setdefault("rate_cache", {})
    book = json.loads(_scenario_file("demo_live").read_text(encoding="utf-8"))["state"].get("fx_to_base") or {}
    bank = app.state.agent.proof.airwallex
    rates: dict[str, Decimal] = {}
    sources: dict[str, str] = {}
    for c in sorted({c for c in currencies if c and c != base}):
        if c not in cache and bank.configured:
            with contextlib.suppress(Exception):  # a currency without a rate is listed as missing, not guessed
                cache[c] = await bank.rate_to_base(c, base)
        if c in cache:
            rates[c], sources[c] = Decimal(str(cache[c])), "airwallex"
        elif base == "USD" and c in book:
            rates[c], sources[c] = Decimal(str(book[c])), "demo book"
    missing = sorted({c for c in currencies if c and c != base} - set(rates))
    return {"base": base, "rates": rates, "sources": sources, "missing": missing}


def _in_base(minor: int | None, ccy: str | None, fx: dict) -> int | None:
    from decimal import ROUND_HALF_UP, Decimal

    if minor is None or not ccy:
        return None
    if ccy == fx["base"]:
        return minor
    r = fx["rates"].get(ccy)
    return None if r is None else int((Decimal(minor) * r).to_integral_value(rounding=ROUND_HALF_UP))


@app.get("/v1/reports/spend")
async def reports_spend(category: str = Query("goods_services", pattern="^(payroll|fringe|goods_services|operation|cash_advance|travel|odc)$")):
    """Reporting per spending category: every line with the name, the day it was asked for, its number (PO, PR,
    request, invoice), the amount, what is paid and when, what is left and Done / Partial / New, placed in the
    organisation (country office, branch, unit, person) so it can be filtered."""
    from . import paid_reports

    org = app.state.org
    queue = (await list_intake())
    rows = await asyncio.to_thread(paid_reports.build, category, org=org, store=app.state.executor.store, intake_rows=queue,
                                   payees=app.state.intake.registry.payees)
    units = [{"unit_id": u["unit_id"], "name": u["name"], "kind": u["kind"], "parent_id": u.get("parent_id")} for u in org.list("unit")]
    people = sorted({r["person"] for r in rows if r.get("person")})
    totals: dict[str, dict[str, int]] = {}
    for r in rows:
        t = totals.setdefault(r["currency"] or "-", {"amount_minor": 0, "paid_minor": 0, "remaining_minor": 0})
        for k in t:
            t[k] += r.get(k) or 0
    fx = await _rates_to_base({r["currency"] for r in rows})
    for r in rows:
        r["base_minor"] = _in_base(r.get("amount_minor"), r.get("currency"), fx)
        # what was really spent: the USD Airwallex converted at for paid money; today's rate only where no conversion is on record
        actual = r.get("paid_base_minor")
        r["spent_basis"] = "actual" if actual is not None else "rate" if r.get("paid_minor") else None
        r["spent_base_minor"] = actual if actual is not None else _in_base(r.get("paid_minor") or 0, r.get("currency"), fx)
        r["left_base_minor"] = _in_base(r.get("remaining_minor") or 0, r.get("currency"), fx)
    return {"category": category, "title": paid_reports.CATEGORIES[category], "rows": rows, "units": units, "people": people,
            "types": sorted({r["type"] for r in rows}), "totals": totals, "base_currency": fx["base"],
            "rates": {c: str(v) for c, v in fx["rates"].items()}, "rate_sources": fx["sources"], "missing_rates": fx["missing"]}


class PlacementPolicyIn(BaseModel):
    buffer_pct: int = Field(10, ge=0, le=100)
    min_amount_minor: int = Field(100_000, ge=0)
    destination: str = Field("Term deposit, 1 month", max_length=120)
    enabled: bool = True
    horizon_months: int = Field(3, ge=1, le=24)
    use_horizon_surplus: bool = False
    surplus_min_minor: int = Field(0, ge=0)
    use_lower_bound: bool = False
    lower_bound_minor: int = Field(0, ge=0)
    use_upper_bound: bool = False
    upper_bound_minor: int = Field(0, ge=0)


class PlacementIn(BaseModel):
    amount_minor: int = Field(gt=0, description="in the provider's currency (the base currency without a provider)")
    destination: str = Field("", max_length=120)
    note: str = Field("", max_length=300)
    provider_id: str | None = Field(None, max_length=80, description="a provider from FCO Authorize → Placement providers")


class PlacementDecisionIn(BaseModel):
    status: str = Field(pattern="^(APPROVED|DONE|CANCELLED)$")


async def _placement_basis() -> dict:
    from . import cash_placement

    org = app.state.org
    base = _live_base()
    recap = await reports_spending_recap()
    commitments = sum(line.get("left_base_minor") or 0 for line in recap["lines"])
    out = {"base_currency": recap["base_currency"], "wallet": [], "error": None}
    try:
        pos = await treasury_position()
        available, bills = pos["available"]["base_minor"], pos["bills_unpaid"]["base_minor"]
        out["wallet"] = [{"currency": w["currency"], "available_minor": w["available"], "base_minor": w.get("available_base_minor")} for w in pos["available"].get("wallet") or []]
        out["processing_minor"] = pos["processing_out"]["base_minor"]
    except HTTPException as exc:
        available, bills, out["error"] = 0, 0, f"the Airwallex balance could not be read: {exc.detail}"
    pol = cash_placement.policy(org)
    ccys = {a.get("currency") or out["base_currency"] for a in org.list("allocation")} | {u.get("salary_currency") for u in org.list("user") if u.get("salary_currency")}
    fx = await _rates_to_base(ccys)
    hz = await asyncio.to_thread(cash_placement.horizon, org, months=pol["horizon_months"], base=out["base_currency"], commitments_minor=commitments,
                                 to_base=lambda m, c: _in_base(m, c, fx) or 0)
    return out | cash_placement.compute(available_minor=available, reserve_floor_minor=base.policy.reserve_floor_minor, bills_minor=bills,
                                        commitments_minor=commitments, pol=pol, horizon=hz, placed_minor=cash_placement.placed(org))


@app.get("/v1/cash-placement")
async def cash_placement_view():
    """Budget allocation -> Cash placement: what must stay (reserve floor, approved bills, open commitments, a
    buffer), the idle cash above it, the policy, and every placement instruction a person proposed or approved."""
    from . import cash_placement

    return {"basis": await _placement_basis(), "policy": cash_placement.policy(app.state.org), "instructions": cash_placement.items(app.state.org)}


@app.put("/v1/cash-placement/policy")
async def cash_placement_policy(body: PlacementPolicyIn, role: str = "finance_manager", by: str = "dashboard"):
    from . import cash_placement

    who = _writer(role, by)
    pol = await asyncio.to_thread(cash_placement.save_policy, app.state.org, body.model_dump(), who)
    await asyncio.to_thread(app.state.audit.append, "cash_placement_policy", pol | {"by": who})
    return pol


@app.post("/v1/cash-placement/instructions")
async def cash_placement_instruct(body: PlacementIn, role: str = "finance_manager", by: str = "dashboard"):
    """Propose placing idle cash; never above the idle amount. Nothing moves until a second person approves: with a
    provider (the FCO) it goes by Airwallex transfer; without one it is carried out by hand and marked done."""

    who = _writer(role, by)
    return await _propose_placement(body.amount_minor, body.provider_id, body.destination, body.note, who)


async def _propose_placement(amount_minor: int, provider_id: str | None, destination: str, note: str, who: str) -> dict:
    from . import cash_placement, placement_providers

    basis = await _placement_basis()
    pol = cash_placement.policy(app.state.org)
    base = basis["base_currency"]
    provider, ccy, in_base = None, base, None
    if provider_id:
        try:
            provider = placement_providers.get(app.state.org, provider_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        ccy = provider["currency"]
        if ccy != base:  # the idle cash is in the base currency: the amount is valued at today's indicative rate
            try:
                rate = await _bank().rate_to_base(ccy, base)
            except AirwallexError as exc:
                raise HTTPException(502, f"the {ccy} rate could not be read: {exc}") from exc
            in_base = int(amount_minor * rate)
        held = next((w["available_minor"] for w in basis["wallet"] if w["currency"] == ccy), 0)
        if held < amount_minor:
            raise HTTPException(422, f"the wallet holds {ccy} {held / 100:,.2f}: convert first, or place in a currency held")
    try:
        rec = await asyncio.to_thread(cash_placement.instruct, app.state.org, amount_minor=amount_minor, currency=ccy,
                                      destination=destination or ("" if provider else pol["destination"]), note=note, by=who, provider=provider, base_minor=in_base,
                                      basis={k: basis[k] for k in ("available_minor", "needed_minor", "idle_minor", "reserve_floor_minor", "bills_minor", "commitments_minor", "buffer_minor")})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "cash_placement_proposed", {"id": rec["_id"], "amount_minor": rec["amount_minor"], "currency": ccy,
                                                                                "provider": provider_id, "destination": rec["destination"], "by": who})
    return rec


@app.post("/v1/cash-placement/propose")
async def cash_placement_agent_propose(role: str = "finance_manager", by: str = "dashboard"):
    """VENTHORA proposes: the idle cash above the minimum, to the best provider on the FCO's list (active, account
    checked, currency held, minimum met, the highest indicative rate). The FCO approves it."""
    from . import placement_providers

    _writer(role, by)
    basis = await _placement_basis()
    if basis["place_minor"] <= 0:
        raise HTTPException(409, f"nothing to propose: {basis['why']}")
    base = basis["base_currency"]
    held = next((w["available_minor"] for w in basis["wallet"] if w["currency"] == base), 0)
    amount = min(basis["place_minor"], held)  # the idle cash is counted in every currency; only what the base wallet holds is sent
    p = await asyncio.to_thread(placement_providers.best, app.state.org, lambda c: c == base, lambda c: amount)
    if p is None:
        raise HTTPException(409, f"no active {base} provider with a checked account takes {base} {amount / 100:,.2f}: add one in FCO Authorize → Placement providers")
    note = (f"proposed by VENTHORA: idle {base} {basis['idle_minor'] / 100:,.2f} above what is needed"
            + (f" (capped at the {base} {held / 100:,.2f} the {base} wallet holds)" if amount < basis["place_minor"] else "")
            + f"; {p['name']} at {p['indicative_rate_pct']}% indicative")
    return await _propose_placement(amount, p["provider_id"], "", note, "VENTHORA agent")


@app.post("/v1/cash-placement/instructions/{pid}")
async def cash_placement_decide(pid: str, body: PlacementDecisionIn, role: str = "finance_manager", by: str = "dashboard"):
    from . import cash_placement, placement_providers

    who = _writer(role, by)
    try:
        rec = await asyncio.to_thread(cash_placement.decide, app.state.org, pid, body.status, who, role)
    except KeyError as exc:
        raise HTTPException(404, "no such placement") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "cash_placement_" + body.status.lower(), {"id": pid, "by": who})
    if body.status == "APPROVED" and rec.get("provider_id") and rec.get("provider_kind") != "AIRWALLEX_YIELD":
        # the FCO's approval is the instruction: the money goes to the provider's checked account at once
        rec = await placement_providers.send(app.state.org, _bank(), rec, who)
        await asyncio.to_thread(app.state.audit.append, "cash_placement_sent", {"id": pid, "status": rec["status"], "transfer_id": rec.get("transfer_id"),
                                                                                 "amount_minor": rec["amount_minor"], "currency": rec["currency"], "provider": rec["provider_id"]})
    return rec


def _placement(pid: str) -> dict:
    rec = app.state.org.get("placement", pid)
    if rec is None:
        raise HTTPException(404, "no such placement")
    return rec


@app.post("/v1/cash-placement/instructions/{pid}/refresh")
async def cash_placement_refresh(pid: str, by: str = "dashboard"):
    """Read the placement's transfer back from Airwallex: PLACED once it is PAID."""
    from . import placement_providers

    try:
        return await placement_providers.refresh(app.state.org, _bank(), _placement(pid), by)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex: {exc}") from exc


class WithdrawIn(BaseModel):
    expected_minor: int | None = Field(None, gt=0, description="what the provider will pay back; default principal + the indicative return")


@app.post("/v1/cash-placement/instructions/{pid}/withdraw")
async def cash_placement_withdraw(pid: str, body: WithdrawIn | None = None, role: str = "super_admin", by: str = "dashboard"):
    """The FCO asks for the money back. Airwallex cannot pull it from another bank: VENTHORA writes the redemption
    instruction for the provider (its portal, email or API), with the reference the deposit must carry back."""
    from . import placement_providers

    who = _writer(role, by)
    if role != "super_admin":
        raise HTTPException(403, "a withdrawal is asked for by the FCO (Super admin)")
    try:
        rec = await asyncio.to_thread(placement_providers.request_withdrawal, app.state.org, _placement(pid), body.expected_minor if body else None, who)
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "cash_placement_withdrawal", {"id": pid, "expected_minor": rec["withdrawal"]["expected_minor"], "by": who})
    return rec


@app.get("/v1/cash-placement/instructions/{pid}/matches")
async def cash_placement_matches(pid: str):
    """The deposits on Airwallex that may be this placement coming back (its reference first, then the amount)."""
    from . import placement_providers

    rec = _placement(pid)
    try:
        deps = await _bank().list_deposits()
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex: {exc}") from exc
    return {"reference": rec.get("reference"), "expected_minor": (rec.get("withdrawal") or {}).get("expected_minor"),
            "candidates": placement_providers.candidates(app.state.org, rec, deps)}


class CloseIn(BaseModel):
    deposit_id: str | None = Field(None, max_length=80, description="the Airwallex deposit that brought it back (its amount is read from Airwallex)")
    received_minor: int | None = Field(None, gt=0, description="Airwallex Yield only: what came back to the wallet")


@app.post("/v1/cash-placement/instructions/{pid}/close")
async def cash_placement_close(pid: str, body: CloseIn, role: str = "super_admin", by: str = "dashboard"):
    """Close a placement on the deposit that brought it back; the amount above the principal is the return."""
    from . import placement_providers

    who = _writer(role, by)
    rec = _placement(pid)
    if body.deposit_id:
        try:
            d = await _bank().get_deposit(body.deposit_id)
        except AirwallexError as exc:
            raise HTTPException(502, f"Airwallex: {exc}") from exc
        if str(d.get("currency", "")).upper() != rec["currency"]:
            raise HTTPException(409, f"the deposit is in {d.get('currency')}, the placement in {rec['currency']}")
        received = placement_providers._minor(d.get("amount"))
    elif rec.get("provider_kind") == "AIRWALLEX_YIELD" and body.received_minor:
        received = body.received_minor
    else:
        raise HTTPException(422, "name the Airwallex deposit that brought the money back")
    try:
        rec = await asyncio.to_thread(placement_providers.close, app.state.org, rec, received_minor=received, deposit_id=body.deposit_id, by=who)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "cash_placement_closed", {"id": pid, "deposit_id": body.deposit_id, **rec["closing"], "by": who})
    return rec


@app.post("/v1/cash-placement/instructions/{pid}/agreement")
async def cash_placement_agreement(pid: str, role: str = "finance_manager", by: str = "dashboard"):
    """Draft the placement agreement from what VENTHORA holds (company, provider, account, amount, term, rate, where
    the money comes back). One click; nothing is sent anywhere."""
    from . import placement_providers

    who = _writer(role, by)
    rec = _placement(pid)
    if not rec.get("provider_id"):
        raise HTTPException(409, "an agreement needs a placement with a provider")
    rec = await asyncio.to_thread(placement_providers.agreement, app.state.org, rec, who)
    await asyncio.to_thread(app.state.audit.append, "cash_placement_agreement_drafted", {"id": pid, "sha256": rec["agreement"]["sha256"], "by": who})
    return rec


@app.post("/v1/cash-placement/instructions/{pid}/agreement/accept")
async def cash_placement_agreement_accept(pid: str, role: str = "super_admin", by: str = "dashboard"):
    """The FCO accepts the agreement as written; its SHA-256 goes into the audit chain (and Alta, with placements)."""
    from . import placement_providers

    who = _writer(role, by)
    try:
        rec = await asyncio.to_thread(placement_providers.accept_agreement, app.state.org, _placement(pid), role, who)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "cash_placement_agreement_accepted", {"id": pid, "sha256": rec["agreement"]["sha256"], "by": who})
    return rec


# ---- Developer Console → Placement connections: how VENTHORA reaches each provider (manual, email or its API) ----
class PlacementConnectionIn(BaseModel):
    mode: str = Field(pattern="^(MANUAL|EMAIL|API)$")
    api_base_url: str = Field("", max_length=300)
    api_key: str | None = Field(None, max_length=500, description="write-only")
    email: str = Field("", max_length=200)


@app.get("/v1/placement-connections")
async def placement_connections():
    """Each provider's connection (never its key): the Developer's side of Placement providers."""
    from . import secret_values

    out = []
    for p in app.state.org.list("placement_provider"):
        c = p.get("connection") or {"mode": "MANUAL"}
        out.append({"provider_id": p["provider_id"], "name": p["name"], "kind": p["kind"], "connection": c | {
            "api_key": secret_values.info(f"placement_api:{p['provider_id']}")}})
    return sorted(out, key=lambda r: r["name"])


@app.put("/v1/placement-connections/{pid}")
async def placement_connection_save(pid: str, body: PlacementConnectionIn, role: str = "developer", by: str = "dashboard"):
    from . import placement_providers, secret_values

    who = _setup_writer(role, by)
    try:
        p = placement_providers.get(app.state.org, pid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if body.mode == "API" and not body.api_base_url.startswith("https://"):
        raise HTTPException(422, "an API connection needs the provider's https base URL")
    if body.api_key:
        await asyncio.to_thread(secret_values.put, f"placement_api:{pid}", body.api_key.strip(), who)
    p["connection"] = {"mode": body.mode, "api_base_url": body.api_base_url.strip(), "email": body.email.strip(), "updated_by": who}
    await asyncio.to_thread(app.state.org._put, "placement_provider", p, who)
    await asyncio.to_thread(app.state.audit.append, "placement_connection_saved", {"provider": pid, "mode": body.mode, "key_changed": bool(body.api_key), "by": who})
    return {"provider_id": pid, "connection": p["connection"]}


@app.post("/v1/placement-connections/{pid}/test")
async def placement_connection_test(pid: str):
    """Does the provider's API answer (a GET on its base URL with the key as a Bearer token)? Nothing is placed."""
    import httpx

    from . import placement_providers, secret_values

    p = placement_providers.get(app.state.org, pid)
    c = p.get("connection") or {}
    if c.get("mode") != "API":
        return {"ok": None, "detail": f"{c.get('mode') or 'MANUAL'}: nothing to test"}
    key = secret_values.get(f"placement_api:{pid}")
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r = await cl.get(c["api_base_url"], headers={"Authorization": f"Bearer {key}"} if key else {})
        return {"ok": r.status_code < 400, "status": r.status_code}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"no answer: {exc.__class__.__name__}"}


@app.post("/v1/cash-placement/instructions/{pid}/simulate")
async def cash_placement_simulate(pid: str, role: str = "super_admin", by: str = "dashboard"):
    """Sandbox only: the next outside step. A SENT transfer is made PAID; a requested withdrawal arrives as a deposit
    on the USD Global Account carrying the placement's reference."""
    from .cli import usd_global_account

    _writer(role, by)
    rec = _placement(pid)
    try:
        if rec["status"] == "SENT":
            t = await _bank().get_transfer(rec["transfer_id"])
            for nxt in ("SENT", "PAID"):
                if t.status not in ("PAID", nxt):
                    t = await _bank().simulate_transfer_transition(rec["transfer_id"], nxt)
            t = await _bank().get_transfer(rec["transfer_id"])  # the transition's answer can lag one step: read it back
            return {"simulated": "transfer", "status": t.status}
        if rec["status"] == "WITHDRAWAL_REQUESTED":
            if rec["currency"] != "USD":
                raise HTTPException(409, "the sandbox deposit simulation is for the USD Global Account")
            from . import placement_providers

            p = placement_providers.get(app.state.org, rec["provider_id"])
            res = await _bank().simulate_deposit(global_account_id=await usd_global_account(_bank(), fake=False),
                                                 amount_minor=rec["withdrawal"]["expected_minor"], payer_name=f"{p['name'][:80]} {rec['reference']}")
            return {"simulated": "deposit", "deposit": res}
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex: {exc}") from exc
    raise HTTPException(409, f"nothing to simulate for a {rec['status'].lower()} placement")


# ---- FCO Authorize → Placement providers ----
class ProviderIn(BaseModel):
    provider_id: str | None = Field(None, max_length=80)
    name: str = Field(max_length=120)
    kind: str = Field(pattern="^(TERM_DEPOSIT|MONEY_MARKET_FUND|AIRWALLEX_YIELD)$")
    currency: str = Field(max_length=3)
    tenor_days: int = Field(0, ge=0, le=1830)
    indicative_rate_pct: float = Field(0, ge=0, le=50)
    minimum_minor: int = Field(0, ge=0)
    redeem_by: str | None = Field(None, pattern="^(PORTAL|EMAIL|API|AIRWALLEX_APP)$")
    redeem_contact: str = Field("", max_length=200)
    our_reference: str = Field("", max_length=80)
    account_holder: str | None = Field(None, pattern="^(COMPANY_OWN|FUND_CUSTODIAN)$")
    payout_method: str = Field("LOCAL", pattern="^(LOCAL|SWIFT)$")
    bank: dict | None = None
    address: dict | None = None
    note: str = Field("", max_length=300)
    active: bool = True


@app.get("/v1/placement-providers")
async def placement_providers_list():
    from . import placement_providers

    org = app.state.org
    return [placement_providers.public(org, p) for p in await asyncio.to_thread(placement_providers.listing, org)]


@app.put("/v1/placement-providers")
async def placement_providers_save(body: ProviderIn, role: str = "super_admin", by: str = "dashboard"):
    """Add or change a provider (the FCO). A provider reached by transfer has its account checked first; a refused
    account is not kept, and changed bank details need a new check and a new Airwallex beneficiary."""
    from . import placement_providers

    who = _writer(role, by)
    try:
        bank = _bank()
    except HTTPException:
        bank = None
    try:
        rec = await placement_providers.save(app.state.org, bank, _xendit_or_none(), body.model_dump(), who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "placement_provider_saved", {"provider": rec["provider_id"], "kind": rec["kind"], "currency": rec["currency"],
                                                                                  "bank_check": (rec.get("bank_check") or {}).get("verdict"), "by": who})
    return placement_providers.public(app.state.org, rec)


def _anchor_heads() -> dict:
    """What Alta anchors: the categories the FCO chose (Platform Setup → Blockchain Record)."""
    from . import controls

    org = app.state.org
    audit = app.state.audit.verify()
    from . import blockchain_record

    base = {"audit": {"head": audit.get("head"), "events": audit.get("checked"), "verified": audit.get("verified")},
            "contracts": {r["_id"]: r.get("head") for r in sorted(org.list("contract_seal"), key=lambda r: r["_id"])}}
    heads = controls.metal_heads(org, app.state.audit, base)
    docs = blockchain_record.document_heads(org, app.state.executor.store.list_payees())
    if docs:  # the documents the Finance Director chose (Platform Setup → Blockchain Record), each provable on its own
        heads["documents"] = {k: {"records": v["records"], "min_minor": v["min_minor"], "digest": v["digest"]} for k, v in docs.items()}
    return heads


@app.get("/v1/blockchain-record")
async def blockchain_record_get():
    """Platform Setup → Blockchain Record: why, what it costs, Alta's products, and what goes on the record."""
    from . import blockchain_record

    return await asyncio.to_thread(blockchain_record.view, app.state.org, app.state.executor.store.list_payees())


@app.put("/v1/blockchain-record")
async def blockchain_record_put(body: Annotated[dict, Body()], role: str = "super_admin", by: str = "dashboard"):
    from . import blockchain_record

    who = _setup_writer(role, by)
    try:
        out = await asyncio.to_thread(blockchain_record.save, app.state.org, body, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "blockchain_record_saved", {"by": who} | out)
    return await asyncio.to_thread(blockchain_record.view, app.state.org, app.state.executor.store.list_payees())


# ---- Platform Setup → Optional: the FCO's switches for Visa, Airi and Alta (controls.py) ----
@app.middleware("http")
async def visa_switches(request: Request, call_next):
    """A Visa feature the FCO switched off is refused here, whoever calls it."""
    from . import controls

    if request.url.path.startswith("/v1/visa/"):
        why = await asyncio.to_thread(controls.visa_refusal, request.app.state.org, request.url.path)
        if why:
            return JSONResponse({"detail": why}, status_code=409)
    return await call_next(request)


@app.get("/v1/controls/{group}")
async def controls_get(group: str = PathParam(pattern="^(visa|airi|metal)$")):
    """The FCO's current choices for Visa, Airi or Alta, with their defaults and labels."""
    from . import controls

    return await asyncio.to_thread(controls.view, app.state.org, group)


@app.put("/v1/controls/{group}")
async def controls_put(body: Annotated[dict, Body()], group: str = PathParam(pattern="^(visa|airi|metal)$"), role: str = "super_admin", by: str = "dashboard"):
    """Change the FCO's choices (the FCO only; a Developer reads them)."""
    from . import controls

    who = _writer(role, by)
    try:
        vals = await asyncio.to_thread(controls.save, app.state.org, group, body, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "controls_saved", {"group": group, "values": vals, "by": who})
    return await asyncio.to_thread(controls.view, app.state.org, group)


# ---- Transfers: money moved by a person, one route per form (manual_transfers.py) ----
def _source_account() -> dict | None:
    """The Airwallex account the money leaves: the company account this VENTHORA works for, else the platform's own."""
    from .airwallex_settings import AirwallexStore

    return AirwallexStore().acting()


@app.get("/v1/transfers/routes")
async def transfers_routes():
    """What each form needs and whether it can be used now: the companies on this platform, the card account, the
    cards, the saved destinations."""
    from . import manual_transfers
    from .airwallex_settings import card_account_for

    src = _source_account()
    try:
        accts = [_account_row(a) for a in await _bank().list_accounts()]
    except (AirwallexError, HTTPException):
        accts = []
    me = (src or {}).get("account_id")
    companies = [{"id": a["id"], "name": a["name"], "country": a["country"]} for a in accts if a["status"] == "ACTIVE" and a["id"] != me]
    if me:
        companies.insert(0, {"id": "platform", "name": "The platform's own account (company 1)", "country": None})
    cards = [{"id": c["_id"], "name": c.get("payee"), "masked": c.get("masked"), "currency": c.get("currency")}
             for c in app.state.org.list("card") if not c.get("frozen")]
    dests = {r: [manual_transfers.public_dest(d) for d in manual_transfers.destinations(app.state.org, r)] for r in manual_transfers.BANK_ROUTES}
    card_acct = card_account_for()
    return {"routes": manual_transfers.ROUTES, "from": src or {"account_id": None, "name": "the platform's own account"},
            "companies": companies, "cards": cards, "card_account": card_acct, "destinations": dests,
            "unavailable": {"airi_card": "Airwallex offers no way to fund the Airi card: Airi spends from the card linked to it",
                            **({} if card_acct else {"card_account": "no card-issuing account chosen (Platform Setup → Airwallex Cards)",
                                                     "virtual_card": "no card-issuing account chosen (Platform Setup → Airwallex Cards)"})}}


class DestinationIn(BaseModel):
    route: str = Field(pattern="^(id_bank|xendit|midtrans|international)$")
    label: str = Field("", max_length=120)
    bank: dict
    address: dict | None = None
    entity_type: str = Field("COMPANY", pattern="^(COMPANY|PERSONAL)$")
    payout_method: str = Field("LOCAL", pattern="^(LOCAL|SWIFT)$")


@app.post("/v1/transfers/destinations")
async def transfers_destination_save(body: DestinationIn, role: str = "finance_manager", by: str = "dashboard"):
    """Keep a bank destination after its bank check (a Xendit or Midtrans top-up account, a bank account)."""
    from . import manual_transfers

    who = _writer(role, by)
    try:
        bank = _bank()
    except HTTPException:
        bank = None
    try:
        d = await manual_transfers.save_destination(app.state.org, bank, _xendit_or_none(), body.route, body.model_dump(), who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "transfer_destination_saved", {"id": d["_id"], "route": body.route, "verdict": d["bank_check"]["verdict"], "by": who})
    return manual_transfers.public_dest(d)


class TransferIn(BaseModel):
    route: str = Field(pattern="^(airwallex|card_account|virtual_card|airi_card|id_bank|xendit|midtrans|international)$")
    amount_minor: int = Field(gt=0, le=10**12)
    currency: str = Field(min_length=3, max_length=3)
    purpose: str = Field("", max_length=200)
    target_account_id: str | None = Field(None, max_length=80, description="airwallex: a company on this platform ('platform' = its own account)")
    account_number: str | None = Field(None, max_length=60, description="airwallex: another Airwallex customer's account number")
    account_name: str | None = Field(None, max_length=140)
    card_id: str | None = Field(None, max_length=80)
    destination_id: str | None = Field(None, max_length=40)


@app.get("/v1/transfers")
async def transfers_list():
    from . import manual_transfers

    return await asyncio.to_thread(manual_transfers.listing, app.state.org)


@app.post("/v1/transfers")
async def transfers_create(body: TransferIn, role: str = "finance_manager", by: str = "dashboard"):
    """Ask for a transfer. Nothing moves until the FCO approves it."""
    from . import manual_transfers

    who = _writer(role, by)
    org, ccy = app.state.org, body.currency.upper()
    if body.route == "airwallex":
        if body.target_account_id:
            name = next((c["name"] for c in (await transfers_routes())["companies"] if c["id"] == body.target_account_id), None)
            if name is None:
                raise HTTPException(422, "that company is not an ACTIVE account on this platform")
            dest = {"kind": "internal", "account_id": body.target_account_id, "name": name}
        elif body.account_number and body.account_name:
            dest = {"kind": "airwallex_pay", "account_number": body.account_number.strip(), "name": body.account_name.strip()}
        else:
            raise HTTPException(422, "choose a company on this platform, or give the other company's Airwallex account number and name")
    elif body.route in ("card_account", "virtual_card"):
        from .airwallex_settings import card_account_for

        acct = card_account_for()
        if not acct:
            raise HTTPException(409, "no card-issuing account chosen (Platform Setup → Airwallex Cards)")
        dest = {"kind": "internal", "account_id": acct, "name": "card-issuing account"}
        if body.route == "virtual_card":
            card = org.get("card", body.card_id or "")
            if not card:
                raise HTTPException(422, "choose a card VENTHORA issued")
            if card.get("currency") and card["currency"] != ccy:
                raise HTTPException(422, f"the card spends in {card['currency']}: top it up in {card['currency']}")
            dest |= {"card_id": card["_id"], "card": f"{card.get('payee')} {card.get('masked') or ''}".strip()}
    elif body.route in manual_transfers.BANK_ROUTES:
        d = org.get(manual_transfers.DEST, body.destination_id or "")
        if not d or d["route"] != body.route:
            raise HTTPException(422, "choose a saved destination for this form (or add one)")
        if body.route != "international" and ccy != "IDR":
            raise HTTPException(422, "transfers to Indonesia (bank, Xendit, Midtrans) are in IDR")
        dest = {"kind": "bank", "destination_id": d["_id"], "name": d["label"], "account_name": d["bank"]["account_name"],
                "country": d["bank"].get("bank_country_code"), "account_masked": "•••• " + str(d["bank"].get("account_number") or d["bank"].get("iban"))[-4:]}
    else:
        dest = {}
    try:
        rec = await asyncio.to_thread(manual_transfers.create, org, route=body.route, amount_minor=body.amount_minor, currency=ccy, by=who,
                                      purpose=body.purpose, destination=dest)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    rec["from"] = _source_account() or {"account_id": None, "name": "the platform's own account"}
    await asyncio.to_thread(org._put, manual_transfers.KIND, rec, who)
    await asyncio.to_thread(app.state.audit.append, "manual_transfer_requested", {"id": rec["_id"], "route": rec["route"], "amount_minor": rec["amount_minor"],
                                                                                "currency": ccy, "by": who})
    return rec


async def _internal_move(src: str | None, dst: str | None, amount_minor: int, ccy: str, ref: str, rid: str) -> dict:
    """Inside Airwallex, no fee: from a company account to the platform (Charges), then the platform to the other
    account (Connected Account Transfers). None = the platform's own account."""
    bank, out = _bank(), {}
    if src == dst:
        raise ValueError("the money would go to the account it leaves")
    if src:
        ch = await bank.charge_connected_account(account_id=src, amount_minor=amount_minor, currency=ccy, reference=ref, request_id=f"{rid}-out", reason="business_expenses")
        out["charge_id"], out["charge_status"] = ch.get("id"), ch.get("status")
    if dst:
        t = await bank.fund_connected_account(account_id=dst, amount_minor=amount_minor, currency=ccy, reference=ref, request_id=f"{rid}-in", reason="business_expenses")
        out["cat_id"], out["cat_status"] = t.get("id"), t.get("status")
    return out


@app.post("/v1/transfers/{tid}/approve")
async def transfers_approve(tid: str, role: str = "super_admin", by: str = "dashboard"):
    """The FCO approves (never the person who asked) and the transfer is sent at once."""
    from . import bankcheck, manual_transfers
    from .fx_routes import pays_through

    who = _writer(role, by)
    org = app.state.org
    try:
        rec = manual_transfers.get(org, tid)
        manual_transfers.approve_check(rec, role, by)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    rec = await asyncio.to_thread(manual_transfers.mark, org, rec, "APPROVED", who, "approved by the FCO")
    d, src = rec["destination"], (rec.get("from") or {}).get("account_id")
    ref = f"VENTHORA {rec['_id']} {rec['purpose']}".strip()[:120]
    try:
        if d.get("kind") == "internal":
            dst = None if d["account_id"] == "platform" else d["account_id"]
            moved = await _internal_move(src, dst, rec["amount_minor"], rec["currency"], ref, rec["request_id"])
            extra = {"internal": moved}
            if rec["route"] == "virtual_card":
                card = await _bank().get_card(d["card_id"])
                ctl = card.get("authorization_controls") or {}
                lims = (ctl.get("transaction_limits") or {}).get("limits") or []
                add = rec["amount_minor"] / 100
                raised = [lim | {"amount": float(lim["amount"]) + add} if lim.get("interval") in ("ALL_TIME", "MONTHLY") else lim for lim in lims]
                if not any(lim.get("interval") in ("ALL_TIME", "MONTHLY") for lim in lims):
                    raised.append({"amount": add, "interval": "ALL_TIME"})
                await _bank().update_card_controls(d["card_id"], {"allowed_transaction_count": ctl.get("allowed_transaction_count", "MULTIPLE"),
                                                                  "transaction_limits": {"currency": (ctl.get("transaction_limits") or {}).get("currency") or rec["currency"], "limits": raised}})
                extra["card_limits"] = raised
            rec = await asyncio.to_thread(manual_transfers.mark, org, rec, "SENT", who, "moved inside Airwallex (no fee)", **extra)
        elif d.get("kind") in ("bank", "airwallex_pay"):
            if d["kind"] == "bank":
                dest = org.get(manual_transfers.DEST, d["destination_id"])
                pay = manual_transfers.bank_payload(dest)
                ok, why = bankcheck.passed(org, f"dest:{dest['_id']}", pay)
                if not ok:
                    raise ValueError(f"not sent: {why}")
                ben = dest.get("beneficiary_id")
                method = dest.get("payout_method") or "LOCAL"
            else:
                dest, pay, ben, method = None, manual_transfers.wallet_payload(d["account_number"], d["name"]), None, "LOCAL"
            if not ben:
                await _bank().validate_beneficiary(pay["payload"])
                ben = await _bank().create_beneficiary(pay["payload"])
                if dest is not None:
                    await asyncio.to_thread(org._put, manual_transfers.DEST, dest | {"beneficiary_id": ben}, who)
            base = app.state.org.standards().get("base_currency", "USD")
            t = await _bank().create_transfer(request_id=rec["request_id"], beneficiary_id=ben, currency=rec["currency"], amount_minor=rec["amount_minor"],
                                              transfer_method=method, reference=ref, reason="business_expenses",
                                              source_currency=base if pays_through(rec["currency"], base) else None)
            rec = await asyncio.to_thread(manual_transfers.mark, org, rec, "SENT", who, f"Airwallex transfer {t.transfer_id} ({t.status})",
                                          transfer_id=t.transfer_id, transfer_status=t.status, fee_minor=t.fee_minor, source_currency=t.source_currency)
    except (AirwallexError, ValueError) as exc:
        rec = await asyncio.to_thread(manual_transfers.mark, org, rec, "FAILED", who, f"refused: {str(exc)[:300]}")
    await asyncio.to_thread(app.state.audit.append, "manual_transfer_approved", {"id": tid, "status": rec["status"], "by": who})
    return rec


@app.post("/v1/transfers/{tid}/refresh")
async def transfers_refresh(tid: str, by: str = "dashboard"):
    """Read the transfer back from Airwallex: PAID only when Airwallex says so."""
    from . import manual_transfers

    org = app.state.org
    try:
        rec = manual_transfers.get(org, tid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if rec["status"] != "SENT":
        return rec
    try:
        if rec.get("transfer_id"):
            t = await _bank().get_transfer(rec["transfer_id"])
            if t.status == "PAID":
                rec = manual_transfers.mark(org, rec, "PAID", by, "Airwallex: PAID", transfer_status=t.status)
            elif t.status in ("FAILED", "CANCELLED"):
                rec = manual_transfers.mark(org, rec, "FAILED", by, f"Airwallex: {t.status} ({t.failure_reason or '-'})", transfer_status=t.status)
        elif rec.get("internal"):
            mv = rec["internal"]
            states = []
            if mv.get("cat_id"):
                states.append((await _bank()._request("GET", f"/api/v1/connected_account_transfers/{mv['cat_id']}", own=True)).get("status"))
            if mv.get("charge_id"):
                states.append((await _bank()._request("GET", f"/api/v1/charges/{mv['charge_id']}", own=True)).get("status"))
            if states and all(s in ("SETTLED", "SUCCEEDED", "COMPLETED") for s in states):
                rec = manual_transfers.mark(org, rec, "PAID", by, f"Airwallex: {', '.join(states)}")
            elif any(s in ("FAILED", "CANCELLED", "REJECTED") for s in states):
                rec = manual_transfers.mark(org, rec, "FAILED", by, f"Airwallex: {', '.join(states)}")
            else:
                rec["internal_status"] = states
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex: {exc}") from exc
    return rec


@app.post("/v1/transfers/{tid}/simulate")
async def transfers_simulate(tid: str, role: str = "super_admin", by: str = "dashboard"):
    """Sandbox only: a SENT bank or Airwallex transfer made PAID (an internal move settles by itself)."""
    from . import manual_transfers

    _writer(role, by)
    rec = manual_transfers.get(app.state.org, tid)
    if rec["status"] != "SENT" or not rec.get("transfer_id"):
        raise HTTPException(409, "only a SENT bank or Airwallex transfer is simulated")
    try:
        t = await _bank().get_transfer(rec["transfer_id"])
        for nxt in ("SENT", "PAID"):
            if t.status not in ("PAID", nxt):
                t = await _bank().simulate_transfer_transition(rec["transfer_id"], nxt)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex: {exc}") from exc
    return await transfers_refresh(tid, by)


@app.post("/v1/transfers/{tid}/cancel")
async def transfers_cancel(tid: str, role: str = "finance_manager", by: str = "dashboard"):
    from . import manual_transfers

    who = _writer(role, by)
    rec = manual_transfers.get(app.state.org, tid)
    if rec["status"] != "AWAITING_APPROVAL":
        raise HTTPException(409, "only a transfer still waiting for approval can be cancelled")
    return await asyncio.to_thread(manual_transfers.mark, app.state.org, rec, "CANCELLED", who, "cancelled before approval")


@app.get("/v1/t0/status")
async def t0_status():
    """Developer Console → T:0: whether the key is set (never its value), whether T:0's MCP server answers, its tools
    and the entities the key can act for. Read only."""
    from . import t0

    return await t0.status()


@app.post("/v1/airi/check")
async def airi_check(body: Annotated[dict, Body()]):
    """Whether VENTHORA approves an Airi checkout under the FCO's Airi rules (Platform Setup → Airi usage): an approved
    PR, the price inside it, the vendor's invoice, a verified vendor that is the PR's supplier, the limits. Nothing is bought here."""
    from . import controls

    out = await asyncio.to_thread(controls.airi_check, app.state.org, body, _payee_status)
    await asyncio.to_thread(app.state.audit.append, "airi_checkout_checked", {"pr": body.get("pr_number"), "amount_minor": body.get("amount_minor"),
                                                                              "vendor": body.get("vendor"), "decision": out["decision"]})
    return out


@app.get("/v1/airi/status")
async def airi_status():
    """Developer Console → Airi: whether the Airi CLI is installed on this server, signed in, and how many payment
    methods the wallet holds. Read only: VENTHORA never places an order from here. The command is AIRI_CLI in .env
    (on Windows, through WSL), since Airi CLI ships for macOS and Linux only."""
    import json as _json
    import shlex
    import subprocess

    cmd = env("AIRI_CLI")
    out = {"installed": False, "command_set": bool(cmd), "signed_in": None, "payment_methods": None, "error": None,
           "how": "install with: npm install -g @airwallex/airi-cli@beta (macOS/Linux; on Windows inside WSL), sign in with airi-cli auth login, "
                  "then set AIRI_CLI in .env to the command that runs it"}

    def run(*args):
        r = subprocess.run([*shlex.split(cmd, posix=False), *args, "--no-telemetry", "-o", "json"], capture_output=True, text=True, timeout=60,
                           env={**__import__("os").environ, "AIRI_TELEMETRY_DISABLED": "1"})
        return _json.loads((r.stdout or "").replace("\x00", "").strip() or "null")

    if not cmd:
        return out | {"error": "AIRI_CLI is not set on this server"}
    try:
        who = await asyncio.to_thread(run, "auth", "whoami")
        out |= {"installed": True, "signed_in": bool((who or {}).get("authenticated")), "session_expires": (who or {}).get("session_expires")}
        if out["signed_in"]:
            pms = await asyncio.to_thread(run, "payment-methods", "list")
            out["payment_methods"] = len(pms if isinstance(pms, list) else (pms or {}).get("items", []))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        out["error"] = f"could not run the Airi CLI: {str(exc)[:200]}"
    return out


class DocsQuestion(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.get("/v1/mcp")
async def mcp_view():
    """Platform Setup -> Airwallex MCP: the docs and developer MCP servers, connected or not, and the docs server's tools."""
    from . import mcp_client

    return await mcp_client.status()


@app.post("/v1/mcp/docs/ask")
async def mcp_docs_ask(body: DocsQuestion, role: str = "finance_manager", by: str = "dashboard"):
    """Ask Airwallex's docs MCP server (search_public_docs); nothing about the organisation is sent, only the question."""
    from . import mcp_client

    _writer(role, by)
    try:
        return {"question": body.question, "answer": await mcp_client.ask_docs(body.question)}
    except mcp_client.MCPError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/v1/metal")
async def metal_view():
    """Platform Setup -> Alta: the testnet, VENTHORA's address and its balance (the key is never shown), the heads
    an anchor would write now, and every anchor sent with its explorer link."""
    from . import metal

    heads = await asyncio.to_thread(_anchor_heads)
    return await metal.status(app.state.org) | {"heads": heads, "digest_now": metal.digest(heads), "anchors": metal.anchors(app.state.org),
                                                "topups": metal.topups(app.state.org)[:20], "topup_limit_per_day": metal.MAX_TOPUP_PER_DAY}


@app.put("/v1/metal/settings")
async def metal_settings(body: Annotated[dict, Body()], role: str = "super_admin", by: str = "dashboard"):
    from . import metal

    who = _setup_writer(role, by)
    try:
        return await asyncio.to_thread(metal.save_settings, app.state.org, body, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/v1/metal/key")
async def metal_key(role: str = "super_admin", by: str = "dashboard"):
    """Create VENTHORA's testnet key (a new one replaces the old; only the address is returned)."""
    from . import metal

    who = _setup_writer(role, by)
    rec = await asyncio.to_thread(metal.generate, who)
    await asyncio.to_thread(app.state.audit.append, "metal_key_created", {"address": rec["address"], "by": who})
    return rec


@app.post("/v1/metal/fund")
async def metal_fund(role: str = "super_admin", by: str = "dashboard"):
    """Top VENTHORA's testnet address up to 100 test USDZ (Alta's gas token) from the faucet."""
    from . import metal

    who = _setup_writer(role, by)
    try:
        out = await metal.fund()
    except metal.MetalError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "metal_funded", out | {"by": who})
    return out


@app.post("/v1/metal/endpoint")
async def metal_endpoint(role: str = "super_admin", by: str = "dashboard"):
    """VENTHORA buys its own Alta RPC endpoint over x402 (1 test USDZ, signed with its key); the URL is kept as a secret."""
    from . import metal

    who = _setup_writer(role, by)
    try:
        out = await metal.buy_endpoint(who)
    except metal.MetalError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "metal_endpoint_bought", {k: out[k] for k in ("paid", "token", "payment_tx")} | {"by": who})
    return out


@app.post("/v1/metal/anchor")
async def metal_anchor(role: str = "finance_manager", by: str = "dashboard"):
    """Write the digest of the audit head and every contract head on Alta, in one transaction."""
    from . import metal

    who = _writer(role, by)
    heads = await asyncio.to_thread(_anchor_heads)
    try:
        rec = await metal.anchor(app.state.org, heads, who)
    except metal.MetalError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "metal_anchor", {"tx_hash": rec["tx_hash"], "digest": rec["digest"], "by": who})
    return rec


@app.get("/v1/reports/spending-recap")
async def reports_spending_recap():
    """Reporting -> Payment reports: one recap of every report above. Each line (payroll, fringe, goods and services,
    operation, cash advance, travel, ODC; the FX and transfer costs; live Visa card charges) with its place (country
    office, branch, unit, person), charge code, the payments actually made (date, amount, the USD Airwallex really
    converted at) and what is still committed and unpaid (at today's rate). The page filters and totals it."""
    from . import fx_cost_report, paid_reports

    org, store = app.state.org, app.state.executor.store
    queue = await list_intake()
    payees = app.state.intake.registry.payees
    units = {u["unit_id"]: u for u in org.list("unit")}
    name = lambda uid: (units.get(uid or "") or {}).get("name")  # noqa: E731
    raw: list[tuple[str, dict]] = []
    for cat in paid_reports.CATEGORIES:
        rows = await asyncio.to_thread(paid_reports.build, cat, org=org, store=store, intake_rows=queue, payees=payees)
        raw += [(cat, r) for r in rows]
    fx = await _rates_to_base({r["currency"] for _, r in raw if r.get("currency")} | {p["currency"] for _, r in raw for p in r.get("payments") or []})
    lines = []
    for cat, r in raw:
        pays = []
        for p in r.get("payments") or []:
            actual = p.get("base_minor")
            pays.append({"paid_at": p["paid_at"], "amount_minor": p["amount_minor"], "currency": p["currency"], "transfer_id": p.get("transfer_id"),
                         "base_minor": actual if actual is not None else _in_base(p["amount_minor"], p["currency"], fx), "basis": "actual" if actual is not None else "rate"})
        open_ = r.get("status") not in ("Done", "Cancelled")
        lines.append({"report": cat, "report_label": paid_reports.CATEGORIES[cat], "type": r.get("type"), "name": r.get("name"), "person": r.get("person"),
                      "reference": r.get("reference"), "reference_kind": r.get("reference_kind"), "request_date": r.get("request_date"), "status": r.get("status"),
                      "currency": r.get("currency"), "amount_minor": r.get("amount_minor") or 0, "paid_minor": r.get("paid_minor") or 0,
                      "remaining_minor": (r.get("remaining_minor") or 0) if open_ else 0,
                      "left_base_minor": _in_base((r.get("remaining_minor") or 0) if open_ else 0, r.get("currency"), fx),
                      "country_office": name(r.get("country_office_id")), "branch": name(r.get("branch_id")), "unit": name(r.get("unit_id")),
                      "charge_code": r.get("charge_code"), "payments": pays})
    rep = await asyncio.to_thread(fx_cost_report.build, store, None)
    reqs = await asyncio.to_thread(app.state.intake.store.list, 2000)
    rep = await asyncio.to_thread(fx_cost_report.lines, rep, store, intake_rows=reqs, org=org)
    for x in rep["rows"]:
        if not x.get("total_base_minor"):
            continue
        lines.append({"report": "fx", "report_label": "FX & transfer costs", "type": x.get("kind_label"), "name": x.get("payee"), "person": None,
                      "reference": x.get("txn_code") or x.get("ref"), "reference_kind": "Airwallex transaction", "request_date": x.get("value_date"), "status": "Done",
                      "currency": rep["base_currency"], "amount_minor": x["total_base_minor"], "paid_minor": x["total_base_minor"], "remaining_minor": 0, "left_base_minor": 0,
                      "country_office": x.get("country_office"), "branch": None, "unit": x.get("unit"), "charge_code": x.get("charge_code"),
                      "payments": [{"paid_at": x.get("value_date") or x.get("at"), "amount_minor": x["total_base_minor"], "currency": rep["base_currency"],
                                    "transfer_id": x.get("ref"), "base_minor": x["total_base_minor"], "basis": "actual"}]})
    cards = {c["_id"]: c for c in org.list("card")}
    for d in org.list("card_auth"):
        if d.get("decision") != "AUTHORIZED" or d.get("source") == "test":
            continue
        cfx = await _rates_to_base({d.get("currency")})
        b = _in_base(d.get("amount_minor"), d.get("currency"), cfx)
        lines.append({"report": "card", "report_label": "Virtual card (Visa)", "type": "card charge", "name": d.get("merchant") or (cards.get(d.get("card_id")) or {}).get("payee"),
                      "person": None, "reference": d.get("_id"), "reference_kind": "card charge", "request_date": str(d.get("at") or "")[:10], "status": "Done",
                      "currency": d.get("currency"), "amount_minor": d.get("amount_minor") or 0, "paid_minor": d.get("amount_minor") or 0, "remaining_minor": 0, "left_base_minor": 0,
                      "country_office": None, "branch": None, "unit": None, "charge_code": None,
                      "payments": [{"paid_at": d.get("at"), "amount_minor": d.get("amount_minor"), "currency": d.get("currency"), "transfer_id": d.get("_id"), "base_minor": b, "basis": "rate"}]})
    labels = dict(paid_reports.CATEGORIES) | {"fx": "FX & transfer costs", "card": "Virtual card (Visa)"}
    return {"base_currency": fx["base"], "lines": lines, "reports": labels, "rate_sources": fx["sources"], "missing_rates": fx["missing"],
            "as_of": datetime.now(UTC).isoformat(timespec="seconds")}


@app.get("/v1/reports/virtual-cards")
async def reports_virtual_cards():
    """Reporting -> Virtual card (Visa): every Visa virtual card VENTHORA issued, its limits and spend, and every
    charge on it: VENTHORA's authorization decision with the rule that decided it, joined with what Airwallex recorded
    (authorization, clearing, refund); plus where the card program stands."""
    from . import card_report, cards, secret_values, visa_tap

    org = app.state.org
    holder = org.get("issuing", "cardholder") or {}
    program = {"cardholder": holder.get("name"), "cardholder_status": holder.get("status"), "cards_issued": len(org.list("card")),
               "remote_auth": None, "secret_kept": secret_values.info(cards.SECRET)["set"], "error": None}
    airwallex = None
    try:
        bank = _bank()
        cfg = await bank.issuing_config()
        st = cfg.get("remote_auth_settings") or {}
        program["remote_auth"] = bool(st.get("enabled")) if st else False
        if org.list("card"):
            airwallex = await bank.card_transactions()
    except HTTPException as exc:
        program["error"] = exc.detail
    except AirwallexError as exc:
        program["error"] = f"Airwallex request failed: {exc}"
    ccys = {d.get("currency") for d in org.list("card_auth")} | {t.get("transaction_currency") for t in airwallex or []}
    fx = await _rates_to_base({c for c in ccys if c})
    rep = await asyncio.to_thread(card_report.build, org=org, airwallex=airwallex, to_base=lambda m, c: _in_base(m, c, fx), program=program,
                                  tap=visa_tap.public_info())
    return rep | {"base_currency": fx["base"], "missing_rates": fx["missing"]}


@app.get("/v1/reports/contract-chain")
async def reports_contract_chain():
    """Reporting -> Contract blockchain: every purchase order and consultant contract as its own SHA-256 hash chain
    (the agreed terms, then each invoice and payment against it, with the Airwallex transaction code), compared with
    the seal kept from the last read (new blocks are sealed, and the seal written to the audit hash chain), next to the
    checks on the contract: value, payee, currency, period, duplicate invoice, terms, charge code, level of effort."""
    from . import contract_chain

    org = app.state.org
    reqs = await asyncio.to_thread(app.state.intake.store.list, 2000)
    fx = await _rates_to_base({p["currency"] for p in org.list("po")})
    rep = await asyncio.to_thread(contract_chain.build, org=org, store=app.state.executor.store, intake_rows=reqs,
                                  payees=app.state.intake.registry.payees, to_base=lambda m, c: _in_base(m, c, fx), audit=app.state.audit)
    from . import metal

    return rep | {"base_currency": fx["base"], "rate_sources": fx["sources"], "missing_rates": fx["missing"], "anchor": (metal.anchors(org) or [None])[0]}


@app.get("/v1/reports/fx-costs")
async def reports_fx_costs(period: str | None = Query(None, max_length=20)):
    """Reporting -> FX & transfer costs: every exchange (a booked conversion, or a payout Airwallex converted inside the
    transfer) at its real cost against the mid-market rate, and every payout fee, in the base currency, per reporting
    period; set against what the period converted and paid."""
    from . import fx_cost_report

    rep = await asyncio.to_thread(fx_cost_report.build, app.state.executor.store, period or None)
    reqs = await asyncio.to_thread(app.state.intake.store.list, 2000)
    rep = await asyncio.to_thread(fx_cost_report.lines, rep, app.state.executor.store, intake_rows=reqs, org=app.state.org)
    card_rows, note = await _card_fx_rows(rep.get("base_currency") or "USD", period)
    rep["rows"] = card_rows + rep.get("rows", [])
    rep["card_conversions"] = {"rows": len(card_rows), "cost_base_minor": sum(r["total_base_minor"] or 0 for r in card_rows), "note": note}
    tot = rep.setdefault("totals", {})
    tot["card_fx_base_minor"] = rep["card_conversions"]["cost_base_minor"]
    tot["total_base_minor"] = (tot.get("total_base_minor") or 0) + tot["card_fx_base_minor"]
    return rep


async def _card_fx_rows(base: str, period: str | None) -> tuple[list[dict], str]:
    """Every card payment Airwallex converted (spent in one currency, billed to the wallet in another): the rate it
    used against the indicative mid rate, and the difference in the base currency = the conversion cost."""
    try:
        bank = _bank()
        txs = await bank.card_transactions()
    except (HTTPException, AirwallexError) as exc:
        return [], f"card transactions not read: {getattr(exc, 'detail', exc)}"
    cards = {c["_id"]: c for c in app.state.org.list("card")}
    rates: dict[str, float] = {}

    async def to_base(ccy: str) -> float:
        if ccy == base:
            return 1.0
        if ccy not in rates:
            rates[ccy] = float(await bank.rate_to_base(ccy, base))
        return rates[ccy]
    out = []
    for t in txs:
        tc, bc = t.get("transaction_currency"), t.get("billing_currency")
        if str(t.get("status", "")).upper() not in ("APPROVED", "CLEARED", "SETTLED", "COMPLETED", "POSTED") or not bc or tc == bc:
            continue
        at = str(t.get("transaction_date") or t.get("posted_date") or "")
        if period and not at.startswith(period):
            continue
        spent, billed = abs(float(t.get("transaction_amount") or 0)), abs(float(t.get("billing_amount") or 0))
        if not spent or not billed:
            continue
        try:
            spent_base, billed_base = spent * await to_base(tc), billed * await to_base(bc)
        except AirwallexError:
            spent_base = billed_base = None
        cost = round((billed_base - spent_base) * 100) if spent_base is not None else None
        card = cards.get(t.get("card_id") or "") or {}
        out.append({"at": at, "period": at[:7], "kind": "card", "kind_label": "Card payment converted by Airwallex", "ref": t.get("transaction_id"),
                    "obligation_id": card.get("fund_request") or card.get("payee"), "currency": tc, "bought_minor": round(spent * 100),
                    "sold_minor": round(billed * 100), "sold_currency": bc, "client_rate": f"{billed / spent:.6f}",
                    "mid_rate": f"{(await to_base(tc)) / (await to_base(bc)):.6f}" if spent_base is not None else None, "currency_pair": f"{tc}{bc}",
                    "spread_base_minor": cost, "spread_bp": round((billed_base - spent_base) * 10_000 / spent_base) if spent_base else None,
                    "fee_base_minor": 0, "fee": None, "total_base_minor": cost, "merchant": (t.get("merchant") or {}).get("name"), "card": card.get("masked")})
    return out, f"{len(out)} card payment(s) converted"


@app.get("/v1/exceptions")
async def exceptions_list():
    """Executions & incidents: everything that waits for a person, one list per kind. Empty means VENTHORA pays as
    normal; an item here is either paid once a person confirms it (inside the mandate) or sent to Pending payment
    (outside it)."""
    from . import doa

    org, store = app.state.org, app.state.executor.store
    today = datetime.now(UTC).date()
    payees = app.state.intake.registry.payees
    rows = await asyncio.to_thread(doa.evaluate, org, _payee_status, payees, today)
    runs = {r["_id"]: r for r in org.list("doa_run")}
    pers = next((r for r in rows if r["key"] == "personnel"), None) or {}
    payroll = []
    pre = {a["_id"]: a for a in org.list("payroll_approval")}
    for ln in pers.get("lines", []):
        done = runs.get(ln.get("ref") or "")
        if ln["verdict"] == "auto" or (done and done["status"] in ("PAID", "IN_FLIGHT", "IN_REVIEW")):
            continue
        fix, where = doa._fix(ln["reasons"][0]) if ln["reasons"] else ("", "")
        payroll.append(ln | {"period": pers["period"], "pay_date": pers["pay_date"], "due": pers["due"], "fix": fix, "where": where,
                             "payee_name": (payees.get(ln.get("payee_id") or "") or {}).get("name"),
                             "inside_mandate": ln["verdict"] == "approve",
                             "approved": (pre.get(ln["ref"]) or {}).get("by") if all((pre.get(ln["ref"]) or {}).get(k) == ln.get(k)
                                                                          for k in ("amount_minor", "currency", "payee_id", "charge_code")) else None})
    p_ = await asyncio.to_thread(doa.pending, org, _payee_status, payees, today)
    other = [b for b in p_["blocked"] if b["category"] != doa.BY_KEY["personnel"]["title"]]
    call_backs = [{"payee_id": x["beneficiary_id"], "name": (payees.get(x["beneficiary_id"]) or {}).get("name", x["beneficiary_id"]), "status": x["status"],
                   "registered_phone": x.get("registered_phone"), "cooling_until": x.get("cooling_until"), "updated_at": x.get("updated_at")}
                  for x in await asyncio.to_thread(store.list_payees) if x["status"] in ("NEW", "CHANGED")]
    queue = await asyncio.to_thread(app.state.intake.store.list, 1000)
    reviews = [{"intake_id": q.intake_id, "status": q.status, "kind": q.kind, "from": q.display_name, "counterparty": q.counterparty_name,
                "amount_minor": q.amount_minor, "currency": q.currency, "invoice_no": q.invoice_no, "reason": (q.reasons or [""])[0], "received_at": q.received_at}
               for q in queue if q.status in ("NEEDS_REVIEW", "QUARANTINED") and not q.payment]
    recon = await asyncio.to_thread(store.open_recon_cases)
    incidents = [i for i in await asyncio.to_thread(store.list_incidents, 200) if i.get("status") != "RESOLVED"]
    counts = {"payroll": sum(1 for x in payroll if not x["approved"]), "due_blocked": len(other), "call_backs": len(call_backs), "reviews": len(reviews),
              "recon": len(recon), "incidents": len(incidents)}
    return {"counts": counts, "total": sum(counts.values()), "payroll": payroll, "due_blocked": other, "call_backs": call_backs, "reviews": reviews,
            "payroll_period": pers.get("period"), "pay_date": pers.get("pay_date"),
            "payroll_lines": [{k: ln.get(k) for k in ("employee", "payee_id", "currency", "amount_minor", "charge_code")} for ln in pers.get("lines", [])],
            "adjustments": (doa.register(org, pers["period"]) or {}).get("lines", []) if pers.get("period") else []}


class PayrollApproveIn(BaseModel):
    ref: str = Field(min_length=3, max_length=120)


@app.post("/v1/exceptions/payroll/approve")
async def exceptions_payroll_approve(body: PayrollApproveIn, role: str = "finance_manager", by: str = "dashboard"):
    """Approve a changed payroll line once (a first salary, a changed net pay, a line adjusted for this period). It is
    then paid with the others on the pay day, by VENTHORA, as long as the amount is still the one approved. The person
    who made the change cannot approve it."""
    from . import doa

    if role not in ("finance_manager", "cfo", "super_admin", "hq_admin"):
        raise HTTPException(403, f"role {role} may not approve a salary")
    org = app.state.org
    rows = await asyncio.to_thread(doa.evaluate, org, _payee_status, app.state.intake.registry.payees, datetime.now(UTC).date())
    pers = next((r for r in rows if r["key"] == "personnel"), None) or {}
    ln = next((x for x in pers.get("lines", []) if x.get("ref") == body.ref), None)
    if ln is None:
        raise HTTPException(404, "no such payroll line this period")
    if ln["verdict"] != "approve":
        raise HTTPException(409, f"{ln['employee']}: " + ("it is paid on its own; nothing to approve" if ln["verdict"] == "auto"
                                                         else "outside the delegation: fix it, or send it to Pending payment"))
    me = by.strip().lower()
    if me and me == str(ln.get("changed_by") or "").split(" (")[0].strip().lower():
        raise HTTPException(403, f"{ln['employee']}: the person who made the change cannot approve it")
    rec = {"_id": body.ref, "employee": ln["employee"], "period": pers["period"], "amount_minor": ln["amount_minor"], "currency": ln["currency"],
           "payee_id": ln.get("payee_id"), "charge_code": ln.get("charge_code"),  # bound to what the approver saw: amount, currency, account, charge
           "reasons": ln["reasons"], "by": f"{by} ({role})", "at": datetime.now(UTC).isoformat(timespec="seconds")}
    await asyncio.to_thread(org._put, "payroll_approval", rec, by)
    await asyncio.to_thread(app.state.audit.append, "payroll_line_approved", rec)
    return rec


class ToReviewIn(BaseModel):
    ref: str = Field(min_length=3, max_length=120, description="the payroll line, doa:personnel:<period>:<employee>")


@app.post("/v1/exceptions/payroll/to-review")
async def exceptions_payroll_to_review(body: ToReviewIn, role: str = "finance_manager", by: str = "dashboard"):
    """A payroll line outside what the delegation lets VENTHORA pay (a currency it does not pay in, a change it may
    not accept on its own): sent to Pending payment as a payment order, where a person decides it like any other."""
    from . import doa

    org = app.state.org
    payees = app.state.intake.registry.payees
    rows = await asyncio.to_thread(doa.evaluate, org, _payee_status, payees, datetime.now(UTC).date())
    pers = next((r for r in rows if r["key"] == "personnel"), None) or {}
    ln = next((x for x in pers.get("lines", []) if x.get("ref") == body.ref), None)
    if ln is None:
        raise HTTPException(404, "no such payroll line this period")
    if not ln.get("payee_id") or ln["payee_id"] not in payees:
        raise HTTPException(409, f"{ln['employee']} has no registered bank account: register it in People and roles first")
    order = PaymentOrderIn(vendor_id=ln["payee_id"], invoice_no=f"PAY-{pers['period']}-{doa._slug(ln['employee'])}"[:40], amount_minor=ln["amount_minor"],
                           currency=ln["currency"], due_date=pers.get("pay_date") or datetime.now(UTC).strftime("%Y-%m-%d"),
                           description=f"Salary {pers['period']}: {ln['employee']} ({'; '.join(ln['reasons'])})"[:300])
    req = await payment_order(order, role=role, by=by)
    rec = await asyncio.to_thread(doa.record_run, org, pers, ln, "IN_REVIEW", f"{by} ({role})", None, None, f"sent to Pending payment as {req.intake_id}")
    return {"line": rec, "intake_id": req.intake_id, "status": req.status}


class PrioritySopIn(BaseModel):
    items: list[dict] = Field(description="every category in the order wanted, each {key, level: high | medium | low}")
    late_cost: bool = Field(True, description="inside a level, a payment that costs more when late moves up one step")
    policy: dict | None = Field(None, description="mode (fifo | priority), horizon_hours (24-120), deemed_approval, pay_what_fits, "
                                                  "notify_fco, cascade, fx_at_plan, run_times [\"09:00\", ...] (company time), run_days (every | weekdays)")


@app.get("/v1/priority-sop")
async def priority_sop_get(role: str | None = None):
    """System Setting -> Standard Payment Priority Policy: every spending category with its level (High, Medium, Low), its place and
    the priority it gives in the plan for the horizon; and how the payments waiting now would be ranked by it."""
    from . import mandate, priority_sop

    org = app.state.org
    rows = priority_sop.current(org)
    pr = {r["key"]: r["priority"] for r in rows}
    payees = app.state.intake.registry.payees
    base = TreasuryState.model_validate(json.loads(_scenario_file("intake_only").read_text(encoding="utf-8"))["state"])
    queue = [q for q in await asyncio.to_thread(app.state.intake.store.list, 1000) if q.status in ("VERIFIED", "NEEDS_REVIEW") and not q.payment]
    try:
        state = await asyncio.to_thread(app.state.intake.to_state, base, as_of=datetime.now(UTC).date(), requests=queue)
    except ValueError:
        state = base
    ids = {o.obligation_id for o in base.obligations}
    preview = []
    for o in state.obligations:
        if o.obligation_id in ids:
            continue
        d = o.model_dump()
        k = priority_sop.classify(d, payees)
        why = priority_sop.costly_when_late(d, datetime.now(UTC).date().isoformat())
        preview.append({"obligation_id": o.obligation_id, "payee": d.get("payee_name") or o.beneficiary_id, "amount_minor": o.amount_minor,
                        "currency": o.currency, "due_in_hours": o.due_in_hours, "category": k, "category_label": priority_sop.ITEMS[k][0],
                        "priority": pr[k], "late": why, "payee_priority": (payees.get(o.beneficiary_id or "") or {}).get("default_priority")})
    preview.sort(key=lambda x: (-x["priority"], x["due_in_hours"], x["obligation_id"]))
    st = mandate.standing(org)
    rec = priority_sop.saved(org) or {}
    horizon = (st.get("book") or {}).get("horizon_hours", 72)
    return {"items": rows, "levels": priority_sop.LEVELS, "bands": {k: list(v) for k, v in priority_sop.BANDS.items()}, "saved": bool(rec),
            "late_cost": priority_sop.late_cost_on(org), "top_shared": priority_sop.TOP_SHARED, "pay_on_arrival": mandate.pay_on_arrival(org),
            "updated_by": rec.get("updated_by"), "horizon_hours": priority_sop.policy(org)["horizon_hours"] if rec.get("policy") else horizon, "preview": preview,
            "policy": priority_sop.policy(org), "policy_saved": bool(rec.get("policy")), "modes": priority_sop.MODES, "horizons": list(priority_sop.HORIZONS),
            "timing": {"deadline_hour_utc": priority_sop.DEADLINE_HOUR_UTC, "deemed_before_h": priority_sop.DEEMED_BEFORE_H, "fx_before_h": priority_sop.FX_BEFORE_H},
            "run": _run_info(org),
            "notices": priority_sop.notices(org), "holds": list(priority_sop.holds(org).values()),
            "authority": {"role": st["authority_role"], "you_may_change": role == st["authority_role"] if role else None}}


def _run_info(org) -> dict:
    """The payment run times in the company's time zone, the next one and the runs already made."""
    from zoneinfo import ZoneInfo

    from . import priority_sop, processlog

    pol = priority_sop.policy(org)
    tz = priority_sop.run_timezone(pol, processlog.company_tz(org))
    at = priority_sop.next_run(pol, datetime.now(UTC), tz)
    zone = datetime.now(UTC).astimezone(ZoneInfo(tz)).tzname()
    last = sorted(org.list("policy_slot"), key=lambda r: r["_id"])[-5:]
    h, m = priority_sop.SCHEDULED_PAY_UTC
    return {"timezone": tz, "zone": zone, "days": priority_sop.RUN_DAYS, "zones": priority_sop.RUN_TZ, "window_min": priority_sop.RUN_WINDOW_MIN,
            "scheduled_at_utc": f"{h:02d}:{m:02d}",
            "next": at.strftime("%a %d %b %Y %H:%M") + f" {zone}" if at else None, "last": [r["_id"] for r in last]}


@app.put("/v1/priority-sop")
async def priority_sop_put(body: PrioritySopIn, role: str = "super_admin", by: str = "dashboard"):
    """Save the Standard Payment Priority Policy. Only the role holding the mandate authority may change it; from then on every plan
    built from the organisation's payments ranks them by it."""
    from . import mandate, priority_sop

    st = mandate.standing(app.state.org)
    if role != st["authority_role"]:
        raise HTTPException(403, f"only the {mandate.AUTHORITY_ROLES[st['authority_role']]} changes the Standard Payment Priority Policy")
    try:
        rec = await asyncio.to_thread(priority_sop.save, app.state.org, body.items, f"{by} ({role})", body.late_cost, body.policy)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "priority_sop_saved", rec)
    return await priority_sop_get(role)


QUEUE_PLAN_MAX = 90  # requests read into one book (TreasuryState keeps at most 100 pieces of evidence)
APPROVER_ROLES = ("finance_manager", "cfo")


def _with_mandate(state: TreasuryState) -> TreasuryState:
    """The FCO's standing mandate on a book: the allocation ring-fenced from the wallet, the reserve floor, and who may
    approve (the finance manager, the CFO and the holder of the mandate authority, the FCO)."""
    from . import mandate

    st = mandate.standing(app.state.org)
    book = st.get("book") or {}
    upd: dict = {}
    if book.get("allocation_minor") is not None:
        upd["balances"] = {**state.balances, state.base_currency: int(book["allocation_minor"])}
    pol: dict = {"approver_roles": sorted(set(state.policy.approver_roles) | set(APPROVER_ROLES) | {st["authority_role"]})}
    if book.get("reserve_floor_minor") is not None:
        pol["reserve_floor_minor"] = int(book["reserve_floor_minor"])
    upd["policy"] = state.policy.model_copy(update=pol)
    return state.model_copy(update=upd)


async def _todays_state() -> tuple[TreasuryState | None, dict]:
    """The organisation's book as it stands now: the payment requests waiting (verified or held for a person), the
    payroll lines ready for the pay day, the customer receipts expected, the budget; on the live Airwallex balance and
    rates when the bank is connected (the scenario's balances otherwise), with the Standard Payment Priority Policy applied."""
    from . import doa, mandate

    org = app.state.org
    today = datetime.now(UTC).date()
    payees = app.state.intake.registry.payees
    base = _live_base()
    base = base.model_copy(update={"obligations": [], "receivables": [], "evidence": []})
    queue = [q for q in await asyncio.to_thread(app.state.intake.store.list, 1000)
             if not q.payment and q.status in ("VERIFIED", "NEEDS_REVIEW", "QUARANTINED")]
    queue_waiting = len(queue)
    if len(queue) > QUEUE_PLAN_MAX:  # a book holds at most 100 pieces of evidence: the most urgent first, the rest next round
        queue = sorted(queue, key=lambda q: (q.status != "VERIFIED", q.due_date or "9999-12-31", q.received_at))[:QUEUE_PLAN_MAX]
    overflow: list[dict] = []
    try:
        state = await asyncio.to_thread(app.state.intake.to_state, base, as_of=today, requests=queue) if queue else None
        overflow = list(getattr(app.state.intake, "overflow", None) or []) if queue else []  # beyond one book of 12: next round
    except ValueError as exc:  # nothing in the queue that is a payable obligation (or a request the book refuses)
        state = None
        queue_error = str(exc)[:400]
    else:
        queue_error = None
    rows = await asyncio.to_thread(doa.evaluate, org, _payee_status, payees, today)
    pers = next((r for r in rows if r["key"] == "personnel"), None) or {}
    runs = {r["_id"] for r in org.list("doa_run") if r["status"] in ("PAID", "IN_FLIGHT", "IN_REVIEW")}
    pre = {a["_id"]: a for a in org.list("payroll_approval")}
    approved = {ln["ref"] for ln in pers.get("lines", []) if ln.get("ref") in pre
                and all(pre[ln["ref"]].get(k) == ln.get(k) for k in ("amount_minor", "currency", "payee_id", "charge_code"))}
    # only what the delegation pays alone, or a change one person approved; a first or changed salary not approved yet waits
    pay_lines = [ln for ln in pers.get("lines", []) if ln.get("ref") not in runs and (ln["verdict"] == "auto" or ln.get("ref") in approved)]         if pers.get("pay_date") else []
    waiting_lines = [{"employee": ln["employee"], "amount_minor": ln["amount_minor"], "currency": ln["currency"], "reasons": ln["reasons"]}
                     for ln in pers.get("lines", []) if ln.get("ref") not in runs and ln not in pay_lines]
    extra: tuple[list, list] = ([], [])
    if pay_lines:
        from . import priority_sop

        pb = priority_sop.apply(doa.book(base, pers, pay_lines, payees, today), org, payees)
        if state is None:
            state = pb
        else:
            extra = ([o.model_dump() for o in pb.obligations], [e.model_dump() for e in pb.evidence])
    source = {"requests": queue_waiting, "payroll_lines": len(pay_lines), "pay_date": pers.get("pay_date"), "balances": "scenario", "next_round": 0,
              "payroll_waiting": waiting_lines, "queue_error": queue_error}
    if state is None:
        return None, source
    data = state.model_dump()
    data["obligations"] += extra[0]
    data["evidence"] += extra[1]
    # an invoice already paid or in flight (the duplicate lock holds it) is never planned again
    done = await asyncio.to_thread(lambda: app.state.executor.store.live_payout_fingerprints() | app.state.executor.store.paid_fingerprints())
    already = [o["obligation_id"] for o in data["obligations"] if o.get("invoice_fingerprint") and o["invoice_fingerprint"] in done]
    if already:
        source["already_paid"] = already
        data["obligations"] = [o for o in data["obligations"] if o["obligation_id"] not in already]
        if not data["obligations"]:
            return None, source
    if len(data["obligations"]) > 12:  # one plan proves at most 12 obligations: the most urgent by the SOP now, the rest next round
        source["next_round"] = len(data["obligations"]) - 12
        ok = {e["subject_id"] for e in data.get("evidence", []) if e.get("status") == "verified"}  # payable first; stopped ones fill the rest
        ranked = sorted(data["obligations"], key=lambda o: (o["obligation_id"] not in ok, -o["priority"], o["due_in_hours"], o["obligation_id"]))
        data["obligations"] = ranked[:12]
        overflow = ranked[12:] + overflow
    # the ring fence is the FCO's "Allocation ring-fenced from the wallet" (Delegation of Authority) when it is set;
    # the scenario's balance only when it is not (a blank company starts with none, so the agent could pay nothing)
    book = mandate.standing(app.state.org).get("book") or {}
    for k in ("allocation_minor", "reserve_floor_minor"):
        if book.get(k) is not None:
            source[k] = int(book[k])
    state = _with_mandate(TreasuryState.model_validate(data))  # allocation, reserve floor and approvers of the FCO's mandate
    try:
        state = await with_live_balances_and_rates(state, _bank(), ring_fence=True)
        source["balances"] = "live Airwallex"
    except (HTTPException, AirwallexError) as exc:
        source["balances_error"] = str(getattr(exc, "detail", exc))[:200]
    state, _ = apply_operational_facts(state, app.state.executor.store)
    source["overflow"] = overflow  # shown on Agent plan as "waits: next round", never planned or paid this round
    return _with_budget(state), source


class SimulateIn(BaseModel):
    count: int = Field(136, ge=8, le=500)
    batch: int = Field(17, ge=1, le=100, description="emails between two re-plans")
    ai_sample: int = Field(2, ge=0, le=20, description="the first N emails are read by the AI model too; the rest by the rules only")
    seed: int | None = None


async def _plan_snapshot() -> dict:
    d = await agent_plan_today()
    t = d.get("today") or {}
    c = t.get("coverage") or {}
    return {"plan_hash": ((d.get("plan") or {}).get("plan_hash") or "")[:12], "decision": (d.get("plan") or {}).get("decision"),
            "ready": t.get("ready_count", 0), "ready_minor": t.get("ready_minor", 0), "approval": t.get("approval_count", 0),
            "blocked": t.get("blocked_count", 0), "count": t.get("count", 0), "short_minor": c.get("short_minor", 0),
            "enough": c.get("enough", True), "base": t.get("base_currency") or "USD",
            "ids": sorted(r["obligation_id"] for r in t.get("due") or [])}


async def _sim_run(run: str, emails: list[dict], batch: int, ai_sample: int) -> None:
    from collections import Counter

    org = app.state.org
    st = {"_id": run, "run": run, "count": len(emails), "done": 0, "status": "running", "batches": [], "by_kind": {},
          "started_at": datetime.now(UTC).isoformat(timespec="seconds")}
    await asyncio.to_thread(org._put, "sim_run", st, "VENTHORA (simulation)")
    n_b = -(-len(emails) // batch)
    _note("Intake", f"Invoice simulation {run} started: {len(emails)} supplier emails in {n_b} batches of {batch}; the agent re-plans after each batch; "
                    f"nothing is paid on arrival and no payee's bank status changes")
    try:
        prev = await _plan_snapshot()
        st["before"] = {k: v for k, v in prev.items() if k != "ids"}
        for i in range(n_b):
            outcome: Counter = Counter()
            for e in emails[i * batch:(i + 1) * batch]:
                try:
                    req = await _ingest(e["eml"], None, False, channel="email", simulate=True, use_model=e["n"] <= ai_sample)
                    res = req.status
                except Exception as exc:  # noqa: BLE001 - one bad email stops nothing else
                    res = "ERROR"
                    e["error"] = str(getattr(exc, "detail", exc))[:200]
                outcome[res] += 1
                st["by_kind"].setdefault(e["kind"], {})
                st["by_kind"][e["kind"]][res] = st["by_kind"][e["kind"]].get(res, 0) + 1
                st["done"] += 1
            snap = await _plan_snapshot()
            new, gone = set(snap["ids"]) - set(prev["ids"]), set(prev["ids"]) - set(snap["ids"])
            b = snap["base"]
            row = {"batch": i + 1, "emails": sum(outcome.values()), "outcome": dict(outcome), "plan": {k: v for k, v in snap.items() if k != "ids"},
                   "new_in_plan": len(new), "left_plan": len(gone), "replanned": snap["plan_hash"] != prev["plan_hash"],
                   "at": datetime.now(UTC).isoformat(timespec="seconds")}
            st["batches"].append(row)
            await asyncio.to_thread(org._put, "sim_run", st, "VENTHORA (simulation)")
            _note("Payments", f"Simulation {run} · batch {i + 1}/{n_b}: {row['emails']} invoices in ("
                              + ", ".join(f"{v} {k.lower().replace('_', ' ')}" for k, v in sorted(outcome.items())) + "); "
                              + ("agent re-planned" if row["replanned"] else "plan unchanged") + f" (plan {snap['plan_hash'][:8] or '-'}): "
                              f"{snap['count']} due in the horizon, {snap['ready']} ready ({b} {snap['ready_minor'] / 100:,.2f}), "
                              f"{snap['approval']} wait for a person, {snap['blocked']} stopped; +{len(new)} new / -{len(gone)} out; "
                              + (f"short {b} {snap['short_minor'] / 100:,.2f}" if snap["short_minor"] else "balance covers it"))
            prev = snap
        st["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        st["status"], st["error"] = "failed", str(exc)[:300]
    st["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    await asyncio.to_thread(org._put, "sim_run", st, "VENTHORA (simulation)")
    await asyncio.to_thread(app.state.audit.append, "invoice_simulation", {k: st.get(k) for k in ("run", "count", "done", "status", "by_kind")})
    _note("Intake", f"Invoice simulation {run} {st['status']}: {st['done']} of {st['count']} emails; by kind: "
                    + "; ".join(f"{k} " + ", ".join(f"{n} {s.lower()}" for s, n in v.items()) for k, v in st["by_kind"].items()))


@app.post("/v1/demo/simulate-invoices")
async def demo_simulate_invoices(body: SimulateIn, role: str = "super_admin", by: str = "dashboard"):
    """Start an invoice simulation on this company (Demo-Only): the emails are built from the vendors and open orders
    on file now and go in batch by batch, with a re-plan after each batch. Runs in the background; follow it with GET."""
    from . import simulator

    if role not in ("super_admin", "finance_manager", "developer"):
        raise HTTPException(403, "only the FCO, a finance manager or the developer runs a simulation")
    if any(r.get("status") == "running" for r in app.state.org.list("sim_run")):
        raise HTTPException(409, "a simulation is already running")
    run = secrets.token_hex(3)
    try:
        emails = simulator.build(simulator.vendors(app.state.intake.registry, app.state.org), body.count, run, datetime.now(UTC).date(), body.seed)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    asyncio.get_running_loop().create_task(_sim_run(run, emails, body.batch, body.ai_sample))
    return {"run": run, "count": len(emails), "batch": body.batch, "kinds": simulator.KINDS,
            "vendors": sorted({e["vendor"] for e in emails}), "started_by": f"{by} ({role})"}


@app.get("/v1/demo/simulate-invoices")
async def demo_simulate_invoices_status(run: str | None = None):
    from . import simulator

    runs = sorted(app.state.org.list("sim_run"), key=lambda r: r.get("started_at", ""), reverse=True)
    cur = next((r for r in runs if r["_id"] == run), None) if run else (runs[0] if runs else None)
    return {"run": cur, "runs": [{k: r.get(k) for k in ("_id", "status", "count", "done", "started_at")} for r in runs[:10]], "kinds": simulator.KINDS}


@app.get("/v1/agent-plan/forecast")
async def agent_plan_forecast(months: int = 3):
    """Agent plan -> the next three months: payroll, the payment requests waiting, what is left on open purchase
    orders, the customer receipts expected; the balance month by month against the reserve floor, and the deposit
    VENTHORA suggests (how much, by when, why). Nothing is paid by reading it."""
    from . import forecast

    months = max(1, min(months, 6))
    state, source = await _todays_state()
    base_state = state if state is not None else _live_base()
    if state is None:
        with contextlib.suppress(Exception):
            base_state = await with_live_balances_and_rates(base_state, _bank(), ring_fence=True)
    reqs = await asyncio.to_thread(app.state.intake.store.list, 2000)
    cur = base_state.base_currency
    fx = dict(base_state.fx_to_base)
    need = {u.get("salary_currency") for u in app.state.org.list("user") if u.get("salary_currency")} | {q.currency for q in reqs if q.currency} \
        | {p.get("currency") for p in app.state.org.list("po") if p.get("currency")}
    missing = {c for c in need if c and c != cur and c not in fx}
    if missing:
        with contextlib.suppress(Exception):
            got = await _rates_to_base(missing)
            fx |= {c: str(v) for c, v in got["rates"].items() if v}
    short = 0
    with contextlib.suppress(Exception):
        today = (await agent_plan_today()).get("today") or {}
        short = int((today.get("coverage") or {}).get("short_minor") or 0)
    out = await asyncio.to_thread(forecast.build, app.state.org, reqs, list(base_state.receivables), cur, fx, base_state.balance(cur),
                                  base_state.policy.reserve_floor_minor, datetime.now(UTC).date(), months, short)
    return out | {"balances": source.get("balances") if isinstance(source, dict) else None}


@app.get("/v1/agent-plan")
async def agent_plan_today():
    """Agent plan -> Today's plan: what VENTHORA would do now with the organisation's real payments, and why; the
    part it pays on its own authority and the part that waits for a person. Nothing is paid by reading it."""
    from . import autonomy, mandate

    state, source = await _todays_state()
    st = mandate.standing(app.state.org)
    from . import priority_sop

    pol = priority_sop.policy(app.state.org)
    meta = {"as_of": datetime.now(UTC).isoformat(timespec="seconds"), "source": source, "pay_on_arrival": mandate.pay_on_arrival(app.state.org),
            "horizon_hours": pol["horizon_hours"] if (priority_sop.saved(app.state.org) or {}).get("policy") else (st.get("book") or {}).get("horizon_hours", 72),
            "policy": pol, "holds": list(priority_sop.holds(app.state.org).values()), "notices": priority_sop.notices(app.state.org)}
    if state is None:
        return meta | {"plan": None, "split": None}
    holds = priority_sop.holds(app.state.org)
    if holds:  # a payment a person holds or put pending is out of today's plan until its date
        left = [o for o in state.obligations if o.obligation_id not in holds]
        if not left:
            return meta | {"plan": None, "split": None, "today": None}
        state = state.model_copy(update={"obligations": left})
    s = await autonomy.split(app.state.agent, state)
    from . import agent_today

    today = await asyncio.to_thread(agent_today.summary, state, source, s, app.state.org, app.state.intake.registry.payees,
                                    meta["horizon_hours"], _next_policy_run(pol))
    try:  # the whole wallet (the plan uses the allocation ring-fenced from it): what a deposit changes first
        pos = await treasury_position()
        today["wallet_minor"] = pos["available"]["base_minor"]
    except Exception:  # noqa: BLE001 - no bank: the plan's balance is all there is
        today["wallet_minor"] = None
    today["allocation_minor"] = (mandate.standing(app.state.org).get("book") or {}).get("allocation_minor")
    return meta | {"plan": s.full.model_dump(), "split": s.summary(), "today": today,
                   "balances": {c: v for c, v in state.balances.items()}, "obligations": [o.model_dump() for o in state.obligations]}


def _next_policy_run(pol: dict) -> str | None:
    """When the ready part is paid: the policy's next run (high priority first, saved, bank connected); None = by hand."""
    from . import priority_sop

    if not (priority_sop.saved(app.state.org) or {}).get("policy") or pol["mode"] != "priority" or not app.state.agent.proof.airwallex.configured:
        return None
    if pol.get("run_times"):
        from . import processlog

        at = priority_sop.next_run(pol, datetime.now(UTC), priority_sop.run_timezone(pol, processlog.company_tz(app.state.org)))
        return at.isoformat(timespec="minutes") if at else None
    t0, period = getattr(app.state, "policy_clock", (datetime.now(UTC), 300.0))
    now = datetime.now(UTC)
    n = int((now - t0).total_seconds() // period) + 1
    return (t0 + timedelta(seconds=n * period)).isoformat(timespec="seconds")


@app.post("/v1/agent-plan/execute")
async def agent_plan_execute(role: str = "finance_manager", by: str = "dashboard"):
    """Pay now the part of today's plan that is inside the mandate (the agent's own authority, no approval); what
    needs a person stays where it is. The book passes the treasury proof again at execution."""
    from . import autonomy

    _writer(role, by)
    from . import priority_sop

    state, source = await _todays_state()
    holds = priority_sop.holds(app.state.org)
    if state is not None and holds:
        left = [o for o in state.obligations if o.obligation_id not in holds]
        state = state.model_copy(update={"obligations": left}) if left else None
    if state is None:
        raise HTTPException(409, "nothing waits for payment")
    if source["balances"] != "live Airwallex":
        raise HTTPException(503, f"the bank is not connected: {source.get('balances_error', 'no live balance')}")
    s = await autonomy.split(app.state.agent, state)
    if s.auto_plan is None or not any(d.action in autonomy.PAYING for d in s.auto_plan.obligations):
        return {"paid": [], "split": s.summary(), "message": "nothing is inside the mandate to pay now"}
    res = await app.state.executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash, requested_by=f"{by} (Agent plan)"))
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "agent_plan_executed", {"by": by, "execution_id": res.execution_id, "status": res.status})
    return {"paid": [d.obligation_id for d in s.auto_plan.obligations if d.action in autonomy.PAYING], "split": s.summary(),
            "execution": {"execution_id": res.execution_id, "status": res.status, "message": res.message}}


class PlanHoldIn(BaseModel):
    obligation_id: str = Field(min_length=1, max_length=80)
    note: str = Field("", max_length=300)


@app.post("/v1/agent-plan/hold")
async def agent_plan_hold(body: PlanHoldIn, role: str = "finance_manager", by: str = "dashboard"):
    """A person's intervention: the policy does not pay this payment (no deemed approval, no autonomous run) until released."""
    from . import priority_sop

    _writer(role, by)
    rec = await asyncio.to_thread(priority_sop.hold, app.state.org, body.obligation_id, f"{by} ({role})", body.note)
    await asyncio.to_thread(app.state.audit.append, "plan_hold", rec)
    return rec


@app.delete("/v1/agent-plan/hold/{obligation_id}")
async def agent_plan_release(obligation_id: str, role: str = "finance_manager", by: str = "dashboard"):
    from . import priority_sop

    _writer(role, by)
    await asyncio.to_thread(priority_sop.release, app.state.org, obligation_id, f"{by} ({role})")
    await asyncio.to_thread(app.state.audit.append, "plan_hold_released", {"obligation_id": obligation_id, "by": by})
    return {"released": obligation_id}


class PlanRejectIn(BaseModel):
    obligation_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=3, max_length=500)


class PlanPendingIn(BaseModel):
    obligation_id: str = Field(min_length=1, max_length=80)
    until: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="the new date: the payment is not paid before it")
    note: str = Field("", max_length=300)


async def _obligation(oid: str):
    state, _ = await _todays_state()
    o = next((x for x in (state.obligations if state else []) if x.obligation_id == oid), None)
    if o is None:
        raise HTTPException(404, f"{oid} is not in today's plan")
    return o


@app.post("/v1/agent-plan/reject")
async def agent_plan_reject(body: PlanRejectIn, role: str = "finance_manager", by: str = "dashboard"):
    """Cancel one payment of the plan, with the reason: its request is rejected (a salary line is held as rejected)."""
    from . import agent_today, priority_sop

    who = _writer(role, by)
    o = await _obligation(body.obligation_id)
    ref = agent_today.intake_ref(o)
    if ref:
        req = next((r for r in await asyncio.to_thread(app.state.intake.store.list, 1000) if r.intake_id.startswith(ref)), None)
        if req is None:
            raise HTTPException(404, "the request behind this payment is not found")
        await review_intake(req.intake_id, ReviewRequest(decision="reject", reviewer=by, role=role, note=body.reason))
    else:
        await asyncio.to_thread(priority_sop.hold, app.state.org, body.obligation_id, who, f"rejected: {body.reason}", None, True)
    await asyncio.to_thread(app.state.audit.append, "plan_rejected", {"obligation_id": body.obligation_id, "reason": body.reason, "by": who})
    return {"rejected": body.obligation_id}


@app.post("/v1/agent-plan/pending")
async def agent_plan_pending(body: PlanPendingIn, role: str = "finance_manager", by: str = "dashboard"):
    """Put one payment pending to a new date: it is not paid before that date, then it comes back into the plan."""
    from . import priority_sop

    who = _writer(role, by)
    if body.until <= datetime.now(UTC).date().isoformat():
        raise HTTPException(422, "the new date must be after today")
    await _obligation(body.obligation_id)
    rec = await asyncio.to_thread(priority_sop.hold, app.state.org, body.obligation_id, who, body.note or f"pending to {body.until}", body.until)
    await asyncio.to_thread(app.state.audit.append, "plan_pending", {"obligation_id": body.obligation_id, "until": body.until, "note": body.note, "by": who})
    return rec


class VendorNoticeIn(BaseModel):
    obligation_id: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=3, max_length=2000)


@app.get("/v1/agent-plan/payment/{obligation_id}")
async def agent_plan_payment(obligation_id: str):
    """One payment in full: the request, today's decision and why, the vendor's registered mailbox, and its whole log."""
    from . import agent_today

    log = await asyncio.to_thread(agent_today.log_for, app.state, obligation_id=obligation_id)
    state, _ = await _todays_state()
    o = next((x for x in (state.obligations if state else []) if x.obligation_id == obligation_id), None)
    payee = (app.state.intake.registry.payees.get(o.beneficiary_id) if o else None) or {}
    req = log.get("request") or {}
    contacts = payee.get("contacts") or ([req["sender"]] if req.get("sender") else [])
    from . import outbox

    why = next((e["detail"] for e in reversed(log["events"]) if e["kind"] == "decision"), None)
    return log | {"obligation": o.model_dump() if o else None, "vendor": {"name": payee.get("name") or log.get("payee"), "contacts": contacts},
                  "findings": agent_today.findings(req, why), "outbox": outbox.items(app.state.org, obligation_id),
                  "email": {"configured": outbox.smtp(app.state.org)["configured"]}}


@app.post("/v1/agent-plan/notify-vendor")
async def agent_plan_notify_vendor(body: VendorNoticeIn, role: str = "finance_manager", by: str = "dashboard"):
    """A notice to the vendor about its payment, written by a person: recorded on the payment's log; VENTHORA prepares
    the email to the vendor's registered mailbox, the person sends it from their own mail program."""
    who = _writer(role, by)
    d = await agent_plan_payment(body.obligation_id)
    to = d["vendor"]["contacts"]
    if not to:
        raise HTTPException(409, "the vendor has no registered mailbox")
    from urllib.parse import quote

    from . import outbox

    subject = f"Your payment {body.obligation_id}" + (f" (invoice {d['request']['invoice_no']})" if (d.get("request") or {}).get("invoice_no") else "")
    rec = await asyncio.to_thread(outbox.queue, app.state.org, to=to, subject=subject, body=body.message, ref=body.obligation_id, by=who)
    rec = await asyncio.to_thread(outbox.deliver, app.state.org, rec["_id"])
    await asyncio.to_thread(app.state.audit.append, "vendor_notified", {"obligation_id": body.obligation_id, "to": to, "message": body.message, "by": who,
                                                                        "outbox_id": rec["_id"], "status": rec["status"]})
    return {"to": to, "subject": subject, "status": rec["status"], "error": rec.get("error"), "outbox_id": rec["_id"],
            "mailto": f"mailto:{','.join(to)}?subject={quote(subject)}&body={quote(body.message)}"}


class SmtpIn(BaseModel):
    provider: str = Field("", max_length=20, description="a mail provider preset (gmail, outlook, yahoo, ...): VENTHORA then fills host, port, security and user")
    host: str = Field("", max_length=200, description="needed only for provider 'other' or none")
    port: int = Field(587, ge=1, le=65535)
    user: str = Field("", max_length=200)
    password: str | None = Field(None, max_length=300, description="write-only; empty keeps the stored one")
    sender: str = Field(min_length=3, max_length=200)
    security: str = Field("starttls", pattern="^(starttls|ssl|none)$")
    password_from_inbox: str | None = Field(None, max_length=41, description="use the saved app password of this inbox (same address)")


@app.get("/v1/outbox")
async def outbox_list():
    """The outbox: every email VENTHORA prepared for someone outside, its status (queued, sent, failed), and whether
    outgoing email is set up (the password is never returned)."""
    from . import outbox

    return {"smtp": outbox.smtp(app.state.org), "items": outbox.items(app.state.org)[:200]}


@app.put("/v1/outbox/smtp")
async def outbox_smtp(body: SmtpIn, role: str = "super_admin", by: str = "dashboard"):
    from . import outbox

    who = _setup_writer(role, by)
    data = body.model_dump(exclude={"password_from_inbox"})
    if body.password_from_inbox and not body.password:  # the inbox VENTHORA reads is also the sender: its app password, kept server-side
        from .email_settings import EmailStore

        raw = await asyncio.to_thread(EmailStore().raw, body.password_from_inbox)
        if raw is None or raw["address"].lower() != body.sender.lower() or not raw["_secret"]:
            raise HTTPException(422, "no saved app password for this address yet: type it once")
        data["password"] = raw["_secret"]
    try:
        s = await asyncio.to_thread(outbox.save_smtp, app.state.org, data, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "outbox_smtp", {"host": s["host"], "sender": s["sender"], "by": who})
    return s


@app.post("/v1/outbox/smtp/try")
async def outbox_smtp_try(body: SmtpIn, role: str = "super_admin", by: str = "dashboard"):
    """Test connect for the sender before it is kept: sign in to the SMTP server with what the form holds (a blank
    password: the saved one, or the inbox's when it is the same address). Nothing is saved or sent."""
    from . import outbox

    _setup_writer(role, by)
    pw = body.password
    if not pw and body.password_from_inbox:
        from .email_settings import EmailStore

        raw = await asyncio.to_thread(EmailStore().raw, body.password_from_inbox)
        if raw is None or raw["address"].lower() != body.sender.lower() or not raw["_secret"]:
            raise HTTPException(422, "no saved app password for this address yet: type it once")
        pw = raw["_secret"]
    try:
        return await asyncio.to_thread(outbox.try_smtp, body.model_dump(exclude={"password_from_inbox"}) | {"password": pw})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/v1/outbox/smtp/check")
async def outbox_smtp_check(role: str = "super_admin", by: str = "dashboard"):
    """Connect and log in to the SMTP server; nothing is sent."""
    from . import outbox

    _connector(role, by)
    return await asyncio.to_thread(outbox.check, app.state.org)


@app.post("/v1/outbox/{msg_id}/send")
async def outbox_send(msg_id: str, role: str = "finance_manager", by: str = "dashboard"):
    """Send a queued or failed message again (when outgoing email is set up)."""
    from . import outbox

    _writer(role, by)
    try:
        return await asyncio.to_thread(outbox.deliver, app.state.org, msg_id)
    except KeyError as exc:
        raise HTTPException(404, "no such message") from exc


@app.get("/v1/transaction-log")
async def transaction_log(obligation_id: str | None = None, intake_id: str | None = None):
    """Every payment's story, step by step with date and time: received, checked, valid, every plan decision, every
    person's intervention, the conversion and its rate, the transfer, done. One payment when an id is given."""
    from . import agent_today

    if obligation_id or intake_id:
        return await asyncio.to_thread(agent_today.log_for, app.state, obligation_id=obligation_id, intake_id=intake_id)
    return await asyncio.to_thread(agent_today.log_index, app.state)


@app.get("/v1/notifications")
async def notifications_list(status: str | None = Query("open", pattern="^(open|closed|all)$")):
    """Notices for the FCO from the Standard Payment Priority Policy (balance short, a planned payment not made, ...)."""
    from . import priority_sop

    return priority_sop.notices(app.state.org, None if status == "all" else status)


@app.post("/v1/notifications/{key}/close")
async def notifications_close(key: str, role: str = "super_admin", by: str = "dashboard"):
    _writer(role, by)
    rec = app.state.org.get("notification", key)
    if rec is None:
        raise HTTPException(404, "no such notice")
    app.state.org._put("notification", rec | {"status": "closed", "closed_by": f"{by} ({role})"}, by)
    return {"closed": key}


@app.post("/v1/agent-plan/policy-run")
async def agent_plan_policy_run(role: str = "super_admin", by: str = "dashboard"):
    """Run the Standard Payment Priority Policy now (the scheduler runs it every five minutes)."""
    _writer(role, by)
    return await _policy_run()


class PromptSystemIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    notes: str = Field("", max_length=4000)


@app.get("/v1/prompt-system")
async def prompt_system_get():
    """The Generic Prompting System: the FCO's generic prompt and notes, the rules compiled from what is in force, the
    full text the model receives, and when it is sent."""
    from . import prompt_system

    org = app.state.org
    rec = prompt_system.saved(org)
    model = active_model()
    return {"prompt": rec.get("prompt") or prompt_system.DEFAULT_PROMPT, "notes": rec.get("notes", ""), "saved": bool(rec),
            "updated_by": rec.get("updated_by"), "updated_at": rec.get("updated_at"), "default_prompt": prompt_system.DEFAULT_PROMPT,
            "sections": prompt_system.sections(org), "compiled": prompt_system.compile(org), "when": prompt_system.WHEN,
            "sent": dict(prompt_system.SENT),
            "model": {"configured": bool(getattr(model, "configured", False)), "name": getattr(model, "model", None) or "none (rules only)"}}


@app.put("/v1/prompt-system")
async def prompt_system_put(body: PromptSystemIn, role: str = "super_admin", by: str = "dashboard"):
    from . import prompt_system

    who = _writer(role, by)
    try:
        rec = await asyncio.to_thread(prompt_system.save, app.state.org, body.prompt, body.notes, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "prompt_system_saved", {"by": who, "chars": len(rec["prompt"]), "notes_chars": len(rec["notes"])})
    return await prompt_system_get()


# ---- Platform Setup -> Airwallex Cards / Card Authorization: card-billed vendors under the mandate ----------------
class CardholderIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    email: str = Field(min_length=3, max_length=200)
    date_of_birth: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    mobile_number: str | None = Field(None, max_length=30)
    address: dict = Field(description="line1, city, postcode, country (2 letters), state")


class CardIn(BaseModel):
    payee_id: str = Field(min_length=1, max_length=80)
    per_transaction_minor: int = Field(gt=0)
    monthly_minor: int = Field(gt=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    merchant: str = Field("", max_length=120, description="the vendor's merchant name the card is locked to (empty: the payee's name)")
    merchant_category_codes: list[str] = Field(default_factory=list, max_length=20)
    purpose: str = Field("SUBSCRIPTIONS", max_length=40)


class CardChargeIn(BaseModel):
    amount_minor: int = Field(gt=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    merchant: str = Field("", max_length=120)
    merchant_category_code: str = Field("5734", max_length=4, description="5734: computer software stores")
    capture: bool = True
    channel: str = Field("online", pattern="^(online|card_number|contactless|qr)$",
                         description="how the person pays: online, the card number typed in, a tap at a terminal, a QR scan")


class RemoteAuthIn(BaseModel):
    url: str = Field(pattern=r"^https://", max_length=300)
    default_action: str = Field("DECLINED", pattern="^(AUTHORIZED|DECLINED)$")


class SecretIn(BaseModel):
    shared_secret: str = Field(min_length=8, max_length=500)


class FreezeIn(BaseModel):
    frozen: bool


async def _remote_auth_url() -> str | None:
    """Where Airwallex can reach the card decision: the gateway that already receives Airwallex webhooks."""
    from urllib.parse import urlparse

    from .webhook_gateway import REMOTE_AUTH_PATH

    try:
        hooks = await _bank().list_webhooks()
    except Exception:  # noqa: BLE001 - no bank or no webhook: the person types the address
        return None
    url = next((h.get("url") for h in hooks if h.get("url")), None)
    return f"https://{urlparse(url).netloc}{REMOTE_AUTH_PATH}" if url else None


def _card_program_view(org) -> dict:
    """The card program, the FCO's card limit as saved (no limit shown as None), and each staff card's open window."""
    from . import card_program, cards, mandate

    book = mandate.standing(org).get("book") or {}
    cmax = book.get("card_max_minor", mandate.BOOK_DEFAULTS["card_max_minor"])
    staff = {}
    for c in org.list("card"):
        if c.get("kind") == "staff":
            staff[c["_id"]] = card_program.state(c, lambda since, cid=c["_id"]: cards._spent_since(org, cid, since))
    users = [u for u in org.list("user") if u.get("active", True)]
    return card_program.public(card_program.get(org)) | {
        "card_max_minor": None if cmax is None or cmax >= card_program.NO_LIMIT else cmax, "staff_state": staff,
        "staff_total": len(users), "staff_without_card": sum(1 for u in users if not card_program.staff_card(org, u["user_id"]))}


def _cardholder_default(org) -> dict:
    """The company cardholder as the company already knows it: its Finance Director (the director in the Airwallex
    registration, else the Super admin) with the registered address, so creating the cardholder is one click."""
    from . import airwallex_onboarding as ob

    prof, fco, hq = org.get(*ob.PROFILE) or {}, ob._fco(org), ob._hq(org)
    first, _, last = (fco.get("name") or "").strip().partition(" ")
    return {"first_name": prof.get("director_first_name") or first, "last_name": prof.get("director_last_name") or last or first,
            "email": fco.get("email") or prof.get("contact_email") or "", "date_of_birth": prof.get("director_date_of_birth") or "",
            "address": {"line1": prof.get("address_line1") or "", "city": prof.get("suburb") or "", "postcode": prof.get("postcode") or "",
                        "country": prof.get("country_code") or hq.get("country") or ""}}


@app.get("/v1/cards")
async def cards_list():
    """Airwallex Cards: the issuing configuration, the company cardholder, VENTHORA's cards (live status from Airwallex),
    the card-billed vendors to choose from, and the mandate ceiling every card limit stays under."""
    from . import cards

    org = app.state.org
    out = {"cardholder": org.get("issuing", "cardholder"), "cards": org.list("card"), "mandate_ceiling_minor": cards.mandate_ceiling(org),
           "purposes": list(cards.PURPOSES), "payees": [{"id": k, "name": v.get("name"), "currency": v.get("currency"), "kind": v.get("kind")}
                                                       for k, v in app.state.intake.registry.payees.items() if v.get("kind") != "employee"],
           "cardholder_default": _cardholder_default(org),
           "program": _card_program_view(org), "card_limit_minor": _card_limit(org),
           # on the SaaS platform Airwallex asks the platform, which checks the signature and asks this company (its token)
           "platform_remote_auth": bool(env("VENTHORA_PLATFORM_TOKEN")),
           "config": None, "error": None}
    try:
        bank = _bank()
        cfg = await bank.issuing_config()
        live = {c.get("card_id"): c for c in await bank.list_cards()}
        out["config"] = {"primary_currency": cfg.get("primary_currency"), "enable_auto_conversion": cfg.get("enable_auto_conversion"),
                         "per_transaction_limits": (cfg.get("spending_limit_settings") or {}).get("per_transaction_limits", []),
                         "remote_auth": cfg.get("remote_auth_settings")}
        for c in out["cards"]:
            if c["_id"] in live:
                c["status"] = live[c["_id"]].get("card_status") or c.get("status")
        out["airwallex_cards"] = len(live)
    except HTTPException as exc:
        out["error"] = exc.detail
    except AirwallexError as exc:
        out["error"] = f"Airwallex request failed: {exc}"
    return out


@app.post("/v1/cards/cardholder")
async def cards_cardholder(body: CardholderIn, role: str = "finance_manager", by: str = "dashboard"):
    """The company's cardholder (a DELEGATE: the person who answers for VENTHORA's cards), created once on Airwallex."""
    from . import cards

    who = _connector(role, by)
    try:
        resp = await _bank().create_cardholder(cards.cardholder_body(body.model_dump()))
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex refused the cardholder: {exc}") from exc
    rec = {"_id": "cardholder", "cardholder_id": resp.get("cardholder_id"), "status": resp.get("status"), "type": resp.get("type"),
           "name": f"{body.first_name} {body.last_name}", "email": body.email, "created_by": who}
    app.state.org._put("issuing", rec, who)
    await asyncio.to_thread(app.state.audit.append, "issuing_cardholder", {k: rec[k] for k in ("cardholder_id", "status", "type", "created_by")})
    return rec


@app.post("/v1/cards")
async def cards_issue(body: CardIn, role: str = "finance_manager", by: str = "dashboard"):
    """Issue a Visa virtual card for one card-billed vendor, its limits inside the mandate."""
    from . import cards

    who = _writer(role, by)
    org = app.state.org
    holder = org.get("issuing", "cardholder")
    if not holder or not holder.get("cardholder_id"):
        raise HTTPException(409, "create the company cardholder first")
    payee = app.state.intake.registry.payees.get(body.payee_id)
    if payee is None:
        raise HTTPException(404, "unknown payee")
    try:
        cards.check_limits(org, body.per_transaction_minor, body.monthly_minor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    merchant = body.merchant.strip() or payee["name"]
    req = cards.card_body(cardholder_id=holder["cardholder_id"], payee={"_id": body.payee_id, "name": payee["name"]},
                          per_tx_minor=body.per_transaction_minor, monthly_minor=body.monthly_minor, currency=body.currency.upper(), by=who,
                          purpose=body.purpose, mcc=body.merchant_category_codes)
    try:
        resp = await _bank().create_card(req)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex refused the card: {exc}") from exc
    rec = await asyncio.to_thread(cards.record_card, org, resp, payee={"_id": body.payee_id, "name": payee["name"]}, per_tx_minor=body.per_transaction_minor,
                                  monthly_minor=body.monthly_minor, currency=body.currency.upper(), merchant=merchant, mcc=body.merchant_category_codes, by=who)
    await asyncio.to_thread(app.state.audit.append, "card_issued", {k: rec.get(k) for k in ("_id", "payee_id", "per_tx_minor", "monthly_minor", "currency", "brand", "issued_by")})
    return rec


def _card_limit(org) -> int:
    """What one card charge may be: the FCO's card limit, inside the mandate's single payout limit."""
    from . import cards, mandate

    cmax = (mandate.standing(org).get("book") or {}).get("card_max_minor", mandate.BOOK_DEFAULTS["card_max_minor"])
    ceiling = cards.mandate_ceiling(org) or 0
    return min(x for x in (cmax, ceiling) if x is not None) if cmax is not None else ceiling


@app.get("/v1/cards/program")
async def cards_program_get():
    from . import card_program

    return card_program.public(card_program.get(app.state.org))


@app.put("/v1/cards/program")
async def cards_program_put(body: Annotated[dict, Body()], role: str = "super_admin", by: str = "dashboard"):
    """The card program (purposes, staff cards, activation window, merchants): the Finance Director's choice."""
    from . import card_program

    from . import mandate

    who = _setup_writer(role, by)
    org = app.state.org
    if "card_max_minor" in body:  # the card limit per charge is the mandate's "pay by virtual card up to" (one source)
        v = body.pop("card_max_minor")
        v = card_program.NO_LIMIT if v is None else int(v)
        if not 0 <= v <= card_program.NO_LIMIT:
            raise HTTPException(422, "the card limit is 0 or more")
        st = mandate.standing(org)
        req = {"values": st["values"], "enabled": st["enabled"], "book": st["book"] | {"card_max_minor": v}, "checks": st["checks"]}
        setup = live_session.defaults("demo_live", app.state.executor, st)
        try:
            try:
                await asyncio.to_thread(mandate.save, org, req, who, role=role, setup=setup)
            except mandate.NeedsAcknowledgement as exc:  # the Finance Director chose this limit here, on purpose
                await asyncio.to_thread(mandate.save, org, req | {"acknowledged": [w["key"] for w in exc.warnings]}, who, role=role, setup=setup)
        except mandate.AuthorityError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    try:
        out = await asyncio.to_thread(card_program.save, org, body, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "card_program_saved", {"by": who} | out)
    return _card_program_view(org)


@app.post("/v1/cards/staff")
async def cards_issue_staff(role: str = "super_admin", by: str = "dashboard"):
    """Each staff has a virtual card: one Visa virtual card per active person who has none yet, labelled with the
    employee number (Airwallex, the issuer, sets the card number), kept inactive until an approved advance opens it."""
    from . import card_program, cards

    who = _setup_writer(role, by)
    org = app.state.org
    prog = card_program.get(org)
    if not prog["staff_cards"]:
        raise HTTPException(409, "tick \"Each staff has a virtual card\" and save the card program first")
    holder = org.get("issuing", "cardholder")
    if not holder or not holder.get("cardholder_id"):
        raise HTTPException(409, "create the company cardholder first")
    limit = _card_limit(org)
    if limit <= 0:
        raise HTTPException(422, "the card limit is zero: no card may be issued")
    codes = card_program.allowed_codes(prog)
    issued, failed = [], []
    bank = _bank()
    for u in org.list("user"):
        if not u.get("active", True) or card_program.staff_card(org, u["user_id"]):
            continue
        lbl = card_program.label(u)
        payee = {"_id": f"staff:{u['user_id']}", "name": lbl}
        req = cards.card_body(cardholder_id=holder["cardholder_id"], payee=payee, per_tx_minor=limit, monthly_minor=limit, currency="USD", by=who,
                              purpose="BUSINESS_EXPENSES", mcc=sorted(codes) if codes else None, nick_name=f"Staff {lbl}")
        try:
            resp = await bank.create_card(req)
        except AirwallexError as exc:
            failed.append({"user_id": u["user_id"], "name": u.get("name"), "error": str(exc)[:200]})
            continue
        with contextlib.suppress(AirwallexError):  # inactive until an approved advance opens it
            await bank.set_card_status(resp["card_id"], "INACTIVE")
        rec = await asyncio.to_thread(cards.record_card, org, resp, payee=payee, per_tx_minor=limit, monthly_minor=limit, currency="USD",
                                      merchant="", mcc=sorted(codes) if codes else [], by=who)
        rec = rec | {"kind": "staff", "user_id": u["user_id"], "label": lbl, "status": "INACTIVE", "allowances": []}
        await asyncio.to_thread(org._put, "card", rec, who)
        issued.append({"user_id": u["user_id"], "label": lbl, "masked": rec.get("masked")})
    await asyncio.to_thread(app.state.audit.append, "staff_cards_issued", {"by": who, "issued": len(issued), "failed": len(failed)})
    return {"issued": issued, "failed": failed}


async def _staff_card_window(card: dict, who: str) -> dict:
    """Open or close a staff card on Airwallex to match its approved advances (active only inside a window, while
    the advance still holds money)."""
    from . import card_program, cards

    org = app.state.org
    st = card_program.state(card, lambda since: cards._spent_since(org, card["_id"], since))
    want = "ACTIVE" if st["active"] and not card.get("frozen") else "INACTIVE"
    if card.get("status") != want:
        with contextlib.suppress(HTTPException, AirwallexError):
            await _bank().set_card_status(card["_id"], want)
            card = card | {"status": want}
            org._put("card", card, who)
    return card


async def _card_window_worker(every_s: int = 60) -> None:
    """Every minute: each staff card is active only while an approved advance is open on it (auto-activate N days
    after approval, auto-deactivate M days later or at its settle-by date, or once it is spent)."""
    while True:
        await asyncio.sleep(every_s)
        with contextlib.suppress(Exception):
            for c in await asyncio.to_thread(app.state.org.list, "card"):
                if c.get("kind") == "staff":
                    await _staff_card_window(c, "VENTHORA (card window)")


@app.post("/v1/cards/{card_id}/freeze")
async def cards_freeze(card_id: str, body: FreezeIn, role: str = "finance_manager", by: str = "dashboard"):
    """Freeze a card: on Airwallex (card_status INACTIVE, so the network declines it whatever VENTHORA answers) and in
    VENTHORA (every charge declined at remote authorization), until it is unfrozen. If Airwallex cannot be reached the
    card is still frozen in VENTHORA, and the answer says so."""
    who = _writer(role, by)
    rec = app.state.org.get("card", card_id)
    if rec is None:
        raise HTTPException(404, "not a card VENTHORA issued")
    on_airwallex = None
    try:
        res = await _bank().set_card_status(card_id, "INACTIVE" if body.frozen else "ACTIVE")
        on_airwallex = res.get("card_status") or ("INACTIVE" if body.frozen else "ACTIVE")
    except HTTPException as exc:
        on_airwallex = f"not updated: {exc.detail}"
    except AirwallexError as exc:
        on_airwallex = f"not updated: {exc}"
    app.state.org._put("card", rec | {"frozen": body.frozen, "status": on_airwallex if not str(on_airwallex).startswith("not") else rec.get("status")}, who)
    await asyncio.to_thread(app.state.audit.append, "card_frozen" if body.frozen else "card_unfrozen", {"card_id": card_id, "by": who, "airwallex": on_airwallex})
    return rec | {"frozen": body.frozen, "airwallex": on_airwallex}


@app.post("/v1/cards/{card_id}/simulate")
async def cards_simulate(card_id: str, body: CardChargeIn, role: str = "finance_manager", by: str = "dashboard"):
    """Sandbox: the vendor charges the card (and captures it). With remote authorization on, Airwallex asks VENTHORA first."""
    from . import cards

    who = _writer(role, by)
    rec = app.state.org.get("card", card_id)
    if rec is None:
        raise HTTPException(404, "not a card VENTHORA issued")
    bank = _bank()
    if not bank.sandbox:
        raise HTTPException(409, "card charges are simulated on the sandbox only")
    label = {"online": "", "card_number": "[CARD NUMBER] ", "contactless": "[TAP] ", "qr": "[QR] "}[body.channel]
    req = {"card_id": card_id, "transaction_amount": float(cards.major(body.amount_minor)), "transaction_currency": body.currency.upper(),
           "merchant_category_code": body.merchant_category_code, "merchant_info": (label + (body.merchant or rec.get("merchant") or rec["payee"]))[:60]}
    if body.channel == "card_number":
        # the merchant has only the number the person typed in: Airwallex finds the card by it (the number is read
        # from Airwallex for this call only; it is never kept or shown)
        try:
            pan = (await bank.card_details(card_id)).get("card_number")
        except AirwallexError:
            # Airwallex never shows a connected account's card number through the API (the person reads it in the card
            # app); the sandbox charge then goes by the card's id, labelled as typed in
            pan = None
        if pan:
            req = {k: v for k, v in req.items() if k != "card_id"} | {"card_number": str(pan).replace(" ", "")}
    if body.capture:
        req["single_phase"] = True
    try:
        res = await bank.simulate_card_authorization(req)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex refused the simulated charge: {exc}") from exc
    await asyncio.to_thread(app.state.audit.append, "card_charge_simulated", {"card_id": card_id, "amount_minor": body.amount_minor, "currency": body.currency,
                                                                              "status": res.get("status"), "channel": body.channel, "by": who})
    return res | {"channel": body.channel}


# ---- Demo-Only → phone pay simulator (paysim.py): a cashier phone shows a QR, a payer phone pays with an advance card ----
@app.get("/v1/demo/pay-sim/link")
async def paysim_link(new: bool = False, role: str = "super_admin", by: str = "dashboard"):
    """The cashier's link (with its key) to open on a phone, from Sandbox simulator."""
    from . import paysim, sourcing

    if role not in ("super_admin", "developer", "finance_manager"):
        raise HTTPException(403, "the FCO, a Developer or a finance manager opens the cashier")
    key = await asyncio.to_thread(paysim.cashier_key, app.state.org, f"{by} ({role})", new)
    return {"cashier_url": f"{sourcing.public_url()}/pay-sim/cashier?k={key}", "cards": await asyncio.to_thread(paysim.advance_cards, app.state.org)}


@app.get("/pay-sim/cashier", include_in_schema=False)
async def paysim_cashier_page():
    return FileResponse(DASHBOARD.parent / "pay_cashier.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/pay-sim/p/{token}", include_in_schema=False)
async def paysim_payer_page(token: str):
    return FileResponse(DASHBOARD.parent / "pay_payer.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


class PaySimRequestIn(BaseModel):
    merchant: str = Field(min_length=1, max_length=40)
    amount_minor: int = Field(gt=0, le=10_000_000)
    currency: str = Field("USD", pattern="^(USD|SGD|EUR|GBP|AUD|HKD)$", description="currencies the sandbox simulation accepts")
    mcc: str = Field("5812", pattern=r"^\d{4}$")
    mode: str = Field("qr", pattern="^(qr|contactless|card_number)$")


@app.post("/pay-sim/api/requests", include_in_schema=False)
async def paysim_new(body: PaySimRequestIn, k: str = ""):
    from . import paysim, sourcing

    if not paysim.key_ok(app.state.org, k):
        raise HTTPException(403, "open the cashier from VENTHORA (Demo-Only → Sandbox simulator)")
    rec = await asyncio.to_thread(paysim.new_request, app.state.org, merchant=body.merchant, amount_minor=body.amount_minor,
                                  currency=body.currency, mcc=body.mcc, mode=body.mode)
    return rec | {"pay_url": f"{sourcing.public_url()}/pay-sim/p/{rec['_id']}"}


@app.get("/pay-sim/api/requests/{token}", include_in_schema=False)
async def paysim_status(token: str, k: str = ""):
    from . import paysim

    if not paysim.key_ok(app.state.org, k):
        raise HTTPException(403, "not the cashier")
    rec = await asyncio.to_thread(paysim.get, app.state.org, token)
    if rec is None:
        raise HTTPException(404, "no such payment")
    return rec


@app.get("/pay-sim/api/p/{token}", include_in_schema=False)
async def paysim_payer_data(token: str):
    from . import paysim

    rec = await asyncio.to_thread(paysim.get, app.state.org, token)
    if rec is None:
        raise HTTPException(404, "this payment link is not valid")
    return {k: rec.get(k) for k in ("merchant", "amount_minor", "currency", "mode", "status", "result")} | {
        "mode_label": paysim.MODES[rec["mode"]], "cards": await asyncio.to_thread(paysim.advance_cards, app.state.org) if rec["status"] == "WAITING" else []}


class PaySimPayIn(BaseModel):
    card_id: str = Field(min_length=1, max_length=80)
    last4: str = Field("", max_length=4)


@app.post("/pay-sim/api/p/{token}/pay", include_in_schema=False)
async def paysim_pay(token: str, body: PaySimPayIn):
    """The payer pays: the charge goes to Airwallex's sandbox simulation on the chosen advance card, once."""
    from . import paysim

    org = app.state.org
    rec = await asyncio.to_thread(paysim.get, org, token)
    if rec is None:
        raise HTTPException(404, "this payment link is not valid")
    if rec["status"] != "WAITING":
        raise HTTPException(409, f"this payment is {rec['status'].lower()}")
    card = next((c for c in await asyncio.to_thread(paysim.advance_cards, org) if c["card_id"] == body.card_id), None)
    if card is None:
        raise HTTPException(422, "choose one of the advance cards")
    if rec["mode"] == "card_number" and body.last4 != card["last4"]:
        raise HTTPException(422, "the card number does not match that card")
    await asyncio.to_thread(org._put, paysim.KIND, rec | {"status": "PAYING"}, "pay-sim payer")
    try:
        res = await cards_simulate(body.card_id, CardChargeIn(amount_minor=rec["amount_minor"], currency=rec["currency"], merchant=rec["merchant"],
                                                              merchant_category_code=rec["mcc"], channel=rec["mode"]), role="finance_manager", by="pay-sim payer")
    except HTTPException as exc:
        out = await asyncio.to_thread(paysim.finish, org, token, {"status": "DECLINED", "reason": str(exc.detail)[:200], "card": card["last4"]})
        return out
    status, reason = "APPROVED", ""
    with contextlib.suppress(Exception):  # Airwallex's own record of the charge decides what both phones show
        await asyncio.sleep(1.5)
        tx = next((t for t in await _bank().card_transactions(body.card_id) if t.get("transaction_id") == res.get("card_transaction_id")), None)
        if tx and str(tx.get("status", "")).upper() not in ("APPROVED", "CLEARED", "SETTLED", "PENDING", "AUTHORIZED"):
            status, reason = "DECLINED", tx.get("failure_reason") or tx.get("status")
        if status == "APPROVED" and tx and tx.get("billing_currency") and tx.get("billing_currency") != tx.get("transaction_currency"):
            reason = f"billed {tx['billing_currency']} {abs(float(tx.get('billing_amount') or 0)):,.2f}"
    return await asyncio.to_thread(paysim.finish, org, token, {"status": status, "reason": reason, "card": card["last4"], "request": card["request"],
                                                               "transaction_id": res.get("card_transaction_id")})


# ---- the card-issuing account: Airwallex Issuing is enabled per account and country, so cards may live on a
# connected account the platform opened (e.g. a Malaysia account) while transfers and balances stay where they are ----
@app.get("/v1/cards/issuing-account")
async def cards_issuing_account_get():
    from .airwallex_settings import AirwallexStore

    return {"issuing_account": await asyncio.to_thread(AirwallexStore().card_account)}


class IssuingAccountIn(BaseModel):
    account_id: str | None = Field(None, max_length=80, description="an ACTIVE account this platform opened; empty: cards on the main account")


@app.put("/v1/cards/issuing-account")
async def cards_issuing_account_put(body: IssuingAccountIn, role: str = "super_admin", by: str = "dashboard"):
    from .airwallex_settings import AirwallexStore

    who = _connector(role, by)
    name = ""
    if body.account_id:
        try:
            acct = next((_account_row(a) for a in await _bank().list_accounts() if a.get("id") == body.account_id), None)
        except AirwallexError as exc:
            raise HTTPException(502, f"Airwallex: {exc}") from exc
        if acct is None or acct["status"] != "ACTIVE":
            raise HTTPException(422, "choose an ACTIVE account this platform opened (Platform Setup → Airwallex Account)")
        name = f"{acct['name']} ({acct['country']})"
    await asyncio.to_thread(AirwallexStore().set_card_account, body.account_id, name, who)
    await asyncio.to_thread(app.state.audit.append, "card_issuing_account", {"account_id": body.account_id, "name": name, "by": who})
    return {"issuing_account": await asyncio.to_thread(AirwallexStore().card_account)}


# ---- Xendit: the Indonesian rails Airwallex lacks (e-wallet payouts, the holder's name of an Indonesian account).
# Indonesian bank accounts are still paid through Airwallex. gateways/xendit.py, wallet_payouts.py ----
def _xendit_or_none():
    from . import wallet_payouts
    from .gateways.xendit import XenditError

    from . import local_rails

    if not local_rails.enabled(app.state.org, "xendit", "name_check"):  # turned off in Platform Setup -> Local Rails
        return None
    try:
        x = wallet_payouts.client(app.state.org)
    except XenditError:
        return None
    return None if x.mode == "off" else x


def _xendit():
    from . import wallet_payouts
    from .gateways.xendit import XenditError

    try:
        return wallet_payouts.client(app.state.org)
    except XenditError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/v1/providers/xendit")
async def xendit_get():
    """Xendit's mode, whether a key and callback token are set (never their values), the balance and the channels."""
    from . import wallet_payouts
    from .gateways.xendit import XenditError

    out = await asyncio.to_thread(wallet_payouts.settings, app.state.org)
    out["why"] = ("Airwallex pays Indonesian bank accounts but not e-wallets (its wallet payouts are Airwallex and PayPal only) "
                  "and has no name check for Indonesian accounts: Xendit covers those two, nothing else")
    out["balance_minor"], out["channels"], out["error"] = None, [], None
    if out["effective_mode"] != "off":
        try:
            x = _xendit()
            out["balance_minor"] = await x.balance()
            out["channels"] = await x.channels()
        except (XenditError, HTTPException) as exc:
            out["error"] = getattr(exc, "detail", None) or str(exc)
    return out


class XenditSettingsIn(BaseModel):
    mode: str = Field(pattern="^(off|simulator|live)$")
    secret_key: str | None = Field(None, max_length=200, description="write-only; a test-mode key (xnd_development_...)")
    callback_token: str | None = Field(None, max_length=200, description="write-only; Xendit's webhook verification token")


@app.put("/v1/providers/xendit")
async def xendit_put(body: XenditSettingsIn, role: str = "super_admin", by: str = "dashboard"):
    from . import wallet_payouts

    who = _setup_writer(role, by)
    try:
        out = await asyncio.to_thread(wallet_payouts.save_settings, app.state.org, body.mode, who, body.secret_key, body.callback_token)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "xendit_settings", {"mode": body.mode, "by": who,  # never the key itself
                                                                        "key_changed": bool(body.secret_key), "token_changed": bool(body.callback_token)})
    return out


# ---- Local Rails (Platform Setup): what Airwallex, Visa and Alta do not cover in Indonesia; Xendit and Midtrans.
# local_rails.py ----
async def _local_rails_view() -> dict:
    from . import local_rails

    x = await xendit_get()
    return await asyncio.to_thread(local_rails.view, app.state.org, x)


@app.get("/v1/local-rails")
async def local_rails_get():
    """The gaps, the two providers with their connection state (never a key) and the products chosen."""
    return await _local_rails_view()


@app.put("/v1/local-rails/products")
async def local_rails_products(body: Annotated[dict, Body()], role: str = "super_admin", by: str = "dashboard"):
    """The Finance Director turns each local product on or off ({"xendit": {"ewallet_payout": true}, ...})."""
    from . import local_rails

    who = _setup_writer(role, by)
    try:
        out = await asyncio.to_thread(local_rails.save_products, app.state.org, body, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "local_rails_products", {"by": who} | out)
    return await _local_rails_view()


class MidtransSettingsIn(BaseModel):
    mode: str = Field(pattern="^(off|sandbox)$")
    merchant_id: str | None = Field(None, max_length=40)
    server_key: str | None = Field(None, max_length=200, description="write-only; a sandbox key (SB-Mid-server-...)")
    client_key: str | None = Field(None, max_length=200, description="write-only; a sandbox key (SB-Mid-client-...)")


@app.get("/v1/providers/midtrans")
async def midtrans_get():
    from . import local_rails

    return await asyncio.to_thread(local_rails.midtrans_settings, app.state.org)


@app.put("/v1/providers/midtrans")
async def midtrans_put(body: MidtransSettingsIn, role: str = "super_admin", by: str = "dashboard"):
    from . import local_rails

    who = _setup_writer(role, by)
    try:
        out = await asyncio.to_thread(local_rails.save_midtrans, app.state.org, body.mode, who, body.merchant_id, body.server_key, body.client_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "midtrans_settings", {"mode": body.mode, "by": who,  # never the keys
                                                                          "server_key_changed": bool(body.server_key), "client_key_changed": bool(body.client_key)})
    return out


@app.get("/v1/providers/{provider}/test")
async def provider_test(provider: str):
    """Test a local provider's connection with its saved keys: nothing is created or paid."""
    from . import local_rails
    from .gateways.xendit import XenditError

    if provider == "midtrans":
        return await asyncio.to_thread(local_rails.midtrans_test)
    if provider != "xendit":
        raise HTTPException(404, "unknown provider")
    try:
        x = _xendit()
        if x.mode == "off":
            return {"ok": False, "detail": "Xendit is off: choose simulator or live first"}
        bal = await x.balance()
    except (XenditError, HTTPException) as exc:
        return {"ok": False, "detail": getattr(exc, "detail", None) or str(exc)}
    sim = " (simulator: no money exists)" if x.mode == "simulator" else ""
    return {"ok": True, "detail": f"Xendit answered{sim}: balance IDR {bal:,}"}


@app.post("/webhooks/midtrans", include_in_schema=True)
async def midtrans_webhook(request: Request):
    """Midtrans' payment notification, verified by its signature_key (SHA-512 with the server key) and recorded."""
    from . import local_rails

    try:
        n = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "not JSON") from exc
    if not await asyncio.to_thread(local_rails.midtrans_signature_ok, n):
        raise HTTPException(401, "signature missing or wrong")
    await asyncio.to_thread(app.state.audit.append, "midtrans_notification", {k: n.get(k) for k in (
        "order_id", "transaction_status", "payment_type", "gross_amount", "currency", "transaction_time")})
    return {"received": True}


class XenditTopupIn(BaseModel):
    amount_minor: int = Field(gt=0, le=10_000_000_000)


@app.post("/v1/demo/xendit/topup")
async def xendit_sim_topup(body: XenditTopupIn, role: str = "super_admin", by: str = "dashboard"):
    """Demo only: put rupiah on the simulator's Xendit balance (on live, the balance is topped up by an Airwallex IDR
    transfer to the business's Xendit top-up account)."""
    who = _connector(role, by)
    x = _xendit()
    if not x.simulated:
        raise HTTPException(409, "the top-up is for the simulator; a live Xendit balance is topped up by a bank transfer")
    bal = await asyncio.to_thread(x.sim_topup, body.amount_minor)
    await asyncio.to_thread(app.state.audit.append, "xendit_sim_topup", {"amount_minor": body.amount_minor, "by": who})
    return {"balance_minor": bal, "simulated": True}


class WalletPayoutIn(BaseModel):
    channel_code: str = Field(max_length=30, description="ID_OVO, ID_DANA, ID_GOPAY, ID_SHOPEEPAY or ID_LINKAJA")
    account_number: str = Field(max_length=30, description="the holder's registered mobile number")
    account_holder_name: str = Field(max_length=140)
    amount_minor: int = Field(gt=0, description="whole rupiah")
    purpose: str = Field("", max_length=200)
    vendor_id: str | None = Field(None, max_length=80)
    budget_line: str | None = Field(None, max_length=80)
    fund_request: str | None = Field(None, max_length=80)


@app.get("/v1/wallet-payouts")
async def wallet_payouts_list():
    from . import wallet_payouts

    return [wallet_payouts.public(r) for r in await asyncio.to_thread(wallet_payouts.listing, app.state.org)]


@app.post("/v1/wallet-payouts")
async def wallet_payouts_create(body: WalletPayoutIn, role: str = "finance_manager", by: str = "dashboard"):
    """Ask for an e-wallet payout: the holder's name is checked first; a second person approves before anything is sent."""
    from . import wallet_payouts

    from . import local_rails

    who = _writer(role, by)
    if not local_rails.enabled(app.state.org, "xendit", "ewallet_payout"):
        raise HTTPException(409, "e-wallet payouts are turned off in Platform Setup -> Local Rails (Xendit)")
    try:
        rec = await wallet_payouts.create(app.state.org, _xendit(), body.model_dump(), who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "wallet_payout_requested", {
        "payout": rec["payout_number"], "channel": rec["channel_code"], "amount_minor": rec["amount_minor"], "status": rec["status"], "by": who})
    return wallet_payouts.public(rec)


async def _wallet_step(pid: str, fn, event: str, who: str):
    from . import wallet_payouts
    from .gateways.xendit import XenditError

    try:
        rec = await fn()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except XenditError as exc:
        raise HTTPException(502, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, event, {"payout": pid, "status": rec["status"], "by": who,
                                                            "xendit_id": (rec.get("xendit") or {}).get("id")})
    return wallet_payouts.public(rec)


class CallbackIn(BaseModel):
    note: str = Field("", max_length=200)


@app.post("/v1/wallet-payouts/{pid}/confirm-holder")
async def wallet_payouts_confirm(pid: str, body: CallbackIn | None = None, role: str = "finance_manager", by: str = "dashboard"):
    """The holder confirmed on a call-back to the registered number (when no name check was available)."""
    from . import wallet_payouts

    who = _writer(role, by)
    return await _wallet_step(pid, lambda: asyncio.to_thread(wallet_payouts.confirm_holder, app.state.org, pid, who, (body.note if body else "")),
                              "wallet_payout_callback", who)


@app.post("/v1/wallet-payouts/{pid}/approve")
async def wallet_payouts_approve(pid: str, role: str = "super_admin", by: str = "dashboard"):
    """The second person approves; the payout goes to Xendit at once, with its Idempotency-key kept first."""
    from . import wallet_payouts

    who = _writer(role, by)
    return await _wallet_step(pid, lambda: wallet_payouts.approve_and_send(app.state.org, _xendit(), pid, role, by, who), "wallet_payout_approved", who)


@app.post("/v1/wallet-payouts/{pid}/refresh")
async def wallet_payouts_refresh(pid: str, by: str = "dashboard"):
    """Read the payout back from Xendit: PAID only when Xendit says SUCCEEDED."""
    from . import wallet_payouts

    return await _wallet_step(pid, lambda: wallet_payouts.refresh(app.state.org, _xendit(), pid, by), "wallet_payout_refreshed", by)


@app.post("/v1/wallet-payouts/{pid}/cancel")
async def wallet_payouts_cancel(pid: str, role: str = "finance_manager", by: str = "dashboard"):
    from . import wallet_payouts

    who = _writer(role, by)
    return await _wallet_step(pid, lambda: wallet_payouts.cancel(app.state.org, _xendit(), pid, who), "wallet_payout_cancelled", who)


@app.post("/webhooks/xendit", include_in_schema=True)
async def xendit_webhook(request: Request):
    """Xendit's payout callback, verified by its x-callback-token. The payout is read back from Xendit before its
    status is believed: the callback only says when to look."""
    from . import wallet_payouts
    from .gateways.xendit import XenditError

    if not wallet_payouts.callback_ok(request.headers.get("x-callback-token")):
        raise HTTPException(401, "callback token missing or wrong")
    try:
        ev = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "not JSON") from exc
    data = ev.get("data") or ev
    ref = data.get("reference_id")
    rec = await asyncio.to_thread(wallet_payouts.by_reference, app.state.org, ref) if ref else None
    if rec is None or not (rec.get("xendit") or {}).get("id"):
        return {"received": True, "matched": False}
    try:
        p = await _xendit().get_payout(rec["xendit"]["id"])
    except XenditError:
        return {"received": True, "matched": True, "read_back": False}
    rec = await asyncio.to_thread(wallet_payouts.apply_status, app.state.org, rec, p, "xendit-webhook")
    await asyncio.to_thread(app.state.audit.append, "wallet_payout_webhook", {"payout": ref, "status": rec["status"]})
    return {"received": True, "matched": True, "status": rec["status"]}


ADVANCE_PURPOSE ={"cash_advance": "TRAVEL_EXPENSES", "petty_cash": "OFFICE_SUPPLIES", "odc": "BUSINESS_EXPENSES"}


async def _issue_advance_card(number: str, who: str) -> dict:
    """An approved cash advance, petty cash float or other direct cost paid as a virtual Visa card: the card can spend
    the advance once (whole-life limit), until the settle-by date, and is the person's to use; no money moves until
    they pay with it, and what they do not spend never leaves the wallet."""
    from . import cards
    from .organisation import FUND_KINDS

    org = app.state.org
    fr = org.get("fund_request", number)
    if fr is None:
        raise HTTPException(404, "no such request")
    if fr["status"] != "APPROVED":
        raise HTTPException(409, f"{number} is {fr['status'].lower()}; only an approved request is paid")
    holder = org.get("issuing", "cardholder")
    if not holder or not holder.get("cardholder_id"):
        raise HTTPException(409, "no cardholder yet: create it in Platform Setup → Airwallex Cards")
    try:
        cards.check_limits(org, fr["amount_minor"], fr["amount_minor"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    person = org.get("user", fr["requested_by"]) or {}
    from . import card_program

    prog = card_program.get(org)
    staff = card_program.staff_card(org, fr["requested_by"]) if card_program.applies(prog, fr) else None
    if staff is not None:  # the card program: the advance opens the person's own staff card for its window
        start, until = card_program.window(fr.get("decided_at") or fr.get("approved_at"), fr.get("settle_by"), prog)
        funding = await _fund_card_account(number, fr, who)
        staff = card_program.attach(staff, ref=number, amount_minor=fr["amount_minor"], start=start, until=until, purpose=card_program.purpose_of(fr))
        await asyncio.to_thread(org._put, "card", staff, who)
        staff = await _staff_card_window(staff, who)
        paid = fr | {"status": "IN_PAYMENT", "paid_by": "staff_card", "card_id": staff["_id"], "card_masked": staff.get("masked"),
                     "card_window": {"from": start, "until": until}, "paid_at": datetime.now(UTC).isoformat(timespec="seconds"), "funding": funding}
        await asyncio.to_thread(org._put, "fund_request", paid, who)
        await asyncio.to_thread(app.state.audit.append, "advance_on_staff_card", {"request": number, "card_id": staff["_id"], "from": start, "until": until, "by": who})
        return paid | {"card": {k: staff.get(k) for k in ("_id", "masked", "brand", "status", "label")} | {"window": {"from": start, "until": until}}}
    label = f"{person.get('name') or fr['requested_by']} · {number}"
    payee = {"_id": f"fr:{number}", "name": label}
    is_travel = fr["kind"] == "cash_advance" and any(w in (fr.get("purpose", "") + fr.get("activity", "")).lower() for w in ("travel", "trip", "flight", "hotel", "transport"))
    purpose = "TRAVEL_EXPENSES" if is_travel else ADVANCE_PURPOSE.get(fr["kind"], "BUSINESS_EXPENSES")
    req = cards.card_body(cardholder_id=holder["cardholder_id"], payee=payee, per_tx_minor=fr["amount_minor"], monthly_minor=fr["amount_minor"],
                          currency=fr["currency"], by=who, purpose=purpose, active_to=fr.get("settle_by"), all_time_minor=fr["amount_minor"],
                          nick_name=f"{FUND_KINDS[fr['kind']][0]} {label}")
    funding = await _fund_card_account(number, fr, who)
    try:
        resp = await _bank().create_card(req)
    except AirwallexError as exc:
        if funding and funding.get("transfer_id"):  # no card: the money goes back to the main account
            with contextlib.suppress(Exception):
                await _return_from_card_account(number, fr["amount_minor"], fr["currency"], who)
        raise HTTPException(502, f"Airwallex refused the card: {exc}") from exc
    rec = await asyncio.to_thread(cards.record_card, org, resp, payee=payee, per_tx_minor=fr["amount_minor"], monthly_minor=fr["amount_minor"],
                                  currency=fr["currency"], merchant="", mcc=[], by=who)
    rec = rec | {"fund_request": number, "all_time_minor": fr["amount_minor"], "active_to": fr.get("settle_by"), "purpose": purpose}
    await asyncio.to_thread(org._put, "card", rec, who)
    paid = fr | {"status": "IN_PAYMENT", "paid_by": "virtual_card", "card_id": rec["_id"], "card_masked": rec.get("masked"),
                 "paid_at": datetime.now(UTC).isoformat(timespec="seconds"), "funding": funding}
    await asyncio.to_thread(org._put, "fund_request", paid, who)
    if person.get("email"):
        from . import outbox

        await asyncio.to_thread(outbox.queue, org, to=[person["email"]], subject=f"{FUND_KINDS[fr['kind']][1]} {number}: your virtual card", ref=number, by=who,
                                kind="advance_card",
                                body=(f"Dear {person.get('name', '')},\n\nYour {FUND_KINDS[fr['kind']][1].lower()} {number} ({fr['purpose']}) is ready as a virtual Visa card "
                                      f"ending {str(rec.get('masked') or '')[-4:]}.\n\nIt can spend {fr['currency']} {fr['amount_minor'] / 100:,.2f} in total"
                                      + (f", until {fr['settle_by']}" if fr.get("settle_by") else "") + ". Use it online by its number, or add it to your phone's wallet to "
                                      "tap at a terminal. Keep every receipt: what you do not spend is never taken from the company.\n\n"
                                      "The card number is never sent by email: open it in the card app.\n\nFinance team"))
    await asyncio.to_thread(app.state.audit.append, "advance_card_issued", {"request": number, "card_id": rec["_id"], "amount_minor": fr["amount_minor"],
                                                                          "currency": fr["currency"], "by": who})
    return paid | {"card": {k: rec.get(k) for k in ("_id", "masked", "brand", "status", "purpose", "active_to", "all_time_minor")}}


async def _fund_card_account(number: str, fr: dict, who: str) -> dict | None:
    """The advance's money moves inside Airwallex, from the main account to the card-issuing account, in the advance's
    currency (Connected Account Transfers: no fee, nothing leaves Airwallex), so the card spends it without a
    conversion. None when cards live on the main account itself."""
    from .airwallex_settings import card_account_for

    acct = card_account_for()
    if not acct:
        return None
    try:
        t = await _bank().fund_connected_account(account_id=acct, amount_minor=fr["amount_minor"], currency=fr["currency"],
                                                 reference=f"VENTHORA {number} card funding", request_id=f"fund-{number}-{secrets.token_hex(4)}")
    except AirwallexError as exc:
        raise HTTPException(409, f"the card account could not be funded from the main account ({exc}): top up the main account's "
                                 f"{fr['currency']} balance first") from exc
    out = {"to": acct, "transfer_id": t.get("id"), "status": t.get("status"), "amount_minor": fr["amount_minor"], "currency": fr["currency"],
           "fee": t.get("fee", 0), "at": datetime.now(UTC).isoformat(timespec="seconds")}
    await asyncio.to_thread(app.state.audit.append, "card_account_funded", {"request": number, **out, "by": who})
    return out


async def _return_from_card_account(number: str, amount_minor: int, currency: str, who: str) -> dict | None:
    """What an advance did not spend goes back from the card-issuing account to the main account (Charges: internal, no
    fee); never more than that account still holds in the currency."""
    from .airwallex_settings import card_account_for

    acct = card_account_for()
    if not acct or amount_minor <= 0:
        return None
    try:
        held = (await _bank().balances_of(acct)).get(currency, 0)
    except AirwallexError:
        held = amount_minor
    back = min(amount_minor, held)
    if back <= 0:
        return {"returned_minor": 0, "note": f"the card account holds no {currency}"}
    try:
        ch = await _bank().charge_connected_account(account_id=acct, amount_minor=back, currency=currency,
                                                    reference=f"VENTHORA {number} unspent advance", request_id=f"back-{number}-{secrets.token_hex(4)}")
    except AirwallexError as exc:
        return {"returned_minor": 0, "note": f"not returned: {exc}"}
    out = {"returned_minor": back, "currency": currency, "charge_id": ch.get("id"), "status": ch.get("status"),
           "at": datetime.now(UTC).isoformat(timespec="seconds")}
    await asyncio.to_thread(app.state.audit.append, "card_account_returned", {"request": number, **out, "by": who})
    return out


@app.post("/v1/fund-requests/{number}/card")
async def fund_request_card(number: str, role: str = "finance_manager", by: str = "dashboard"):
    """Pay an approved advance, float or ODC as a virtual card (instead of a transfer to the person's account)."""
    return await _issue_advance_card(number, _writer(role, by))


@app.post("/v1/fund-requests/{number}/settle-card")
async def fund_request_settle_card(number: str, role: str = "finance_manager", by: str = "dashboard"):
    """Settle an advance paid by card from what the card actually spent on Airwallex (in the card's currency, after
    any conversion), and freeze the card: what was not spent never left the wallet."""
    who = _writer(role, by)
    org = app.state.org
    fr = org.get("fund_request", number)
    if fr is None or not fr.get("card_id"):
        raise HTTPException(404, "no advance paid by card with this number")
    if fr["status"] != "IN_PAYMENT":
        raise HTTPException(409, f"{number} is {fr['status'].lower()}")
    try:
        txs = await _bank().card_transactions(fr["card_id"])
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
    ok = [t for t in txs if str(t.get("status", "")).upper() in ("APPROVED", "CLEARED", "SETTLED", "COMPLETED", "POSTED")]

    async def in_advance_ccy(t) -> int:
        """What a charge spent of the advance, in the advance's currency (Airwallex signs spend negative)."""
        for amt, ccy in ((t.get("transaction_amount"), t.get("transaction_currency")), (t.get("billing_amount"), t.get("billing_currency"))):
            if ccy == fr["currency"] and amt is not None:
                return abs(round(float(amt) * 100))
        rate = await _bank().rate_to_base(t.get("transaction_currency"), fr["currency"])
        return abs(round(float(t.get("transaction_amount") or 0) * float(rate) * 100))
    spent = sum([await in_advance_ccy(t) for t in ok])
    with contextlib.suppress(Exception):
        await _bank().set_card_status(fr["card_id"], "INACTIVE")
    card = org.get("card", fr["card_id"]) or {}
    org._put("card", card | {"frozen": True, "settled_for": number}, who)
    returned = await _return_from_card_account(number, max(fr["amount_minor"] - spent, 0), fr["currency"], who) if fr.get("funding") else None
    rec = fr | {"status": "SETTLED", "settled_minor": spent, "settlement_note": f"from the card's spend: {len(ok)} charge(s); card frozen", "settled_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "returned": returned}
    await asyncio.to_thread(org._put, "fund_request", rec, who)
    await asyncio.to_thread(app.state.audit.append, "fund_request_settled", {"request": number, "settled_minor": spent, "by": who, "from": "card"})
    return rec | {"charges": len(ok), "unspent_minor": max(fr["amount_minor"] - spent, 0),
                  "lines": [{k: t.get(k) for k in ("transaction_amount", "transaction_currency", "billing_amount", "billing_currency", "merchant", "status", "transaction_date")} for t in ok]}


@app.get("/v1/cards/{card_id}/limits")
async def cards_limits(card_id: str):
    """The card's limits as Airwallex enforces them (with what remains), beside the limits VENTHORA issued and the
    mandate ceiling: a limit changed on Airwallex outside VENTHORA shows as drift."""
    from . import cards

    rec = app.state.org.get("card", card_id)
    if rec is None:
        raise HTTPException(404, "not a card VENTHORA issued")
    try:
        live = await _bank().card_limits(card_id)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
    out = cards.compare_limits(rec, live, cards.mandate_ceiling(app.state.org))
    if out["drift"]:
        await asyncio.to_thread(app.state.audit.append, "card_limit_drift", {"card_id": card_id, "drift": out["drift"]})
    return out


@app.get("/v1/cards/{card_id}/transactions")
async def cards_transactions(card_id: str):
    try:
        return await _bank().card_transactions(card_id)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc


@app.get("/v1/card-authorization")
async def card_auth_status():
    """Card Authorization: whether Airwallex asks VENTHORA before a card charge goes, the address it calls, whether
    the shared secret is kept, and every decision with its reason."""
    from . import cards, secret_values

    out = {"settings": None, "error": None, "secret": secret_values.info(cards.SECRET), "suggested_url": await _remote_auth_url(),
           "decisions": sorted(app.state.org.list("card_auth"), key=lambda d: d.get("at", ""), reverse=True)[:100]}
    try:
        cfg = await _bank().issuing_config()
        out["settings"] = cfg.get("remote_auth_settings")
    except HTTPException as exc:
        out["error"] = exc.detail
    except AirwallexError as exc:
        out["error"] = f"Airwallex request failed: {exc}"
    return out


@app.post("/v1/card-authorization/enable")
async def card_auth_enable(body: RemoteAuthIn, role: str = "finance_manager", by: str = "dashboard"):
    """Turn remote authorization on: Airwallex calls VENTHORA's address for every charge; the shared secret it returns
    (once) is kept here, write-only. Airwallex must have enabled remote authorization for the account first."""
    from . import cards, secret_values

    who = _connector(role, by)
    have = secret_values.info(cards.SECRET)["set"]
    try:
        resp = await _bank().update_issuing_config({"remote_auth": {"url": body.url, "enabled": True, "default_action": body.default_action,
                                                                    "version": 2, "new_shared_secret": not have}})
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex refused it: {exc}") from exc
    st = resp.get("remote_auth_settings") or resp.get("remote_auth") or {}
    if st.get("shared_secret"):
        await asyncio.to_thread(secret_values.put, cards.SECRET, st["shared_secret"], who)
    await asyncio.to_thread(app.state.audit.append, "card_authorization_enabled", {"url": body.url, "default_action": body.default_action, "by": who,
                                                                                   "secret_received": bool(st.get("shared_secret"))})
    return await card_auth_status()


@app.post("/v1/card-authorization/secret")
async def card_auth_secret(body: SecretIn, role: str = "finance_manager", by: str = "dashboard"):
    """Keep a shared secret Airwallex gave another way (write-only)."""
    from . import cards, secret_values

    who = _connector(role, by)
    await asyncio.to_thread(secret_values.put, cards.SECRET, body.shared_secret, who)
    return secret_values.info(cards.SECRET)


@app.post("/v1/card-authorization/test")
async def card_auth_test(body: CardChargeIn, card_id: str = Query(..., max_length=80), role: str = "finance_manager", by: str = "dashboard"):
    """A signed authorization request, as Airwallex would send it, through the same check and decision (nothing is
    charged; the decision is recorded as a test)."""
    from . import cards, secret_values

    _writer(role, by)
    rec = app.state.org.get("card", card_id)
    if rec is None:
        raise HTTPException(404, "not a card VENTHORA issued")
    secret = secret_values.get(cards.SECRET)
    test_secret = not secret
    secret = secret or "venthora-local-test-secret"
    req = cards.test_request(rec, body.amount_minor, body.currency.upper(), body.merchant or rec.get("merchant") or rec["payee"], body.merchant_category_code)
    nonce = cards.new_nonce()
    ok, why = cards.verify(nonce, cards.sign(nonce, secret), secret)
    decision, reason = cards.decide(app.state.org, req) if ok else ("DECLINED", why)
    rec_d = await asyncio.to_thread(cards.record_decision, app.state.org, req, decision, reason, source="test")
    return {"signature": why, "test_secret": test_secret, "request": req, "answer": cards.answer(req, decision, reason), "decision": rec_d}


_SNS_CERTS: dict[str, bytes] = {}


async def _sns_verified(msg: dict) -> bool:
    """Amazon SNS message signature (SignatureVersion 1: SHA1, 2: SHA256) with the certificate from an AWS SNS host,
    and the topic must be the one VENTHORA_SES_TOPIC_ARN names."""
    import base64
    import re as _re

    import httpx
    from urllib.parse import urlparse

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    topic = env("VENTHORA_SES_TOPIC_ARN")
    if not topic or msg.get("TopicArn") != topic:
        return False
    cert_url = str(msg.get("SigningCertURL") or "")
    u = urlparse(cert_url)
    if u.scheme != "https" or not _re.fullmatch(r"sns\.[a-z0-9-]+\.amazonaws\.com", u.hostname or "") or not u.path.endswith(".pem"):
        return False
    if cert_url not in _SNS_CERTS:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(cert_url)
            r.raise_for_status()
            _SNS_CERTS[cert_url] = r.content
    keys = (["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"] if msg.get("Type") == "Notification"
            else ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"])
    text = "".join(f"{k}\n{msg[k]}\n" for k in keys if k in msg and msg[k] is not None)
    algo = hashes.SHA256() if str(msg.get("SignatureVersion")) == "2" else hashes.SHA1()
    try:
        x509.load_pem_x509_certificate(_SNS_CERTS[cert_url]).public_key().verify(
            base64.b64decode(msg.get("Signature") or ""), text.encode("utf-8"), padding.PKCS1v15(), algo)
        return True
    except Exception:  # noqa: BLE001 - any failure is "not verified"
        return False


@app.post("/webhooks/ses", include_in_schema=True)
async def ses_feedback(request: Request):
    """Amazon SES bounces and complaints, delivered by Amazon SNS. The SNS signature and topic are checked; a
    subscription is confirmed once; a bounce or complaint for one of this company's messages updates it in the outbox
    (bounced, failed or complained) and records the address as an email issue. Not ours: 404 (the platform asks the
    next company)."""
    from urllib.parse import urlparse

    import httpx

    from . import outbox

    try:
        msg = json.loads(await request.body() or b"{}")
    except ValueError as exc:
        raise HTTPException(400, "not JSON") from exc
    if not await _sns_verified(msg):
        raise HTTPException(403, "not a verified Amazon SNS message for this topic")
    if msg.get("Type") == "SubscriptionConfirmation":
        url = str(msg.get("SubscribeURL") or "")
        if not (url.startswith("https://") and (urlparse(url).hostname or "").endswith(".amazonaws.com")):
            raise HTTPException(400, "unexpected SubscribeURL")
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url)
        await asyncio.to_thread(app.state.audit.append, "ses_subscription_confirmed", {"topic": msg.get("TopicArn"), "status": r.status_code})
        return {"confirmed": r.status_code == 200}
    if msg.get("Type") != "Notification":
        return {"ignored": msg.get("Type")}
    try:
        note = json.loads(msg.get("Message") or "{}")
    except ValueError as exc:
        raise HTTPException(400, "SES message is not JSON") from exc
    kind = note.get("notificationType") or note.get("eventType")
    if kind not in ("Bounce", "Complaint"):
        return {"ignored": kind}
    mail = note.get("mail") or {}
    msg_id = next((h.get("value") for h in mail.get("headers") or [] if str(h.get("name", "")).lower() == "x-venthora-outbox-id"), None)
    if kind == "Bounce":
        b = note.get("bounce") or {}
        rcpts = b.get("bouncedRecipients") or []
        permanent = b.get("bounceType") == "Permanent"
        reason = "; ".join(filter(None, [b.get("bounceType"), b.get("bounceSubType")] + [r.get("diagnosticCode") or "" for r in rcpts]))
    else:
        c_ = note.get("complaint") or {}
        rcpts = c_.get("complainedRecipients") or []
        permanent = True
        reason = c_.get("complaintFeedbackType") or "recipient marked the message as spam"
    addresses = [r.get("emailAddress") for r in rcpts if r.get("emailAddress")]
    rec = await asyncio.to_thread(outbox.apply_feedback, app.state.org, kind=kind, msg_id=msg_id, addresses=addresses, reason=reason, permanent=permanent)
    if rec is None:
        raise HTTPException(404, "not a message of this company")
    await asyncio.to_thread(app.state.audit.append, "email_feedback", {"kind": kind, "permanent": permanent, "message": rec["_id"],
                                                                        "addresses": addresses, "status": rec["status"], "reason": reason[:200]})
    return {"message": rec["_id"], "status": rec["status"]}


class ReaddressIn(BaseModel):
    to: list[str] = Field(min_length=1, max_length=10)


@app.post("/v1/outbox/{msg_id}/readdress")
async def outbox_readdress(msg_id: str, body: ReaddressIn, role: str = "finance_manager", by: str = "dashboard"):
    """Correct the address of a bounced or failed message and send it again (the old address's issue is resolved)."""
    from . import outbox

    who = _writer(role, by)
    try:
        rec = await asyncio.to_thread(outbox.readdress, app.state.org, msg_id, body.to, who)
    except KeyError as exc:
        raise HTTPException(404, "no such message") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "email_readdressed", {"message": msg_id, "to": body.to, "status": rec["status"], "by": who})
    return rec


@app.get("/v1/outbox/issues")
async def outbox_issues():
    """Addresses that bounced or complained, open first: what the call-back needs to correct."""
    rows = app.state.org.list("email_issue")
    return sorted(rows, key=lambda r: (r.get("resolved", False), r.get("at", "")), reverse=False)


@app.post("/webhooks/airwallex/remote-auth", include_in_schema=True)
async def card_remote_auth(request: Request):
    """Airwallex asks whether a card charge may go. Answered from the mandate alone, well inside the 2.5 s limit."""
    from . import cards, secret_values

    import hmac

    raw = await request.body()
    token, given = env("VENTHORA_PLATFORM_TOKEN"), request.headers.get("x-venthora-platform")
    by_platform = bool(token and given and hmac.compare_digest(token, given))  # SaaS: the platform already checked Airwallex's signature
    if not by_platform:
        ok, why = cards.verify(request.headers.get("x-nonce"), request.headers.get("x-signature"), secret_values.get(cards.SECRET))
        if not ok:
            await asyncio.to_thread(app.state.audit.append, "card_authorization_refused", {"reason": why})
            raise HTTPException(401, why)
    try:
        req = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(400, "not JSON") from exc
    if by_platform and app.state.org.get("card", req.get("card_id") or "") is None:
        raise HTTPException(404, "not a card this company issued")  # the platform asks the next company
    decision, reason = cards.decide(app.state.org, req)
    rec = await asyncio.to_thread(cards.record_decision, app.state.org, req, decision, reason, source="airwallex")
    await asyncio.to_thread(app.state.audit.append, "card_authorization", {k: rec.get(k) for k in ("_id", "card_id", "payee", "amount_minor", "currency",
                                                                                                   "merchant", "decision", "reason")})
    return cards.answer(req, decision, reason)


# ---- Platform Setup -> Visa Agent Identity (TAP) and Visa Intelligent Commerce --------------------------------------
class TapSignIn(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=500)
    tag: str = Field("agent-payer-auth", pattern="^(agent-browser-auth|agent-payer-auth)$")


class VisaCredentialsIn(BaseModel):
    api_key: str | None = Field(None, max_length=200)
    shared_secret: str | None = Field(None, max_length=300)
    consumer_id: str | None = Field(None, max_length=200)
    token_id: str | None = Field(None, max_length=300)


class VicPreviewIn(BaseModel):
    card_id: str = Field(min_length=1, max_length=80)
    amount_minor: int | None = Field(None, gt=0)
    description: str = Field("Software subscription", max_length=100)
    recurring: bool = True


@app.get("/v1/visa/tap")
async def visa_tap_status():
    """The agent's signing identity: the public key merchants check, where it is published, and the tags it signs."""
    from . import visa_tap

    return visa_tap.public_info() | {"tags": visa_tap.TAGS, "published_at": "/.well-known/venthora-agent.json (gateway) · /v1/visa/tap/keys.json",
                                     "private_key_kept": visa_tap.secret_values.info(visa_tap.PRIVATE)["set"]}


@app.get("/v1/visa/tap/keys.json")
async def visa_tap_keys():
    """Public: the agent's TAP public keys (what a merchant or a registry reads)."""
    from . import visa_tap

    return visa_tap.public_info()


@app.post("/v1/visa/tap/keys")
async def visa_tap_generate(role: str = "finance_manager", by: str = "dashboard"):
    """A new Ed25519 signing key for the agent (rotates: the previous key stops signing at once)."""
    from . import visa_tap

    who = _setup_writer(role, by)
    info = await asyncio.to_thread(visa_tap.generate, who)
    await asyncio.to_thread(app.state.audit.append, "visa_tap_key", {"key_id": info["keys"][0]["key_id"] if info["keys"] else None, "by": who})
    return info


@app.post("/v1/visa/tap/sign-test")
async def visa_tap_sign_test(body: TapSignIn, role: str = "finance_manager", by: str = "dashboard"):
    """Sign a request to a merchant URL as the agent, then check it as the merchant would with the published key."""
    from . import visa_tap

    _setup_writer(role, by)
    try:
        headers = visa_tap.sign(body.url, body.tag)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    pub = visa_tap.secret_values.get(visa_tap.PUBLIC)
    ok, why = visa_tap.verify(body.url, headers, pub)
    return {"headers": headers, "verified": ok, "check": why}


@app.get("/v1/visa/vic")
async def visa_vic_status():
    """Visa Intelligent Commerce: which credentials are kept (never their values) and what is still missing."""
    from . import visa_vic

    return {"credentials": visa_vic.credentials_info(), "ready": visa_vic.vic_ready(), "last_test": app.state.org.get("visa", "last_test"),
            "sandbox": visa_vic.SANDBOX}


@app.put("/v1/visa/vic/credentials")
async def visa_vic_credentials(body: VisaCredentialsIn, role: str = "finance_manager", by: str = "dashboard"):
    from . import visa_vic

    who = _setup_writer(role, by)
    info = await asyncio.to_thread(visa_vic.save_credentials, body.model_dump(), who)
    await asyncio.to_thread(app.state.audit.append, "visa_credentials", {"set": [k for k, v in info.items() if v["set"]], "by": who})
    return info


@app.get("/v1/visa/fx-rate")
async def visa_fx_rate(source: str = Query(pattern="^[A-Z]{3}$"), destination: str = Query(pattern="^[A-Z]{3}$"),
                       amount_minor: int | None = Query(None, ge=0, le=10**13)):
    """Visa's daily rate (Foreign Exchange Rates, rate product A) for source -> destination, kept 30 minutes per pair;
    with an amount, what that amount comes to at Visa's rate. On the sandbox Visa answers with test data, and says so."""
    from decimal import ROUND_HALF_UP, Decimal

    from . import visa_vic

    cache = app.state.__dict__.setdefault("visa_fx_cache", {})
    hit = cache.get((source, destination))
    if not hit or time.monotonic() - hit[0] > 1800:
        r = await visa_vic.fx_rate(source, destination, "100")
        if r.get("ok"):
            cache[(source, destination)] = (time.monotonic(), r)
        hit = (time.monotonic(), r)
    r = dict(hit[1])
    r["environment"] = "sandbox" if visa_vic.SANDBOX.startswith("https://sandbox") else "production"
    r["note"] = "Visa sandbox: test data, not market rates" if r["environment"] == "sandbox" else "Visa daily rate"
    if r.get("ok") and amount_minor is not None and r.get("conversion_rate"):
        r["amount_minor"] = amount_minor
        r["destination_minor"] = int((Decimal(amount_minor) * Decimal(str(r["conversion_rate"]))).to_integral_value(rounding=ROUND_HALF_UP))
    return r


class XPaySecretIn(BaseModel):
    encrypted_shared_secret: str = Field(min_length=16, max_length=4000)


@app.post("/v1/visa/vic/xpay-public-key")
async def visa_xpay_public_key(role: str = "super_admin", by: str = "dashboard"):
    """VENTHORA's public key to upload to Visa (Credentials -> X-Pay Token -> Public Key): created once, then the same."""
    from . import visa_vic

    who = _setup_writer(role, by)
    out = await asyncio.to_thread(visa_vic.xpay_public_key, who)
    if out["created"]:
        await asyncio.to_thread(app.state.audit.append, "visa_xpay_keypair_created", {"fingerprint": out["fingerprint"], "by": who})
    return out


@app.post("/v1/visa/vic/xpay-secret")
async def visa_xpay_secret(body: XPaySecretIn, role: str = "super_admin", by: str = "dashboard"):
    """The encrypted shared secret Visa shows, decrypted with VENTHORA's private key and kept write-only."""
    from . import visa_vic

    who = _setup_writer(role, by)
    try:
        info = await asyncio.to_thread(visa_vic.save_encrypted_secret, body.encrypted_shared_secret, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "visa_shared_secret_kept", {"by": who})
    return info


@app.post("/v1/visa/vic/test")
async def visa_vic_test(role: str = "finance_manager", by: str = "dashboard"):
    """Visa's connectivity check for the sandbox project (GET /vdp/helloworld with an X-Pay token)."""
    from . import visa_vic

    _writer(role, by)
    res = await visa_vic.hello_world()
    app.state.org._put("visa", {"_id": "last_test", **res}, by)
    return res


@app.post("/v1/visa/vic/preview")
async def visa_vic_preview(body: VicPreviewIn):
    """VENTHORA's mandate for a card-billed vendor as Visa's purchase instruction (what would be sent, nothing sent)."""
    from . import visa_vic

    rec = app.state.org.get("card", body.card_id)
    if rec is None:
        raise HTTPException(404, "not a card VENTHORA issued")
    amount = min(body.amount_minor or rec["per_tx_minor"], rec["per_tx_minor"])
    return visa_vic.purchase_instruction(payee=rec.get("merchant") or rec["payee"], amount_minor=amount, currency=rec["currency"],
                                         mcc=(rec.get("mcc") or [None])[0], category="Software",
                                         description=body.description, recurring=body.recurring) | {"ready": visa_vic.vic_ready()}


@app.get("/v1/receipts")
async def receipts_list():
    """Balance received: what other parties paid into the Airwallex account. Every deposit Airwallex reports, and every
    receipt recorded here; a recorded receipt is confirmed when a deposit of the same currency and amount arrives."""
    from .gateways.airwallex import to_minor
    from .masterdata import MasterStore

    deposits, error, accounts = [], None, {}
    try:
        bank = _bank()
        accounts = {g["id"]: g for g in (_ga_row(x) for x in await bank.list_global_accounts())}
        deposits = [{"deposit_id": d.get("id"), "amount_minor": to_minor(d.get("amount", 0)), "currency": d.get("currency"), "status": d.get("status"),
                     "payer": (d.get("payer") or {}).get("name") or "", "reference": d.get("reference") or "", "received_at": d.get("created_at"),
                     "account": _ga_label(accounts.get(d.get("global_account_id"))),
                     "global_account_id": d.get("global_account_id")} for d in await bank.list_deposits()]
    except HTTPException as exc:
        error = exc.detail
    except AirwallexError as exc:
        error = f"Airwallex request failed: {exc}"
    recorded = sorted(app.state.org.list("receipt"), key=lambda r: (r["received_date"], r["receipt_id"]))
    payers = app.state.intake.registry.payers
    recv = {r["receivable_id"]: r for r in MasterStore().list("receivable")}
    used: set[str] = set()
    rows = []
    for r in recorded:
        hit = next((d for d in deposits if d["deposit_id"] not in used and d["currency"] == r["currency"] and d["amount_minor"] == r["amount_minor"]), None)
        if hit:
            used.add(hit["deposit_id"])
        rv = recv.get(r.get("receivable_id") or "")
        rows.append(r | {"state": "Confirmed on Airwallex" if hit and hit["status"] == "SETTLED" else "Deposit arriving" if hit else "Awaiting the deposit",
                         "deposit": hit, "customer": (payers.get(r.get("customer_id") or "") or {}).get("name"),
                         "invoice": rv and {"invoice_no": rv["invoice_no"], "amount_minor": rv["amount_minor"], "due_date": rv["due_date"]}})
    for d in deposits:
        if d["deposit_id"] not in used:
            who = next((k for k, p in payers.items() if d["payer"] and d["payer"].lower() in [p["name"].lower(), *[a.lower() for a in p.get("aliases", [])]]), None)
            rows.append({"receipt_id": None, "payer_name": d["payer"] or "unknown payer", "customer_id": who, "customer": (payers.get(who or "") or {}).get("name"),
                         "amount_minor": d["amount_minor"], "currency": d["currency"], "received_date": str(d["received_at"] or "")[:10],
                         "account": d["account"], "reference": d["reference"], "receivable_id": None, "category": None, "note": "",
                         "state": "Received, not recorded", "deposit": d, "invoice": None})
    rows.sort(key=lambda x: (x["received_date"] or ""), reverse=True)
    totals: dict[str, int] = {}
    for x in rows:
        if x["deposit"] and x["deposit"]["status"] == "SETTLED":
            totals[x["currency"]] = totals.get(x["currency"], 0) + x["amount_minor"]
    open_rv = [r for r in recv.values()]
    return {"rows": rows, "settled_by_currency": totals, "airwallex_error": error,
            "accounts": [{"id": g["id"], "label": _ga_label(g),
                          "currencies": g["currencies"]} for g in accounts.values() if g["status"] != "CLOSED"],
            "customers": [{"id": k, "name": p["name"], "currency": p.get("currency")} for k, p in sorted(payers.items(), key=lambda x: x[1]["name"])],
            "receivables": [{"receivable_id": r["receivable_id"], "customer_id": r["customer_id"], "invoice_no": r["invoice_no"], "amount_minor": r["amount_minor"],
                             "currency": r["currency"], "due_date": r["due_date"]} for r in sorted(open_rv, key=lambda r: r["due_date"])]}


# Intake -> Sync via API / Sync via Dataset: what each tab imports, its number and the date a range is read on
IMPORT_KINDS = {
    "cash_advance": {"entity": "fund_request", "label": "cash advances", "number": "request_number", "dates": ("request_date", "needed_by"), "fund": "cash_advance"},
    "petty_cash": {"entity": "fund_request", "label": "petty cash requests", "number": "request_number", "dates": ("request_date", "needed_by"), "fund": "petty_cash"},
    "odc": {"entity": "fund_request", "label": "other direct costs", "number": "request_number", "dates": ("request_date", "needed_by"), "fund": "odc"},
    "pr": {"entity": "pr", "label": "purchase requests", "number": "pr_number", "dates": ("requested_date", "needed_by")},
    "po": {"entity": "po", "label": "purchase orders", "number": "po_id", "dates": ("start_date", "issued_date"), "contract": False},
    "contract": {"entity": "po", "label": "consultant contracts", "number": "po_id", "dates": ("start_date", "issued_date"), "contract": True},
    "engagement": {"entity": "engagement", "label": "one-off engagements", "number": "engagement_id", "dates": ("event_date",)},
    "ta": {"entity": "ta", "label": "travel authorisations", "number": "ta_number", "dates": ("start",)},
}


class ImportIn(BaseModel):
    via: str = Field(pattern="^(api|dataset)$")
    kind: str = Field(pattern="^(pr|po|contract|engagement|ta|cash_advance|petty_cash|odc)$")
    connector_id: str | None = Field(None, description="via api: the connection to read")
    upload_id: str | None = Field(None, description="via dataset: the file uploaded with POST /v1/sources/uploads")
    mapping: dict[str, str] | None = Field(None, description="via dataset: column -> field, as the upload proposed it")
    date_from: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    number: str | None = Field(None, max_length=40, description="import only this one number")
    dry_run: bool = True


def _fund_kind(v) -> str:
    k = str(v or "").strip().lower()
    return k if k in ("cash_advance", "petty_cash", "odc") else ("petty_cash" if any(w in k for w in ("petty", "slush", "imprest", "float"))
                                                                  else "odc" if ("odc" in k or "direct" in k) else "cash_advance")


def _import_filter(kind: str, rows: list[dict], ids: list, date_from: str | None, date_to: str | None, number: str | None):
    """The rows of this kind inside the dates (or the one number), with the reason each other row was left out."""
    k = IMPORT_KINDS[kind]
    keep, kept_ids, out = [], [], {"other kind": 0, "outside the dates": 0, "another number": 0, "no date": 0}
    want = (number or "").strip().upper()
    for r, i in zip(rows, ids, strict=False):
        if "_error" in r:
            keep.append(r)
            kept_ids.append(i)
            continue
        if "contract" in k and (r.get("loe") not in (None, "")) != k["contract"]:
            out["other kind"] += 1
            continue
        if "fund" in k and _fund_kind(r.get("kind")) != k["fund"]:
            out["other kind"] += 1
            continue
        if want:
            if str(r.get(k["number"]) or "").strip().upper() != want:
                out["another number"] += 1
                continue
        elif date_from or date_to:
            day = next((str(r[f])[:10] for f in k["dates"] if r.get(f)), None)
            if day is None:
                out["no date"] += 1
                continue
            if (date_from and day < date_from) or (date_to and day > date_to):
                out["outside the dates"] += 1
                continue
        keep.append(r)
        kept_ids.append(i)
    return keep, kept_ids, {a: b for a, b in out.items() if b}


@app.get("/v1/intake/travel-authorisations")
async def travel_authorisations_list():
    """Every travel authorisation: traveller, route, dates, estimate, who approved it, and where it came from."""
    org = app.state.org
    users = {u["user_id"]: u for u in org.list("user")}
    rows = [t | {"traveller_name": (users.get(t["traveller"]) or {}).get("name", t["traveller"]),
                 "synced_from": _synced_from(org.source("ta", t["ta_number"]))} for t in org.list("ta")]
    return sorted(rows, key=lambda r: (r["start"], r["ta_number"]), reverse=True)


@app.post("/v1/intake/import")
async def intake_import(body: ImportIn, role: str = "finance_manager", by: str = "dashboard"):
    """Pull purchase requests, purchase orders, consultant contracts, one-off engagements or travel authorisations from
    the connected application (via api) or from an uploaded file (via dataset): every one between two dates, or one
    number. dry_run shows what would be created, updated or refused; nothing is written until dry_run is false. A
    record that exists is locked on its number and value; the rules are the same as for any other upload or sync."""
    from . import imports, ingestion
    from .connectors import ConnectorStore, fetch

    who = _writer(role, by)
    k = IMPORT_KINDS[body.kind]
    if body.date_from and body.date_to and body.date_to < body.date_from:
        raise HTTPException(422, "the end date is before the start date")
    if body.via == "api":
        raw = ConnectorStore().get_raw(body.connector_id or "") if body.connector_id else None
        if raw is None:
            raise HTTPException(404, "no such connection: add the API connection first")
        em = raw["entities"].get(k["entity"])
        if not em or not em.get("enabled", True):
            raise HTTPException(422, f"{raw['name']} is not set to provide {k['label']}: switch it on and map its fields in App Integration · Sync API")
        try:
            rows, ids = await fetch(raw, k["entity"])
        except Exception as exc:  # noqa: BLE001 - the other application's answer, shown as it is
            raise HTTPException(502, f"{raw['name']} did not answer for {k['label']}: {type(exc).__name__}: {str(exc)[:200]}") from exc
        source = f"sync:{raw['connector_id']}"
    else:
        up = getattr(app.state, "uploads", {}).get(body.upload_id or "")
        if up is None:
            raise HTTPException(404, "unknown upload; upload the file again")
        if up["entity"] != k["entity"]:
            raise HTTPException(422, f"the file was read as {imports.ENTITIES[up['entity']][0].lower()}, not {k['label']}")
        mapping = body.mapping or imports.suggest(k["entity"], up["headers"])
        bad = [f for f in mapping if f not in imports.FIELDS[k["entity"]]]
        if bad:
            raise HTTPException(422, f"unknown field {bad[0]}")
        rows = imports.clean(k["entity"], up["rows"], mapping)
        ids = [None] * len(rows)
        source = f"upload:{up['filename']}"
    total = len(rows)
    rows, ids, left_out = _import_filter(body.kind, rows, ids, body.date_from, body.date_to, body.number)
    if body.number and not rows:
        return {"kind": body.kind, "total": total, "matched": 0, "left_out": left_out, "created": [], "updated": [], "unchanged": [], "rejected": [],
                "message": f"{body.number.upper()} is not in the {'connection' if body.via == 'api' else 'file'}"}
    report = await asyncio.to_thread(ingestion.apply, k["entity"], rows, ids, source=source, dry_run=body.dry_run, **_ingest_ctx())
    out = {k2: v for k2, v in report.items() if k2 in ("created", "updated", "unchanged", "rejected", "followed")} | {
        "kind": body.kind, "total": total, "matched": len(rows), "left_out": left_out, "dry_run": body.dry_run}
    if not body.dry_run:
        app.state.position_cache = None
        if body.via == "dataset":
            getattr(app.state, "uploads", {}).pop(body.upload_id, None)
        await asyncio.to_thread(app.state.audit.append, "intake_imported", {
            "by": who, "via": body.via, "kind": body.kind, "source": source, "date_from": body.date_from, "date_to": body.date_to,
            "number": body.number, "created": len(report["created"]), "updated": len(report["updated"]), "rejected": len(report["rejected"])})
    return out


class TestConnectionIn(BaseModel):
    connection: str | None = Field(None, min_length=8, max_length=800, description="a new connection: base URL, then its key")
    connector_id: str | None = Field(None, description="or a saved connection")
    kind: str | None = Field(None, pattern="^(pr|po|contract|engagement|ta|cash_advance|petty_cash|odc)$", description="also read this item, to see it is there")


@app.post("/v1/sources/test-connection")
async def sources_test_connection(body: TestConnectionIn, role: str = "finance_manager", by: str = "dashboard"):
    """Reach the application before anything is imported: for a saved connection the item's own address is read (so
    the key and the path are both tested); for a new one its base URL. Nothing is written."""
    import time as _t

    import httpx

    from .connectors import ConnectorStore, dig, headers_for
    from .sync_catalog import parse_connection

    _writer(role, by)
    if body.connector_id:
        raw = ConnectorStore().get_raw(body.connector_id)
        if raw is None:
            raise HTTPException(404, "no such connection")
        url, headers, name = raw["base_url"], headers_for(raw), raw["name"]
        em = raw["entities"].get(IMPORT_KINDS[body.kind]["entity"]) if body.kind else None
        if em:
            url = raw["base_url"] + "/" + em["path"].lstrip("/")
    elif body.connection:
        try:
            base, auth = parse_connection(body.connection)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        url, headers, name, em = base, headers_for({"auth": auth, "_secret": auth.get("secret", "")}), base, None
    else:
        raise HTTPException(422, "give the connection line or a saved connection")
    t0 = _t.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "name": name, "message": f"could not reach it: {type(exc).__name__}", "ms": round((_t.perf_counter() - t0) * 1000)}
    ms = round((_t.perf_counter() - t0) * 1000)
    if res.status_code in (401, 403):
        return {"ok": False, "name": name, "status_code": res.status_code, "ms": ms, "message": "reached, but the key was refused"}
    items = None
    if em and res.status_code < 300:
        try:
            got = dig(res.json(), em.get("items_path") or "")
            items = len(got) if isinstance(got, list) else None
        except ValueError:
            items = None
    ok = res.status_code < 500 and (not em or res.status_code < 300)
    what = ("purchase orders and consultant contracts" if body.kind in ("po", "contract") else "cash advances, petty cash and ODC requests"
            if body.kind in ("cash_advance", "petty_cash", "odc") else IMPORT_KINDS[body.kind]["label"] if body.kind else "")
    msg = ("connected" + (f"; {items} {what} available" if items is not None else "")) if ok else \
        f"reached, but it answered {res.status_code}"
    return {"ok": ok, "name": name, "status_code": res.status_code, "ms": ms, "items": items, "message": msg}


@app.get("/v1/intake/commitments")
async def intake_commitments():
    """Intake -> Commitments register: every PO, consultant contract and one-off engagement received, entered by hand,
    uploaded or synced, with its state (In Commitments, Expired, Closed, Cancelled, Needs payee) and its source."""
    from . import commitments

    pos = await purchase_orders()
    return await asyncio.to_thread(commitments.register, org=app.state.org, po_statuses=pos, payees=app.state.intake.registry.payees)


# what a PO is locked on once it exists: its number is the key, these are its value
PO_LOCKED = {"supplier_id": "supplier", "currency": "currency", "total_minor": "value", "loe": "LoE", "rate_minor": "rate"}
ENGAGEMENT_LOCKED = {"payee_id": "payee", "currency": "currency", "fee_minor": "fee"}


def _refuse_value_change(kind: str, rid: str, new: dict) -> None:
    """A PO (or an engagement with its PO) is locked on its number and value: what is paid against it was matched to
    them. A different value is a new PO or an amendment registered in Intake."""
    old = app.state.org.get(kind, rid)
    if old is None:
        return
    if kind == "engagement" and app.state.org.get("po", rid) is None:
        return  # not a commitment yet: it has no PO until it has a payee
    fields = PO_LOCKED if kind == "po" else ENGAGEMENT_LOCKED
    changed = [label for k, label in fields.items() if k in new and new.get(k) != old.get(k)]
    if changed:
        raise HTTPException(409, f"{rid} is locked on its number and value; its {', '.join(changed)} cannot change. "
                                 "Register a new PO, or an amendment, in Intake.")


@app.get("/v1/commitments")
async def commitments_list():
    """Per vendor, consultant, one-off engagement, customer and employee: what is committed, paid and still open.
    Vendors: PO number, active date, PO amount, paid, remaining, expiry, status New / Partial / Multi Payment / Done.
    Consultants: contract number, active date, LoE, rate, paid, LoE left, budget left, expiry, status New /
    Partial x of y LoE / Done. One-off engagements: profile, event, fee and the documents still missing."""
    from . import commitments
    from .masterdata import MasterStore

    pos = await purchase_orders()
    vendors, consultants = await beneficiaries_list("vendor"), await beneficiaries_list("consultant")
    customers = await beneficiaries_list("customer")
    return await asyncio.to_thread(commitments.build, org=app.state.org, po_statuses=pos, vendors=vendors, consultants=consultants,
                                   customers=customers, receivables=MasterStore().list("receivable"), payees=app.state.intake.registry.payees)


@app.get("/v1/procurement/spending")
async def procurement_spending(period: str | None = None):
    """Paid spend by office (own and with the offices below), project, account and account code; open PO commitments."""
    from . import procurement

    return procurement.spending(app.state.org, app.state.executor.store, period)


@app.get("/v1/org/charge-codes")
async def charge_codes_view():
    """The default charge code scheme, the codes in use (each marked Company or VENTHORA default) and what 'Fill
    VENTHORA defaults' would add."""
    from . import charge_codes

    org = app.state.org
    return {"scheme": charge_codes.scheme(), "current": await asyncio.to_thread(charge_codes.current, org),
            "plan": await asyncio.to_thread(charge_codes.plan, org)}


@app.post("/v1/org/charge-codes/defaults")
async def charge_codes_fill(role: str = "finance_manager", by: str = "dashboard"):
    """Fill only the blanks with VENTHORA's defaults: cost centres, the OPS project, the six-digit accounts, employee
    numbers and salary charge codes. A code the company set is never changed."""
    from . import charge_codes
    from .organisation import MODELS

    who = _writer(role, by)
    out = await asyncio.to_thread(charge_codes.apply, app.state.org, models=MODELS)
    await asyncio.to_thread(app.state.audit.append, "charge_codes_defaults", {"filled": out["filled"], "by": who})
    return out


@app.get("/v1/reports/staff-spend")
async def reports_staff_spend(country: str = "", unit_id: str = "", project: str = "", user_id: str = ""):
    """Per staff member (employee number, office, branch, country): the cash advances and petty cash received and what
    their virtual cards spent, by project, in the base currency."""
    from . import charge_codes

    org = app.state.org
    ccys = {f.get("currency") for f in org.list("fund_request")}
    fx = await _rates_to_base(ccys)
    return await asyncio.to_thread(charge_codes.staff_spend, org, to_base=lambda m, c: _in_base(m, c, fx) or 0, base=fx["base"],
                                   country=country, unit_id=unit_id, project=project, user_id=user_id)


@app.get("/v1/org/allocation-tree")
async def org_allocation_tree(period: str = "2026"):
    return app.state.org.allocation_tree(period)


@app.get("/v1/org/{collection}")
async def org_list(collection: str):
    kind, _ = _org_kind(collection)
    return app.state.org.list(kind)


def _org_writer(role: str, by: str, kind: str, rec: dict | None) -> str:
    """Company-wide writers write anywhere; a scoped admin (country, branch, project finance) only inside its own unit and
    the units under it, only organisation records, and never gives a role at or above its own."""
    from .organisation import GRANTABLE, SCOPED_ADMINS

    if role not in SCOPED_ADMINS:
        return _writer(role, by)
    org = app.state.org
    me = next((u for u in org.list("user") if u.get("active", True) and u["role"] == role
               and by.strip().lower() in (u["name"].lower(), u["email"].lower())), None)
    if me is None:
        raise HTTPException(403, f"{by} is not an active {role.replace('_', ' ')}")
    if kind not in ("unit", "user", "allocation", "price", "project", "po"):
        raise HTTPException(403, f"a {role.replace('_', ' ')} may not change {kind} records; ask a company-wide admin")
    reach = org.subtree(me["unit_id"])
    unit = org.unit_of_record(kind, rec or {})
    if unit is None or unit not in reach:
        raise HTTPException(403, f"outside your reach: {role.replace('_', ' ')} of {org.get('unit', me['unit_id'])['name']} manages that unit and the units under it")
    if kind == "unit" and (rec or {}).get("unit_id") == me["unit_id"] and org.get("unit", me["unit_id"]) and \
            (rec or {}).get("parent_id") != org.get("unit", me["unit_id"]).get("parent_id"):
        raise HTTPException(403, "you cannot move your own unit")
    if kind == "user" and (rec or {}).get("role") not in GRANTABLE[role]:
        raise HTTPException(403, f"a {role.replace('_', ' ')} may give: {', '.join(sorted(GRANTABLE[role]))}")
    return f"{by} ({role})"


ADMIN_TEAM_ROLES = {"super_admin", "developer", "hq_admin", "country_admin", "branch_admin", "project_admin", "admin"}


def _admin_team_guard(kind: str, role: str, new: dict | None, old: dict | None) -> None:
    """FCO Authorize → Admin team: only the FCO adds, changes or removes a person with an admin role (Developer included)."""
    if kind != "user" or role == "super_admin":
        return
    if (new or {}).get("role") in ADMIN_TEAM_ROLES or (old or {}).get("role") in ADMIN_TEAM_ROLES:
        raise HTTPException(403, "Admin team (FCO Authorize): only the FCO (Super admin) adds or changes an admin or a Developer")


@app.post("/v1/org/{collection}")
async def org_save(collection: str, body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    kind, model = _org_kind(collection)
    _admin_team_guard(kind, role, body, app.state.org.get(kind, body.get(ID_FIELD[kind])) if isinstance(body, dict) and body.get(ID_FIELD[kind]) else None)
    who = _org_writer(role, by, kind, body)
    rid = body.get(ID_FIELD[kind]) if isinstance(body, dict) else None
    if rid:
        _refuse_synced(f"{rid}", app.state.org.source(kind, rid))
        if kind in ("po", "engagement"):
            _refuse_value_change(kind, rid, model.model_validate(body).model_dump())
        if kind in ("pr", "fund_request"):
            old = app.state.org.get(kind, rid)
            if old and old["status"] not in ("DRAFT", "SUBMITTED"):
                raise HTTPException(409, f"{rid} is {old['status'].lower()}; it no longer changes")
    if kind in ("pr", "fund_request"):
        if body.get("status") not in (None, "DRAFT", "SUBMITTED"):
            raise HTTPException(409, "a request is approved or rejected through its decision, not saved as such")
        body = {**body, "decided_by": None, "decided_at": None} | ({"po_id": None} if kind == "pr" else {"intake_id": None, "settled_minor": None})
    try:
        rec = await asyncio.to_thread(app.state.org.save, kind, model.model_validate(body), who)
    except OrgError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:  # pydantic validation
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, f"org_{kind}_saved", {"id": rec[ID_FIELD[kind]], "by": who})
    return rec


@app.delete("/v1/org/{collection}/{record_id}")
async def org_delete(collection: str, record_id: str, role: str = "finance_manager", by: str = "dashboard"):
    kind, _ = _org_kind(collection)
    _admin_team_guard(kind, role, None, app.state.org.get(kind, record_id))
    who = _org_writer(role, by, kind, app.state.org.get(kind, record_id))
    _refuse_synced(record_id, app.state.org.source(kind, record_id))
    if kind in ("po", "engagement") and app.state.executor.store.po_payments(record_id):
        raise HTTPException(409, "money has been paid against this PO; close or cancel it instead, so its payments keep their PO")
    try:
        await asyncio.to_thread(app.state.org.delete, kind, record_id, who)
        if kind == "engagement" and app.state.org.get("po", record_id):  # the PO it was paid against goes with it
            await asyncio.to_thread(app.state.org.delete, "po", record_id, who)
    except KeyError as exc:
        raise HTTPException(404, "no such record") from exc
    except OrgError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, f"org_{kind}_deleted", {"id": record_id, "by": who})
    return {"deleted": record_id}


# ---- Platform Setup → App Integration: keys for other applications, and the catalogue of what they can call ----
@app.get("/v1/developer/catalog")
async def developer_catalog(request: Request):
    """Every endpoint, grouped by area, with the scope an API key needs to call it."""
    areas = [{"area": a, "label": lbl, "covers": cov, "scopes": [f"{a}:read", f"{a}:write"]} for a, (lbl, cov, _) in apiaccess.AREAS.items()]
    return {"areas": areas, "roles": list(apiaccess.ROLES), "endpoints": apiaccess.catalog(app.openapi()),
            "auth": {"header": "Authorization: Bearer <key>", "alternative": "X-API-Key: <key>",
                     "read": "GET and .../eval calls", "write": "every other call",
                     "role": "the key's role and name replace the role and by query parameters",
                     "idempotency": f"send Idempotency-Key on a write to make retries safe (kept {apiaccess.IDEMPOTENCY_HOURS} h)",
                     "errors": "every refusal is JSON {\"detail\": ...} with the HTTP status"},
            "openapi": "/openapi.json", "docs": "/docs",
            "gateway": {"command": "python scripts/webhook_gateway.py --api", "note": "forwards the API only for requests carrying a key"}}


def _public_base() -> str:
    from .sourcing import public_url

    return public_url()


@app.get("/developers", include_in_schema=False)
async def developers_page():
    """The public API Catalog page for other applications' developers (read only; no key, no data)."""
    return FileResponse(DASHBOARD.parent / "developers.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/developers/catalog.json", include_in_schema=False)
async def developers_catalog():
    from . import devportal

    return devportal.catalog(app.openapi(), _public_base())


@app.get("/developers/openapi.json", include_in_schema=False)
async def developers_openapi():
    from . import devportal

    return JSONResponse(devportal.openapi(app.openapi(), _public_base()), headers={"Content-Disposition": 'attachment; filename="venthora-openapi.json"'})


@app.get("/developers/postman.json", include_in_schema=False)
async def developers_postman():
    from . import devportal

    return JSONResponse(devportal.postman(app.openapi(), _public_base()), headers={"Content-Disposition": 'attachment; filename="venthora.postman_collection.json"'})


@app.get("/v1/developer/keys/{key_id}/requests")
async def developer_key_requests(key_id: str, limit: int = Query(50, ge=1, le=1000)):
    if await asyncio.to_thread(app.state.keys.get, key_id) is None:
        raise HTTPException(404, "unknown key")
    return await asyncio.to_thread(app.state.keys.requests, key_id, limit)


@app.get("/v1/developer/keys")
async def developer_keys():
    return await asyncio.to_thread(app.state.keys.list)


@app.post("/v1/developer/keys")
async def developer_key_create(req: apiaccess.KeyRequest, role: str = "finance_manager", by: str = "dashboard"):
    """Create a key. The key is in this response only; VENTHORA keeps its hash."""
    who = _setup_writer(role, by)
    rec, token = await asyncio.to_thread(app.state.keys.create, req, who)
    await asyncio.to_thread(app.state.audit.append, "api_key_created", {"key_id": rec["key_id"], "name": rec["name"], "scopes": rec["scopes"], "role": rec["role"], "by": who})
    return rec | {"key": token}


@app.post("/v1/developer/keys/{key_id}/revoke")
async def developer_key_revoke(key_id: str, role: str = "finance_manager", by: str = "dashboard"):
    who = _setup_writer(role, by)
    rec = await asyncio.to_thread(app.state.keys.revoke, key_id)
    if rec is None:
        raise HTTPException(404, "no such key")
    await asyncio.to_thread(app.state.audit.append, "api_key_revoked", {"key_id": key_id, "by": who})
    return rec


# ---- Settings → Budget allocation: the policy, the allocations' usage, the gate a payment passes, the sync ----
class BudgetPolicyIn(BaseModel):
    enforcement: str = "per_allocation"
    period_type: str = "annual"
    daily_sync_time: str = "23:59"
    monthly_sync_day: int = Field(1, ge=1, le=31)
    sync_enabled: bool = True
    fiscal_year_start_month: int = Field(1, ge=1, le=12)
    cutoff_day: int = Field(25, ge=1, le=31)
    cutoff_time: str = Field("23:59", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field("Asia/Jakarta", max_length=60)


def _budget_policy_view() -> dict:
    from . import budget_allocation as ba

    org, cfg = app.state.org, load_reporting_config()
    pol, std = org.budget_policy(), org.standards()
    now = datetime.now(UTC)
    month = reporting_period(now, cfg)
    start = int(std.get("fiscal_year_start_month", 1))
    nxt = {k: ba.next_scheduled(k, pol, now, cfg.timezone).isoformat() for k in ("daily", "monthly")} if pol["sync_enabled"] else None
    return pol | {"fiscal_year_start_month": start, "cutoff_day": cfg.cutoff_day, "cutoff_time": cfg.cutoff_time,
                  "timezone": cfg.timezone, "base_currency": std["base_currency"], "quarters": ba.quarters(start),
                  "period_types": ba.PERIOD_LABEL, "current_month": month,
                  "active_period": ba.period_of(month, pol["period_type"], start), "next_sync": nxt,
                  "period_options": ba.period_options(pol["period_type"], start, month)}


@app.get("/v1/budget-allocation/policy")
async def budget_policy_get():
    """Enforcement, the active period type, fiscal-year start (Q1), cut-off and the daily / monthly sync times."""
    return await asyncio.to_thread(_budget_policy_view)


@app.put("/v1/budget-allocation/policy")
async def budget_policy_put(body: BudgetPolicyIn, role: str = "finance_manager", by: str = "dashboard"):
    from zoneinfo import ZoneInfo

    from .budget_allocation import BudgetPolicy
    from .organisation import Standards

    who = _writer(role, by)
    sent = body.model_dump(exclude_unset=True)  # what the caller did not send keeps its current value
    try:
        ZoneInfo(sent.get("timezone", load_reporting_config().timezone))
        cur = app.state.org.budget_policy()
        pol = BudgetPolicy.model_validate(cur | {k: v for k, v in sent.items() if k in ("enforcement", "period_type", "daily_sync_time", "monthly_sync_day", "sync_enabled")})
    except Exception as exc:  # noqa: BLE001 - a bad time zone or value is the caller's to fix
        raise HTTPException(422, str(exc)) from exc
    org = app.state.org
    await asyncio.to_thread(org.save_budget_policy, pol, who)
    if "fiscal_year_start_month" in sent:
        std = org.standards() | {"fiscal_year_start_month": sent["fiscal_year_start_month"]}
        await asyncio.to_thread(org.save_standards, Standards.model_validate(std), who)
    cut = {k: sent[k] for k in ("cutoff_day", "cutoff_time", "timezone") if k in sent}
    if cut:
        save_reporting_config(load_reporting_config().model_copy(update=cut | {"set_by": who}))
    await asyncio.to_thread(app.state.audit.append, "budget_policy", sent | {"by": who})
    return await asyncio.to_thread(_budget_policy_view)


@app.get("/v1/budget-allocation/status")
async def budget_allocation_status(period: str | None = None, every: bool = False):
    """Every allocation of a budget period with what was spent against it and what is left; every=true: all the
    allocations, each in its own period (monthly, quarterly, annual or a project's duration)."""
    from .budget_allocation import Budget

    view = await asyncio.to_thread(_budget_policy_view)
    period = period or view["active_period"]
    b = Budget(app.state.org, app.state.executor.store)
    if every:
        return {"active_period": view["active_period"], "enforcement": view["enforcement"], "current_month": view["current_month"],
                "allocations": await asyncio.to_thread(b.usage_all)}
    periods = sorted({a["period"] for a in b.allocs} | {view["active_period"]})
    return {"period": period, "active_period": view["active_period"], "periods": periods, "enforcement": view["enforcement"],
            "allocations": await asyncio.to_thread(b.usage, period)}


class BudgetCheckIn(BaseModel):
    account_code: str = Field(min_length=5, max_length=60)
    amount_minor: int = Field(gt=0, le=10**13)
    currency: str = Field("USD", min_length=3, max_length=3)
    base_cost_minor: int | None = Field(None, gt=0, le=10**13, description="the cost in the base currency; default = amount")


@app.post("/v1/budget-allocation/check")
async def budget_allocation_check(body: BudgetCheckIn):
    """What the execution gate would say about one payment now (nothing is booked)."""
    from .budget_allocation import Budget

    item = {"obligation_id": "check", "account_code": body.account_code.upper(), "currency": body.currency.upper(),
            "amount_minor": body.amount_minor, "base_cost_minor": body.base_cost_minor or body.amount_minor}
    month = reporting_period(datetime.now(UTC), load_reporting_config())
    return (await asyncio.to_thread(Budget(app.state.org, app.state.executor.store).check, [item], month))[0]


async def _budget_sync(kind: str, who: str) -> dict:
    """Re-read every connected source, then record each allocation's usage in the active period."""
    from .budget_allocation import Budget, BudgetPolicy
    from .connectors import ConnectorStore

    sources = {}
    for c in ConnectorStore().list():
        try:
            reps = await _sync_connector(c["connector_id"], False, None, who)
            sources[c["connector_id"]] = {e: (r.get("error") or {k: len(v) for k, v in r.items() if isinstance(v, list)}) for e, r in reps.items()}
        except Exception as exc:  # noqa: BLE001 - one source failing does not stop the others
            sources[c["connector_id"]] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    view = await asyncio.to_thread(_budget_policy_view)
    usage = await asyncio.to_thread(Budget(app.state.org, app.state.executor.store).usage, view["active_period"])
    over = [a["alloc_id"] for a in usage if a["remaining_minor"] is not None and a["remaining_minor"] < 0]
    summary = {"kind": kind, "at": datetime.now(UTC).isoformat(timespec="seconds"), "period": view["active_period"],
               "allocations": len(usage), "over_allocation": over, "sources": sources}
    pol = app.state.org.budget_policy() | {f"last_{kind}_sync_at": summary["at"], "last_sync_summary": summary}
    await asyncio.to_thread(app.state.org.save_budget_policy, BudgetPolicy.model_validate(pol), who)
    await asyncio.to_thread(app.state.audit.append, f"budget_{kind}_sync", summary | {"by": who})
    return summary


@app.post("/v1/budget-allocation/sync")
async def budget_allocation_sync(kind: str = Query("daily", pattern="^(daily|monthly)$"), role: str = "finance_manager", by: str = "dashboard"):
    """Run the daily or monthly sync now."""
    return await _budget_sync(kind, _writer(role, by))


async def _budget_scheduler(every_s: float = 30.0) -> None:
    """Runs the daily and monthly syncs at the times Settings → Budget allocation sets."""
    from .budget_allocation import BudgetPolicy, due

    while True:
        with contextlib.suppress(Exception):  # a failed run is retried on the next tick; the audit log has the rest
            pol = app.state.org.budget_policy()
            tz = load_reporting_config().timezone
            now = datetime.now(UTC)
            fresh = {f"last_{k}_sync_at": now.isoformat(timespec="seconds") for k in ("daily", "monthly") if not pol.get(f"last_{k}_sync_at")}
            if fresh:  # the first scheduled run is the next one, not one at start-up
                await asyncio.to_thread(app.state.org.save_budget_policy, BudgetPolicy.model_validate(pol | fresh), "scheduler")
            else:
                for kind in ("monthly", "daily"):
                    if due(kind, pol, now, tz):
                        await _budget_sync(kind, "scheduler (budget allocation)")
                        break
        await asyncio.sleep(every_s)


# ---- Settings → Document validation: required documents and the standard finance checks ----
async def _validate_documents(req: IntakeRequest, raw: bytes, model, *, save: bool) -> IntakeRequest:
    """Local checks on the email and its files, with the model's grounded reading; a failed Required check holds a
    VERIFIED request for a person. A request already in the queue (same Message-ID) is returned as it is."""
    from .intake import validation

    if req.checks or req.kind not in validation.KINDS:
        return req
    from . import doa, mandate

    rules = validation.DocRules.model_validate(app.state.org.doc_rules())
    mode = mandate.check_modes(app.state.org)["documents"]
    consultant = doa.consultant_mandate(app.state.org) if req.kind == "contractor_claim" else None
    if consultant is not None:  # the delegation is the consultant's mandate, whatever the general document settings say
        rules = rules.model_copy(update={"enabled": True, "required_documents": rules.required_documents | {"contractor_claim": consultant["documents"]},
                                         "checks": rules.checks | {"required_documents": "required"}})
        mode = "hold"
    if req.kind == "expense_claim" and "doa_cash_advance" in req.flags and doa.cash_mandate(app.state.org) is not None:
        # an advance is paid before anything is spent: no receipt or claim reference yet; it is settled later
        rules = rules.model_copy(update={"required_documents": rules.required_documents | {"expense_claim": []},
                                         "checks": rules.checks | {"invoice_number_present": "off"}})
    travel = doa.travel_mandate(app.state.org) if req.kind == "expense_claim" and "doa_travel" in req.flags else None
    if travel is not None:  # the travel checklist: every document ticked is required
        need = [doa.TRAVEL_DOC_TYPES[d] for d in travel["documents"]]
        # the travel request is the claim's reference: no invoice number to look for
        rules = rules.model_copy(update={"enabled": True, "required_documents": rules.required_documents | {"expense_claim": need},
                                         "checks": rules.checks | {"required_documents": "required", "invoice_number_present": "off"}})
        mode = "hold"
        consultant = travel  # an incomplete travel claim is rejected the same way
    if not rules.enabled or mode == "off":
        return req
    docs = await asyncio.to_thread(validation.read_documents, raw)
    received = datetime.fromisoformat(req.received_at.replace("Z", "+00:00")).date()
    assist = await validation.model_assist(model, req.subject, req.text, docs, received) if model.configured else {}
    vendor = app.state.intake.registry.payees.get(req.counterparty_id or "") if req.kind != "expense_claim" else None
    bank = None
    if vendor is not None:
        ben = app.state.executor.beneficiaries.get(req.counterparty_id) or {}
        bank = ((ben.get("payload") or {}).get("beneficiary") or {}).get("bank_details") or None
    po = app.state.org.get("po", req.po_number) if req.po_number else None
    checks = validation.validate(req, docs, rules, company=app.state.org.standards().get("company_name", ""), vendor=vendor,
                                 registered_bank=bank, po=po, model=assist)
    out = validation.apply(req, checks)
    missing = next((c for c in checks if c.check == "required_documents" and c.status == "fail"), None)
    if consultant is not None and missing is not None and out.status not in ("QUARANTINED", "DUPLICATE"):
        who = "travel claim" if req.kind == "expense_claim" else "consultant's request"
        out = out.model_copy(update={"status": "REJECTED", "reasons": out.reasons + [f"rejected under the delegation of authority: the {who} is incomplete ({missing.detail})"]})
    if mode == "warn" and out.status != req.status:  # flagged for the approver, not held
        out = out.model_copy(update={"status": req.status, "reasons": out.reasons + ["document checks are set to Flag only in the mandate: flagged, not held"]})
    if save:
        await asyncio.to_thread(app.state.intake.store.save, out)
    return out


@app.get("/v1/settings/documents")
async def doc_rules_get():
    """The document-validation rules, with the catalogue of document types and checks the page offers."""
    from .intake import validation

    return {"rules": app.state.org.doc_rules(), "kinds": validation.KIND_LABEL,
            "document_types": {k: v[0] for k, v in validation.DOC_TYPES.items()},
            "checks": {k: {"label": v[0], "default": v[1], "description": v[2]} for k, v in validation.CHECKS.items()}}


@app.put("/v1/settings/documents")
async def doc_rules_put(body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    from .intake import validation

    who = _writer(role, by)
    try:
        rules = validation.DocRules.model_validate(app.state.org.doc_rules() | body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    bad = [t for ts in rules.required_documents.values() for t in ts if t not in validation.DOC_TYPES]
    bad += [k for k in rules.required_documents if k not in validation.KINDS] + [k for k in rules.checks if k not in validation.CHECKS]
    if bad or rules.stamp_min_mm > rules.stamp_max_mm:
        raise HTTPException(422, f"unknown: {bad}" if bad else "the stamp's minimum size is above its maximum")
    saved = await asyncio.to_thread(app.state.org.save_doc_rules, rules, who)
    await asyncio.to_thread(app.state.audit.append, "doc_rules", saved | {"by": who})
    return saved


# ---- Payment reports: running and accumulated, and every PO reconciled ----
def _report_rows() -> tuple[list[dict], str, int]:
    from . import payment_reports

    cfg = load_reporting_config()
    names = {k: v.get("name", k) for k, v in app.state.intake.registry.payees.items()}
    rows = payment_reports.enrich(app.state.executor.store.paid_rows(), cfg.timezone, names)
    return rows, cfg.timezone, int(app.state.org.standards().get("fiscal_year_start_month", 1))


@app.get("/v1/reports/payments")
async def payment_report(view: str = Query("running", pattern="^(running|accumulated)$"),
                         span: str = Query("month", pattern="^(day|week|month|quarter|half|year)$"),
                         date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"), format: str = Query("json", pattern="^(json|csv)$")):
    """Every PAID payment in one statement: running (today, this week, month or fiscal quarter to date, by paid date)
    or accumulated (monthly, quarterly, half-yearly or annual buckets with running totals, by reporting period)."""
    from zoneinfo import ZoneInfo

    from . import payment_reports

    rows, tz, start = await asyncio.to_thread(_report_rows)
    day = datetime.fromisoformat(date).date() if date else datetime.now(ZoneInfo(tz)).date()
    if view == "running":
        if span not in payment_reports.RUNNING:
            raise HTTPException(422, f"a running report is one of {list(payment_reports.RUNNING)}")
        rep = payment_reports.running(rows, span, day, start)
    else:
        if span not in payment_reports.ACCUMULATED:
            raise HTTPException(422, f"an accumulated report is one of {list(payment_reports.ACCUMULATED)}")
        rep = payment_reports.accumulated(rows, span, day, start)
    rep |= {"timezone": tz, "fiscal_year_start_month": start, "base_currency": app.state.org.standards()["base_currency"]}
    if format == "csv":
        name = f"venthora-payments-{view}-{span}-{day.isoformat()}.csv"
        return Response(payment_reports.to_csv(rep), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return rep


@app.get("/v1/reports/purchase-orders")
async def po_reconciliation_report():
    """Every PO against what was paid, what is in flight and what is invoiced and waiting: never over its total;
    UNDERPAID when it was closed before it was paid in full."""
    from . import payment_reports, procurement

    def build():
        paid = {r["invoice_fingerprint"] for r in app.state.executor.store.po_payments()}
        waiting: dict[str, list[dict]] = {}
        for r in app.state.intake.store.list(1000):
            if r.po_number and procurement.holds(r, paid):
                waiting.setdefault(r.po_number, []).append({"invoice_no": r.invoice_no, "amount_minor": r.amount_minor})
        return payment_reports.po_reconciliation(app.state.org, app.state.executor.store, waiting)
    return await asyncio.to_thread(build)


# ---- Overview: the balance, its equivalents, and the four numbers a finance manager acts on ----
@app.get("/v1/treasury/overview")
async def treasury_overview(span: str = Query("month", pattern="^(day|week|month|quarter|year)$")):
    """The available balance at Airwallex with what it is worth in the major currencies and the reporting currency,
    and for the chosen period: pending payments (what cannot go without a person), cash out (paid), cash in (settled
    customer payments, not top-ups), and every action that waits for a person, with where to clear it."""
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from . import payment_reports
    from .position import MAJORS, due_within, next_rate_slot, rate_slot

    pos = await treasury_position()
    base, rates = pos["base_currency"], pos["rates"]
    cfg = load_reporting_config()
    local = cfg.reporting_currency
    avail = pos["available"]["base_minor"]
    # the main account in other currencies, at the rates read at the last 00:01 / 11:59 UTC slot (kept, not re-read each view)
    now = datetime.now(UTC)
    snap = app.state.org.get("rate_snapshot", "dashboard") or {}
    if snap.get("slot") != rate_slot(now) or snap.get("base") != base or not snap.get("rates"):
        snap = {"_id": "dashboard", "slot": rate_slot(now), "base": base, "rates": dict(rates), "fetched_at": now.isoformat(timespec="seconds")}
        await asyncio.to_thread(app.state.org._put, "rate_snapshot", snap, "dashboard")
    srates = snap["rates"]
    equivalents = []
    for c in (*MAJORS, local):
        if c == base or c not in srates or any(e["currency"] == c for e in equivalents):
            continue
        equivalents.append({"currency": c, "amount": round(avail / 100 / float(srates[c]), 2), "rate": srates[c], "local": c == local})
    rows, tz, start = await asyncio.to_thread(_report_rows)
    day = datetime.now(ZoneInfo(tz)).date()
    out = payment_reports.running(rows, span, day, start)
    window = (out["from"], out["to"])
    settled = [d | {"on": payment_reports._local_day(str(d["created_at"]), tz).isoformat()} for d in pos.get("deposits_settled", []) if d.get("created_at")]
    in_window = [d for d in settled if window[0] <= d["on"] <= window[1]]
    cust = [d for d in in_window if d["customer"]]
    tops = [d for d in in_window if not d["customer"]]

    def by_ccy(items):
        o: dict[str, int] = {}
        for d in items:
            o[d["currency"]] = o.get(d["currency"], 0) + d["amount_minor"]
        return o
    bills = pos["bills_unpaid"]
    waiting = bills["waiting"]
    waiting_base = sum(x["amount_minor"] if x["currency"] == base else int(x["amount_minor"] * float(rates.get(x["currency"], 0))) for x in waiting)
    queue = await asyncio.to_thread(app.state.intake.store.list, 1000)
    store = app.state.executor.store
    payees = await asyncio.to_thread(store.list_payees)
    actions = [
        {"label": "invoices and claims to approve or reject", "count": sum(1 for r in queue if r.status in ("NEEDS_REVIEW", "QUARANTINED")), "href": "#/pending?needs"},
        {"label": "payees waiting for a call-back", "count": sum(1 for p in payees if p["status"] in ("NEW", "CHANGED")), "href": "#/payee-lifecycle"},
        {"label": "books locked by a reconciliation case", "count": len(await asyncio.to_thread(store.open_recon_cases)), "href": "#/executions"},
        {"label": "payment incidents open", "count": sum(1 for i in await asyncio.to_thread(store.list_incidents, 200) if i.get("status") != "RESOLVED"), "href": "#/executions"},
    ]
    # commitments: everything still owed (the Total Spending recap's unpaid part), what falls due in 72 hours, and after
    recap = await reports_spending_recap()
    by_report: dict[str, int] = {}
    for line in recap["lines"]:
        if line.get("left_base_minor"):
            by_report[line["report_label"]] = by_report.get(line["report_label"], 0) + line["left_base_minor"]
    committed = sum(by_report.values())
    drates = {c: Decimal(str(v)) for c, v in rates.items()}
    paid_fp = await asyncio.to_thread(lambda: store.paid_fingerprints() | store.live_payout_fingerprints())
    due72 = due_within(queue, paid_fp, drates, base, day, 72)
    horizon = (day + timedelta(days=3)).isoformat()
    inc = pos["incoming_uncleared"]

    def in_base(x):
        return x["amount_minor"] if x["currency"] == base else int(Decimal(x["amount_minor"]) * drates.get(x["currency"], Decimal(0)))
    incoming72 = sum(in_base(x) for x in inc.get("arrived_pending") or []) + sum(
        in_base(x) for x in inc.get("announced") or [] if x.get("expected") and str(x["expected"])[:10] <= horizon) + pos["deposits_pending"]["base_minor"]
    return {"as_of": pos["as_of"], "base_currency": base, "reporting_currency": local, "span": span, "span_label": out["label"],
            "window": {"from": window[0], "to": window[1]},
            "rates_snapshot": {"slot": snap["slot"], "fetched_at": snap["fetched_at"], "next": next_rate_slot(now), "slots_utc": ["00:01", "11:59"]},
            "commitments": {"total_base_minor": committed, "by_report": [{"report": k, "base_minor": v} for k, v in sorted(by_report.items(), key=lambda kv: -kv[1])],
                            "due_72h": due72, "incoming_72h_base_minor": incoming72,
                            "balance_after_commitments_base_minor": avail - committed,
                            "available_after_72h_base_minor": avail - due72["base_minor"] + incoming72, "horizon": horizon},
            "balance": {"available_base_minor": avail, "wallet": pos["available"]["wallet"], "processing_out": pos["processing_out"],
                        "equivalents": equivalents, "missing_rates": pos["missing_rates"],
                        "shortfall": bills["base_minor"] > avail, "bills_unpaid_base_minor": bills["base_minor"]},
            "pending_payment": {"base_minor": waiting_base, "count": bills["waiting_count"], "ready_base_minor": bills["base_minor"] - waiting_base,
                                "ready_count": bills["approved_count"], "exceeds_balance": bills["base_minor"] > avail},
            "cash_out": {"base_minor": out["summary"]["base_cost_minor"], "count": out["summary"]["payments"], "by_currency": out["summary"]["by_currency"],
                         "top_vendor": out["summary"]["by_vendor"][0]["payee"] if out["summary"]["by_vendor"] else None},
            "cash_in": {"base_minor": sum(d["base_minor"] or 0 for d in cust), "count": len(cust), "by_currency": by_ccy(cust),
                        "top_ups_base_minor": sum(d["base_minor"] or 0 for d in tops), "top_ups_count": len(tops)},
            "actions": {"count": sum(a["count"] for a in actions), "items": actions}}


# ---- User management → Airwallex account: connect it (sandbox), open Global Accounts ----
CONNECT_ROLES = {"super_admin"}  # Platform Setup: the FCO connects or changes the account that holds the money
GLOBAL_ACCOUNT_OPTIONS = [  # the receiving accounts Airwallex offers by currency and the country the account is held in
    {"currency": "USD", "country_code": "US", "label": "USD · United States (ACH, Fedwire)"},
    {"currency": "GBP", "country_code": "GB", "label": "GBP · United Kingdom (Faster Payments)"},
    {"currency": "EUR", "country_code": "DE", "label": "EUR · Germany (SEPA)"},
    {"currency": "AUD", "country_code": "AU", "label": "AUD · Australia (BSB)"},
    {"currency": "HKD", "country_code": "HK", "label": "HKD · Hong Kong (FPS)"},
    {"currency": "SGD", "country_code": "SG", "label": "SGD · Singapore (FAST)"},
    {"currency": "CNY", "country_code": "CN", "label": "CNY · China"},
    {"currency": "CAD", "country_code": "CA", "label": "CAD · Canada"},
    {"currency": "JPY", "country_code": "JP", "label": "JPY · Japan"},
    {"currency": "NZD", "country_code": "NZ", "label": "NZD · New Zealand"},
]


def _connector(role: str, by: str) -> str:
    if role not in CONNECT_ROLES:
        raise HTTPException(403, f"role '{role}' may not change the Airwallex connection; allowed: {', '.join(sorted(CONNECT_ROLES))}")
    return f"{by} ({role})"


def _ga_routing(g: dict) -> list[dict]:
    """The routing codes a payer needs (ACH routing number, sort code, BSB…): Airwallex gives them per supported
    feature (supported_features[].routing_codes), older answers as account_routing. Each once."""
    seen, out = set(), []
    rows = [(r, None) for r in (g.get("account_routing") or [])]
    rows += [(r, f.get("local_clearing_system")) for f in (g.get("supported_features") or []) for r in (f.get("routing_codes") or [])]
    for r, system in rows:
        key = (r.get("type"), r.get("value"))
        if r.get("value") and key not in seen:
            seen.add(key)
            out.append({"type": r.get("type"), "value": r.get("value"), "system": system})
    return out


def _ga_row(g: dict) -> dict:
    feats = g.get("supported_features") or g.get("required_features") or []
    return {"id": g.get("id"), "nick_name": g.get("nick_name") or "", "status": g.get("status"), "country_code": g.get("country_code"),
            "currencies": sorted({f.get("currency") for f in feats if f.get("currency")}) or ([g["currency"]] if g.get("currency") else []),
            "account_name": g.get("account_name"), "iban": g.get("iban"),
            "account_number": g.get("account_number") if str(g.get("account_number") or "").strip("- ") else None,  # "-" while Airwallex issues it
            "routing": _ga_routing(g),
            "swift_code": g.get("swift_code"), "institution": (g.get("institution") or {}).get("name"),
            "methods": sorted({f.get("transfer_method") for f in feats if f.get("transfer_method")})}


@app.get("/v1/airwallex/connection")
async def airwallex_connection():
    """Which Airwallex account VENTHORA works on (secrets never returned) and whether it answers now."""
    from . import airwallex_settings

    out = airwallex_settings.public()
    bank = app.state.agent.proof.airwallex
    out |= {"sandbox": bank.sandbox, "base_url": bank.base_url, "connected": False, "currencies": [], "global_accounts": 0,
            "acting_for": airwallex_settings.AirwallexStore().acting()}
    if not bank.configured:
        return out | {"error": "no Airwallex credentials: connect an account below, or set them in .env"}
    if airwallex_settings.tenant() is not None and not airwallex_settings.has_own_account():
        try:  # SaaS, no company account yet: can the platform open one for it? (its own call, nothing of its money)
            await bank.industry_categories()
        except AirwallexError as exc:
            return out | {"error": f"the VENTHORA platform cannot reach Airwallex now: {exc}"}
        return out | {"platform_ready": True, "error": None}
    try:
        bal = await bank.balances_detail()
        gas = await bank.list_global_accounts()
    except AirwallexError as exc:
        return out | {"error": f"Airwallex did not accept the credentials or did not answer: {exc}"}
    return out | {"connected": True, "currencies": sorted(c for c, b in bal.items() if any(b.values())), "global_accounts": len(gas)}


@app.put("/v1/airwallex/connection")
async def airwallex_connect(body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    """Connect an Airwallex account: the credentials log in once (sandbox) before they are kept."""
    from . import airwallex_settings
    from .gateways.airwallex import SANDBOX_URL, Airwallex

    who = _setup_writer(role, by)
    if str(body.get("environment", "sandbox")) != "sandbox":
        raise HTTPException(422, "VENTHORA executes on the Airwallex sandbox only")
    try:
        conn = airwallex_settings.Connection.model_validate(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    store = airwallex_settings.AirwallexStore()
    rec = store.merged(conn)
    if not rec["api_key"]:
        raise HTTPException(422, "paste the API key (a new client ID needs its own key)")
    trial = Airwallex(base_url=SANDBOX_URL, credentials=(rec["client_id"], rec["api_key"]), max_attempts=1)
    try:
        bal = await trial.balances_detail()
    except AirwallexError as exc:
        raise HTTPException(422, f"Airwallex refused these credentials: {exc}") from exc
    finally:
        await trial.close()
    await asyncio.to_thread(store.save, rec, who)
    app.state.agent.proof.airwallex.forget_token()
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "airwallex_connected", {"by": who, "client_id_hint": rec["client_id"][-4:],
                                                                          "currencies": sorted(c for c, b in bal.items() if any(b.values()))})
    return await airwallex_connection()


@app.delete("/v1/airwallex/connection")
async def airwallex_disconnect(role: str = "finance_manager", by: str = "dashboard"):
    """Forget the connected account; VENTHORA falls back to the credentials in .env, if any."""
    from . import airwallex_settings

    who = _setup_writer(role, by)
    await asyncio.to_thread(airwallex_settings.AirwallexStore().clear)
    app.state.agent.proof.airwallex.forget_token()
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "airwallex_disconnected", {"by": who})
    return await airwallex_connection()


class CompanyLinkIn(BaseModel):
    """The company's own Airwallex account, made before VENTHORA: its API key, created in Airwallex → Developer → API keys."""
    client_id: str = Field(min_length=8, max_length=200)
    api_key: str = Field(min_length=8, max_length=500, description="write-only, never returned")
    webhook_secret: str | None = Field(default=None, max_length=500, description="write-only; optional")
    account_name: str = Field(default="", max_length=200, description="what to call it when Airwallex does not say")


async def _read_linked(body: CompanyLinkIn) -> dict:
    """Log in with the company's own key on the sandbox and read what VENTHORA will work on: its name, balances and
    Global Accounts with their numbers. Nothing is saved."""
    from .gateways.airwallex import SANDBOX_URL, Airwallex

    trial = Airwallex(base_url=SANDBOX_URL, credentials=(body.client_id.strip(), body.api_key.strip()), max_attempts=1)
    try:
        try:
            bal = await trial.balances_detail()
        except AirwallexError as exc:
            raise HTTPException(422, f"Airwallex refused this key on the sandbox: {exc}. Check the Client ID and API key, and that the key is a sandbox key") from exc
        try:
            gas = await trial.list_global_accounts()
        except AirwallexError:
            gas = []
        acct: dict = {}
        with contextlib.suppress(Exception):  # the account's own details, when the key may read them
            acct = await trial._request("GET", "/api/v1/account") or {}
    finally:
        await trial.close()
    info = acct.get("account_details") or {}
    biz = info.get("business_details") or {}
    name = biz.get("business_name") or acct.get("nickname") or body.account_name.strip() or "Company Airwallex account"
    return {"account_name": name, "account_id": acct.get("id"), "status": acct.get("status"),
            "balances": [{"currency": c, **v} for c, v in sorted(bal.items())], "global_accounts": [_ga_row(g) for g in gas]}


@app.get("/v1/company-account")
async def company_account():
    """How the company's Airwallex account came to VENTHORA: linked (an account it already had, with its own key),
    opened here (under the platform), or none yet."""
    from . import airwallex_settings

    lk = airwallex_settings.public_link()
    acting = airwallex_settings.AirwallexStore().acting()
    return {"mode": "linked" if lk else "opened" if acting else "none", "linked": lk, "opened": acting}


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    """SaaS: a company without its own Airwallex account is a state to show (409), not an Airwallex failure (502)."""
    from fastapi.exception_handlers import http_exception_handler

    if exc.status_code == 502 and "has no Airwallex account yet" in str(exc.detail):
        exc = HTTPException(409, str(exc.detail).replace("Airwallex request failed: ", "").replace("Airwallex: ", ""), headers=exc.headers)
    return await http_exception_handler(request, exc)


@app.get("/v1/tenant")
async def tenant_info():
    """SaaS: which company this VENTHORA serves (each company has its own process and data), or self-hosted."""
    from . import airwallex_settings

    t = airwallex_settings.tenant()
    return {"saas": t is not None, **(t or {"tenant_id": None, "name": None}), "own_account": airwallex_settings.has_own_account()}


@app.get("/v1/tenant/summary")
async def tenant_summary(x_venthora_platform: str | None = Header(default=None)):
    """For the platform console only (the operator's token, set by the platform when it starts this company's process):
    the company's Airwallex account and its money totals as Airwallex reports them."""
    import hmac

    from . import airwallex_settings
    from .gateways.airwallex import to_minor

    token = env("VENTHORA_PLATFORM_TOKEN")
    if not token or not x_venthora_platform or not hmac.compare_digest(token, x_venthora_platform):
        raise HTTPException(404, "Not Found")
    lk, acting = airwallex_settings.public_link(), airwallex_settings.AirwallexStore().acting()
    out = {"tenant": airwallex_settings.tenant(), "mode": "linked" if lk else "opened" if acting else "none",
           "account": ({"name": lk["account_name"], "id": lk["account_id"]} if lk else
                       {"name": acting["name"], "id": acting["account_id"]} if acting else None),
           "opened_accounts": sorted(airwallex_settings.AirwallexStore().own_ids()),
           "fco": next(({"name": u["name"], "email": u.get("email")} for u in app.state.org.list("user") if u.get("role") == "super_admin"), None),
           "users": len(app.state.org.list("user")), "balances_minor": {}, "transfers": {}, "deposits": {}, "error": None}
    with contextlib.suppress(Exception):  # the company's AI use, last 30 days (tokens; cost only where it entered prices)
        from . import ai_usage

        t = (await asyncio.to_thread(ai_usage.summary, 30, 0))["total"]
        out["ai"] = {k: t[k] for k in ("calls", "failed", "input_tokens", "output_tokens", "cost_usd", "priced")}
    if out["mode"] == "none":
        return out
    bank = _bank()
    try:
        out["balances_minor"] = await bank.balances_minor()
        for t in await bank.list_transfers():
            ccy, st = t.get("source_currency") or t.get("currency") or "?", t.get("status") or "?"
            row = out["transfers"].setdefault(ccy, {"count": 0, "amount_minor": 0, "by_status": {}})
            row["count"] += 1
            row["amount_minor"] += to_minor(t.get("source_amount") or t.get("amount") or 0)
            row["by_status"][st] = row["by_status"].get(st, 0) + 1
        for d in await bank.list_deposits():
            row = out["deposits"].setdefault(d.get("currency") or "?", {"count": 0, "amount_minor": 0})
            row["count"] += 1
            row["amount_minor"] += to_minor(d.get("amount") or 0)
    except AirwallexError as exc:
        out["error"] = str(exc)[:200]
    return out


@app.post("/v1/company-account/link/check")
async def company_account_link_check(body: CompanyLinkIn, role: str = "finance_manager", by: str = "dashboard"):
    """Step 1: test the key and show the account, its balances and account numbers for the FCO to verify. Saves nothing."""
    _writer(role, by)
    return await _read_linked(body)


@app.post("/v1/company-account/link")
async def company_account_link(body: CompanyLinkIn, role: str = "finance_manager", by: str = "dashboard"):
    """Step 2: link the verified account. VENTHORA then works on it with its own key; the platform's keys stay as they
    are, and unlinking goes back to them."""
    from . import airwallex_settings

    who = _writer(role, by)
    seen = await _read_linked(body)
    rec = {"client_id": body.client_id.strip(), "api_key": body.api_key.strip(), "webhook_secret": (body.webhook_secret or "").strip(),
           "account_name": seen["account_name"], "account_id": seen["account_id"]}
    hook = await _subscribe_linked(rec)  # VENTHORA subscribes itself: the FCO is never asked for a webhook secret
    if hook.get("secret"):
        rec["webhook_secret"] = hook.pop("secret")
    await asyncio.to_thread(airwallex_settings.AirwallexStore().save_link, rec, who)
    app.state.agent.proof.airwallex.forget_token()
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "company_account_linked", {"by": who, "account": seen["account_name"],
                                                                              "client_id_hint": rec["client_id"][-4:],
                                                                              "global_accounts": len(seen["global_accounts"]),
                                                                              "webhooks": hook.get("action")})
    return seen | {"linked": airwallex_settings.public_link(), "webhooks": hook}


async def _subscribe_linked(rec: dict) -> dict:
    """Subscribe VENTHORA's webhook address on the company's own account with its key (create, or move an old
    subscription), so deposits and transfer updates arrive without anyone copying a secret. Airwallex returns the
    signing secret once; it is kept with the link, write-only. A failure only leaves the account polled instead."""
    import uuid

    from .gateways.airwallex import SANDBOX_URL, Airwallex
    from .webhook_gateway import PATH

    url = (env("VENTHORA_PUBLIC_URL") or "https://venthora.muwahid.org").rstrip("/") + PATH
    bank = Airwallex(base_url=SANDBOX_URL, credentials=(rec["client_id"], rec["api_key"]), max_attempts=1)
    try:
        ours = [h for h in await bank.list_webhooks() if str(h.get("url", "")).endswith(PATH)]
        if ours and ours[0].get("url") == url:
            return {"action": "already subscribed", "url": url}
        if ours:
            h = await bank.update_webhook(ours[0]["id"], url)
            return {"action": "moved to VENTHORA's address", "url": url, "secret": h.get("secret")}
        h = await bank.create_webhook(request_id=str(uuid.uuid4()), url=url, events=WEBHOOK_EVENTS, version="2025-02-14")
        return {"action": "subscribed", "url": url, "secret": h.get("secret")}
    except AirwallexError as exc:
        return {"action": "not subscribed", "url": url, "error": str(exc)[:200]}
    finally:
        await bank.close()


@app.delete("/v1/company-account/link")
async def company_account_unlink(role: str = "finance_manager", by: str = "dashboard"):
    """Unlink the company's own account: its key is forgotten and VENTHORA goes back to the platform's keys."""
    from . import airwallex_settings

    who = _writer(role, by)
    await asyncio.to_thread(airwallex_settings.AirwallexStore().clear_link)
    app.state.agent.proof.airwallex.forget_token()
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "company_account_unlinked", {"by": who})
    return await company_account()


@app.get("/v1/airwallex/global-accounts")
async def airwallex_global_accounts():
    """The account's Global Accounts (the bank details customers pay into) with the deposits that arrived in each,
    and which ones can be opened."""
    from .gateways.airwallex import to_minor

    bank = _bank()
    try:
        rows = [_ga_row(g) for g in await bank.list_global_accounts()]
        deposits = await bank.list_deposits()
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
    by_ga: dict[str, list[dict]] = {}
    src = app.state.org.get(*FUNDING)
    for d in deposits:
        if d.get("global_account_id"):
            by_ga.setdefault(d["global_account_id"], []).append(
                {"amount_minor": to_minor(d.get("amount", 0)), "currency": d.get("currency"), "status": d.get("status"),
                 "payer": (d.get("payer") or {}).get("name") or "", "reference": d.get("reference") or "", "created_at": d.get("created_at"),
                 "own_topup": _from_funding(src, d)})
    for r in rows:
        got = sorted(by_ga.get(r["id"], []), key=lambda x: str(x["created_at"] or ""), reverse=True)
        settled: dict[str, int] = {}
        for x in got:
            if x["status"] == "SETTLED":
                settled[x["currency"]] = settled.get(x["currency"], 0) + x["amount_minor"]
        r |= {"deposits": got[:5], "deposit_count": len(got), "settled_by_currency": settled}
    return {"items": rows, "options": GLOBAL_ACCOUNT_OPTIONS}


class GlobalAccountIn(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    country_code: str = Field(min_length=2, max_length=2)
    nick_name: str = Field("VENTHORA receipts", min_length=1, max_length=60)
    allow_duplicate: bool = Field(False, description="open another one although this currency and country already has one")


@app.post("/v1/airwallex/global-accounts")
async def airwallex_open_global_account(body: GlobalAccountIn, role: str = "finance_manager", by: str = "dashboard"):
    """Open a Global Account: local bank details in that currency, for customers and top-ups to pay into."""
    import uuid

    who = _connector(role, by)
    ccy, cc = body.currency.upper(), body.country_code.upper()
    if not body.allow_duplicate:
        try:
            same = [r for r in (_ga_row(g) for g in await _bank().list_global_accounts())
                    if r["country_code"] == cc and ccy in r["currencies"] and r["status"] != "CLOSED"]
        except AirwallexError as exc:
            raise HTTPException(502, f"Airwallex request failed: {exc}") from exc
        if same:
            raise HTTPException(409, f"you already have {len(same)} {ccy} account(s) in {cc} ("
                                     + ", ".join(f"{r['nick_name'] or '-'} {(r['iban'] or r['account_number'] or '')[-4:]}" for r in same)
                                     + "); customers can pay into those. Open another only if you need a separate one.")
    try:
        g = await _bank().create_global_account(request_id=str(uuid.uuid4()), country_code=body.country_code.upper(),
                                                currency=body.currency.upper(), nick_name=body.nick_name)
    except AirwallexError as exc:
        raise HTTPException(502, f"Airwallex could not open it: {exc}") from exc
    await asyncio.to_thread(app.state.audit.append, "global_account_opened", {"by": who, "currency": body.currency.upper(),
                                                                            "country_code": body.country_code.upper(), "id": g.get("id")})
    return _ga_row(g)


# ---- Airwallex account → "Don't have an account yet?": open one through the platform's connected accounts ----
class AccountSignUp(BaseModel):
    """What Airwallex needs to open an account: who it is for, where, a contact, and the two agreements."""
    legal_entity_type: str = Field("BUSINESS", pattern="^(BUSINESS|INDIVIDUAL)$")
    business_name: str = Field("", max_length=200, description="the registered company name (business)")
    first_name: str = Field("", max_length=100, description="individual")
    last_name: str = Field("", max_length=100, description="individual")
    country_code: str = Field(min_length=2, max_length=2)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    mobile: str = Field("", max_length=40)
    agreed_to_terms_and_conditions: bool = False
    agreed_to_data_usage: bool = False


class BusinessDetailsIn(BaseModel):
    """The KYB details Airwallex needs before a company account can be submitted for verification."""
    business_structure: str = Field("COMPANY", pattern="^(COMPANY|PARTNERSHIP|SOLE_PROPRIETOR|LIMITED_LIABILITY_COMPANY|TRUST|OTHER)$")
    registration_number: str = Field(min_length=3, max_length=40, description="e.g. the UEN in Singapore")
    industry_code: str = Field(min_length=3, max_length=40, description="from GET /v1/reference/industries")
    description: str = Field(min_length=5, max_length=500, description="what the business sells")
    address_line1: str = Field(min_length=3, max_length=200)
    suburb: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    postcode: str = Field(min_length=2, max_length=20)
    monthly_revenue: int = Field(ge=0, le=10**12, description="estimated, in the currency below, major units")
    revenue_currency: str = Field("USD", min_length=3, max_length=3)
    director_first_name: str = Field(min_length=1, max_length=100)
    director_last_name: str = Field(min_length=1, max_length=100)
    director_date_of_birth: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    director_nationality: str = Field(min_length=2, max_length=2)
    director_id_type: str = Field("PASSPORT", pattern="^(PASSPORT|DRIVERS_LICENSE|PERSONAL_ID)$")


def _account_row(a: dict) -> dict:
    d = a.get("account_details") or {}
    bd, idv = d.get("business_details") or {}, d.get("individual_details") or {}
    from . import airwallex_settings

    registered = bd.get("business_name") or " ".join(filter(None, (idv.get("first_name"), idv.get("last_name")))) or "-"
    # SaaS: a company only ever sees its own accounts, so they carry the company's name; the name Airwallex holds
    # (which a registry lookup on the sandbox can replace) stays visible as the registered name
    t = airwallex_settings.tenant()
    name = t["name"] if t and t.get("name") else registered
    country = ((bd.get("registration_address") or {}).get("country_code") or (idv.get("residential_address") or {}).get("country_code"))
    return {"id": a.get("id"), "status": a.get("status"), "type": d.get("legal_entity_type"), "name": name, "registered_name": registered, "country": country,
            "email": (a.get("primary_contact") or {}).get("email"), "created_at": a.get("created_at"),
            "next_action": (a.get("next_action") or {}).get("type") if isinstance(a.get("next_action"), dict) else a.get("next_action")}


def _airwallex_refusal(exc: AirwallexError) -> HTTPException:
    msg = str(exc)
    hint = (" Airwallex has not enabled account opening (connected accounts) on this platform account yet; ask the Airwallex team to enable it."
            if "cannot onboard" in msg else "")
    if exc.code in ("not_this_company", "no_company_account", "platform_wallet"):  # SaaS refusals, decided before Airwallex
        return HTTPException(exc.status or 409, f"{msg[:1].upper()}{msg[1:]}.")
    return HTTPException(502, f"Airwallex: {msg}.{hint}")


@app.get("/v1/airwallex/accounts")
async def airwallex_accounts():
    """Accounts opened from VENTHORA (Airwallex connected accounts), read from Airwallex itself."""
    try:
        return {"items": [_account_row(a) for a in await _bank().list_accounts()], "environment": "sandbox" if _bank().sandbox else "production"}
    except AirwallexError as exc:
        raise _airwallex_refusal(exc) from exc


@app.post("/v1/airwallex/accounts")
async def airwallex_open_account(body: AccountSignUp, role: str = "finance_manager", by: str = "dashboard"):
    """Open an Airwallex account from VENTHORA. The same call works on the sandbox and in production: only the
    Airwallex base URL differs (AIRWALLEX_BASE_URL). Airwallex then verifies the business or person."""
    who = _connector(role, by)
    if not (body.agreed_to_terms_and_conditions and body.agreed_to_data_usage):
        raise HTTPException(422, "the account holder must agree to Airwallex's terms and conditions and to its data usage")
    if body.legal_entity_type == "BUSINESS" and not body.business_name.strip():
        raise HTTPException(422, "enter the registered company name")
    if body.legal_entity_type == "INDIVIDUAL" and not (body.first_name.strip() and body.last_name.strip()):
        raise HTTPException(422, "enter the first and last name")
    cc = body.country_code.upper()
    details = ({"legal_entity_type": "BUSINESS", "business_details": {"business_name": body.business_name.strip(), "registration_address": {"country_code": cc}}}
               if body.legal_entity_type == "BUSINESS" else
               {"legal_entity_type": "INDIVIDUAL", "individual_details": {"first_name": body.first_name.strip(), "last_name": body.last_name.strip(),
                                                                          "residential_address": {"country_code": cc}}})
    contact = {"email": body.email.strip()} | ({"mobile": body.mobile.strip()} if body.mobile.strip() else {})
    payload = {"account_details": details, "primary_contact": contact,
               "customer_agreements": {"agreed_to_terms_and_conditions": True, "agreed_to_data_usage": True}}
    try:
        acct = await _bank().create_account(payload)
    except AirwallexError as exc:
        await asyncio.to_thread(app.state.audit.append, "airwallex_account_refused", {"by": who, "type": body.legal_entity_type, "country": cc,
                                                                                    "reason": str(exc)[:200]})
        raise _airwallex_refusal(exc) from exc
    row = _account_row(acct)
    await asyncio.to_thread(app.state.audit.append, "airwallex_account_opened", {"by": who, "id": row["id"], "status": row["status"],
                                                                               "type": row["type"], "country": cc})
    return row


@app.get("/v1/airwallex/accounts/prefill")
async def airwallex_account_prefill():
    """Every field Airwallex needs to open and verify the company's account, filled from what VENTHORA already holds
    (Finance standards, the head office, the FCO in People & roles, the company profile), each with its source; the
    required ones still empty are listed as missing. Only those are asked."""
    from . import airwallex_onboarding

    return await asyncio.to_thread(airwallex_onboarding.prefill, app.state.org)


class AccountOpenFull(BaseModel):
    values: dict = Field(description="every field from GET /v1/airwallex/accounts/prefill, as the FCO checked them")
    agreed_to_terms_and_conditions: bool = False
    agreed_to_data_usage: bool = False


@app.post("/v1/airwallex/accounts/open")
async def airwallex_account_open_full(body: AccountOpenFull, role: str = "super_admin", by: str = "dashboard"):
    """The FCO confirmed every value: open the company account, add its KYB details and hand it to Airwallex's
    verification in one go. What was typed is kept in VENTHORA so it is never asked again. If a step is refused,
    the account stays where it got to and the answer says which step and why."""
    from . import airwallex_onboarding

    who = _connector(role, by)
    if not (body.agreed_to_terms_and_conditions and body.agreed_to_data_usage):
        raise HTTPException(422, "the company must agree to Airwallex's terms and conditions and to its data usage")
    try:
        v = airwallex_onboarding.validate(body.values)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(airwallex_onboarding.keep, app.state.org, v, who)
    steps = []
    try:
        acct = await _bank().create_account(airwallex_onboarding.create_body(v))
    except AirwallexError as exc:
        await asyncio.to_thread(app.state.audit.append, "airwallex_account_refused", {"by": who, "country": v["country_code"], "reason": str(exc)[:200]})
        raise _airwallex_refusal(exc) from exc
    row = _account_row(acct)
    aid = row["id"]
    steps.append({"step": "created", "status": row["status"]})
    for name, call in (("details", lambda: _bank().update_account(aid, airwallex_onboarding.details_body(v))),
                       ("submitted", lambda: _bank().submit_account(aid))):
        try:
            row = _account_row(await call())
            steps.append({"step": name, "status": row["status"]})
        except AirwallexError as exc:
            steps.append({"step": name, "error": str(exc)[:300]})
            break
    from . import edition

    if row["status"] == "SUBMITTED" and _bank().sandbox and await asyncio.to_thread(edition.demo_on, app.state.org):
        # demo mode on the sandbox: Airwallex's verification is decided at once, so the account numbers can be shown now
        for _ in range(8):
            try:
                row = _account_row(await _bank().simulate_account_status(aid, "ACTIVE"))
                steps.append({"step": "verified (sandbox)", "status": row["status"]})
                break
            except AirwallexError:
                await asyncio.sleep(3)  # Airwallex is still taking the submission in
    bank_details = None
    if row["status"] == "ACTIVE":
        bank_details = await _open_bank_details(aid, row["name"], v["country_code"], v.get("revenue_currency"), who)
        steps.append({"step": "account numbers", "status": f"{len(bank_details['global_accounts'])} opened"})
        from . import airwallex_settings

        store = airwallex_settings.AirwallexStore()
        if not await asyncio.to_thread(store.acting):  # this VENTHORA is this company's: it now works for its account
            await asyncio.to_thread(store.act_for, aid, row["name"], who)
            app.state.position_cache = None
            steps.append({"step": "VENTHORA works for it", "status": row["name"]})
    await asyncio.to_thread(app.state.audit.append, "airwallex_account_opened", {"by": who, "id": row["id"], "status": row["status"],
                                                                               "type": "BUSINESS", "country": v["country_code"], "steps": steps})
    return row | {"steps": steps, "bank_details": bank_details}


@app.post("/v1/platform/company-profile")
async def platform_company_profile(body: Annotated[dict, Body()], x_venthora_platform: str | None = Header(default=None)):
    """SaaS, for the platform (Root) only: the company's full KYB form, filled when Root registered the company. It is
    kept, not sent: the Airwallex account is opened only once the company's FCO agrees to Airwallex's terms (saving the
    funding account, PUT /v1/funding-source)."""
    import hmac

    from . import airwallex_onboarding

    token = env("VENTHORA_PLATFORM_TOKEN")
    if not token or not x_venthora_platform or not hmac.compare_digest(token, x_venthora_platform):
        raise HTTPException(404, "Not Found")
    try:
        v = airwallex_onboarding.validate(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(airwallex_onboarding.keep, app.state.org, v, "VENTHORA platform (Root)")
    return {"kept": True}


# ---- The company's funding account: the account it already had, where its top-ups come from (no key, no access) ----
class FundingSourceIn(BaseModel):
    bank_name: str = Field(default="Airwallex", min_length=2, max_length=120)
    account_name: str = Field(min_length=2, max_length=140)
    account_number: str = Field(min_length=4, max_length=40, pattern=r"^[0-9 \-]+$")
    currency: str = Field(min_length=3, max_length=3)
    country: str = Field(default="", max_length=2)  # not asked: an Airwallex account is named by its Airwallex number
    swift_code: str = Field(default="", max_length=11)
    agreed_to_terms_and_conditions: bool = False  # the FCO's Yes in the confirmation box: the company's agreement
    agreed_to_data_usage: bool = False


FUNDING = ("funding_source", "funding_source")
FUNDING_CHECK = ("funding_check", "funding_check")
# The funding account is an Airwallex account, named by its own 16-digit Airwallex account number (the number Airwallex
# uses for transfers between Airwallex accounts, "Airwallex Pay") and its account name. Airwallex itself checks the
# number: one it does not know is refused. A US bank number (848..., ACH) cannot be checked at all: Airwallex answers
# "Unsupported transfer method" to a verification of it, so it is not accepted here.
FUNDING_CURRENCIES = ["USD", "SGD", "HKD", "AUD", "NZD", "EUR", "GBP", "CHF", "CAD", "JPY", "CNY", "IDR", "MYR", "THB", "PHP", "INR", "AED"]


class FundingCheckIn(BaseModel):  # loose on purpose: the check answers a wrong number in words, not with a schema error
    account_name: str = Field(default="", max_length=140)
    account_number: str = Field(default="", max_length=60)
    currency: str = Field(default="USD", max_length=3)


def _funding_fp(name: str, number: str, ccy: str) -> str:
    return hashlib.sha256(f"{_norm(name)}|{_norm(number)}|{ccy.upper()}".encode()).hexdigest()[:24]


def _awx_refusal_text(msg: str) -> str:
    if "(019)" in msg:
        return "The Airwallex account number has 16 digits."
    if "(104)" in msg:
        return "Airwallex could not find an account with this number. Check the number and try again."
    if "account_name" in msg:
        return "Airwallex does not accept this account name for this number. Use the name exactly as Airwallex shows it."
    return "Airwallex could not check this account right now. Try again in a moment."


@app.post("/v1/funding-source/check")
async def funding_source_check(body: FundingCheckIn, role: str = "finance_manager", by: str = "dashboard"):
    """Test connect: Airwallex checks the company's existing Airwallex account, its 16-digit Airwallex account number with
    its account name, as a recipient of an Airwallex-to-Airwallex transfer (POST /beneficiaries/validate, type
    DIGITAL_WALLET, provider AIRWALLEX). Nothing is created at Airwallex. A number Airwallex does not recognise is
    refused, and only a passed check of exactly these details can be saved."""
    from . import airwallex_settings

    if role != "super_admin":
        raise HTTPException(403, "only the FCO (Super admin) sets the company's funding account")
    if app.state.org.get(*FUNDING) and airwallex_settings.has_own_account():
        raise HTTPException(409, "the funding account is already saved")
    ccy, number, name = body.currency.upper(), re.sub(r"[\s-]", "", body.account_number), body.account_name.strip()
    if ccy not in FUNDING_CURRENCIES:
        raise HTTPException(422, "Choose the top-up currency from the list.")
    payload = {"beneficiary": {"type": "DIGITAL_WALLET", "digital_wallet": {"provider": "AIRWALLEX", "id_type": "account_number",
                                                                            "id_value": number, "account_name": name}},
               "transfer_methods": ["LOCAL"]}
    if len(name) < 2:
        res = {"passed": False, "detail": "Enter the Airwallex account name."}
    elif not number:
        res = {"passed": False, "detail": "Enter the Airwallex account number."}
    elif not number.isdigit():
        res = {"passed": False, "detail": "The Airwallex account number has digits only."}
    elif len(number) != 16:
        res = {"passed": False, "detail": f"The Airwallex account number has 16 digits; you entered {len(number)}."}
    else:
        try:  # the platform's own call: Airwallex looks the account up; nothing is created, nothing of anyone's money
            await app.state.agent.proof.airwallex._request("POST", "/api/v1/beneficiaries/validate", json=payload, own=True)
            res = {"passed": True, "detail": f"Airwallex recognises account {number[:4]} •••• {number[-4:]} ({name})"}
        except AirwallexError as exc:
            res = {"passed": False, "detail": _awx_refusal_text(str(exc))}
    res |= {"currency": ccy, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    await asyncio.to_thread(app.state.org._put, FUNDING_CHECK[0], {"_id": FUNDING_CHECK[1], "fp": _funding_fp(name, number, ccy)} | res,
                            f"{by} ({role})")
    await asyncio.to_thread(app.state.audit.append, "funding_account_checked", {"by": f"{by} ({role})", "passed": res["passed"],
                                                                                "account_last4": number[-4:], "detail": res["detail"]})
    return res
AIRWALLEX_TERMS = ("I agree, for the company, to Airwallex's Terms and Conditions and to Airwallex's use of the company's data, "
                   "and I ask VENTHORA to open the company's Airwallex account with the details registered with VENTHORA.")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _from_funding(src: dict | None, d: dict) -> bool:
    """Is this deposit a top-up from the company's own funding account? Airwallex always gives the payer's name and the
    reference; the payer's account number only for some banks and rails, so any one of the three is enough."""
    if not src:
        return False
    if src.get("reference") and _norm(src["reference"]) in _norm(d.get("reference") or ""):
        return True
    payer = d.get("payer") or {}
    if src.get("account_name") and _norm(src["account_name"]) == _norm(payer.get("name") or d.get("payer_name") or ""):
        return True
    acct = _norm(src.get("account_number"))
    nums = [_norm(str(v.get("account_number") or v.get("iban") or "")) for v in (payer.get("bank_account") or {}).values() if isinstance(v, dict)]
    return bool(acct) and any(n and (n == acct or (len(n) >= 4 and acct.endswith(n))) for n in nums)


@app.get("/v1/funding-source")
async def funding_source_get():
    """The company's funding account and where to send top-ups: its own account numbers and the reference to quote."""
    from . import airwallex_onboarding, airwallex_settings

    src = app.state.org.get(*FUNDING)
    prof = app.state.org.get(*airwallex_onboarding.PROFILE) or {}
    st = app.state.org.standards()

    out = {"source": src, "topup_to": [], "own_account": airwallex_settings.has_own_account(), "terms": AIRWALLEX_TERMS,
           "profile_ready": bool(prof),
           # what the form starts with: the company's own name, currency and country (each can be changed)
           "defaults": {"bank_name": "Airwallex", "account_name": prof.get("business_name") or st.get("company_name") or "",
                        "currency": st.get("base_currency") if st.get("base_currency") in FUNDING_CURRENCIES else "USD"},
           "currencies": FUNDING_CURRENCIES}

    if src is None:
        return out
    if out["own_account"]:
        with contextlib.suppress(AirwallexError, HTTPException):
            out["topup_to"] = [g for g in (_ga_row(x) for x in await _bank().list_global_accounts())
                               if g["status"] == "ACTIVE" and src["currency"] in g["currencies"]] or \
                              [g for g in (_ga_row(x) for x in await _bank().list_global_accounts()) if g["status"] == "ACTIVE"]
    return out


@app.put("/v1/funding-source")
async def funding_source_put(body: FundingSourceIn, role: str = "finance_manager", by: str = "dashboard"):
    """The FCO enters the account the company already has. VENTHORA never logs in to it: it is only where top-ups come
    from, recognised by its holder's name, the reference below, or its number when Airwallex passes it on.

    The first save carries the FCO's Yes to Airwallex's terms (the confirmation box): that is the company's agreement,
    kept with who and when, and VENTHORA then opens the company's Airwallex account on its own key, with the KYB details
    Root registered: the FCO sees only the result (the account numbers to top up)."""
    from . import airwallex_onboarding, airwallex_settings

    _secrets = secrets
    if role != "super_admin":
        raise HTTPException(403, "only the FCO (Super admin) sets the company's funding account")
    need_account = not airwallex_settings.has_own_account()
    if need_account and not (body.agreed_to_terms_and_conditions and body.agreed_to_data_usage):
        raise HTTPException(422, "the company's Airwallex account is opened only after the FCO agrees to Airwallex's terms (answer Yes)")
    prof = app.state.org.get(*airwallex_onboarding.PROFILE)
    if need_account and not prof:
        raise HTTPException(409, "the company's registration details are not with VENTHORA yet: ask VENTHORA to complete them")
    chk = app.state.org.get(*FUNDING_CHECK) or {}
    if not (chk.get("passed") and chk.get("fp") == _funding_fp(body.account_name, re.sub(r"[\s-]", "", body.account_number), body.currency)):
        raise HTTPException(409, "press Test connect first: these exact details must pass Airwallex's check before they are saved")
    if app.state.org.get(*FUNDING) and not need_account:  # saved and the account is open: locked (a retry is allowed until then)
        raise HTTPException(409, "the funding account is already saved")
    old = app.state.org.get(*FUNDING) or {}
    ref = old.get("reference") or "VT" + "".join(_secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    rec = {"_id": FUNDING[1], "bank_name": "Airwallex", "account_name": body.account_name.strip(),
           "account_number": re.sub(r"[\s-]", "", body.account_number), "currency": body.currency.upper(),
           "kind": "Airwallex account (Airwallex Pay)",
           "checked_at": chk.get("at"), "reference": ref, "updated_by": f"{by} ({role})",
           "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if body.agreed_to_terms_and_conditions and body.agreed_to_data_usage:
        rec["agreement"] = old.get("agreement") or {"text": AIRWALLEX_TERMS, "by": f"{by} ({role})", "at": rec["updated_at"]}
    elif old.get("agreement"):
        rec["agreement"] = old["agreement"]
    await asyncio.to_thread(app.state.org._put, FUNDING[0], rec, f"{by} ({role})")
    await asyncio.to_thread(app.state.audit.append, "funding_source_set", {"by": f"{by} ({role})", "bank": rec["bank_name"],
                                                                          "account_last4": rec["account_number"][-4:]})
    opened = None
    if need_account:
        await asyncio.to_thread(app.state.audit.append, "airwallex_terms_agreed", {"by": f"{by} ({role})", "text": AIRWALLEX_TERMS})
        values = {k: prof.get(k) for k, *_ in airwallex_onboarding.FIELDS}
        try:
            opened = await airwallex_account_open_full(AccountOpenFull(values=values, agreed_to_terms_and_conditions=True, agreed_to_data_usage=True),
                                                       role="super_admin", by=by)
        except HTTPException as exc:  # the funding account and the agreement stay; saving again retries the opening
            opened = {"error": str(exc.detail)}
        opened = {k: opened.get(k) for k in ("status", "name", "steps", "error") if k in opened}
    return await funding_source_get() | {"opened": opened}


# the currency a company's own Global Account is opened in, by its country; USD (United States) is always added
LOCAL_CCY = {"SG": "SGD", "MY": "MYR", "ID": "IDR", "HK": "HKD", "AU": "AUD", "NZ": "NZD", "GB": "GBP", "US": "USD", "CN": "CNY",
             "JP": "JPY", "CA": "CAD", "DE": "EUR", "NL": "EUR", "FR": "EUR", "AE": "AED"}


async def _open_bank_details(account_id: str, name: str, country: str | None, extra_ccy: str | None, who: str) -> dict:
    """Open the company's own receiving accounts (Global Accounts) on its behalf, in its local currency and USD, and
    keep their full details in VENTHORA (they are the numbers the company pays into: not secret). A currency or
    country Airwallex does not offer is reported, not fatal; one already open is not opened twice."""
    import uuid

    bank = _bank()
    want = []
    for cc, ccy in ((country, LOCAL_CCY.get(country or "")), ("US", "USD"), (country, extra_ccy)):
        if cc and ccy and (cc, ccy) not in want and (ccy != "USD" or cc == "US"):
            want.append((cc, ccy))
    try:
        have = [_ga_row(g) for g in (await bank._request_for(account_id, "GET", "/api/v1/global_accounts")).get("items", [])]
    except (AirwallexError, AttributeError):
        have = []
    notes = []
    for cc, ccy in want:
        if any(g["country_code"] == cc and ccy in g["currencies"] for g in have):
            continue
        try:
            g = await bank._request_for(account_id, "POST", "/api/v1/global_accounts/create", json={
                "request_id": str(uuid.uuid4()), "country_code": cc, "currency": ccy, "nick_name": f"{name[:30]} {ccy}",
                "required_features": [{"currency": ccy, "transfer_method": "LOCAL"}]})
            have.append(_ga_row(g))
        except AirwallexError as exc:
            notes.append(f"{ccy} in {cc}: {str(exc)[:160]}")
    fresh = []
    for g in have:  # read each back: the numbers can arrive a moment after creation
        try:
            fresh.append(_ga_row(await bank._request_for(account_id, "GET", f"/api/v1/global_accounts/{g['id']}")))
        except AirwallexError:
            fresh.append(g)
    rec = {"_id": account_id, "account_id": account_id, "company": name, "global_accounts": fresh, "notes": notes,
           "updated_at": datetime.now(UTC).isoformat(timespec="seconds"), "updated_by": who}
    await asyncio.to_thread(app.state.org._put, "company_bank", rec, who)
    await asyncio.to_thread(app.state.audit.append, "company_bank_details", {"account_id": account_id, "accounts": len(fresh), "by": who})
    return rec


@app.get("/v1/airwallex/accounts/{account_id}/bank-details")
async def airwallex_bank_details(account_id: str):
    """The company's account numbers (its Global Accounts) as VENTHORA keeps them, any time."""
    rec = await asyncio.to_thread(app.state.org.get, "company_bank", account_id)
    if rec is None:
        raise HTTPException(404, "no account numbers yet: open them once the account is ACTIVE")
    if any(not (g.get("account_number") or g.get("iban")) for g in rec["global_accounts"]):  # still being issued: read again
        with contextlib.suppress(AirwallexError, HTTPException):
            from . import airwallex_onboarding

            prof = await asyncio.to_thread(app.state.org.get, *airwallex_onboarding.PROFILE) or {}
            row = _account_row(await _bank().get_account(account_id))
            rec = await _open_bank_details(account_id, row["name"], row["country"], prof.get("revenue_currency"), "VENTHORA (read again)")
    return rec


@app.post("/v1/airwallex/accounts/{account_id}/bank-details")
async def airwallex_bank_details_open(account_id: str, role: str = "super_admin", by: str = "dashboard"):
    """Open (or read again) the company's account numbers; only for an ACTIVE account."""
    from . import airwallex_onboarding

    who = _connector(role, by)
    try:
        row = _account_row(await _bank().get_account(account_id))
    except AirwallexError as exc:
        raise _airwallex_refusal(exc) from exc
    if row["status"] != "ACTIVE":
        raise HTTPException(409, f"{row['name']} is {row['status']}: Airwallex opens account numbers once it has verified the company (ACTIVE)")
    prof = await asyncio.to_thread(app.state.org.get, *airwallex_onboarding.PROFILE) or {}
    return await _open_bank_details(account_id, row["name"], row["country"], prof.get("revenue_currency"), who)


@app.get("/v1/airwallex/industries")
async def airwallex_industries():
    """Airwallex's industry categories, flattened, for the KYB form."""
    try:
        cats = await _bank().industry_categories()
    except AirwallexError as exc:
        raise _airwallex_refusal(exc) from exc
    return [{"group": g.get("name"), "name": v.get("name"), "code": v.get("value")} for g in cats for v in g.get("values", [])
            if v.get("value")]


@app.post("/v1/airwallex/accounts/{account_id}/details")
async def airwallex_account_details(account_id: str, body: BusinessDetailsIn, role: str = "finance_manager", by: str = "dashboard"):
    """Add the KYB details to an opened company account: structure, registration number, industry, address, expected
    revenue and the director who is also its authorised person and beneficial owner. Then it can be submitted."""
    who = _connector(role, by)
    try:
        acct = await _bank().get_account(account_id)
    except AirwallexError as exc:
        raise _airwallex_refusal(exc) from exc
    row = _account_row(acct)
    if row["type"] != "BUSINESS":
        raise HTTPException(422, "these are company (KYB) details; this account is for a person")
    if row["status"] not in ("CREATED", "ACTION_REQUIRED"):
        raise HTTPException(409, f"the account is {row['status']}: details can be changed only before it is submitted")
    cc = row["country"] or "SG"
    addr = {"address_line1": body.address_line1.strip(), "suburb": body.suburb.strip(), "state": body.state.strip(),
            "postcode": body.postcode.strip(), "country_code": cc}
    payload = {"account_details": {"legal_entity_type": "BUSINESS", "business_details": {
        "business_name": row["name"], "business_structure": body.business_structure,
        "description_of_goods_or_services": body.description.strip(), "industry_category_code": body.industry_code,
        "operating_country": [cc], "registration_address": addr,
        "business_identifiers": [{"number": body.registration_number.strip(), "type": "BRN", "country_code": cc}],
        "account_usage": {"product_reference": ["MAKE_TRANSFERS"],
                          "estimated_monthly_revenue": {"currency": body.revenue_currency.upper(), "amount": str(body.monthly_revenue)}}},
        "business_person_details": [{
            "first_name": body.director_first_name.strip(), "last_name": body.director_last_name.strip(),
            "date_of_birth": body.director_date_of_birth, "nationality": body.director_nationality.upper(),
            "roles": ["DIRECTOR", "AUTHORISED_PERSON", "BENEFICIAL_OWNER"], "residential_address": addr,
            "identifications": {"primary": {"identification_type": body.director_id_type, "issuing_country_code": body.director_nationality.upper()}}}]}}
    try:
        acct = await _bank().update_account(account_id, payload)
    except AirwallexError as exc:
        raise _airwallex_refusal(exc) from exc
    await asyncio.to_thread(app.state.audit.append, "airwallex_account_details", {"by": who, "id": account_id})
    return _account_row(acct)


@app.post("/v1/airwallex/accounts/{account_id}/sandbox-approve")
async def airwallex_sandbox_approve(account_id: str, role: str = "finance_manager", by: str = "dashboard"):
    """Sandbox only: Airwallex's verification of a SUBMITTED account, decided at once (it becomes ACTIVE)."""
    who = _connector(role, by)
    try:
        acct = await _bank().simulate_account_status(account_id, "ACTIVE")
    except AirwallexError as exc:
        raise _airwallex_refusal(exc) from exc
    await asyncio.to_thread(app.state.audit.append, "airwallex_account_sandbox_approved", {"by": who, "id": account_id})
    return _account_row(acct)


@app.post("/v1/airwallex/accounts/{account_id}/submit")
async def airwallex_submit_account(account_id: str, role: str = "finance_manager", by: str = "dashboard"):
    """Hand an opened account to Airwallex's verification."""
    who = _connector(role, by)
    try:
        acct = await _bank().submit_account(account_id)
    except AirwallexError as exc:
        if exc.status == 400:  # a field Airwallex still needs
            raise HTTPException(422, f"Airwallex needs more details before verification ({exc}); press Complete details") from exc
        raise _airwallex_refusal(exc) from exc
    await asyncio.to_thread(app.state.audit.append, "airwallex_account_submitted", {"by": who, "id": account_id})
    return _account_row(acct)


@app.post("/v1/airwallex/accounts/{account_id}/use")
async def airwallex_use_account(account_id: str, role: str = "finance_manager", by: str = "dashboard"):
    """Work for an account opened from VENTHORA: every call then carries x-on-behalf-of with its ID, on the platform's
    own credentials, so the new account needs no API key of its own. Only an ACTIVE (verified) account can be used."""
    from . import airwallex_settings

    who = _connector(role, by)
    try:
        row = _account_row(await _bank().get_account(account_id))
    except AirwallexError as exc:
        raise _airwallex_refusal(exc) from exc
    if row["status"] != "ACTIVE":
        raise HTTPException(409, f"{row['name']} is {row['status']}: Airwallex must verify it (ACTIVE) before VENTHORA can work for it")
    await asyncio.to_thread(airwallex_settings.AirwallexStore().act_for, account_id, row["name"], who)
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "airwallex_acting_for", {"by": who, "account_id": account_id, "name": row["name"]})
    return await airwallex_connection()


@app.delete("/v1/airwallex/acting")
async def airwallex_stop_acting(role: str = "finance_manager", by: str = "dashboard"):
    """Back to the platform's own account (self-hosted only: on SaaS the platform's account is the operator's)."""
    from . import airwallex_settings

    who = _connector(role, by)
    if airwallex_settings.tenant() is not None:
        raise HTTPException(409, "on VENTHORA SaaS the company always works on its own Airwallex account")
    await asyncio.to_thread(airwallex_settings.AirwallexStore().stop_acting)
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "airwallex_acting_stopped", {"by": who})
    return await airwallex_connection()


# ---- Sign-in (auth.py): on SaaS every person signs in, and a request acts as that person, whatever it says ----------
AUTH_OPEN_EXACT = {"/", "/index.html", "/health", "/favicon.ico", "/v1/tenant", "/v1/tenant/summary", "/v1/auth/login",
                   "/v1/auth/accept", "/v1/auth/platform-invite", "/v1/auth/platform-lookup", "/v1/platform/company-profile", "/v1/platform/mailbox-owner", "/v1/platform/mail-oauth", "/oauth/mail/callback",
                   "/v1/visa/tap/keys.json"}
AUTH_OPEN_PREFIX = ("/v1/auth/invite/", "/webhooks/", "/rfq/", "/vendor-details/", "/pay-sim/", "/developers", "/.well-known/", "/approve/",
                    "/demo/legacy-erp/")  # the stand-in ERP plays an outside system: it checks its own X-Legacy-Key, not a sign-in


def _login_required() -> bool:
    """SaaS companies always sign in; a self-hosted VENTHORA may turn it on (VENTHORA_LOGIN=1)."""
    from . import airwallex_settings

    return airwallex_settings.tenant() is not None or env("VENTHORA_LOGIN") == "1"


def _session_cookie() -> str:
    from . import airwallex_settings

    t = airwallex_settings.tenant()
    return f"vs_{t['tenant_id']}" if t else "venthora_session"


def _me(u: dict) -> dict:
    unit = app.state.org.get("unit", u.get("unit_id") or "") or {}
    return {"user_id": u["user_id"], "name": u["name"], "email": u.get("email"), "role": u["role"], "unit_id": u.get("unit_id"),
            "unit": unit.get("name"), "unit_kind": unit.get("kind")}


def _signed_in(response: Response, user_id: str) -> None:
    from . import auth

    token = auth.AuthStore().session_new(user_id)
    response.set_cookie(_session_cookie(), token, max_age=auth.SESSION_HOURS * 3600, httponly=True, samesite="lax", path="/")


@app.middleware("http")
async def sessions(request: Request, call_next):
    """Declared last, so it runs first. On SaaS a request without a session is refused (401), except the sign-in
    itself, the public pages and channels that carry their own token or signature, and API keys (api_keys checks
    those). With a session, the person's role and name replace any role and by the request carries."""
    from . import auth

    path = request.url.path
    if not _login_required() or apiaccess.token_of(request.headers) is not None or path in AUTH_OPEN_EXACT or path.startswith(AUTH_OPEN_PREFIX):
        return await call_next(request)
    uid = await asyncio.to_thread(auth.AuthStore().session_user, request.cookies.get(_session_cookie()))
    user = app.state.org.get("user", uid) if uid else None
    if user is None or not user.get("active", True):
        return JSONResponse({"detail": "sign in first"}, status_code=401)
    q = [(k, v) for k, v in parse_qsl(request.scope.get("query_string", b"").decode()) if k not in ("role", "by")]
    q += [("role", user["role"]), ("by", user["name"])]
    request.scope["query_string"] = urlencode(q).encode()
    request.scope["headers"] = [(k, v) for k, v in request.scope["headers"] if k != b"x-venthora-role"] + [(b"x-venthora-role", user["role"].encode())]
    request.state.user = user
    return await call_next(request)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class AcceptIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class InviteIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)


@app.post("/v1/auth/login")
async def auth_login(body: LoginIn):
    """Email and password; a wrong one says only that it is wrong (not which), and five in five minutes lock the email."""
    from . import auth

    email = body.email.strip().lower()
    if auth.locked(email):
        raise HTTPException(429, "too many wrong passwords: wait five minutes, or ask your admin for a new sign-in link")
    user = auth.find_user(app.state.org, email)
    ok = await asyncio.to_thread(auth.AuthStore().verify, user["user_id"] if user else "-", body.password)
    if not (user and ok):
        auth.failed(email)
        await asyncio.to_thread(app.state.audit.append, "sign_in_refused", {"email": email})
        raise HTTPException(401, "wrong email or password")
    auth.cleared(email)
    out = JSONResponse(_me(user))
    await asyncio.to_thread(_signed_in, out, user["user_id"])
    await asyncio.to_thread(app.state.audit.append, "signed_in", {"by": user["name"], "role": user["role"]})
    return out


@app.post("/v1/auth/logout")
async def auth_logout(request: Request):
    from . import auth

    await asyncio.to_thread(auth.AuthStore().session_end, request.cookies.get(_session_cookie()))
    out = JSONResponse({"signed_out": True})
    out.delete_cookie(_session_cookie(), path="/")
    return out


@app.get("/v1/auth/me")
async def auth_me(request: Request):
    """Who is signed in (the session decides; on a self-hosted VENTHORA without sign-in there is nobody)."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "not signed in") if _login_required() else HTTPException(404, "sign-in is off on this VENTHORA")
    return _me(user) | {"login": True}


@app.get("/v1/auth/invite/{token}")
async def auth_invite_info(token: str):
    """Whose sign-in link this is (name, email, company), so the page can greet them; nothing is used up."""
    from . import airwallex_settings, auth

    uid = await asyncio.to_thread(auth.AuthStore().invite_user, token)
    u = app.state.org.get("user", uid) if uid else None
    if u is None or not u.get("active", True):
        raise HTTPException(404, "this sign-in link is not valid any more: ask your admin for a new one")
    t = airwallex_settings.tenant()
    return {"name": u["name"], "email": u.get("email"), "role": u["role"], "company": (t or {}).get("name")}


@app.post("/v1/auth/accept")
async def auth_accept(body: AcceptIn):
    """The person chooses their own password with their link, and is signed in."""
    from . import auth

    try:
        uid = await asyncio.to_thread(auth.AuthStore().accept, body.token, body.password)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    u = app.state.org.get("user", uid)
    out = JSONResponse(_me(u))
    await asyncio.to_thread(_signed_in, out, uid)
    await asyncio.to_thread(app.state.audit.append, "password_set", {"by": u["name"], "role": u["role"]})
    return out


def _invite_link(token: str) -> str:
    from . import airwallex_settings

    t = airwallex_settings.tenant()
    return f"/c/{t['tenant_id']}?invite={token}" if t else f"/?invite={token}"


@app.post("/v1/auth/invite")
async def auth_invite(body: InviteIn, role: str = "finance_manager", by: str = "dashboard"):
    """A one-time sign-in link for a person the caller may manage (the same reach as changing that person: the FCO
    anyone, a country, branch or project admin only the people of its unit and below, and never an equal or a
    superior). The link is shown to the caller to hand over; VENTHORA sends nothing."""
    from . import auth

    u = app.state.org.get("user", body.user_id)
    if u is None or not u.get("active", True):
        raise HTTPException(404, "no active person with that ID")
    if not u.get("email"):
        raise HTTPException(422, f"{u['name']} has no email: add one first, it is how they sign in")
    from .organisation import SCOPED_ADMINS

    if role != "super_admin":  # a link sets a password, so it is as strong as taking the account over: admins of the line only
        if role not in SCOPED_ADMINS:
            raise HTTPException(403, "only the Super admin, or a country, branch or project admin for its own people, makes sign-in links")
        if u["role"] == "super_admin":
            raise HTTPException(403, "only the Super admin makes a Super admin's sign-in link")
        _org_writer(role, by, "user", u)
    token = await asyncio.to_thread(auth.AuthStore().invite, u["user_id"], f"{by} ({role})")
    await asyncio.to_thread(app.state.audit.append, "sign_in_link_made", {"by": f"{by} ({role})", "for": u["name"], "role": u["role"]})
    return {"user_id": u["user_id"], "name": u["name"], "email": u["email"], "link": _invite_link(token), "valid_days": auth.INVITE_DAYS}


@app.post("/v1/auth/platform-invite")
async def auth_platform_invite(body: InviteIn, x_venthora_platform: str | None = Header(default=None)):
    """For the platform (Root) only: the sign-in link of a company's FCO, made when the company is added or asked again."""
    import hmac

    from . import auth

    token = env("VENTHORA_PLATFORM_TOKEN")
    if not token or not x_venthora_platform or not hmac.compare_digest(token, x_venthora_platform):
        raise HTTPException(404, "Not Found")
    u = app.state.org.get("user", body.user_id)
    if u is None or u.get("role") != "super_admin":
        raise HTTPException(404, "the platform makes links for the company's Super admin only")
    link = _invite_link(await asyncio.to_thread(auth.AuthStore().invite, u["user_id"], "VENTHORA platform (Root)"))
    await asyncio.to_thread(app.state.audit.append, "sign_in_link_made", {"by": "VENTHORA platform (Root)", "for": u["name"], "role": u["role"]})
    return {"user_id": u["user_id"], "name": u["name"], "email": u.get("email"), "link": link}


@app.post("/v1/auth/platform-lookup")
async def auth_platform_lookup(body: Annotated[dict, Body()], x_venthora_platform: str | None = Header(default=None)):
    """For the platform's /login only: does an active person of this company sign in with that email? Nothing else is
    said, and nothing is counted or logged, so asking every company leaves no trace in the others."""
    import hmac

    from . import auth

    token = env("VENTHORA_PLATFORM_TOKEN")
    if not token or not x_venthora_platform or not hmac.compare_digest(token, x_venthora_platform):
        raise HTTPException(404, "Not Found")
    return {"exists": auth.find_user(app.state.org, str(body.get("email") or "")) is not None}


@app.post("/v1/platform/mailbox-owner")
async def platform_mailbox_owner(body: Annotated[dict, Body()], x_venthora_platform: str | None = Header(default=None)):
    """For the platform only: is any of these addresses one of this company's inbound mailboxes (Settings → Email)?
    The platform asks before it hands an inbound email to a company, so mail reaches the company it was sent to."""
    import hmac

    token = env("VENTHORA_PLATFORM_TOKEN")
    if not token or not x_venthora_platform or not hmac.compare_digest(token, x_venthora_platform):
        raise HTTPException(404, "Not Found")
    asked = {str(a).strip().lower() for a in (body.get("addresses") or []) if a}
    mine = {m["address"].lower() for m in app.state.org.list("mailbox") if m.get("active", True)}
    return {"owns": bool(asked & mine), "matched": sorted(asked & mine)}


@app.get("/v1/auth/status")
async def auth_status():
    """Per person: whether they can sign in yet and when they last did (for People & roles)."""
    from . import auth

    return await asyncio.to_thread(auth.AuthStore().status)


apidocs.publish(app)  # last: one /v1 prefix, written docs, internal endpoints out of the schema
