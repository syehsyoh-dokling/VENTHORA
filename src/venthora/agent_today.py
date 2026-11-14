# ruff: noqa: E501
"""Agent plan -> Today's plan and the transaction log.

Today's plan says, in one paragraph and two tables, what VENTHORA found for the planning horizon: every obligation due
in it by category, what is validated and ready to pay, what needs a person and why, whether the live balance covers it,
and when the ready part is paid (the policy's next run). A person may reject a ready payment (with a reason) or put it
pending to a new date; when the money is short, it recommends what to pay now and what when the balance allows.

The transaction log keeps the whole story of each payment, every step with its date and time: the invoice received,
VENTHORA's checks, valid and ready to pay, every decision the plan made (pay, defer, escalate, reject), every person's
intervention (correction, approval, rejection, hold, pending, a notice to the vendor), the conversion with its rate,
the transfer and the payment done.
"""
from __future__ import annotations

import datetime as dt
import json

PAYING = ("PAY", "CONVERT_AND_PAY")
ACTION_WORDS = {"PAY": "pay", "CONVERT_AND_PAY": "convert and pay", "DEFER": "defer", "ESCALATE": "escalate to a person", "REJECT": "reject"}


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()  # full precision: the log orders steps made in the same second


def intake_ref(o) -> str | None:
    ref = getattr(o, "evidence_ref", None) if not isinstance(o, dict) else o.get("evidence_ref")
    return ref.split(":", 1)[1] if ref and ref.startswith("intake:") else None


# -- the plan's decisions, kept per payment (only when they change) ---------------------------------------------------

def record_decisions(org, rows: list[dict]) -> None:
    for r in rows:
        rec = org.get("decision_log", r["obligation_id"]) or {"_id": r["obligation_id"], "events": [], "intake_ref": r.get("intake_ref")}
        last = rec["events"][-1] if rec["events"] else None
        key = (r["action"], r["why"])
        if last and (last["action"], last["why"]) == key:
            continue
        rec["events"] = (rec["events"] + [{"at": _now(), "action": r["action"], "why": r["why"], "autonomous": r.get("ready", False)}])[-50:]
        rec["intake_ref"] = rec.get("intake_ref") or r.get("intake_ref")
        org._put("decision_log", rec, "VENTHORA (plan)")


# -- Today's plan ----------------------------------------------------------------------------------------------------

def summary(state, source: dict, split, org, payees: dict, horizon: int, next_pay_at: str | None, refused: list | None = None) -> dict:
    """`refused`: (obligation, why) for what the executor would refuse now (plan_gates); shown, never ready."""
    from . import priority_sop
    from .domain.money import cost_in_base

    base = state.base_currency
    by_id = {o.obligation_id: o for o in state.obligations}
    decided = {d.obligation_id: d for d in split.full.obligations}
    auto = {d.obligation_id for d in (split.auto_plan.obligations if split.auto_plan else []) if d.action in PAYING}
    held = {h["obligation_id"]: h["reasons"] for h in split.held}
    rows = []
    for oid, o in by_id.items():
        d = decided.get(oid)
        if d is None:
            continue
        cat = priority_sop.classify(o.model_dump(), payees)
        ready = oid in auto
        if ready:
            why = d.reason
        elif oid in held:
            why = "; ".join(str(x).replace("_", " ") for x in held[oid])
        else:
            why = f"{ACTION_WORDS.get(d.action, d.action.lower())}: {d.reason}"
        rows.append({"obligation_id": oid, "payee": o.payee_name or o.beneficiary_id, "amount_minor": o.amount_minor, "currency": o.currency,
                     "base_minor": d.base_cost_minor, "due_in_hours": o.due_in_hours, "due_date": o.due_date, "category": cat,
                     "category_label": priority_sop.ITEMS[cat][0], "priority": o.priority, "action": d.action, "ready": ready, "why": why,
                     "intake_ref": intake_ref(o), "in_horizon": o.due_in_hours <= horizon,
                     "document_check": "completed" if ready or d.action in PAYING else ("incomplete" if "evidence" in d.reason or "pending" in d.reason else "completed"),
                     "smoke_test": "succeeded" if ready else None})
    for o, why in refused or []:  # over its budget allocation, or above the bank's transfer limit: a person acts first
        try:
            b = o.amount_minor if o.currency == base else cost_in_base(o.amount_minor, state.ppm_up(o.currency))
        except (KeyError, TypeError):
            b = None
        cat = priority_sop.classify(o.model_dump(), payees)
        rows.append({"obligation_id": o.obligation_id, "payee": o.payee_name or o.beneficiary_id, "amount_minor": o.amount_minor, "currency": o.currency,
                     "base_minor": b, "due_in_hours": o.due_in_hours, "due_date": o.due_date, "category": cat,
                     "category_label": priority_sop.ITEMS[cat][0], "priority": o.priority, "action": "ESCALATE", "ready": False, "why": why,
                     "intake_ref": intake_ref(o), "in_horizon": o.due_in_hours <= horizon, "document_check": "completed", "smoke_test": None,
                     "refused": True})
    pay_in_h = None
    if source.get("pay_date"):  # the salaries are due on the pay day: inside the horizon only when it is that close
        try:
            pay_in_h = (dt.datetime.fromisoformat(f"{source['pay_date'][:10]}T00:00:00+00:00") - dt.datetime.now(dt.UTC)).total_seconds() / 3600
        except ValueError:
            pay_in_h = None
    for w in source.get("payroll_waiting") or []:
        ccy = w["currency"]
        try:
            b = w["amount_minor"] if ccy == base else cost_in_base(w["amount_minor"], state.ppm_up(ccy))
        except KeyError:
            b = None
        rows.append({"obligation_id": f"salary · {w['employee']}", "payee": w["employee"], "amount_minor": w["amount_minor"], "currency": ccy,
                     "base_minor": b, "due_in_hours": None, "due_date": source.get("pay_date"), "category": "wages", "category_label": priority_sop.ITEMS["wages"][0],
                     "priority": 10, "action": "ESCALATE", "ready": False, "why": "; ".join(w["reasons"]), "intake_ref": None,
                     "in_horizon": pay_in_h is None or pay_in_h <= horizon,
                     "document_check": "completed", "smoke_test": None, "payroll": True})
    # beyond the twelve one plan proves: shown, ranked by the same policy, and waiting for the next round (never paid now)
    as_of = dt.datetime.now(dt.UTC).date().isoformat()
    for o in source.get("overflow") or []:
        ccy = o.get("currency")
        try:
            b = o["amount_minor"] if ccy == base else cost_in_base(o["amount_minor"], state.ppm_up(ccy))
        except (KeyError, TypeError):
            continue
        cat, prio, _ = priority_sop.priority_for(org, o, payees, as_of)
        rows.append({"obligation_id": o["obligation_id"], "payee": o.get("payee_name") or o.get("beneficiary_id"), "amount_minor": o["amount_minor"],
                     "currency": ccy, "base_minor": b, "due_in_hours": o.get("due_in_hours"), "due_date": o.get("due_date"), "category": cat,
                     "category_label": priority_sop.ITEMS[cat][0], "priority": prio, "action": "DEFER", "ready": False,
                     "why": "next round: one plan proves twelve payments, the most urgent first; this one waits for a place",
                     "intake_ref": intake_ref(o), "in_horizon": (o.get("due_in_hours") or 0) <= horizon,
                     "document_check": "completed", "smoke_test": None, "next_round": True})
    record_decisions(org, [r for r in rows if not r.get("payroll") and not r.get("next_round")])
    ready = sorted([r for r in rows if r["ready"]], key=lambda r: (-r["priority"], r["due_in_hours"] or 0, r["obligation_id"]))
    others = sorted([r for r in rows if not r["ready"]], key=lambda r: (-r["priority"], r["due_in_hours"] if r["due_in_hours"] is not None else 0, r["obligation_id"]))
    horizon_rows = [r for r in rows if r["in_horizon"]]
    cats: dict[str, dict] = {}
    for r in horizon_rows:
        c = cats.setdefault(r["category_label"], {"category": r["category_label"], "count": 0, "base_minor": 0})
        c["count"] += 1
        c["base_minor"] += r["base_minor"] or 0
    total = sum(r["base_minor"] or 0 for r in horizon_rows)
    ready_total = sum(r["base_minor"] or 0 for r in ready)
    manual = [r for r in horizon_rows if not r["ready"]]
    available = state.balance(base)
    floor = state.policy.reserve_floor_minor
    spendable = max(0, available - floor)
    payable_manual = sum(r["base_minor"] or 0 for r in manual if r["action"] not in ("REJECT",))
    enough = ready_total + payable_manual <= spendable
    # when the money is short: pay now what fits, in priority order; the rest when the balance allows
    rec = None
    deferred = [r for r in others if r["action"] == "DEFER" or (split.stopped_for_cash and r["obligation_id"] in held)]
    if not enough or deferred:
        left, now_rows, later = spendable, [], []
        for r in sorted([r for r in rows if r["ready"] or r in deferred], key=lambda r: (-r["priority"], r["due_in_hours"] or 0)):
            if (r["base_minor"] or 0) <= left:
                now_rows.append(r)
                left -= r["base_minor"] or 0
            else:
                later.append(r)
        rec = ({"pay_now": now_rows, "pay_later": later, "spendable_minor": spendable,
                "short_minor": max(0, ready_total + payable_manual - spendable)} if now_rows or later else None)
    cover = coverage(state, rows, available, floor, horizon)
    return {"coverage": cover, "executive": executive(horizon_rows), "horizon_hours": horizon, "base_currency": base, "categories": sorted(cats.values(), key=lambda c: -c["base_minor"]),
            "total_minor": total, "count": len(horizon_rows), "ready_minor": ready_total, "ready_count": len(ready),
            "manual_minor": sum(r["base_minor"] or 0 for r in manual), "manual_count": len(manual),
            "available_minor": available, "reserve_floor_minor": floor, "spendable_minor": spendable, "enough": enough,
            "payable_minor": ready_total + payable_manual, "blocked_minor": sum(r["base_minor"] or 0 for r in manual if r["action"] == "REJECT"),
            "approval_minor": payable_manual, "approval_count": sum(1 for r in manual if r["action"] != "REJECT"),
            "blocked_count": sum(1 for r in manual if r["action"] == "REJECT"),
            "due": [{"obligation_id": r["obligation_id"], "payee": r["payee"], "amount_minor": r["amount_minor"], "currency": r["currency"],
                     "base_minor": r["base_minor"], "group": "ready" if r["ready"] else "blocked" if r["action"] == "REJECT" else "approval"}
                    for r in sorted(horizon_rows, key=lambda r: -(r["base_minor"] or 0))],
            "next_pay_at": next_pay_at if ready else None, "ready": ready, "others": others, "recommendation": rec}


def coverage(state, rows: list[dict], available: int, floor: int, hours: int) -> dict:
    """Is the balance enough for everything due inside the planning horizon (the hours the Standard Payment Priority
    Policy sets)? Every obligation in it, whatever its status today (a stopped one may be corrected), by due time,
    against the balance above the reserve floor, day by day. Expected customer receipts are shown, never counted."""
    from . import priority_sop
    from .domain.money import value_in_base

    now = dt.datetime.now(dt.UTC)
    end = now + dt.timedelta(hours=hours)

    def due_at(r):
        if r.get("due_date"):
            try:
                return dt.datetime.fromisoformat(f"{str(r['due_date'])[:10]}T{priority_sop.DEADLINE_HOUR_UTC:02d}:00:00+00:00")
            except ValueError:
                pass
        return now + dt.timedelta(hours=r.get("due_in_hours") or 0)

    items = sorted(({**r, "due_at": due_at(r)} for r in rows if r.get("base_minor") is not None), key=lambda r: r["due_at"])
    items = [r for r in items if r["due_at"] <= end]
    spendable = max(0, available - floor)
    days, cum, short_at = [], 0, None
    n = -(-hours // 24)
    for d in range(n):
        lo, hi = now + dt.timedelta(hours=24 * d), min(end, now + dt.timedelta(hours=24 * (d + 1)))
        last = d == n - 1  # the last day keeps what is due exactly at the end of the horizon
        due = [r for r in items if (r["due_at"] < hi or (last and r["due_at"] <= end)) and (d == 0 or r["due_at"] >= lo)]  # overdue: day 1
        amt = sum(r["base_minor"] for r in due)
        cum += amt
        days.append({"day": d + 1, "from": lo.isoformat(timespec="minutes"), "to": hi.isoformat(timespec="minutes"), "count": len(due),
                     "due_minor": amt, "cumulative_minor": cum, "left_minor": spendable - cum})
    # the same horizon by priority level (Standard Payment Priority Policy bands): what the balance covers, High first
    levels, lcum = [], 0
    for key, label in priority_sop.LEVELS.items():
        top, floor_p = priority_sop.BANDS[key]
        mine = [r for r in items if floor_p <= int(r.get("priority") or 1) <= top]
        amt = sum(r["base_minor"] for r in mine)
        lcum += amt
        cats = sorted({r.get("category_label") or r.get("category") or "" for r in mine} - {""})
        levels.append({"level": key, "label": label, "priorities": f"{top}–{floor_p}", "count": len(mine), "due_minor": amt,
                       "cumulative_minor": lcum, "left_minor": spendable - lcum, "covered": lcum <= spendable, "categories": cats})
    # the line-up: every payment of the horizon in the order the policy pays it (High first, then the earliest due),
    # against the balance above the reserve floor. PAID while it fits, the last one that fits is the boundary,
    # everything after it waits for money (red), whatever its level: the line is never crossed to pay a lower one.
    level_of = {}
    for key, (top, floor_p) in priority_sop.BANDS.items():
        for p in range(floor_p, top + 1):
            level_of[p] = key
    lineup, run_l, crossed = [], 0, False
    for r in sorted(items, key=lambda r: (bool(r.get("next_round")), -int(r.get("priority") or 1), r["due_at"])):
        run_l += r["base_minor"]
        fits = not crossed and not r.get("next_round") and run_l <= spendable  # a next-round payment is not in today's plan
        crossed = crossed or not fits
        lineup.append({"obligation_id": r.get("obligation_id"), "payee": r.get("payee"), "category": r.get("category_label") or r.get("category"),
                       "priority": int(r.get("priority") or 1), "level": level_of.get(int(r.get("priority") or 1), "low"),
                       "amount_minor": r.get("amount_minor"), "currency": r.get("currency"), "base_minor": r["base_minor"],
                       "due_at": r["due_at"].isoformat(timespec="minutes"), "cumulative_minor": run_l, "left_minor": spendable - run_l,
                       "status": "paid" if fits else "waits", "group": _group(r), "next_round": bool(r.get("next_round"))})
    last_paid = max((i for i, x in enumerate(lineup) if x["status"] == "paid"), default=None)
    if last_paid is not None:
        lineup[last_paid]["status"] = "boundary"
    run = 0
    for r in items:
        run += r["base_minor"]
        if run > spendable:
            short_at = r["due_at"].date().isoformat()
            break
    receipts = []
    for v in state.receivables:
        if v.expected_in_hours <= hours:
            try:
                b = v.amount_minor if v.currency == state.base_currency else value_in_base(v.amount_minor, state.ppm_down(v.currency))
            except KeyError:
                continue
            receipts.append({"payer": v.payer, "amount_minor": v.amount_minor, "currency": v.currency, "base_minor": b,
                             "expected": (now + dt.timedelta(hours=v.expected_in_hours)).date().isoformat(), "confidence_bp": v.confidence_bp})
    total = sum(r["base_minor"] for r in items)
    return {"hours": hours, "until": end.isoformat(timespec="minutes"), "total_minor": total, "count": len(items), "available_minor": available,
            "reserve_floor_minor": floor, "spendable_minor": spendable, "enough": total <= spendable, "short_minor": max(0, total - spendable),
            "short_from": short_at, "days": days, "levels": levels, "lineup": lineup, "receipts": receipts, "receipts_minor": sum(x["base_minor"] for x in receipts),
            "by_group": {g: sum(r["base_minor"] for r in items if _group(r) == g) for g in ("ready", "approval", "blocked")}}


# why a payment is not paid, in the words of the executive summary (counted, never listed one by one)
REASONS = (("pending", "the documents are not verified yet"), ("contract total", "the amount goes above its purchase order or contract"),
           ("purchase order", "there is no purchase order"), ("first salary", "it is the first salary of the person"),
           ("changed", "the bank details changed"), ("duplicate", "it is a duplicate"), ("limit", "it is above a limit of the mandate"),
           ("budget", "it is outside the budget"), ("reserve", "the reserve floor would be at risk"), ("cooling", "the payee is in its cooling period"))


def reason_kind(why: str) -> str:
    w = (why or "").lower()
    return next((label for key, label in REASONS if key in w), "another check")


def executive(rows: list[dict]) -> dict:
    """The figures the executive summary is written from: groups, the largest categories, the commonest reasons."""
    out = {}
    for g in ("ready", "approval", "blocked"):
        rs = [r for r in rows if _group(r) == g]
        why: dict[str, int] = {}
        for r in rs:
            k = reason_kind(r["why"]) if g != "ready" else None
            if k:
                why[k] = why.get(k, 0) + 1
        out[g] = {"count": len(rs), "base_minor": sum(r["base_minor"] or 0 for r in rs),
                  "reasons": [{"reason": k, "count": n} for k, n in sorted(why.items(), key=lambda x: -x[1])]}
    cats: dict[str, dict] = {}
    for r in rows:
        c = cats.setdefault(r["category_label"], {"category": r["category_label"], "count": 0, "base_minor": 0})
        c["count"] += 1
        c["base_minor"] += r["base_minor"] or 0
    ranked = sorted(cats.values(), key=lambda c: -c["base_minor"])
    top, rest = ranked[:3], ranked[3:]
    out["categories"] = top + ([{"category": "other categories", "count": sum(c["count"] for c in rest), "base_minor": sum(c["base_minor"] for c in rest)}] if rest else [])
    return out


def _group(r: dict) -> str:
    return "ready" if r.get("ready") else "blocked" if r.get("action") == "REJECT" else "approval"


# -- the transaction log ---------------------------------------------------------------------------------------------

def _ev(at, kind, title, detail="", by=None) -> dict:
    return {"at": str(at or "")[:32], "kind": kind, "title": title, "detail": detail, "by": by}


def log_for(app_state, *, obligation_id: str | None = None, intake_id: str | None = None) -> dict:
    """Every step of one payment, with its date and time, from the invoice received to the payment done."""
    org, store, audit, ex = app_state.org, app_state.intake.store, app_state.audit, app_state.executor.store
    dlog = org.get("decision_log", obligation_id) if obligation_id else None
    req = None
    if intake_id:
        req = store.get(intake_id)
    elif dlog and dlog.get("intake_ref"):
        req = next((r for r in store.list(1000) if r.intake_id.startswith(dlog["intake_ref"])), None)
    elif obligation_id and obligation_id.startswith("REQ-"):
        key = obligation_id[4:].lower()
        req = next((r for r in store.list(1000) if (r.invoice_no or r.intake_id[:6]).lower() == key.split("-")[0] or (r.invoice_no or "").lower() == key), None)
    if req is not None and not obligation_id:
        guess = f"REQ-{(req.invoice_no or req.intake_id[:6]).upper()}"
        obligation_id = next((d["_id"] for d in org.list("decision_log") if d.get("intake_ref") and req.intake_id.startswith(d["intake_ref"])), guess)
        dlog = org.get("decision_log", obligation_id)
    ev: list[dict] = []
    if req is not None:
        ev.append(_ev(req.received_at, "received", f"Request received: {req.subject}",
                      f"from {req.sender} · {req.kind.replace('_', ' ')}" + (f" · invoice {req.invoice_no}" if req.invoice_no else "")
                      + (f" · {req.currency} {req.amount_minor / 100:,.2f}" if req.amount_minor else "") + (f" · due {req.due_date}" if req.due_date else ""), req.sender))
        if req.expense_date:
            ev.append(_ev(req.expense_date, "invoice", "Date on the document", f"expense date {req.expense_date}"))
        fails = [c for c in req.checks if c.status == "fail"]
        ev.append(_ev(req.received_at, "checked", "Checked by VENTHORA",
                      (f"{len(req.checks)} document check(s): " + (", ".join(c.label for c in fails) + " failed" if fails else "all passed or not required")) if req.checks else
                      f"read by {req.reader}; sender {req.trust.replace('_', ' ')}", "VENTHORA"))
        verdict = {"VERIFIED": "Valid: ready to pay", "NEEDS_REVIEW": "Needs a person", "QUARANTINED": "Quarantined", "REJECTED": "Rejected", "DUPLICATE": "Duplicate"}
        ev.append(_ev(req.received_at, "status", verdict.get(req.status, req.status), "; ".join(req.reasons)[:400], "VENTHORA"))
        for a in audit.search(req.intake_id):
            p, k = a["payload"], a["event_type"]
            if k == "intake":
                continue
            title = {"intake_corrected": "Corrected by a person, checks run again", "intake_review": f"{'Approved' if p.get('decision') == 'verify' else 'Rejected'} by a person",
                     "intake_paid_from_pending": "Paid from Pending payment", "paid_on_arrival": "Paid on arrival", "held_on_arrival": "Held on arrival by VENTHORA",
                     "pay_on_arrival_failed": "Payment on arrival failed"}.get(k, k.replace("_", " "))
            detail = p.get("note") or p.get("reason") or ", ".join(p.get("problems") or []) or p.get("status") or p.get("error") or ""
            ev.append(_ev(a["created_at"], k, title, str(detail)[:400], p.get("by") or p.get("reviewer")))
    if obligation_id:
        for e in (dlog or {}).get("events", []):
            ev.append(_ev(e["at"], "decision", f"Plan: {ACTION_WORDS.get(e['action'], e['action'].lower())}" + (" (VENTHORA on its own)" if e.get("autonomous") else ""), e["why"], "VENTHORA (plan)"))
        for a in audit.search(f'"{obligation_id}"'):
            p, k = a["payload"], a["event_type"]
            if k in ("plan_hold", "plan_hold_released", "plan_rejected", "plan_pending", "vendor_notified") or k.startswith("policy_"):
                title = {"plan_hold": "Held by a person", "plan_hold_released": "Hold released", "plan_rejected": "Rejected by a person",
                         "plan_pending": f"Put pending to {p.get('until', '')}", "vendor_notified": "Vendor notified"}.get(k, f"Policy run: {p.get('status', '')}")
                ev.append(_ev(a["created_at"], k, title, str(p.get("note") or p.get("reason") or p.get("message") or "")[:400], p.get("by")))
        for op in ex.operations_for(obligation_id):
            r = op["response"] or {}
            if op["kind"] == "transfer":
                ev.append(_ev(r.get("created_at") or op["updated_at"], "transfer", f"Transfer {r.get('status', op['status']).lower()} on Airwallex",
                              f"{r.get('transfer_currency', '')} {r.get('transfer_amount', '')} by {r.get('transfer_method', '')}"
                              + (f" · funded {r.get('source_currency')} {r.get('source_amount')}" if r.get("source_currency") != r.get("transfer_currency") else "")
                              + (f" · fee {r.get('fee_currency', '')} {r.get('fee_amount')}" if r.get("fee_amount") else "") + f" · {op['airwallex_id'] or ''}", "Airwallex"))
            elif op["kind"] == "conversion":
                c = r.get("conversion") or {}
                ev.append(_ev(op["updated_at"], "conversion", f"Converted {c.get('sell_currency', '')} → {c.get('buy_currency', '')}",
                              f"sold {c.get('sell_currency', '')} {(c.get('sell_amount_minor') or 0) / 100:,.2f}, bought {c.get('buy_currency', '')} {(c.get('buy_amount_minor') or 0) / 100:,.2f} at {c.get('client_rate', '')}", "Airwallex"))
        for f in [x for x in ex.fx_costs() if x.get("obligation_id") == obligation_id]:
            x = f.get("exchange") or {}
            if x:
                ev.append(_ev(f["at"], "rate", "Exchange rate used", f"client {x.get('client_rate')} · mid-market {x.get('mid_rate')} {x.get('currency_pair') or ''} · spread {base_fmt(f.get('spread_base_minor'))}", "Airwallex"))
        for p in ex.payments_for(obligation_id):
            ev.append(_ev(p["paid_at"], "done", "Paid: done", f"{p['currency']} {p['amount_minor'] / 100:,.2f} · reporting period {p['reporting_period']} · {p['transfer_id']}", "Airwallex"))
    ev = [e for _, e in sorted(enumerate(ev), key=lambda x: (_when(x[1]["at"]), x[0]))]  # by real time; same second: in order
    status = next((e["title"] for e in reversed(ev) if e["kind"] in ("done", "transfer", "plan_rejected", "intake_review", "decision", "status")), None)
    return {"obligation_id": obligation_id, "intake_id": req.intake_id if req else None, "payee": (req.counterparty_name or req.display_name) if req else None,
            "amount_minor": req.amount_minor if req else None, "currency": req.currency if req else None, "status": status, "events": ev,
            "request": json.loads(req.model_dump_json(exclude={"text"})) if req else None}


def _when(at: str) -> dt.datetime:
    """A timestamp as a moment in UTC (text sorting puts 07:30:00.5 before 07:30:00+00:00); a date alone is its start."""
    t = (at or "").replace("Z", "+00:00")
    if len(t) > 5 and t[-5] in "+-" and t[-3] != ":":
        t = f"{t[:-2]}:{t[-2:]}"
    try:
        w = dt.datetime.fromisoformat(t)
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    return w if w.tzinfo else w.replace(tzinfo=dt.UTC)


OK_WORDS = ("registered mailbox", "quoted verbatim", "matched", "charged to", "left after it", "verified", "passed")
BAD_WORDS = ("no ", "not ", "missing", "above", "exceed", "unknown", "mismatch", "without", "expired", "changed", "only against", "over ",
             "does not", "past ", "duplicate", "unreadable", "failed", "too ", "lacks", "differs")


def _money(minor: str) -> str:
    try:
        return f"{int(minor) / 100:,.2f}"
    except ValueError:
        return minor


def findings(req: dict | None, why: str | None) -> list[str]:
    """What a vendor must complete so the payment can go, in plain words (for the notice to the vendor)."""
    import re

    out: list[str] = []
    req = req or {}
    for c in req.get("checks") or []:
        if c.get("status") == "fail":
            out.append(f"{c.get('label')}: {c.get('detail')}".rstrip(": "))
        elif c.get("status") == "warn":
            out.append(f"{c.get('label')} (please confirm): {c.get('detail')}".rstrip(": "))
    for r in req.get("reasons") or []:
        for part in [p.strip() for p in str(r).split(";") if p.strip()]:
            low = part.lower()
            if any(w in low for w in OK_WORDS) and not any(w in low for w in BAD_WORDS):
                continue
            if any(w in low for w in BAD_WORDS):
                out.append(part[0].upper() + part[1:])
    if "payee_bank_details_changed" in (req.get("flags") or []):
        out.append("Your bank details changed: we must confirm them with you by phone, on your registered number, before paying")
    m = re.search(r"paid (\d+) \+ claimed (\d+) > (\d+)", why or "")
    if m:
        ccy = req.get("currency") or ""
        out.append(f"The amount goes above what is left on the purchase order: {ccy} {_money(m.group(1))} already paid, "
                   f"{ccy} {_money(m.group(2))} invoiced now, order total {ccy} {_money(m.group(3))}")
    if not out and why and "pending" in why:
        out.append("The invoice is still being checked by our finance team")
    seen, uniq = set(), []
    for x in out:
        if x.lower() not in seen:
            seen.add(x.lower())
            uniq.append(x)
    return uniq


def base_fmt(v) -> str:
    return "not measured" if v is None else f"USD {v / 100:,.2f}"


def log_index(app_state, limit: int = 200) -> list[dict]:
    """One line per payment for the log tab: the latest step and how many steps it has."""
    out = []
    seen = set()
    for d in app_state.org.list("decision_log"):
        item = log_for(app_state, obligation_id=d["_id"])
        seen.add(item["intake_id"])
        out.append(item)
    for r in app_state.intake.store.list(limit):
        if r.intake_id in seen or r.kind not in ("supplier_invoice", "contractor_claim", "expense_claim"):
            continue
        out.append(log_for(app_state, intake_id=r.intake_id))
    for x in out:
        x["steps"] = len(x["events"])
        x["last_at"] = x["events"][-1]["at"] if x["events"] else None
        x.pop("request", None)
    return sorted(out, key=lambda x: x["last_at"] or "", reverse=True)[:limit]
