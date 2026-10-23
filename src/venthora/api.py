"""HTTP API. Run: uvicorn venthora.api:app --reload"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

from . import __version__
from .audit import AuditStore
from .config import ROOT, load_env

load_env()

from .domain.models import PlanResult, TreasuryState  # noqa: E402
from .orchestrator import Orchestrator  # noqa: E402

SCENARIOS = ROOT / "data" / "scenarios"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.agent = Orchestrator()
    app.state.audit = AuditStore()
    yield
    await app.state.agent.close()


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
    return RedirectResponse("/docs")


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


@app.post("/api/scenarios/{scenario_id}/plan", response_model=PlanResult)
async def plan_scenario(scenario_id: str):
    data = json.loads(_scenario_file(scenario_id).read_text(encoding="utf-8"))
    return await plan(TreasuryState.model_validate(data["state"]))


@app.post("/v1/plans", response_model=PlanResult)
async def plan(state: TreasuryState):
    result = await app.state.agent.decide(state)
    await asyncio.to_thread(app.state.audit.append, "plan", {"state": state.model_dump(), "result": result.model_dump()})
    return result


@app.get("/api/audit")
async def audit(limit: int = Query(20, ge=1, le=200)):
    store = app.state.audit
    return {"events": await asyncio.to_thread(store.list, limit), "integrity": await asyncio.to_thread(store.verify)}


@app.get("/api/airwallex/balances")
async def airwallex_balances():
    bank = app.state.agent.proof.airwallex
    if not bank.configured:
        raise HTTPException(503, "Airwallex sandbox credentials are not configured")
    try:
        return {"environment": "sandbox", "balances_minor": await bank.balances_minor()}
    except Exception as exc:
        raise HTTPException(502, f"Airwallex request failed: {type(exc).__name__}") from exc
