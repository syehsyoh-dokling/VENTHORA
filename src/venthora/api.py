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

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import __version__
from .audit import AuditStore
from .config import ROOT, env, load_env

load_env()

from pydantic import BaseModel, Field  # noqa: E402

from . import ai_settings, webhooks  # noqa: E402
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
from .reporting import ReportingConfig, period_report  # noqa: E402
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
    app.state.intake = Intake(org=app.state.org, payments=app.state.executor.store)  # staff claims and POs use the org settings
    app.state.webhooks = webhooks.WebhookStore()
    app.state.hub = webhooks.Hub()
    await asyncio.to_thread(_warm_horizon)
    pending = asyncio.create_task(webhooks.confirm_pending(app.state.webhooks, app.state.agent.proof.airwallex, app.state.hub.publish))
    yield
    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pending
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
    return app.state.intake.store.list()


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
    try:
        req = await asyncio.to_thread(app.state.intake.review, intake_id, review)
    except KeyError as exc:
        raise HTTPException(404, "unknown intake request") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    await asyncio.to_thread(app.state.audit.append, "intake_review", {"intake_id": intake_id, **review.model_dump()})
    return req


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
                                     rate_cache=app.state.__dict__.setdefault("rate_cache", {}))
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
    from .connectors import ConnectorStore, demo_connector

    store = ConnectorStore()
    if not store.list():  # a fresh install shows the demo connector to the stand-in ERP, ready to sync
        store.save(demo_connector(str(request.base_url).rstrip("/")))
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
    from . import ingestion
    from .connectors import ORDER, ConnectorStore, fetch

    who = _writer(role, by)
    store = ConnectorStore()
    raw = store.get_raw(connector_id)
    if raw is None:
        raise HTTPException(404, "no such connector")
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
    return {"connector_id": connector_id, "dry_run": dry_run, "reports": reports}


@app.get("/v1/sources/master/{entity}")
async def master_list(entity: str):
    from .masterdata import ENTITIES, MasterStore

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
    for kind, label, fix in (("unit", "Organisation units", "#/users"), ("user", "People and approval limits", "#/users"),
                             ("project", "Projects", "#/codes"), ("account", "Chart of accounts", "#/codes")):
        r, a = org_real(kind)
        add("Organisation", label, "ok" if r else "placeholder", f"{a} records ({r} entered or changed by a person)", fix)
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


@app.post("/v1/org/{collection}")
async def org_save(collection: str, body: Annotated[dict, Body()], role: str = "finance_manager", by: str = "dashboard"):
    kind, model = _org_kind(collection)
    who = _writer(role, by)
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
    who = _writer(role, by)
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

