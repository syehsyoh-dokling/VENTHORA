"""Ground truth: hand-derived answers (data/groundtruth/cases.json) compared with what VENTHORA returns.

`compare(expect, result)` is the single judge used in-process (tests), by the API check endpoint, by the
endpoint bench and by the dashboard form, so every path measures the same thing.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import ROOT

CASES = ROOT / "data" / "groundtruth" / "cases.json"


def load_cases() -> list[dict]:
    return json.loads(CASES.read_text(encoding="utf-8"))["cases"]


def plan_view(plan: dict) -> dict:
    """The fields ground truth talks about, read from a PlanResult (dict or model)."""
    if hasattr(plan, "model_dump"):
        plan = plan.model_dump()
    policy = next((s for s in plan["stages"] if s["engine"] == "Policy"), None)
    requires = ((policy or {}).get("details") or {}).get("local", {}).get("requires_approval", []) if policy else []
    actions = {d["obligation_id"]: d["action"] for d in plan["obligations"]}
    return {"decision": plan["decision"], "actions": actions,
            "paid": sorted(o for o, a in actions.items() if a in ("PAY", "CONVERT_AND_PAY")),
            "n_conversions": len(plan["conversions"]),
            "conversions": {c["buy_currency"]: c["buy_amount_minor"] for c in plan["conversions"]},
            "limit": plan.get("conversion_limit_minor"), "requires": sorted(requires),
            "not_blocked": plan["decision"] != "BLOCKED", "duration_ms": plan.get("duration_ms")}


def intake_view(req: dict) -> dict:
    if hasattr(req, "model_dump"):
        req = req.model_dump()
    return {k: req.get(k) for k in ("status", "trust", "kind", "amount_minor", "currency", "invoice_no", "due_date",
                                    "claimed_name", "duplicate_of", "flags")}


def compare(expect: dict, view: dict) -> dict:
    """{'match': bool, 'fields': {name: {'expected', 'actual', 'ok'}}}; list fields must contain the expected items."""
    fields = {}
    for key, exp in expect.items():
        act = view.get(key)
        if key == "actions":
            ok = all(act.get(o) == a for o, a in exp.items()) if isinstance(act, dict) else False
            act = {o: (act or {}).get(o) for o in exp}
        elif key == "requires" or key == "flags":
            ok = set(exp) <= set(act or [])
        elif key == "paid":
            ok = sorted(exp) == sorted(act or [])
        elif key == "conversions":
            ok = all((act or {}).get(c) == v for c, v in exp.items())
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


async def run_case(case: dict, agent, intake) -> dict:
    """Run one case in-process and judge it. Returns {'id','kind','ms','match','fields','view'}."""
    from time import perf_counter

    from ..domain.models import TreasuryState

    t0 = perf_counter()
    if case["kind"] == "plan":
        plan = await agent.decide(TreasuryState.model_validate(case["input"]))
        view = plan_view(plan)
    else:
        req = intake.ingest(case["input"].encode("utf-8"), book_fingerprints=book_fingerprints(case.get("book")), save=False)
        view = intake_view(req)
    ms = (perf_counter() - t0) * 1000
    verdict = compare(case["expect"], view)
    return {"id": case["id"], "kind": case["kind"], "title": case["title"], "ms": round(ms, 2), **verdict, "view": view}


def write_report(results: list[dict], path: Path, *, title: str, extra: dict | None = None) -> None:
    import statistics

    lines = [f"# {title}", ""]
    for k, v in (extra or {}).items():
        lines.append(f"- {k}: {v}")
    by_kind: dict[str, list[dict]] = {}
    for r in results:
        by_kind.setdefault(r["kind"], []).append(r)
    lines += ["", "| Kind | Cases | Match | p50 ms | p95 ms | max ms |", "|---|---|---|---|---|---|"]
    for kind, rs in by_kind.items():
        ms = sorted(r["ms"] for r in rs)
        lines.append(f"| {kind} | {len(rs)} | {sum(r['match'] for r in rs)} | {statistics.median(ms):.1f} | "
                     f"{ms[int(0.95 * (len(ms) - 1))]:.1f} | {ms[-1]:.1f} |")
    lines += ["", "| ID | Case | ms | Match | Mismatched fields |", "|---|---|---|---|---|"]
    for r in results:
        bad = [f"{k}: expected {v['expected']}, got {v['actual']}" for k, v in r["fields"].items() if not v["ok"]]
        lines.append(f"| {r['id']} | {r['title']} | {r['ms']:.1f} | {'✅' if r['match'] else '❌'} | {'; '.join(bad)} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
