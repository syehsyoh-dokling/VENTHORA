# ruff: noqa: E501
"""The live demo session: the builder guide's five demo elements, step by step, on the real Airwallex sandbox.

  goal       the user's goal and the mandate: allocation, reserve floor, FX limit, budget lines, who approves
  plan       the initial plan from the day-1 inbox (Verity reads, Theorem plans, ProofCore proves)
  news       new information: the day-2 inbox, quoted and calibrated (Acme's payment slips five days)
  revise     the revised decision: only the decisions the news touches are reconsidered, with the diff
  deposit    more new information from the rail: the receipt lands on Airwallex (SETTLED), and the plan is revised again
  execute    the resulting financial state: execution on the sandbox, reconciliation to the cent, the book's ledger

Each step is one call; the session keeps the book, the states and the plans in memory, and every step is written to the
audit chain. The same functions the CLI uses (read_inbox, Orchestrator.decide with previous, simulate_receipt, Executor)
do the work, so the page shows the product, not a staged replay.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from time import perf_counter

from .config import ROOT, env
from .domain.models import PlanResult, TreasuryState
from .evidence.llm import ChatModel
from .evidence.reader import InboxMessage, read_inbox
from .execution.executor import Executor, terms_digest
from .execution.facts import apply_operational_facts
from .execution.fresh import fresh_book, fresh_tag
from .execution.live import with_live_balances_and_rates
from .execution.models import Approval, ExecutionRequest
from .proofcore.budget import attach_budget

STEPS = ("goal", "plan", "news", "revise", "deposit", "execute")
TITLES = {"goal": "User's goal and mandate", "plan": "Agent's initial plan", "news": "New information",
          "revise": "Revised decision", "deposit": "New information from the rail: the receipt lands",
          "execute": "Resulting financial state"}
WATCH = ("USD", "EUR", "GBP")


class LiveError(RuntimeError):
    pass


@dataclass
class LiveSession:
    sid: str
    scenario: str
    tag: str
    goal: dict
    messages: list[InboxMessage]
    timeline: list[str]
    step: int = 0
    state: TreasuryState | None = None
    plan: PlanResult | None = None
    previous: tuple[TreasuryState, PlanResult] | None = None
    deposit: dict | None = None
    execution: dict | None = None
    wallet_start: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    views: dict = field(default_factory=dict)

    def short(self, oid: str) -> str:
        return oid.removesuffix(f"-{self.tag}")


def _model() -> ChatModel:
    # Deterministic lexicon reader by default (every claim is still Verity-verified); the local LLM on request.
    return ChatModel() if env("VENTHORA_LIVE_LLM") else ChatModel(endpoint="", model="")


def plan_view(s: LiveSession, plan: PlanResult) -> dict:
    pol = next((x for x in plan.stages if x.engine == "Policy"), None)
    requires = ((pol.details or {}).get("local", {}) if pol else {}).get("requires_approval", [])
    return {"decision": plan.decision, "requires": requires, "plan_hash": plan.plan_hash, "ms": round(plan.duration_ms, 1),
            "conversion_limit_minor": plan.conversion_limit_minor, "forecast_confidence_bp": plan.forecast_confidence_bp,
            "conversions": [{"sell": c.sell_currency, "sell_minor": c.estimated_sell_minor, "buy": c.buy_currency, "buy_minor": c.buy_amount_minor}
                            for c in plan.conversions],
            "conversion_total_minor": sum(c.estimated_sell_minor for c in plan.conversions),
            "obligations": [{"id": s.short(d.obligation_id), "action": d.action, "amount_minor": d.amount_minor, "currency": d.currency,
                             "base_cost_minor": d.base_cost_minor, "reason": d.reason.replace(f"-{s.tag}", "")} for d in plan.obligations],
            "changes": [{"id": s.short(c.obligation_id), "before": c.before, "after": c.after, "reason": c.reason.replace(f"-{s.tag}", "")}
                        for c in (plan.recalculation.changes if plan.recalculation else [])],
            "locked": [s.short(i) for i in (plan.recalculation.locked if plan.recalculation else [])],
            "counterfactuals": [{"question": c.question, "feasible": c.feasible, "breach_bp": c.breach_probability_bp, "headroom_minor": c.headroom_minor}
                                for c in plan.counterfactuals],
            "stages": [{"engine": x.engine, "status": x.status, "summary": x.summary[:220]} for x in plan.stages],
            "budget": [{"line": r["line"], "status": r["status"], "utilisation_bp": r["utilisation_bp"], "available_minor": r["available_minor"]}
                       for r in (plan.budget or {}).get("lines", [])],
            "alerts": [a.get("message") for a in (plan.budget or {}).get("alerts", [])],
            "base_after_minor": plan.projected_base_balance_minor, "floor_minor": plan.required_base_floor_minor,
            "breach_bp": (plan.simulation or {}).get("breach_probability_bp"), "explanation": plan.explanation}


def _log(s: LiveSession, step: str, title: str, ms: float, detail: str) -> None:
    s.events.append({"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "step": step, "title": title, "ms": round(ms, 1), "detail": detail})


async def wallet(bank) -> dict:
    live = await bank.balances_minor()
    return {c: live.get(c, 0) for c in WATCH}


async def start(scenario: str, executor: Executor, audit) -> LiveSession:
    t0 = perf_counter()
    raw = json.loads((ROOT / "data" / "scenarios" / f"{scenario}.json").read_text(encoding="utf-8"))
    box = json.loads((ROOT / "data" / "inbox" / raw.get("inbox", "kit01_inbox.json")).read_text(encoding="utf-8"))
    tag = fresh_tag()
    s = LiveSession(sid=uuid.uuid4().hex[:12], scenario=scenario, tag=tag, goal=raw.get("goal") or {"text": raw.get("story", "")},
                    messages=[InboxMessage(**m) for m in box["messages"]], timeline=list(box.get("timeline") or []))
    bank = executor.bank
    state = fresh_book(attach_budget(TreasuryState.model_validate(raw["state"])), tag)
    state = await with_live_balances_and_rates(state, bank, ring_fence=True)
    state, notes = apply_operational_facts(state, executor.store)
    s.state = state
    s.wallet_start = await wallet(bank)
    p = state.policy
    s.views["goal"] = {"goal": s.goal, "book_id": state.book_id, "managed": dict(state.balances), "wallet": s.wallet_start,
                       "fx_to_base": dict(state.fx_to_base),
                       "mandate": {"reserve_floor_minor": p.reserve_floor_minor, "autonomous_conversion_limit_minor": p.autonomous_conversion_limit_minor,
                                   "full_confidence_bp": p.full_confidence_bp, "zero_limit_below_bp": p.zero_limit_below_bp,
                                   "escalate_flags": list(p.escalate_flags), "approver_roles": list(p.approver_roles),
                                   "horizon_hours": p.planning_horizon_hours, "max_breach_probability_bp": p.max_breach_probability_bp,
                                   "learned_fees": dict(p.payout_fees_minor),
                                   "budget": [{"line": ln.line, "mandate_minor": ln.mandate_minor, "auto_share_bp": ln.auto_share_bp, "alert_share_bp": ln.alert_share_bp}
                                              for ln in (state.budget.lines if state.budget else [])]},
                       "obligations": [{"id": s.short(o.obligation_id), "amount_minor": o.amount_minor, "currency": o.currency, "priority": o.priority,
                                        "due_in_hours": o.due_in_hours, "payee": o.payee_name} for o in state.obligations],
                       "receivables": [{"id": r.receivable_id, "payer": r.payer, "amount_minor": r.amount_minor, "currency": r.currency,
                                        "expected_in_hours": r.expected_in_hours, "confidence_bp": r.confidence_bp} for r in state.receivables],
                       "notes": notes}
    s.step = 1
    _log(s, "goal", TITLES["goal"], (perf_counter() - t0) * 1000,
         f"fresh book {state.book_id}; managed USD {state.balances.get('USD', 0) / 100:,.2f} ring-fenced from the live wallet; floor USD {p.reserve_floor_minor / 100:,.2f}")
    audit.append("live_start", {"sid": s.sid, "book_id": state.book_id, "scenario": scenario})
    return s


async def _read(s: LiveSession, day: str) -> dict:
    reading = await read_inbox(s.state, s.messages, as_of=dt.date.fromisoformat(day), model=_model())
    s.state = reading.state
    return {"as_of": day, "summary": reading.stage.summary,
            "updates": [u.model_dump() for u in reading.updates],
            "flags": [{"id": s.short(f.obligation_id), "payee": f.payee, "flag": f.flag, "verified": f.verified, "quote": f.quote, "doc_id": f.doc_id}
                      for f in reading.flags],
            "messages": [{"date": m.date, "sender": m.sender, "subject": m.subject, "text": m.text} for m in s.messages if m.date[:10] == day]}


async def step(s: LiveSession, executor: Executor, audit, *, approver: str | None = None) -> dict:
    if s.step >= len(STEPS):
        raise LiveError("the session is complete; start a new one")
    name = STEPS[s.step]
    agent = executor.agent
    t0 = perf_counter()
    if name == "plan":
        reading = await _read(s, s.timeline[0])
        s.plan = await agent.decide(s.state)
        s.views["plan"] = {"reading": reading, "plan": plan_view(s, s.plan)}
        detail = f"{s.plan.decision}: " + ", ".join(f"{o['id']} {o['action']}" for o in s.views["plan"]["plan"]["obligations"])
    elif name == "news":
        s.previous = (s.state, s.plan)
        reading = await _read(s, s.timeline[1])
        s.views["news"] = {"reading": reading}
        detail = reading["summary"]
    elif name == "revise":
        s.plan = await agent.decide(s.state, previous=s.previous)
        s.views["revise"] = {"plan": plan_view(s, s.plan)}
        ch = s.views["revise"]["plan"]["changes"]
        detail = f"{s.plan.decision}; changed: " + (", ".join(f"{c['id']} {c['before']} -> {c['after']}" for c in ch) or "nothing")
    elif name == "deposit":
        from .cli import simulate_receipt

        previous = (s.state, s.plan)
        s.state, dep = await simulate_receipt(executor.bank, s.state, executor.store, fake=False, reuse=False)
        s.deposit = dep
        if dep and dep.get("landed"):
            s.plan = await agent.decide(s.state, previous=previous)
        s.views["deposit"] = {"deposit": {k: dep.get(k) for k in ("id", "status", "amount", "currency", "settled_at", "landed")} if dep else None,
                              "plan": plan_view(s, s.plan), "wallet": await wallet(executor.bank)}
        detail = (f"deposit {dep.get('id')} {dep.get('status')}; plan {s.plan.decision}" if dep else "no receivable to land")
    else:  # execute
        approval = None
        if s.plan.decision == "REQUIRES_APPROVAL":
            if not approver:
                raise LiveError("this plan needs a person: give the approver's name (finance_manager)")
            approval = Approval(approver=approver, role="finance_manager", terms_digest=terms_digest(s.plan))
        before = await wallet(executor.bank)
        result = await executor.execute(ExecutionRequest(state=s.state, plan_hash=s.plan.plan_hash, approval=approval))
        if result.status == "COMPLETED" and s.deposit and s.deposit.get("landed"):
            executor.store.count_deposit(s.deposit["id"], s.deposit["receivable_id"], s.deposit["amount_minor"], s.deposit["currency"], result.execution_id)
        after = await wallet(executor.bank)
        book = executor.store.book_balances(s.state.book_id)
        rec = result.reconciliation
        # One row per thing that happened on Airwallex: each transfer carries its last polled status (SENT -> PAID).
        final = {}
        for x in result.steps:
            if x.kind == "simulation" and (x.detail or {}).get("status"):
                final[x.key] = x.detail["status"]
        s.execution = {"status": result.status, "message": result.message, "execution_id": result.execution_id,
                       "steps": [{"kind": x.kind, "key": s.short(x.key), "status": x.status, "airwallex_id": x.airwallex_id,
                                  "final": final.get(x.key) if x.kind == "transfer" else (x.detail or {}).get("status"),
                                  "detail": {k: v for k, v in (x.detail or {}).items() if k in ("amount", "method", "fee_minor", "sell_minor", "client_rate")}}
                                 for x in result.steps if x.kind in ("quote", "conversion", "transfer")],
                       "wallet_before": before, "wallet_after": after, "wallet_start": s.wallet_start,
                       "managed_after": rec.managed_after_minor if rec else {}, "discrepancies": rec.discrepancies_minor if rec else None,
                       "floor_ok": rec.base_floor_ok if rec else None,
                       "book": {c: {k: v for k, v in a.items() if v} for c, a in book.items()},
                       "paid": [o["id"] for o in plan_view(s, s.plan)["obligations"] if o["action"] in ("PAY", "CONVERT_AND_PAY")],
                       "not_paid": [{"id": o["id"], "action": o["action"]} for o in plan_view(s, s.plan)["obligations"] if o["action"] not in ("PAY", "CONVERT_AND_PAY")]}
        s.views["execute"] = s.execution
        detail = f"{result.status}: {result.message[:140]}"
    s.step += 1
    ms = (perf_counter() - t0) * 1000
    _log(s, name, TITLES[name], ms, detail)
    audit.append(f"live_{name}", {"sid": s.sid, "book_id": s.state.book_id, "detail": detail})
    return view(s)


def view(s: LiveSession) -> dict:
    return {"sid": s.sid, "scenario": s.scenario, "book_id": s.state.book_id if s.state else None, "step": s.step,
            "steps": [{"name": n, "title": TITLES[n], "done": i < s.step} for i, n in enumerate(STEPS)],
            "next": STEPS[s.step] if s.step < len(STEPS) else None,
            "next_needs_approval": bool(s.step < len(STEPS) and STEPS[s.step] == "execute" and s.plan and s.plan.decision == "REQUIRES_APPROVAL"),
            "managed": dict(s.state.balances) if s.state else {}, "views": s.views, "events": s.events}
