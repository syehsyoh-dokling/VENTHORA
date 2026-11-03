# ruff: noqa: E501
"""HTTP API. Run: uvicorn venthora.api:app --reload"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qsl, urlencode

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import __version__
from .audit import AuditStore
from .config import ROOT, env, load_env

load_env()

from pydantic import BaseModel, Field  # noqa: E402

from . import ai_settings, apiaccess, webhooks  # noqa: E402
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
    Mailbox,
    OrgError,
    OrgStore,
    Price,
    Project,
    PurchaseOrder,
    Standards,
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
    app.state.org = OrgStore()
    app.state.executor.org = app.state.org  # payments with a charge code are held to their budget allocation
    app.state.executor.on_result = _execution_done
    app.state.keys = apiaccess.KeyStore()
    app.state.intake = Intake(org=app.state.org, payments=app.state.executor.store)  # staff claims and POs use the org settings
    app.state.webhooks = webhooks.WebhookStore()
    app.state.hub = webhooks.Hub()
    await asyncio.to_thread(_warm_horizon)
    pending = asyncio.create_task(webhooks.confirm_pending(app.state.webhooks, app.state.agent.proof.airwallex, app.state.hub.publish))
    scheduler = asyncio.create_task(_budget_scheduler()) if env("VENTHORA_BUDGET_SCHEDULER", "1") != "0" else None
    yield
    for task in (pending, scheduler):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await app.state.agent.close()


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


app = FastAPI(title="VENTHORA", version=__version__, lifespan=lifespan,
              description="Evidence-gated autonomous treasury agent on Airwallex. "
                          "AI proposes, independent engines verify, Airwallex executes.")


@app.middleware("http")
async def api_keys(request: Request, call_next):
    """Requests carrying an API key (Settings → API access) are held to its scopes and act with its role.

    Requests without a key are the local dashboard; the gateway forwards the API to the internet only for
    requests that carry one (scripts/webhook_gateway.py --api)."""
    token = apiaccess.token_of(request.headers)
    if token is None:
        return await call_next(request)
    path, method = request.url.path, request.method
    if path.startswith(apiaccess.NO_KEY):
        return await call_next(request)  # the webhooks authenticate themselves
    key = await asyncio.to_thread(request.app.state.keys.verify, token)
    if key is None:
        return JSONResponse({"detail": "invalid, expired or revoked API key"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
    if path.startswith(apiaccess.NEVER_BY_KEY):
        return JSONResponse({"detail": "API keys are managed only from VENTHORA itself"}, status_code=403)
    if not path.startswith(apiaccess.OPEN_WITH_KEY):
        need = apiaccess.scope_for(method, path)
        if need is None:
            return JSONResponse({"detail": "this endpoint is not available to API keys"}, status_code=403)
        if need not in key["scopes"]:
            return JSONResponse({"detail": f"the key lacks the scope {need}"}, status_code=403)
    # the key's role and name replace whatever the caller sent
    q = [(k, v) for k, v in parse_qsl(request.scope.get("query_string", b"").decode()) if k not in ("role", "by")]
    q += [("role", key["role"]), ("by", f"api:{key['name']}")]
    request.scope["query_string"] = urlencode(q).encode()
    request.state.api_key = key["key_id"]
    return await call_next(request)


def _execution_done(result) -> None:
    """Every execution result reaches the dashboards at once: paid, refused or waiting for a person."""
    app.state.position_cache = None
    paid = sum(1 for st in result.steps if st.kind == "transfer" and (st.detail or {}).get("status") == "PAID")
    app.state.hub.publish("execution", {"execution_id": result.execution_id, "status": result.status, "message": result.message[:300],
                                        "paid": paid})


def _scenario_file(scenario_id: str):
    path = SCENARIOS / f"{scenario_id}.json"
    if not scenario_id.replace("_", "").replace("-", "").isalnum() or not path.is_file():
        raise HTTPException(404, "unknown scenario")
    return path


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(DASHBOARD, media_type="text/html")


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
    """Whether an operations run exists in this process, so the dashboard can offer it without probing for a 404."""
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
    operations run, with what the store has realized since (paid obligations are settled, not paid again)."""
    if scenario_id == "live":
        sessions = getattr(app.state, "live", {})
        if not sessions:
            raise HTTPException(404, "no operations run yet")
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
async def close_period(period: str, req: PeriodCloseRequest):
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
async def period_cutoff(period: str):
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
    """Every payee's live record: status, bank-details fingerprint, cooling period, who verified it."""
    return app.state.executor.store.list_payees()


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
    return FileResponse(DASHBOARD.parent / "live.html", media_type="text/html")


@app.get("/flow", include_in_schema=False)
async def flow_page():
    return FileResponse(DASHBOARD.parent / "flow.html", media_type="text/html")


@app.get("/api/live/setup")
async def live_setup(scenario: str = "demo_live"):
    """The mandate form, filled with the platform's current settings: allocation, reserve, every permission and safeguard,
    budget lines, approvers and the approval matrix, payees, the reporting cut-off, and the requests received."""
    _scenario_file(scenario)
    return live_session.defaults(scenario, app.state.executor)


@app.post("/api/live/start")
async def live_start(req: LiveStart):
    _scenario_file(req.scenario)
    if not app.state.executor.bank.configured:
        raise HTTPException(409, "Airwallex sandbox credentials are not configured")
    try:
        s = await live_session.start(req.scenario, app.state.executor, app.state.audit, req.config)
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
async def reporting_period_report(period: str):
    """Layer 1 (monthly spend by payment instant), layer 2 (invoice statements as of the period's end), layer 3
    (consolidation in the reporting currency at the period's closing rates)."""
    cfg = load_reporting_config()
    store = app.state.executor.store
    rates = store.closing_rates(period) or cfg.closing_rates.get(period, {})
    source = "snapshot" if store.closing_rates(period) else "admin fallback"
    return period_report(period, store.ledger(), cfg, rates) | {"rates_source": source}


@app.post("/v1/reporting/periods/{period}/rates")
async def snapshot_closing_rates(period: str, rates: Annotated[dict[str, str] | None, Body()] = None, role: str = "admin"):
    """Record the period's closing rates: from Airwallex's indicative rates now, or the admin's own figures."""
    if role != "admin":
        raise HTTPException(403, "only an admin may set closing rates")
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
async def grant_budget(period: str, line: str, req: GrantRequest):
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
            p = paid.setdefault(r["invoice_fingerprint"], {"amount_minor": 0, "currency": r["currency"], "transfers": [], "paid_at": r["paid_at"]})
            p["amount_minor"] += r["amount_minor"]
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


async def _ingest(raw: bytes, fps: dict | None, dry_run: bool, *, channel: str) -> IntakeRequest:
    """One email through the model reader (when configured) and the rules; queued, audited and pushed to the dashboards."""
    from .intake import reader
    from .intake.pipeline import parse_eml

    parsed = await asyncio.to_thread(parse_eml, raw)
    model = active_model()  # Settings → AI model, else .env; rules only when neither names a model
    proposals = await reader.read(parsed["text"], parsed["subject"], datetime.fromisoformat(parsed["received_at"]).date(),
                                  model) if model.configured else {}
    req = await asyncio.to_thread(app.state.intake.ingest, raw, book_fingerprints=fps, save=not dry_run, proposals=proposals)
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
        if "payee_bank_details_changed" in req.flags and req.counterparty_id:
            quoted = " | ".join(q.quote for q in req.quotes if q.field == "bank_change") or req.subject
            fp = "email:" + hashlib.sha256(quoted.encode("utf-8")).hexdigest()[:16]
            store = app.state.executor.store
            if store.payee(req.counterparty_id) is not None:
                with contextlib.suppress(PayeeError):
                    store.payee_event(req.counterparty_id, {"kind": "bank_change", "bank_fingerprint": fp,
                                                            "source": f"intake:{req.intake_id}", "quote": quoted[:300]})
    return req


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


@app.post("/v1/intake/payment-orders", response_model=IntakeRequest)
async def payment_order(body: PaymentOrderIn, role: str = "finance_manager", by: str = "dashboard"):
    """An internal payment order into the same queue as emailed invoices, through the same checks: the payee must be
    registered, the PO must match (pulled from a connected source when VENTHORA does not have it), duplicates are
    refused, attached documents are validated. It always waits for a person other than the one who asked."""
    import base64
    import uuid
    from email.message import EmailMessage

    from .intake.documents import attachments_of
    from .intake.models import Attachment

    person = next((u for u in app.state.org.list("user") if u.get("active", True)
                   and by.strip().lower() in (u["name"].lower(), u["email"].lower())), None)
    if person is None:
        raise HTTPException(403, f"{by} is not an active person in User management; only people of the organisation may ask for a payment")
    po_id = (body.po_number or "").strip().upper() or None
    pulled = None
    if po_id and app.state.org.get("po", po_id) is None:
        pulled = await _pull_po(po_id, f"order:{body.invoice_no}")
    bill = {"bill_id": f"ORDER-{uuid.uuid4().hex[:10]}", "vendor_id": body.vendor_id, "invoice_no": body.invoice_no.upper(),
            "amount_minor": body.amount_minor, "currency": body.currency.upper(), "due_date": body.due_date,
            "po_number": po_id, "description": body.description}
    req = await asyncio.to_thread(app.state.intake.ingest_record, bill, source=f"internal:{person['user_id']}")
    msg = EmailMessage()  # the attachments as one message, so documents are read and validated like an email's
    msg["From"] = person["email"]
    msg["Subject"] = f"Payment order {bill['invoice_no']}"
    msg.set_content(f"Payment order {bill['invoice_no']} for {body.vendor_id}. {body.description}")
    for a in body.attachments:
        main, _, sub = a.content_type.partition("/")
        msg.add_attachment(base64.b64decode(a.base64), maintype=main or "application", subtype=sub or "octet-stream", filename=a.name)
    reasons = [f"internal payment order by {person['name']} ({person['role'].replace('_', ' ')}); a different person approves it"]
    if pulled and pulled[0]:
        reasons.append(f"PO {po_id} was not in VENTHORA; pulled from {pulled[0]}")
    elif pulled:
        reasons.append(_po_missing_reason(po_id, pulled[1]))
    reasons += [r for r in req.reasons if not r.startswith("from the company's own system")]
    files = await asyncio.to_thread(attachments_of, msg)
    req = req.model_copy(update={"display_name": person["name"], "sender": person["email"], "flags": [*req.flags, "internal_order"],
                                 "reasons": reasons, "status": "NEEDS_REVIEW" if req.status == "VERIFIED" else req.status,
                                 "attachments": [Attachment(**{k: v for k, v in f.items() if k != "text"}) for f in files]})
    await asyncio.to_thread(app.state.intake.store.save, req)
    req = await _validate_documents(req, msg.as_bytes(), active_model(), save=True)
    app.state.position_cache = None
    app.state.hub.publish("intake", {"channel": "internal"} | req.model_dump(include={
        "intake_id", "kind", "status", "trust", "sender", "counterparty_name", "amount_minor", "currency", "invoice_no", "reader", "flags"}))
    await asyncio.to_thread(app.state.audit.append, "payment_order", req.model_dump(exclude={"text"}) | {"by": f"{person['name']} ({role})"})
    return req


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
        else:
            raw = body
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, f"not an email: {type(exc).__name__}") from exc
    req = await _ingest(raw, None, False, channel="inbound-email")
    req = await asyncio.to_thread(_route_mailbox, req, raw)
    return {"intake_id": req.intake_id, "kind": req.kind, "status": req.status}


def _route_mailbox(req: IntakeRequest, raw: bytes) -> IntakeRequest:
    """Settings → Email sources: which inbound address the email was sent to, and whether it belongs there
    (an invoice sent to the claims address waits for a person). No configured mailboxes: nothing changes."""
    from email import policy
    from email.parser import BytesParser
    from email.utils import getaddresses

    from .organisation import PURPOSE_KINDS

    boxes = [m for m in app.state.org.list("mailbox") if m.get("active", True)]
    if not boxes:
        return req
    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    to = {a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Delivered-To", []))}
    box = next((m for m in boxes if m["address"] in to), None)
    flags, reasons, status = list(req.flags), list(req.reasons), req.status
    if box is None:
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


@app.post("/v1/intake/{intake_id}/review", response_model=IntakeRequest)
async def review_intake(intake_id: str, review: ReviewRequest):
    if review.decision == "verify":
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
ORG_KINDS = {"units": ("unit", Unit), "users": ("user", User), "allocations": ("allocation", Allocation), "prices": ("price", Price), "mailboxes": ("mailbox", Mailbox),
             "projects": ("project", Project), "accounts": ("account", Account), "purchase-orders": ("po", PurchaseOrder)}


def _org_kind(collection: str):
    if collection not in ORG_KINDS:
        raise HTTPException(404, "unknown collection")
    return ORG_KINDS[collection]


def _writer(role: str, by: str) -> str:
    if role not in WRITE_ROLES:
        raise HTTPException(403, f"role '{role}' may read the organisation but not change it")
    return f"{by} ({role})"


# ---- Settings → AI model: rules only, a self-hosted model (any URL), or a provider with the user's own key ----
@app.get("/v1/settings/ai")
async def ai_settings_get():
    """The current choice; secrets are never returned, only whether one is set and its last four characters."""
    out = ai_settings.public(ai_settings.AIStore())
    return out | {"active": ai_settings.describe(active_model())}


@app.put("/v1/settings/ai")
async def ai_settings_put(s: ai_settings.AISettings, role: str = "finance_manager", by: str = "dashboard"):
    who = _writer(role, by)
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
    t0 = perf_counter()
    try:
        reply = await model.complete([{"role": "system", "content": "Reply with the single word OK."},
                                      {"role": "user", "content": "Connection test from VENTHORA."}], max_tokens=20)
    except LLMUnavailable as exc:
        return {"ok": False, "error": str(exc), "model": ai_settings.describe(model)}
    return {"ok": True, "latency_ms": round((perf_counter() - t0) * 1000), "reply": reply.strip()[:80],
            "model": ai_settings.describe(model)}


@app.post("/v1/settings/ai/models")
async def ai_settings_models(s: ai_settings.AISettings | None = None, role: str = "finance_manager"):
    """The model ids the server or provider offers, to pick from instead of typing."""
    _writer(role, "list")
    try:
        model = _draft_model(s)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not getattr(model, "endpoint", None) and not getattr(model, "api_key", None):
        raise HTTPException(422, "enter the server address or the API key first")
    try:
        return {"models": (await ai_settings.list_models(model))[:300]}
    except Exception as exc:  # noqa: BLE001 - the provider's reason, shown on the form
        raise HTTPException(502, f"could not list models: {type(exc).__name__}: {str(exc)[:200]}") from exc


class AIKeyIn(BaseModel):
    api_key: str | None = Field(None, max_length=500)
    model: str = Field("", max_length=200)
    base_url: str = Field("", max_length=300)


@app.put("/v1/settings/ai/keys/{provider}")
async def ai_key_save(provider: str, body: AIKeyIn, role: str = "finance_manager", by: str = "dashboard"):
    """Keep a provider's API key (and preferred model) in the vault, without switching the model in use."""
    who = _writer(role, by)
    if provider not in ai_settings.PROVIDERS:
        raise HTTPException(404, "unknown provider")
    try:
        out = await asyncio.to_thread(ai_settings.AIStore().save_key, provider, body.api_key, body.model, body.base_url, who)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "ai_key_saved", {"by": who, "provider": provider, "model": body.model})  # never the key
    return out | {"active": ai_settings.describe(active_model())}


@app.delete("/v1/settings/ai/keys/{provider}")
async def ai_key_delete(provider: str, role: str = "finance_manager", by: str = "dashboard"):
    who = _writer(role, by)
    try:
        out = await asyncio.to_thread(ai_settings.AIStore().delete_key, provider, who)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "ai_key_deleted", {"by": who, "provider": provider})
    return out | {"active": ai_settings.describe(active_model())}


@app.get("/v1/org/standards")
async def org_standards():
    return app.state.org.standards()


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
    if raw is not None and set(demo.entities) - set(raw["entities"]):  # entities added since it was saved
        add = {k: v for k, v in demo.entities.items() if k not in raw["entities"]}
        store.save(Connector.model_validate(raw | {"entities": raw["entities"] | add}))
    return store.list()


@app.put("/v1/sources/connectors/{connector_id}")
async def connectors_save(connector_id: str, body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    from .connectors import Connector, ConnectorStore

    who = _writer(role, by)
    try:
        c = Connector.model_validate(body | {"connector_id": connector_id})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    out = ConnectorStore().save(c)
    await asyncio.to_thread(app.state.audit.append, "connector_saved", {"id": connector_id, "by": who, "base_url": c.base_url})
    return out


@app.delete("/v1/sources/connectors/{connector_id}")
async def connectors_delete(connector_id: str, role: str = "finance_manager", by: str = "dashboard"):
    from .connectors import ConnectorStore

    _writer(role, by)
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
    if ConnectorStore().get_raw(connector_id) is None:
        raise HTTPException(404, "no such connector")
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


@app.post("/v1/sources/uploads")
async def upload_start(request: Request, entity: str, filename: str, role: str = "finance_manager"):
    """Upload a CSV, Excel or PDF file (the raw bytes as the body). The reply proposes which column feeds which field;
    nothing is written until the preview is committed."""
    import uuid

    from . import imports

    _writer(role, "upload")
    if entity not in imports.FIELDS:
        raise HTTPException(404, "unknown entity")
    data = await request.body()
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "the file is empty or larger than 15 MB")
    try:
        parsed = await asyncio.to_thread(imports.parse, data, filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    model = active_model()
    reader = "rules"
    if parsed["kind"] == "pdf" and model.configured:
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
    return {"upload_id": uid, "filename": filename, "entity": entity, "kind": parsed["kind"], "headers": parsed["headers"],
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
    """Every setting and data set VENTHORA needs, and whether it is real (from the company: sync, upload or a person),
    still a placeholder (the demo seed or the data/*.json files), or missing; with where to fix it."""
    from .masterdata import MasterStore
    from .reporting import load_config

    master, org, store = MasterStore(), app.state.org, app.state.executor.store
    counts, reg = master.counts(), app.state.intake.registry

    def real(entity: str) -> int:
        return sum(n for src, n in counts.get(entity, {}).items() if not src.startswith("seed"))

    def org_real(kind: str) -> tuple[int, int]:
        recs = org.list(kind)
        with org._connect() as c:  # who last wrote each record
            by = {r["record_id"]: r["updated_by"] for r in c.execute("SELECT record_id, updated_by FROM records WHERE kind = ?", (kind,))}
        return sum(1 for r in recs if by.get(r.get(ID_FIELD[kind])) != "seed"), len(recs)

    items = []

    def add(group, name, status, detail, fix):
        items.append({"group": group, "name": name, "status": status, "detail": detail, "fix": fix})

    bank = app.state.agent.proof.airwallex
    add("Connections", "Airwallex sandbox credentials", "ok" if bank.configured else "missing",
        "AWX_CLIENT_ID / AWX_API_KEY in .env" if bank.configured else "set AWX_CLIENT_ID and AWX_API_KEY in .env", "#/integrations")
    add("Connections", "Airwallex webhooks", "ok" if webhooks.secret() else "missing",
        "secret set; subscribe with scripts/register_webhook.py" if webhooks.secret() else "run scripts/register_webhook.py with the tunnel URL", "#/integrations")
    boxes = [m for m in org.list("mailbox") if m.get("active", True)]
    inbound = bool(env("VENTHORA_INBOUND_EMAIL_SECRET"))
    add("Connections", "Inbound email", "ok" if inbound and boxes else ("placeholder" if inbound else "missing"),
        (f"{len(boxes)} mailbox(es): " + ", ".join(m["address"] for m in boxes[:3])) if boxes else
        ("secret set, no mailbox configured yet" if inbound else "set VENTHORA_INBOUND_EMAIL_SECRET and add a mailbox"), "#/sources-email")
    m = active_model()
    add("Connections", "AI model (optional)", "ok", ai_settings.describe(m) if m.configured else "rules only: the deterministic readers decide", "#/ai")
    n_vendor_real, n_payees = real("vendor"), len(reg.payees)
    payable = sum(1 for p in store.list_payees() if p["status"] in ("VERIFIED", "ACTIVE"))
    add("Master data", "Vendors and bank details", "ok" if n_vendor_real else "placeholder",
        f"{n_payees} vendors ({n_vendor_real} from the company's data, {n_payees - n_vendor_real} from data/payees.json); {payable} verified and payable",
        "#/sources-sync")
    add("Master data", "Customers", "ok" if real("customer") else "placeholder",
        f"{len(reg.payers)} customers ({real('customer')} from the company's data)", "#/sources-sync")
    po_real, po_all = org_real("po")
    add("Master data", "Purchase orders", "ok" if po_real else "placeholder", f"{po_all} POs ({po_real} not from the demo seed)", "#/pos")
    add("Master data", "Open bills (AP)", "ok" if real("bill") else "placeholder",
        f"{real('bill')} from the company's system; the rest arrive by email", "#/sources-sync")
    add("Master data", "Receivables (AR)", "ok" if real("receivable") else "placeholder",
        f"{real('receivable')} open customer invoices from the company's system", "#/sources-sync")
    period = datetime.now(UTC).strftime("%Y-%m")
    lines = [ln for ln in master.list("budget_line") if ln["period"] == period]
    add("Plan and policy", f"Budget for {period}", "ok" if lines else "placeholder",
        f"{len(lines)} lines synced or uploaded" if lines else "data/budgets.json (demo lines)", "#/sources-upload")
    add("Plan and policy", "Tenant books", "ok" if real("tenant_book") else "placeholder",
        f"{real('tenant_book')} books from the company's data; the rest from data/tenants.json", "#/mandate")
    rep = load_config()
    add("Plan and policy", "Reporting cut-off", "ok" if getattr(rep, "set_by", None) not in (None, "", "seed") else "placeholder",
        f"day {rep.cutoff_day} {rep.cutoff_time} {rep.timezone}, reporting in {rep.reporting_currency}", "#/standards")
    for kind, label in (("unit", "Organisation units (offices, branches)"), ("user", "People and approval limits"),
                        ("project", "Projects"), ("account", "Chart of accounts"), ("price", "Unit prices")):
        r, a = org_real(kind)
        add("Organisation", label, "ok" if r else "placeholder",
            f"{a} records ({r} synced, uploaded or entered by a person; the rest are the demo seed)", "#/sources-sync")
    pol = org.budget_policy()
    add("Plan and policy", "Budget allocation", "ok",
        "No limit: every unit pays from the balance" if pol["enforcement"] == "no_limit"
        else f"Limited per allocation, {len(org.list('allocation'))} allocations", "#/budget-policy")
    summary = {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "placeholder", "missing")}
    return {"summary": summary, "items": items}


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
        out.append(st | {"po": po})
    return out


@app.get("/v1/procurement/spending")
async def procurement_spending(period: str | None = None):
    """Paid spend by office (own and with the offices below), project, account and account code; open PO commitments."""
    from . import procurement

    return procurement.spending(app.state.org, app.state.executor.store, period)


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


@app.post("/v1/org/{collection}")
async def org_save(collection: str, body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    kind, model = _org_kind(collection)
    who = _org_writer(role, by, kind, body)
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
    who = _org_writer(role, by, kind, app.state.org.get(kind, record_id))
    if kind == "po" and app.state.executor.store.po_payments(record_id):
        raise HTTPException(409, "money has been paid against this PO; close or cancel it instead, so its payments keep their PO")
    try:
        await asyncio.to_thread(app.state.org.delete, kind, record_id, who)
    except KeyError as exc:
        raise HTTPException(404, "no such record") from exc
    except OrgError as exc:
        raise HTTPException(409, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, f"org_{kind}_deleted", {"id": record_id, "by": who})
    return {"deleted": record_id}


# ---- Settings → API access: keys for other applications, and the catalogue of what they can call ----
@app.get("/v1/developer/catalog")
async def developer_catalog(request: Request):
    """Every endpoint, grouped by area, with the scope an API key needs to call it."""
    areas = [{"area": a, "label": lbl, "covers": cov, "scopes": [f"{a}:read", f"{a}:write"]} for a, (lbl, cov, _) in apiaccess.AREAS.items()]
    return {"areas": areas, "roles": list(apiaccess.ROLES), "endpoints": apiaccess.catalog(app.openapi()),
            "auth": {"header": "Authorization: Bearer <key>", "alternative": "X-API-Key: <key>",
                     "read": "GET and .../eval calls", "write": "every other call",
                     "role": "the key's role and name replace the role and by query parameters"},
            "openapi": "/openapi.json", "docs": "/docs",
            "gateway": {"command": "python scripts/webhook_gateway.py --api", "note": "forwards the API only for requests carrying a key"}}


@app.get("/v1/developer/keys")
async def developer_keys():
    return await asyncio.to_thread(app.state.keys.list)


@app.post("/v1/developer/keys")
async def developer_key_create(req: apiaccess.KeyRequest, role: str = "finance_manager", by: str = "dashboard"):
    """Create a key. The key is in this response only; VENTHORA keeps its hash."""
    who = _writer(role, by)
    rec, token = await asyncio.to_thread(app.state.keys.create, req, who)
    await asyncio.to_thread(app.state.audit.append, "api_key_created", {"key_id": rec["key_id"], "name": rec["name"], "scopes": rec["scopes"], "role": rec["role"], "by": who})
    return rec | {"key": token}


@app.post("/v1/developer/keys/{key_id}/revoke")
async def developer_key_revoke(key_id: str, role: str = "finance_manager", by: str = "dashboard"):
    who = _writer(role, by)
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
async def budget_allocation_status(period: str | None = None):
    """Every allocation of a budget period with what was spent against it and what is left."""
    from .budget_allocation import Budget

    view = await asyncio.to_thread(_budget_policy_view)
    period = period or view["active_period"]
    b = Budget(app.state.org, app.state.executor.store)
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
    rules = validation.DocRules.model_validate(app.state.org.doc_rules())
    if not rules.enabled:
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
    from zoneinfo import ZoneInfo

    from . import payment_reports
    from .position import MAJORS

    pos = await treasury_position()
    base, rates = pos["base_currency"], pos["rates"]
    cfg = load_reporting_config()
    local = cfg.reporting_currency
    avail = pos["available"]["base_minor"]
    equivalents = []
    for c in (*MAJORS, local):
        if c == base or c not in rates or any(e["currency"] == c for e in equivalents):
            continue
        equivalents.append({"currency": c, "amount": round(avail / 100 / float(rates[c]), 2), "rate": rates[c], "local": c == local})
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
        {"label": "invoices and claims to approve or reject", "count": sum(1 for r in queue if r.status in ("NEEDS_REVIEW", "QUARANTINED")), "href": "#/intake?needs"},
        {"label": "payees waiting for a call-back", "count": sum(1 for p in payees if p["status"] in ("NEW", "CHANGED")), "href": "#/payees"},
        {"label": "books locked by a reconciliation case", "count": len(await asyncio.to_thread(store.open_recon_cases)), "href": "#/executions"},
        {"label": "payment incidents open", "count": sum(1 for i in await asyncio.to_thread(store.list_incidents, 200) if i.get("status") != "RESOLVED"), "href": "#/executions"},
    ]
    return {"as_of": pos["as_of"], "base_currency": base, "reporting_currency": local, "span": span, "span_label": out["label"],
            "window": {"from": window[0], "to": window[1]},
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
CONNECT_ROLES = {"super_admin", "admin", "cfo", "finance_manager"}
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


def _ga_row(g: dict) -> dict:
    feats = g.get("supported_features") or g.get("required_features") or []
    return {"id": g.get("id"), "nick_name": g.get("nick_name") or "", "status": g.get("status"), "country_code": g.get("country_code"),
            "currencies": sorted({f.get("currency") for f in feats if f.get("currency")}) or ([g["currency"]] if g.get("currency") else []),
            "account_name": g.get("account_name"), "account_number": g.get("account_number"), "iban": g.get("iban"),
            "routing": [{"type": r.get("type"), "value": r.get("value")} for r in (g.get("account_routing") or [])],
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

    who = _connector(role, by)
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

    who = _connector(role, by)
    await asyncio.to_thread(airwallex_settings.AirwallexStore().clear)
    app.state.agent.proof.airwallex.forget_token()
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "airwallex_disconnected", {"by": who})
    return await airwallex_connection()


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
    for d in deposits:
        if d.get("global_account_id"):
            by_ga.setdefault(d["global_account_id"], []).append(
                {"amount_minor": to_minor(d.get("amount", 0)), "currency": d.get("currency"), "status": d.get("status"),
                 "payer": (d.get("payer") or {}).get("name") or "", "reference": d.get("reference") or "", "created_at": d.get("created_at")})
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


def _account_row(a: dict) -> dict:
    d = a.get("account_details") or {}
    bd, idv = d.get("business_details") or {}, d.get("individual_details") or {}
    name = bd.get("business_name") or " ".join(filter(None, (idv.get("first_name"), idv.get("last_name")))) or "-"
    country = ((bd.get("registration_address") or {}).get("country_code") or (idv.get("residential_address") or {}).get("country_code"))
    return {"id": a.get("id"), "status": a.get("status"), "type": d.get("legal_entity_type"), "name": name, "country": country,
            "email": (a.get("primary_contact") or {}).get("email"), "created_at": a.get("created_at"),
            "next_action": (a.get("next_action") or {}).get("type") if isinstance(a.get("next_action"), dict) else a.get("next_action")}


def _airwallex_refusal(exc: AirwallexError) -> HTTPException:
    msg = str(exc)
    hint = (" Airwallex has not enabled account opening (connected accounts) on this platform account yet; ask the Airwallex team to enable it."
            if "cannot onboard" in msg else "")
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


@app.post("/v1/airwallex/accounts/{account_id}/submit")
async def airwallex_submit_account(account_id: str, role: str = "finance_manager", by: str = "dashboard"):
    """Hand an opened account to Airwallex's verification."""
    who = _connector(role, by)
    try:
        acct = await _bank().submit_account(account_id)
    except AirwallexError as exc:
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
    """Back to the platform's own account."""
    from . import airwallex_settings

    who = _connector(role, by)
    await asyncio.to_thread(airwallex_settings.AirwallexStore().stop_acting)
    app.state.position_cache = None
    await asyncio.to_thread(app.state.audit.append, "airwallex_acting_stopped", {"by": who})
    return await airwallex_connection()
