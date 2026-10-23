"""Ground truth: hand-derived answers compared with what VENTHORA returns.

Two suites in data/groundtruth/: cases.json (the rules, one by one) and anomalies.json (the organizers'
failure modes, smallest to largest). Four kinds of case:
  plan       a TreasuryState  -> PlanResult            (in-process, POST /v1/plans, dashboard form)
  intake     a raw email      -> IntakeRequest         (in-process, POST /v1/intake/emails?dry_run, form)
  incident   rail facts       -> WAIT/REPLACE/ESCALATE (in-process, POST /v1/incidents/decide, form)
  execution  a plan executed on the stateful fake sandbox with injected faults -> statuses (in-process only:
             it moves fake money; the real service would move real sandbox money)
`compare(expect, view)` is the single judge used on every path.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import ROOT

SUITES = ROOT / "data" / "groundtruth"


def load_cases(suite: str = "cases") -> list[dict]:
    if not suite.replace("_", "").isalnum():
        raise FileNotFoundError(suite)
    return json.loads((SUITES / f"{suite}.json").read_text(encoding="utf-8"))["cases"]


def plan_view(plan: dict) -> dict:
    """The fields ground truth talks about, read from a PlanResult (dict or model)."""
    if hasattr(plan, "model_dump"):
        plan = plan.model_dump()
    policy = next((s for s in plan["stages"] if s["engine"] == "Policy"), None)
    requires = ((policy or {}).get("details") or {}).get("local", {}).get("requires_approval", []) if policy else []
    actions = {d["obligation_id"]: d["action"] for d in plan["obligations"]}
    fails = [s["engine"] for s in plan["stages"] if s["status"] == "FAIL"]
    live = next((s["status"] for s in plan["stages"] if s["engine"] == "Airwallex · live balances"), None)
    budget = plan.get("budget") or {}
    return {"decision": plan["decision"], "actions": actions, "live_balances": live,
            "budget_attached": bool(budget.get("attached")),
            "budget_status": {r["line"]: r["status"] for r in budget.get("lines", [])},
            "budget_available": {r["line"]: r["available_minor"] for r in budget.get("lines", [])},
            "budget_minus": {r["line"]: r["minus_minor"] for r in budget.get("lines", [])},
            "budget_alerts": len(budget.get("alerts", [])),
            "budget_requests": {q["line"]: 1 for q in budget.get("requests", [])},
            "budget_carry_forward": len(budget.get("carry_forward", [])),
            "budget_late_posted": len(budget.get("late_posted", [])),
            "budget_paying_in_period": budget.get("paying_in_period_minor"),
            "paid": sorted(o for o, a in actions.items() if a in ("PAY", "CONVERT_AND_PAY")),
            "n_conversions": len(plan["conversions"]),
            "conversions": {c["buy_currency"]: c["buy_amount_minor"] for c in plan["conversions"]},
            "limit": plan.get("conversion_limit_minor"), "requires": sorted(requires), "failed_stages": fails,
            "breach_bp": (plan.get("simulation") or {}).get("breach_probability_bp"),
            "not_blocked": plan["decision"] != "BLOCKED", "duration_ms": plan.get("duration_ms")}


def intake_view(req: dict) -> dict:
    if hasattr(req, "model_dump"):
        req = req.model_dump()
    return {k: req.get(k) for k in ("status", "trust", "kind", "amount_minor", "currency", "invoice_no", "due_date",
                                    "claimed_name", "duplicate_of", "flags")}


def incident_view(d) -> dict:
    if hasattr(d, "action"):
        return {"action": d.action, "reason": d.reason}
    return {"action": d.get("action"), "reason": d.get("reason")}


def execution_view(rec: dict) -> dict:
    """From a scenario-matrix record (testing.matrix.run_variant)."""
    ex = rec.get("execution") or {}
    inc = rec.get("incident") or {}
    return {"plan": (rec.get("plan") or {}).get("decision"), "execution": ex.get("status"), "transfers": ex.get("transfers"),
            "conversions": ex.get("conversions"), "message": ex.get("message"),
            "receipt_landed": (rec.get("receipt") or {}).get("landed"),
            "incident": inc.get("status"), "incident_decision": inc.get("decision"),
            "invariants_ok": rec.get("ok"), "errors": rec.get("errors")}


def view_of(kind: str, result) -> dict:
    return {"plan": plan_view, "intake": intake_view, "incident": incident_view, "execution": execution_view}[kind](result)


def compare(expect: dict, view: dict) -> dict:
    """{'match': bool, 'fields': {name: {'expected', 'actual', 'ok'}}}.
    actions/conversions: every expected entry present; requires/flags: expected ⊆ actual; paid: equal as sets;
    *_in: actual in the expected list; *_contains: substring; *_min / *_max: bounds."""
    fields = {}
    for key, exp in expect.items():
        act = view.get(key)
        if key == "actions":
            ok = all(act.get(o) == a for o, a in exp.items()) if isinstance(act, dict) else False
            act = {o: (act or {}).get(o) for o in exp}
        elif key in ("requires", "flags", "failed_stages"):
            ok = set(exp) <= set(act or [])
        elif key == "paid":
            ok = sorted(exp) == sorted(act or [])
        elif key == "conversions" and isinstance(exp, dict):
            ok = all((act or {}).get(c) == v for c, v in exp.items())
        elif key.endswith("_in"):
            act = view.get(key[:-3])
            ok = act in exp
        elif key.endswith("_contains"):
            act = view.get(key[:-9])
            ok = isinstance(act, str) and exp.lower() in act.lower()
        elif key.endswith("_min"):
            act = view.get(key[:-4])
            ok = act is not None and act >= exp
        elif key.endswith("_max"):
            act = view.get(key[:-4])
            ok = act is not None and act <= exp
        else:
            ok = act == exp
        fields[key] = {"expected": exp, "actual": act, "ok": bool(ok)}
    return {"match": all(f["ok"] for f in fields.values()), "fields": fields}


def book_fingerprints(scenario_id: str | None) -> dict[str, str]:
    if not scenario_id:
        return {}
    path = ROOT / "data" / "scenarios" / f"{scenario_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))["state"]
    return {o["invoice_fingerprint"]: o["obligation_id"] for o in data["obligations"] if o.get("invoice_fingerprint")}


def decide_incident(inp: dict):
    from types import SimpleNamespace

    from ..execution.incidents import decide
    from ..gateways.airwallex import TransferResult

    transfer = TransferResult(transfer_id="t", status=str(inp["status"]).upper(), source_currency="", transfer_currency="",
                              transfer_amount_minor=0, amount_payer_pays_minor=0, fee_minor=0, failure_reason=None, raw={},
                              failure_type=inp.get("failure_type"))
    policy = SimpleNamespace(incident_wait_hours=inp.get("incident_wait_hours", 48),
                             max_replacements=inp.get("max_replacements", 1))
    return decide(transfer, inp.get("hours_in_flight", 0), policy, inp.get("replacements_so_far", 0))


async def run_execution(inp: dict, workdir: Path) -> dict:
    """An execution case is a scenario-matrix variant: the fake sandbox with injected faults."""
    from .matrix import Variant, run_variant

    v = Variant(id=inp.get("id", "gt"), family="groundtruth", severity="n/a", title=inp.get("title", ""),
                state=inp["state"], wallet=inp.get("wallet"),
                faults={k: [tuple(x) for x in q] for k, q in inp.get("faults", {}).items()},
                lose_response=set(inp.get("lose_response", [])), quote_markup=inp.get("quote_markup"),
                fail_on_paid=set(inp.get("fail_on_paid", [])), deposits_settle=inp.get("deposits_settle", True),
                conversion_status=inp.get("conversion_status", "SETTLED"), rail_rates=inp.get("rail_rates"),
                approve=inp.get("approve", True), receipt=inp.get("receipt", False), twice=inp.get("twice", False),
                close_cases=inp.get("close_cases", False), fees=inp.get("fees"), hidden_charge=inp.get("hidden_charge"),
                incident=tuple(inp["incident"]) if inp.get("incident") else None)
    return await run_variant(v, workdir)


async def run_case(case: dict, agent, intake, workdir: Path | None = None) -> dict:
    """Run one case in-process and judge it. Returns {'id','kind','ms','match','fields','view'}."""
    import os
    import tempfile
    from time import perf_counter

    from ..domain.models import TreasuryState

    t0 = perf_counter()
    kind = case["kind"]
    if kind == "plan":
        view = plan_view(await agent.decide(TreasuryState.model_validate(case["input"])))
    elif kind == "intake":
        req = intake.ingest(case["input"].encode("utf-8"), book_fingerprints=book_fingerprints(case.get("book")), save=False)
        view = intake_view(req)
    elif kind == "incident":
        view = incident_view(decide_incident(case["input"]))
    else:
        # The fake sandbox needs "credentials" to exist; restore the environment afterwards so later plan cases
        # (which share the agent) do not suddenly see a configured-but-unreachable Airwallex and get BLOCKED.
        saved = {k: os.environ.get(k) for k in ("AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY")}
        os.environ["AIRWALLEX_CLIENT_ID"] = os.environ["AIRWALLEX_API_KEY"] = "groundtruth-fake"
        try:
            view = execution_view(await run_execution(case["input"] | {"id": case["id"], "title": case["title"]},
                                                      workdir or Path(tempfile.mkdtemp(prefix="venthora-gt-"))))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    ms = (perf_counter() - t0) * 1000
    verdict = compare(case["expect"], view)
    return {"id": case["id"], "kind": kind, "title": case["title"], "ms": round(ms, 2), **verdict, "view": view}
