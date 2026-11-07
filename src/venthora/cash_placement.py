# ruff: noqa: E501
"""Budget allocation -> Cash placement: the cash the organisation does not need, and where a person may place it.

Needed = the reserve floor + approved bills not yet paid + every open commitment still owed (POs and contracts,
payroll, requests, invoices waiting; the Total Spending recap) + a safety buffer on top. Money already leaving
(processing) is already out of the available balance. Idle = available − needed. Above the minimum, VENTHORA proposes
a placement (a term deposit, a money market fund, another account). With a provider from FCO Authorize → Placement
providers, the FCO's approval sends the money by Airwallex transfer to the provider's checked account, and the
withdrawal comes back into the Airwallex Global Account (placement_providers.py). Without one, a person carries the
instruction out and marks it done; it stays on the record either way.
"""
from __future__ import annotations

import datetime as dt
import uuid

DEFAULT = {"buffer_pct": 10, "min_amount_minor": 100_000, "destination": "Term deposit, 1 month", "enabled": True}
STATUSES = ("PROPOSED", "APPROVED", "DONE", "CANCELLED")


def policy(org) -> dict:
    return DEFAULT | {k: v for k, v in (org.get("cash_placement", "policy") or {}).items() if k in DEFAULT}


def save_policy(org, body: dict, by: str) -> dict:
    pct, low = int(body.get("buffer_pct", DEFAULT["buffer_pct"])), int(body.get("min_amount_minor", DEFAULT["min_amount_minor"]))
    if not 0 <= pct <= 100:
        raise ValueError("the buffer is a percentage between 0 and 100")
    if low < 0:
        raise ValueError("the minimum cannot be negative")
    rec = {"_id": "policy", "buffer_pct": pct, "min_amount_minor": low, "destination": str(body.get("destination") or DEFAULT["destination"])[:120],
           "enabled": bool(body.get("enabled", True)), "updated_by": by}
    org._put("cash_placement", rec, by)
    return policy(org)


def compute(*, available_minor: int, reserve_floor_minor: int, bills_minor: int, commitments_minor: int, pol: dict) -> dict:
    """What must stay, what is idle, and what to place (all in the base currency, minor units)."""
    base_need = max(reserve_floor_minor, 0) + max(bills_minor, 0) + max(commitments_minor, 0)
    buffer = (base_need * int(pol["buffer_pct"]) + 99) // 100  # rounded up: the buffer never comes out short
    needed = base_need + buffer
    idle = available_minor - needed
    place = idle if pol.get("enabled") and idle >= int(pol["min_amount_minor"]) else 0
    why = ("placement is switched off" if not pol.get("enabled") else
           "nothing idle: what is held does not cover what is needed" if idle <= 0 else
           "idle cash below the minimum to place" if idle < int(pol["min_amount_minor"]) else
           "idle cash above the minimum: a placement is proposed")
    return {"available_minor": available_minor, "reserve_floor_minor": reserve_floor_minor, "bills_minor": bills_minor,
            "commitments_minor": commitments_minor, "buffer_minor": buffer, "needed_minor": needed, "idle_minor": idle,
            "place_minor": place, "why": why}


def instruct(org, *, amount_minor: int, currency: str, destination: str, note: str, by: str, basis: dict,
             provider: dict | None = None, base_minor: int | None = None) -> dict:
    """`amount_minor` in `currency` (the provider's); `base_minor` its value in the base currency, checked against the
    idle cash (the same number when the currency is the base)."""
    if amount_minor <= 0:
        raise ValueError("the amount must be above zero")
    in_base = amount_minor if base_minor is None else base_minor
    if in_base > max(basis.get("idle_minor", 0), 0):
        raise ValueError("the amount is above the idle cash: it would touch money that is needed")
    if provider is not None:
        if not provider.get("active", True):
            raise ValueError(f"{provider['name']} is not an active provider")
        if amount_minor < provider.get("minimum_minor", 0):
            raise ValueError(f"{provider['name']} takes at least {provider['currency']} {provider['minimum_minor'] / 100:,.2f}")
        destination = destination or f"{provider['name']} · " + (f"{provider['tenor_days']}-day term deposit" if provider["kind"] == "TERM_DEPOSIT" else
                                                                 "money market fund" if provider["kind"] == "MONEY_MARKET_FUND" else "Airwallex Yield (flexible)")
    rec = {"_id": str(uuid.uuid4()), "amount_minor": amount_minor, "currency": currency, "base_minor": in_base, "destination": destination[:120], "note": note[:300],
           "status": "PROPOSED", "proposed_by": by, "proposed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "basis": basis,
           "decided_by": None, "decided_at": None,
           "provider_id": provider["provider_id"] if provider else None, "provider_kind": provider["kind"] if provider else None,
           "indicative_rate_pct": provider.get("indicative_rate_pct") if provider else None,
           "history": [{"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "by": by, "what": "proposed"}]}
    org._put("placement", rec, by)
    return rec


def decide(org, pid: str, status: str, by: str, role: str | None = None) -> dict:
    """Approve, mark done or cancel. A placement with a provider reached by transfer is approved by the FCO only and
    then goes on by itself (placement_providers.send); it is never marked done by hand."""
    rec = org.get("placement", pid)
    if rec is None:
        raise KeyError(pid)
    if status not in STATUSES[1:]:
        raise ValueError("a placement is approved, done or cancelled")
    by_transfer = rec.get("provider_id") and rec.get("provider_kind") != "AIRWALLEX_YIELD"
    allowed = {"PROPOSED": ("APPROVED", "CANCELLED"), "APPROVED": ("CANCELLED",) if by_transfer else ("DONE", "CANCELLED")}.get(rec["status"], ())
    if status not in allowed:
        raise ValueError(f"a {rec['status'].lower()} placement cannot become {status.lower()}")
    if status == "APPROVED" and by.split(" (")[0] == rec["proposed_by"].split(" (")[0]:
        raise ValueError("the person who proposed a placement cannot approve it")
    if status == "APPROVED" and rec.get("provider_id") and role != "super_admin":
        raise PermissionError("a placement with a provider is approved by the FCO (Super admin)")
    rec |= {"status": status, "decided_by": by, "decided_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    rec.setdefault("history", []).append({"at": rec["decided_at"], "by": by, "what": status.lower()})
    org._put("placement", rec, by)
    return rec


def items(org) -> list[dict]:
    return sorted(org.list("placement"), key=lambda r: r.get("proposed_at", ""), reverse=True)
