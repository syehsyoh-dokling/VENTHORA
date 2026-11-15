# ruff: noqa: E501
"""The two questions every payer asks before it moves money.

  may_pay(...)            is this server the writer? On one server with no shared lease database
                          (VENTHORA_CONTINUITY_URL not set) the answer is always yes: there is nobody to pay beside.
                          With one configured, only the server holding the live writer lease pays (lease.py).
  may_pay_automatically   the same, and safe mode must be off.

Safe mode is the switch for a dangerous incident: a wrong payment noticed, a key that may be exposed, a server that
just took over and has not been checked. While it is on, nothing pays by itself: no scheduled payment run, no
payment on arrival, no autopilot. A person can still approve and pay one request deliberately, and everything else
keeps working (reading, planning, the audit chain). Turning it on and off is the FCO's, with a reason, and both are
in the audit chain.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

KIND, ID = "continuity", "safe_mode"
_lease = None


def lease_url() -> str | None:
    return os.environ.get("VENTHORA_CONTINUITY_URL") or None


def writer_lease():
    """The shared writer lease, when one is configured; None on a single server."""
    global _lease
    url = lease_url()
    if url is None:
        return None
    if _lease is None or _lease.url != url:
        from .lease import WriterLease

        _lease = WriterLease(url)
    return _lease


def safe_mode(org) -> dict:
    rec = org.get(KIND, ID) or {}
    return {"on": bool(rec.get("on")), "reason": rec.get("reason") or "", "by": rec.get("by"), "at": rec.get("at")}


def set_safe_mode(org, on: bool, reason: str, by: str) -> dict:
    if on and not reason.strip():
        raise ValueError("say why automatic payments are stopped")
    org._put(KIND, {"_id": ID, "on": bool(on), "reason": reason.strip(), "by": by, "at": datetime.now(UTC).isoformat(timespec="seconds")}, by)
    return safe_mode(org)


def may_pay() -> tuple[bool, str]:
    """(yes or no, why). Asked by the executor immediately before money moves."""
    if lease_url() is None:
        return True, "single server: no other writer exists"
    try:
        lease = writer_lease()
        if lease.renew() or lease.acquire():
            return True, f"{lease.node} holds the writer lease"
        holder = lease.holder() or {}
        return False, f"this server ({lease.node}) is not the writer: {holder.get('holder')} holds the lease"
    except Exception as exc:  # noqa: BLE001 - a lease that cannot be read is a lease not held: fail closed
        return False, f"the writer lease cannot be read ({type(exc).__name__}): this server does not pay until it can"


def may_pay_automatically(org) -> tuple[bool, str]:
    mode = safe_mode(org)
    if mode["on"]:
        return False, f"safe mode is on ({mode['reason']}): no automatic payment until the FCO lifts it"
    return may_pay()


def status(org) -> dict:
    ok, why = may_pay()
    lease = holder = None
    try:
        lease = writer_lease()
        holder = lease.holder() if lease else None
    except Exception:  # noqa: BLE001 - the status page still answers when the lease database does not
        pass
    return {"safe_mode": safe_mode(org), "may_pay": ok, "why": why, "node": lease.node if lease else None,
            "lease": holder, "shared_lease": lease_url() is not None}
