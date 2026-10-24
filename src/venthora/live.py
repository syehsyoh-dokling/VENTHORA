# ruff: noqa: E501
"""The operations console: the builder guide's five demo elements, step by step, on the real Airwallex sandbox.

  goal       the finance manager grants the mandate (a form: allocation, reserve, every permission and safeguard, budget,
             approvers, payees, reporting cut-off) and the book opens on the live wallet
  plan       the initial plan from the requests received so far (Verity reads, Theorem plans, ProofCore proves)
  news       new information: a later email, quoted and calibrated
  revise     the revised decision: only the decisions the news touches are reconsidered, with the diff
  deposit    new information from the rail: the incoming payment lands on Airwallex (SETTLED), and the plan is revised again
  execute    the resulting financial state: execution on the sandbox, reconciliation to the cent, the book's ledger

The mandate is one specification (MANDATES): the form is drawn from it, and the same specification turns the submitted
form into the policy, the budget, the approvers and the allowed payees, so no row of the form is decorative. A permission
that is unchecked is revoked: VENTHORA must then ask a person for that action. A safeguard that is unchecked is not
enforced. Rows marked locked cannot be revoked.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from time import perf_counter

from .config import ROOT, env
from .domain.models import BookApprover, BookConfig, PlanResult, Tenancy, TreasuryState
from .evidence.llm import ChatModel
from .evidence.reader import InboxMessage, read_inbox
from .execution.executor import Executor, terms_digest
from .execution.facts import apply_operational_facts
from .execution.fresh import fresh_book, fresh_tag
from .execution.live import with_live_balances_and_rates
from .execution.models import Approval, ExecutionRequest
from .proofcore.budget import attach_budget

STEPS = ("goal", "plan", "news", "revise", "deposit", "execute")
TITLES = {"goal": "Goal and mandate", "plan": "Initial plan", "news": "New information",
          "revise": "Revised decision", "deposit": "Incoming payment confirmed by Airwallex",
          "execute": "Execution and resulting state"}
WATCH = ("USD", "EUR", "GBP")
CHANNELS = {"ap:invoice": "Accounts payable system", "hris": "Payroll system", "email:ap-inbox": "Email · AP inbox",
            "email:customer": "Email · customer", "inbox": "Email", "airwallex": "Airwallex"}

# The mandate. kind: grant = a permission VENTHORA holds (unchecked = revoked, a person decides instead);
# guard = a safeguard VENTHORA keeps (unchecked = not enforced); locked = always on.
# unit: usd (minor units in the payload), pct (basis points), days, hours, level.
MANDATES: list[dict] = [
    {"key": "pay_single", "kind": "grant", "label": "Make a single payout on its own, up to", "unit": "usd", "value": 1_000_000,
     "policy": "max_single_payment_minor", "revoked": 0, "help": "Revoked: every payout waits for an approver."},
    {"key": "pay_per_payee", "kind": "grant", "label": "Pay one payee on its own, per reporting period, up to", "unit": "usd", "value": 1_500_000,
     "policy": "max_per_payee_period_minor", "revoked": 0, "help": "Revoked: every payout waits for an approver."},
    {"key": "pay_first", "kind": "grant", "label": "Make a first payment to a payee on its own, up to", "unit": "usd", "value": 1_000_000,
     "policy": "max_first_payment_minor", "revoked": 0, "help": "Revoked: a payee's first payment always waits for an approver."},
    {"key": "outflow_daily", "kind": "grant", "label": "Move money out on its own, per 24 hours, up to", "unit": "usd", "value": 3_000_000,
     "policy": "max_outflow_daily_minor", "revoked": 0, "help": "Revoked: no outflow without an approver."},
    {"key": "outflow_weekly", "kind": "grant", "label": "Move money out on its own, per 7 days, up to", "unit": "usd", "value": 6_000_000,
     "policy": "max_outflow_weekly_minor", "revoked": 0, "help": "Revoked: no outflow without an approver."},
    {"key": "convert_fx", "kind": "grant", "label": "Convert currencies on its own, when receipts are fully supported, up to", "unit": "usd", "value": 1_000_000,
     "policy": "autonomous_conversion_limit_minor", "revoked": 0, "help": "The limit shrinks as receipt confidence falls. Revoked: every conversion waits for an approver."},
    {"key": "convert_daily", "kind": "grant", "label": "Sell base currency in conversions on its own, per 24 hours, up to", "unit": "usd", "value": 2_000_000,
     "policy": "max_conversion_daily_minor", "revoked": 0, "help": "Revoked: every conversion waits for an approver."},
    {"key": "rely_receipts", "kind": "grant", "label": "Defer a bill against an expected customer payment, at a confidence of at least", "unit": "pct", "value": 8_000,
     "policy": "min_receivable_confidence_bp", "revoked": 10_000, "help": "Revoked: no expected payment is relied on; what cannot be paid now goes to a person."},
    {"key": "budget_auto", "kind": "grant", "label": "Spend within each budget line's autonomous share (set per line below)", "unit": None, "value": None,
     "policy": None, "revoked": None, "help": "Revoked: every budgeted payout waits for an approver."},
    {"key": "risk", "kind": "grant", "label": "Accept a simulated probability of breaching the reserve of up to", "unit": "pct", "value": 500,
     "policy": "max_breach_probability_bp", "revoked": 0, "help": "Revoked: any simulated breach risk waits for an approver."},
    {"key": "fx_open", "kind": "guard", "label": "Keep the open position in each foreign currency at or below", "unit": "usd", "value": 100_000,
     "policy": "max_open_position_minor", "off": {}, "help": "Applies to EUR and GBP, in that currency's units."},
    {"key": "hedge", "kind": "guard", "label": "Keep foreign obligations due in the horizon covered at least", "unit": "pct", "value": 8_000,
     "policy": "min_hedge_ratio_bp", "off": None, "help": "Cash held plus bought, against what is due."},
    {"key": "overdue", "kind": "guard", "label": "Call a person when an unpaid invoice is overdue by more than", "unit": "days", "value": 30,
     "policy": "overdue_escalation_days", "off": None, "help": "Late interest is accrued at the contract's rate, else the default below."},
    {"key": "late_interest", "kind": "guard", "label": "Call a person when late-interest exposure exceeds", "unit": "usd", "value": 50_000,
     "policy": "max_late_interest_exposure_minor", "off": None, "help": "Accrued plus projected over the horizon, for what is left unpaid."},
    {"key": "dunning", "kind": "guard", "label": "Call a person when a payee's reminder reaches level (1 reminder, 2 second, 3 final notice)", "unit": "level", "value": 3,
     "policy": "dunning_escalation_level", "off": None, "help": "Read from the payee's own emails."},
    {"key": "late_rate", "kind": "locked", "label": "Default late-payment interest when the contract states none, per year", "unit": "pct", "value": 800,
     "policy": "late_interest_bp_default", "help": "Used for the aging report and exposure."},
    {"key": "three_way", "kind": "locked", "label": "Pay an invoice only within its contract (three-way match tolerance)", "unit": "pct", "value": 50,
     "policy": "amount_tolerance_bp", "help": "Above the tolerance a person decides."},
    {"key": "cooling", "kind": "locked", "label": "After a payee changes bank details: call-back on the registered number, then a cooling period of", "unit": "hours", "value": 72,
     "policy": "payee_cooling_hours", "help": "No payment at all until the call-back; a person approves every payment until the cooling period has passed."},
    {"key": "duplicates", "kind": "locked", "label": "Never pay the same invoice, or the same instalment, twice", "unit": None, "value": None, "policy": None,
     "help": "Claimed before any money moves."},
    {"key": "evidence", "kind": "locked", "label": "Act only on evidence quoted verbatim from a dated document", "unit": None, "value": None, "policy": None,
     "help": "A claim nobody can quote has no effect."},
    {"key": "recon", "kind": "locked", "label": "Stop the book when a reconciliation does not add up, until a person closes the case", "unit": None, "value": None, "policy": None,
     "help": "Wallet movement against every book's cash lines, to the cent."},
]


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
    book_name: str = ""

    def short(self, oid: str) -> str:
        return oid.removesuffix(f"-{self.tag}")


def _model() -> ChatModel:
    # Deterministic lexicon reader by default (every claim is still Verity-verified); the local LLM on request.
    return ChatModel() if env("VENTHORA_LIVE_LLM") else ChatModel(endpoint="", model="")


def _scenario(scenario: str) -> tuple[dict, dict]:
    raw = json.loads((ROOT / "data" / "scenarios" / f"{scenario}.json").read_text(encoding="utf-8"))
    box = json.loads((ROOT / "data" / "inbox" / raw.get("inbox", "kit01_inbox.json")).read_text(encoding="utf-8"))
    return raw, box


def period_of(kind: str, today: dt.date) -> dict:
    if kind == "quarterly":
        q = (today.month - 1) // 3
        start = dt.date(today.year, 3 * q + 1, 1)
        end_month = 3 * q + 3
        end = dt.date(today.year, end_month, calendar.monthrange(today.year, end_month)[1])
        return {"period": f"{today.year}-Q{q + 1}", "label": f"Q{q + 1} {today.year}", "start": start.isoformat(), "end": end.isoformat()}
    if kind == "annual":
        return {"period": str(today.year), "label": str(today.year), "start": f"{today.year}-01-01", "end": f"{today.year}-12-31"}
    end = dt.date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    return {"period": f"{today.year}-{today.month:02d}", "label": today.strftime("%B %Y"), "start": today.replace(day=1).isoformat(), "end": end.isoformat()}


def _channel(source: str) -> str:
    for prefix, name in CHANNELS.items():
        if source.startswith(prefix):
            return name
    return source


def incoming(raw: dict, box: dict, upto: str | None) -> list[dict]:
    """Every request the book holds, by the channel it arrived through: systems of record and emails received so far."""
    st = raw["state"]
    by_ref = {o["evidence_ref"]: o for o in st["obligations"]}
    rows = []
    for e in st["evidence"]:
        o = by_ref.get(e["ref"])
        if o is None and not e["source"].startswith("email:customer"):
            continue
        rows.append({"channel": _channel(e["source"]), "from": (o or {}).get("payee_name") or "Acme Retail Ltd",
                     "subject": e["summary"], "amount_minor": (o or {}).get("amount_minor"), "currency": (o or {}).get("currency"),
                     "ref": (o or {}).get("obligation_id") or e["subject_id"], "date": None})
    for m in box["messages"]:
        if upto and m["date"][:10] > upto:
            continue
        rows.append({"channel": "Email", "from": m["sender"], "subject": m["subject"], "amount_minor": None, "currency": None,
                     "ref": m.get("doc_id"), "date": m["date"][:10], "text": m["text"]})
    return rows


def defaults(scenario: str, executor: Executor) -> dict:
    """The mandate form, filled with the platform's current settings."""
    raw, box = _scenario(scenario)
    st = raw["state"]
    pol = st["policy"]
    today = dt.date.today()
    from .reporting import config_path, load_config

    rep = load_config()
    registry = json.loads((ROOT / "data" / "payees.json").read_text(encoding="utf-8"))["payees"]
    live_records = {r["beneficiary_id"]: r for r in executor.store.list_payees()} if executor else {}
    used = {o["beneficiary_id"] for o in st["obligations"]}
    payees = [{"id": cid, "name": p["name"], "currency": p.get("currency"), "method": p.get("payout_method"), "contacts": p.get("contacts", []),
               "phone": (p.get("lifecycle") or {}).get("registered_phone"), "status": (live_records.get(cid) or {}).get("status", (p.get("lifecycle") or {}).get("status", "NEW")),
               "allowed": True, "in_book": cid in used} for cid, p in registry.items()]
    mandates = []
    for m in MANDATES:
        row = {k: v for k, v in m.items() if k not in ("policy", "revoked", "off")}
        row["enabled"] = True
        if m.get("policy") and m["policy"] in pol and m["unit"] is not None:
            row["value"] = pol[m["policy"]]
        mandates.append(row)
    budget = st.get("budget") or {}
    return {"scenario": scenario, "goal": (raw.get("goal") or {}).get("text", ""), "book_name": raw.get("book_name", st["book_id"]),
            "tenant_name": raw.get("tenant_name", ""), "allocation_minor": st["balances"]["USD"], "reserve_floor_minor": pol["reserve_floor_minor"],
            "horizon_hours": pol.get("planning_horizon_hours", 72), "period_type": "monthly",
            "periods": {k: period_of(k, today) for k in ("monthly", "quarterly", "annual")},
            "full_confidence_bp": pol.get("full_confidence_bp", 9000), "zero_limit_below_bp": pol.get("zero_limit_below_bp", 5000),
            "mandates": mandates,
            "budget": [{"line": ln["line"], "amount_minor": ln["amount_minor"], "auto_share_bp": ln.get("auto_share_bp", 10_000),
                        "alert_share_bp": ln.get("alert_share_bp", 8_000), "require_approval_on_alert": ln.get("require_approval_on_alert", False),
                        "bypass": ln.get("bypass", False), "bypass_approved_by": ln.get("bypass_approved_by")} for ln in budget.get("lines", [])],
            "unbudgeted_requires_approval": budget.get("unbudgeted_requires_approval", True),
            "approvers": [{"name": "Dana Finch", "role": "finance_manager"}, {"name": "Omar Reyes", "role": "cfo"}],
            "approval_matrix": [{"name": "standard", "min_total_minor": 0, "reasons": [], "approvals_required": 1, "roles": ["finance_manager", "cfo"]},
                                {"name": "four-eyes", "min_total_minor": 2_500_000, "reasons": ["payee_in_cooling_period", "payee_not_yet_active"],
                                 "approvals_required": 2, "roles": ["finance_manager", "cfo"]}],
            "payees": payees, "fees": pol.get("payout_fees_minor", {}),
            "reporting": {"timezone": rep.timezone, "cutoff_day": rep.cutoff_day, "cutoff_time": rep.cutoff_time, "reporting_currency": rep.reporting_currency,
                          "editable": config_path() != (ROOT / "data" / "reporting.json")},
            "incoming": incoming(raw, box, (box.get("timeline") or [None])[0]),
            "mailboxes": [{"payee": p["name"], "contacts": p["contacts"]} for p in payees if p["contacts"]]}


def _num(v, lo: int = 0, hi: int = 10**13) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError) as exc:
        raise LiveError(f"not a whole number: {v!r}") from exc
    if not lo <= n <= hi:
        raise LiveError(f"out of range: {n}")
    return n


def apply_config(state: TreasuryState, cfg: dict, today: dt.date) -> tuple[TreasuryState, dict]:
    """The submitted form -> the book's policy, budget, approvers and payees. Returns the state and the granted mandate."""
    pol = state.policy.model_dump()
    granted = {m["key"]: m for m in (cfg.get("mandates") or [])}
    summary = []
    for spec in MANDATES:
        row = granted.get(spec["key"], {})
        on = True if spec["kind"] == "locked" else bool(row.get("enabled", True))
        value = row.get("value", spec.get("value"))
        if spec["policy"] is None:
            summary.append({"key": spec["key"], "kind": spec["kind"], "label": spec["label"], "enabled": on, "value": None, "unit": None})
            continue
        if spec["unit"] is not None:
            value = _num(value, 0, 10_000 if spec["unit"] == "pct" else 10**13)
        if spec["key"] == "fx_open":
            pol["max_open_position_minor"] = {"EUR": value, "GBP": value} if on else {}
        elif spec["kind"] == "grant":
            pol[spec["policy"]] = value if on else spec["revoked"]
        elif spec["kind"] == "guard":
            pol[spec["policy"]] = value if on else spec["off"]
        else:
            pol[spec["policy"]] = value
        summary.append({"key": spec["key"], "kind": spec["kind"], "label": spec["label"], "enabled": on, "value": value, "unit": spec["unit"]})
    pol["reserve_floor_minor"] = _num(cfg.get("reserve_floor_minor", pol["reserve_floor_minor"]))
    pol["planning_horizon_hours"] = _num(cfg.get("horizon_hours", pol["planning_horizon_hours"]), 1, 720)
    pol["full_confidence_bp"] = _num(cfg.get("full_confidence_bp", pol["full_confidence_bp"]), 1, 10_000)
    pol["zero_limit_below_bp"] = _num(cfg.get("zero_limit_below_bp", pol["zero_limit_below_bp"]), 0, 9_999)
    approvers = [a for a in (cfg.get("approvers") or []) if str(a.get("name", "")).strip() and str(a.get("role", "")).strip()]
    if not approvers:
        raise LiveError("name at least one approver")
    pol["approver_roles"] = sorted({a["role"] for a in approvers})
    pol["approval_matrix"] = [t | {"min_total_minor": _num(t.get("min_total_minor", 0)), "approvals_required": _num(t.get("approvals_required", 1), 1, 3)}
                              for t in (cfg.get("approval_matrix") or [])]
    from .domain.models import TreasuryPolicy

    try:
        policy = TreasuryPolicy.model_validate(pol)
    except ValueError as exc:
        raise LiveError(f"the mandate is not valid: {exc}") from exc
    auto_on = granted.get("budget_auto", {}).get("enabled", True)
    kind = cfg.get("period_type", "monthly")
    per = period_of(kind, today)
    lines = []
    for ln in cfg.get("budget") or []:
        lines.append({"line": ln["line"], "amount_minor": _num(ln["amount_minor"]),
                      "auto_share_bp": _num(ln.get("auto_share_bp", 10_000), 0, 10_000) if auto_on else 0,
                      "alert_share_bp": _num(ln.get("alert_share_bp", 8_000), 0, 10_000),
                      "require_approval_on_alert": bool(ln.get("require_approval_on_alert")),
                      "bypass": bool(ln.get("bypass")), "bypass_approved_by": (ln.get("bypass_approved_by") or None) if ln.get("bypass") else None})
    budget = {"period": per["period"], "lines": lines, "unbudgeted_requires_approval": bool(cfg.get("unbudgeted_requires_approval", True)),
              "period_start": per["start"], "period_end": per["end"]}
    allowed = [p["id"] for p in (cfg.get("payees") or []) if p.get("allowed")]
    tenancy = Tenancy(known=True, strict=False, matched_by="mandate",
                      config=BookConfig(tenant=str(cfg.get("tenant_name") or "operations"), book_id=state.book_id, allowed_payees=allowed or ["-"],
                                        approvers=[BookApprover(name=a["name"], role=a["role"]) for a in approvers], policy_overrides={}))
    data = state.model_dump()
    data["policy"] = policy.model_dump()
    data["budget"] = budget
    data["as_of"] = today.isoformat()
    data["balances"]["USD"] = _num(cfg.get("allocation_minor", data["balances"]["USD"]))
    out = TreasuryState.model_validate(data).model_copy(update={"tenancy": tenancy})
    granted_view = {"mandates": summary, "budget": {"period": per["label"], "type": kind, "lines": lines}, "approvers": approvers,
                    "approval_matrix": pol["approval_matrix"], "allowed_payees": allowed,
                    "revoked": [s["label"] for s in summary if s["kind"] == "grant" and not s["enabled"]]}
    return out, granted_view


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
            "stages": [{"engine": x.engine, "status": x.status, "summary": x.summary[:220].replace(f"-{s.tag}", "")} for x in plan.stages],
            "budget": [{"line": r["line"], "status": r["status"], "utilisation_bp": r["utilisation_bp"], "available_minor": r["available_minor"]}
                       for r in (plan.budget or {}).get("lines", [])],
            "alerts": [a.get("message") for a in (plan.budget or {}).get("alerts", [])],
            "approval": plan.approval_requirement,
            "base_after_minor": plan.projected_base_balance_minor, "floor_minor": plan.required_base_floor_minor,
            "breach_bp": (plan.simulation or {}).get("breach_probability_bp"), "explanation": plan.explanation}


def _log(s: LiveSession, step: str, title: str, ms: float, detail: str) -> None:
    s.events.append({"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "step": step, "title": title, "ms": round(ms, 1), "detail": detail})


async def wallet(bank) -> dict:
    live = await bank.balances_minor()
    return {c: live.get(c, 0) for c in WATCH}


async def start(scenario: str, executor: Executor, audit, config: dict | None = None) -> LiveSession:
    t0 = perf_counter()
    raw, box = _scenario(scenario)
    cfg = config or defaults(scenario, executor)
    tag = fresh_tag()
    today = dt.date.today()
    s = LiveSession(sid=uuid.uuid4().hex[:12], scenario=scenario, tag=tag, goal={"text": cfg.get("goal") or ""},
                    messages=[InboxMessage(**m) for m in box["messages"]], timeline=list(box.get("timeline") or []),
                    book_name=str(cfg.get("book_name") or raw.get("book_name") or ""))
    base = TreasuryState.model_validate(raw["state"])
    state, granted = apply_config(base, cfg, today)
    state = fresh_book(attach_budget(state), tag)
    state = state.model_copy(update={"tenancy": state.tenancy.model_copy(update={"config": state.tenancy.config.model_copy(update={"book_id": state.book_id})})})
    if cfg.get("reporting", {}).get("editable"):
        from .reporting import ReportingConfig, load_config, save_config

        rep = load_config().model_dump()
        rep.update({k: cfg["reporting"][k] for k in ("timezone", "cutoff_day", "cutoff_time", "reporting_currency") if k in cfg["reporting"]})
        save_config(ReportingConfig.model_validate(rep | {"set_by": "finance manager (mandate form)"}))
    bank = executor.bank
    state = await with_live_balances_and_rates(state, bank, ring_fence=True)
    state, notes = apply_operational_facts(state, executor.store)
    s.state = state
    s.wallet_start = await wallet(bank)
    p = state.policy
    s.views["goal"] = {"goal": s.goal, "book_id": state.book_id, "book_name": s.book_name, "tenant": cfg.get("tenant_name"),
                       "managed": dict(state.balances), "wallet": s.wallet_start, "fx_to_base": dict(state.fx_to_base), "granted": granted,
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
                       "incoming": incoming(raw, box, s.timeline[0] if s.timeline else None), "notes": notes}
    s.step = 1
    revoked = granted["revoked"]
    _log(s, "goal", TITLES["goal"], (perf_counter() - t0) * 1000,
         f"mandate granted for {s.book_name or state.book_id}: USD {state.balances.get('USD', 0) / 100:,.2f} allocated, floor USD {p.reserve_floor_minor / 100:,.2f}"
         + (f"; revoked: {len(revoked)}" if revoked else "; every permission granted"))
    audit.append("live_start", {"sid": s.sid, "book_id": state.book_id, "scenario": scenario, "mandate": granted})
    return s


async def _read(s: LiveSession, day: str) -> dict:
    reading = await read_inbox(s.state, s.messages, as_of=dt.date.fromisoformat(day), model=_model())
    s.state = reading.state
    return {"as_of": day, "summary": reading.stage.summary,
            "updates": [u.model_dump() for u in reading.updates],
            "flags": [{"id": s.short(f.obligation_id), "payee": f.payee, "flag": f.flag, "verified": f.verified, "quote": f.quote, "doc_id": f.doc_id}
                      for f in reading.flags],
            "messages": [{"date": m.date, "sender": m.sender, "subject": m.subject, "text": m.text} for m in s.messages if m.date[:10] == day]}


async def step(s: LiveSession, executor: Executor, audit, *, approver: str | None = None, role: str = "finance_manager") -> dict:
    if s.step >= len(STEPS):
        raise LiveError("this run is complete; start a new one")
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
        detail = (f"payment {dep.get('id')} {dep.get('status')}; plan {s.plan.decision}" if dep else "no incoming payment expected")
    else:  # execute
        approval = None
        if s.plan.decision == "REQUIRES_APPROVAL":
            if not approver:
                raise LiveError("this plan needs an approver: name the person approving it")
            approval = Approval(approver=approver, role=role, terms_digest=terms_digest(s.plan))
        before = await wallet(executor.bank)
        result = await executor.execute(ExecutionRequest(state=s.state, plan_hash=s.plan.plan_hash, approval=approval, requested_by="VENTHORA"))
        if result.status == "COMPLETED" and s.deposit and s.deposit.get("landed"):
            executor.store.count_deposit(s.deposit["id"], s.deposit["receivable_id"], s.deposit["amount_minor"], s.deposit["currency"], result.execution_id)
        after = await wallet(executor.bank)
        book = executor.store.book_balances(s.state.book_id)
        rec = result.reconciliation
        final = {}
        for x in result.steps:
            if x.kind == "simulation" and (x.detail or {}).get("status"):
                final[x.key] = x.detail["status"]
        pv = plan_view(s, s.plan)
        s.execution = {"status": result.status, "message": result.message, "execution_id": result.execution_id,
                       "steps": [{"kind": x.kind, "key": s.short(x.key), "status": x.status, "airwallex_id": x.airwallex_id,
                                  "final": final.get(x.key) if x.kind == "transfer" else (x.detail or {}).get("status"),
                                  "detail": {k: v for k, v in (x.detail or {}).items() if k in ("amount", "method", "fee_minor", "sell_minor", "client_rate")}}
                                 for x in result.steps if x.kind in ("quote", "conversion", "transfer")],
                       "refusal": next(((x.detail or {}).get("reason") for x in result.steps if x.status == "REFUSED"), None),
                       "wallet_before": before, "wallet_after": after, "wallet_start": s.wallet_start,
                       "managed_after": rec.managed_after_minor if rec else {}, "discrepancies": rec.discrepancies_minor if rec else None,
                       "floor_ok": rec.base_floor_ok if rec else None,
                       "book": {c: {k: v for k, v in a.items() if v} for c, a in book.items()},
                       "paid": [o["id"] for o in pv["obligations"] if o["action"] in ("PAY", "CONVERT_AND_PAY")] if result.status in ("COMPLETED", "IN_FLIGHT") else [],
                       "not_paid": [{"id": o["id"], "action": o["action"]} for o in pv["obligations"] if o["action"] not in ("PAY", "CONVERT_AND_PAY")]}
        s.views["execute"] = s.execution
        detail = f"{result.status}: {result.message[:140]}"
    s.step += 1
    ms = (perf_counter() - t0) * 1000
    _log(s, name, TITLES[name], ms, detail)
    audit.append(f"live_{name}", {"sid": s.sid, "book_id": s.state.book_id, "detail": detail})
    return view(s)


def view(s: LiveSession) -> dict:
    approvers = (s.views.get("goal") or {}).get("granted", {}).get("approvers", [])
    return {"sid": s.sid, "scenario": s.scenario, "book_id": s.state.book_id if s.state else None, "book_name": s.book_name, "step": s.step,
            "steps": [{"name": n, "title": TITLES[n], "done": i < s.step} for i, n in enumerate(STEPS)],
            "next": STEPS[s.step] if s.step < len(STEPS) else None,
            "next_needs_approval": bool(s.step < len(STEPS) and STEPS[s.step] == "execute" and s.plan and s.plan.decision == "REQUIRES_APPROVAL"),
            "approvers": approvers,
            "managed": dict(s.state.balances) if s.state else {}, "views": s.views, "events": s.events}
