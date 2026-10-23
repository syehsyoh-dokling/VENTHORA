# ruff: noqa: E501
"""Payee lifecycle: NEW -> VERIFIED -> ACTIVE, a cooling period after a bank-details change, a call-back that is mandatory.

The state machine is pure (transition / evaluate); the execution store persists it; the gate reads it.

  register                       -> NEW (bank fingerprint and the registered phone number on file)
  verify (call-back)             NEW | CHANGED -> VERIFIED. Refused unless: the role may approve, the number called is exactly the
                                 registered number, and there is a registered number at all. From CHANGED the cooling period is kept.
  bank_change                    NEW -> NEW (new fingerprint), VERIFIED | ACTIVE -> CHANGED with cooling_until = at + cooling_hours;
                                 the same fingerprint is not a change; SUSPENDED is left alone.
  paid (a PAID payout)           VERIFIED -> ACTIVE once the payment instant is at or after cooling_until (or there is none);
                                 before that the payee stays VERIFIED. Paying NEW / CHANGED / SUSPENDED is refused (the gate never asks).
  suspend / reinstate            a person suspends; reinstating goes back to NEW (another call-back).

What the plan does with a status (proofcore gate, Verity):
  NEW, CHANGED, SUSPENDED, unregistered -> the obligation is ESCALATED: no payment, not even with an approval (tier 3)
  VERIFIED inside the cooling period    -> REQUIRES_APPROVAL payee_in_cooling_period
  VERIFIED, never paid / cooling over   -> REQUIRES_APPROVAL payee_not_yet_active
  ACTIVE                                -> nothing from this control
"""
from __future__ import annotations

import datetime as dt

from .domain.models import ObligationDecision, PayeeRecord, PayeeRegistry, TreasuryState

PAYING = ("PAY", "CONVERT_AND_PAY")
BLOCKING = ("NEW", "CHANGED", "SUSPENDED")


class PayeeError(ValueError):
    pass


def _parse(ts: str | None) -> dt.datetime | None:
    if ts is None:
        return None
    t = dt.datetime.fromisoformat(str(ts).replace("+0000", "+00:00").replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat()


def in_cooling(record: PayeeRecord, now: dt.datetime) -> bool:
    until = _parse(record.cooling_until)
    return until is not None and now < until


def transition(record: PayeeRecord | None, event: dict, now: dt.datetime, *, cooling_hours: int = 72,
               approver_roles: tuple[str, ...] | list[str] = ("finance_manager",)) -> PayeeRecord:
    """Apply one event; raise PayeeError when the event is refused (the record is unchanged)."""
    kind = event.get("kind")
    if kind == "register":
        if record is not None:
            raise PayeeError(f"{record.beneficiary_id} is already registered ({record.status})")
        return PayeeRecord(beneficiary_id=event["beneficiary_id"], status="NEW", bank_fingerprint=event.get("bank_fingerprint"),
                           registered_phone=event.get("registered_phone"))
    if record is None:
        raise PayeeError("unregistered payee: register it first")
    r = record.model_dump()
    if kind == "verify":
        if event.get("role") not in approver_roles:
            raise PayeeError(f"role {event.get('role')!r} may not verify a payee; allowed: {list(approver_roles)}")
        if record.status not in ("NEW", "CHANGED"):
            raise PayeeError(f"nothing to verify: payee is {record.status}")
        if not record.registered_phone:
            raise PayeeError("no registered phone number on file: a call-back cannot be made; register the number first")
        if _digits(event.get("called_number")) != _digits(record.registered_phone):
            raise PayeeError("the call-back must be made to the registered number, not to a number taken from the request")
        if not str(event.get("by", "")).strip():
            raise PayeeError("the person who made the call-back must be named")
        r.update(status="VERIFIED", verified_at=_iso(now), verified_by=f"{event['by']} ({event['role']})")
        if record.status == "NEW":
            r["cooling_until"] = None
        return PayeeRecord.model_validate(r)
    if kind == "bank_change":
        new = event.get("bank_fingerprint")
        if not new:
            raise PayeeError("a bank change needs the new details' fingerprint")
        if new == record.bank_fingerprint:
            raise PayeeError("bank details unchanged: the fingerprint on file is the same")
        if record.status == "SUSPENDED":
            raise PayeeError("payee is suspended; reinstate before changing its details")
        r.update(bank_fingerprint=new, changed_at=_iso(now))
        if record.status != "NEW":
            r.update(status="CHANGED", cooling_until=_iso(now + dt.timedelta(hours=cooling_hours)), verified_at=None, verified_by=None)
        return PayeeRecord.model_validate(r)
    if kind == "paid":
        if record.status in BLOCKING:
            raise PayeeError(f"a {record.status} payee cannot be paid")
        at = _parse(event.get("at")) or now
        r["paid_count"] = record.paid_count + 1
        if record.status == "VERIFIED" and not in_cooling(record, at):
            r["status"] = "ACTIVE"
        return PayeeRecord.model_validate(r)
    if kind == "suspend":
        if event.get("role") not in approver_roles:
            raise PayeeError(f"role {event.get('role')!r} may not suspend a payee")
        r.update(status="SUSPENDED")
        return PayeeRecord.model_validate(r)
    if kind == "reinstate":
        if event.get("role") not in approver_roles:
            raise PayeeError(f"role {event.get('role')!r} may not reinstate a payee")
        if record.status != "SUSPENDED":
            raise PayeeError(f"payee is not suspended ({record.status})")
        r.update(status="NEW", verified_at=None, verified_by=None, cooling_until=None)
        return PayeeRecord.model_validate(r)
    raise PayeeError(f"unknown event {kind!r}")


def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def evaluate(inp: dict) -> dict:
    """Pure: {'record': {...}|null, 'events': [{kind, at?, ...}], 'cooling_hours'?, 'approver_roles'?} -> the record after
    the events, with every refusal named. Used by ground truth and the form; the store applies the same transitions."""
    record = PayeeRecord.model_validate(inp["record"]) if inp.get("record") else None
    cooling = int(inp.get("cooling_hours", 72))
    roles = tuple(inp.get("approver_roles") or ("finance_manager",))
    refused: list[dict] = []
    applied = 0
    for i, ev in enumerate(inp.get("events", [])):
        now = _parse(ev.get("at")) or dt.datetime.now(dt.UTC)
        try:
            record = transition(record, ev, now, cooling_hours=cooling, approver_roles=roles)
            applied += 1
        except (PayeeError, KeyError) as exc:
            refused.append({"index": i, "kind": ev.get("kind"), "reason": str(exc)})
    out = record.model_dump() if record else {"status": None, "cooling_until": None, "bank_fingerprint": None, "paid_count": 0,
                                               "verified_by": None, "registered_phone": None}
    out.update({"applied": applied, "refused": refused, "refused_n": len(refused),
                "last_refusal": refused[-1]["reason"] if refused else None,
                "payable": bool(record) and record.status in ("VERIFIED", "ACTIVE"),
                "autonomous": bool(record) and record.status == "ACTIVE"})
    return out


# -- onboarding records: the registry (data/payees.json) and the bank details on file (data/beneficiaries.json) -----

def bank_fingerprint_of(bank_details: dict) -> str:
    """A stable fingerprint of the bank details on file; a different account, routing or name is a different fingerprint."""
    import hashlib
    import json

    return "bank:" + hashlib.sha256(json.dumps(bank_details, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def onboarding_records(payees_path=None, beneficiaries_path=None) -> list[dict]:
    """One PayeeRecord per payee in the registry, from its `lifecycle` block (status at onboarding, registered phone) and
    the fingerprint of the bank details on file. Payees without a lifecycle block start NEW."""
    import json

    from .config import ROOT

    payees = json.loads((payees_path or ROOT / "data" / "payees.json").read_text(encoding="utf-8"))["payees"]
    bens = json.loads((beneficiaries_path or ROOT / "data" / "beneficiaries.json").read_text(encoding="utf-8"))
    out = []
    for cid, rec in payees.items():
        life = rec.get("lifecycle") or {}
        details = ((bens.get(cid) or {}).get("payload") or {}).get("beneficiary", {}).get("bank_details")
        status = life.get("status", "NEW")
        out.append(PayeeRecord(beneficiary_id=cid, status=status, registered_phone=life.get("registered_phone"),
                               bank_fingerprint=bank_fingerprint_of(details) if details else None,
                               verified_at=life.get("verified_at") if status != "NEW" else None,
                               verified_by=life.get("verified_by") if status != "NEW" else None,
                               paid_count=int(life.get("paid_count", 1 if status == "ACTIVE" else 0))).model_dump())
    return out


def attach_registry(state: TreasuryState, records: list[dict], as_of: str | None = None) -> TreasuryState:
    """The records for the payees this book names (an obligation whose payee has no record is unregistered)."""
    wanted = {o.beneficiary_id for o in state.obligations}
    reg = PayeeRegistry(as_of=as_of, records=[PayeeRecord.model_validate(r) for r in records if r["beneficiary_id"] in wanted])
    return state.model_copy(update={"payees": reg})


# -- what the plan reasons from -------------------------------------------------------------------------------------

def registry_now(reg: PayeeRegistry) -> dt.datetime:
    return _parse(reg.as_of) or dt.datetime.now(dt.UTC)


def blocking_reason(reg: PayeeRegistry, beneficiary_id: str) -> str | None:
    """Why this payee may not be paid at all right now (Verity turns it into a policy exception -> ESCALATE)."""
    rec = reg.get(beneficiary_id)
    if rec is None:
        return "payee_unregistered"
    if rec.status == "NEW":
        return "payee_unverified"
    if rec.status == "CHANGED":
        return "payee_bank_details_changed"
    if rec.status == "SUSPENDED":
        return "payee_suspended"
    return None


def payee_facts(state: TreasuryState, decisions: list[ObligationDecision]) -> dict:
    """Rows for every payout the plan would make now, with the payee's status and cooling position (mirrored in Rego)."""
    reg = state.payees
    if reg is None:
        return {"attached": False, "rows": [], "as_of": None}
    now = registry_now(reg)
    by_id = {o.obligation_id: o for o in state.obligations}
    rows = []
    for d in decisions:
        if d.action not in PAYING or d.obligation_id not in by_id:
            continue
        b = by_id[d.obligation_id].beneficiary_id
        rec = reg.get(b)
        rows.append({"obligation_id": d.obligation_id, "beneficiary_id": b, "status": rec.status if rec else None,
                     "cooling_until": rec.cooling_until if rec else None, "in_cooling": bool(rec and in_cooling(rec, now)),
                     "paid_count": rec.paid_count if rec else 0})
    return {"attached": True, "rows": rows, "as_of": _iso(now)}


def payee_reasons(f: dict) -> list[str]:
    """Mirrored one-to-one in policy/treasury.rego."""
    if not f.get("attached"):
        return []
    reasons = set()
    for r in f["rows"]:
        if r["status"] == "VERIFIED" and r["in_cooling"]:
            reasons.add("payee_in_cooling_period")
        if r["status"] == "VERIFIED" and not r["in_cooling"]:
            reasons.add("payee_not_yet_active")
    return sorted(reasons)


def describe(f: dict, reasons: list[str]) -> str:
    bits = []
    if "payee_in_cooling_period" in reasons:
        bits.append("inside the cooling period after a bank change: " + ", ".join(
            f"{r['beneficiary_id']} (until {r['cooling_until']})" for r in f["rows"] if r["status"] == "VERIFIED" and r["in_cooling"]))
    if "payee_not_yet_active" in reasons:
        bits.append("verified but never paid on these details: " + ", ".join(
            r["beneficiary_id"] for r in f["rows"] if r["status"] == "VERIFIED" and not r["in_cooling"]))
    active = sorted({r["beneficiary_id"] for r in f["rows"] if r["status"] == "ACTIVE"})
    if active:
        bits.append("active: " + ", ".join(active))
    return "; ".join(bits) if bits else "no payout to a registered payee"
