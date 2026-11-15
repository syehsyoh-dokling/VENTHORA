# ruff: noqa: E501
"""Continuity endpoints (continuity/): who may pay, safe mode, the copy to PostgreSQL, taking over.

On a deployment with one server (this staging demo) there is no standby to take over and no shared lease, and the
endpoints that need them say so plainly instead of pretending: 409 with what is missing and how it is switched on.
Safe mode works everywhere.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..continuity import guard, replicate, takeover

router = APIRouter()
NOT_CONNECTED = ("No standby is connected in this environment. This deployment runs on one server, so there is no second "
                 "server to take over and no shared writer lease. Failover is switched on by configuration, on both "
                 "servers: VENTHORA_CONTINUITY_URL (the shared PostgreSQL that holds the lease) and VENTHORA_REPLICA_URL "
                 "(where the stores are copied). See docs/CONTINUITY.md.")


def replica_url() -> str | None:
    return os.environ.get("VENTHORA_REPLICA_URL") or None


def _fco(role: str) -> None:
    if role != "super_admin":
        raise HTTPException(403, "Continuity: only the FCO (Super admin) changes this")


@router.get("/v1/continuity")
async def continuity_status(request: Request):
    """Whether this server may pay and why, whether safe mode is on, who holds the writer lease, and when the stores
    were last copied. On one server it says that no standby is connected."""
    state = request.app.state
    out = await asyncio.to_thread(guard.status, state.org)
    url = replica_url()
    copied = None
    if url:
        try:
            copied = await asyncio.to_thread(replicate.last_run, url)
        except Exception as exc:  # noqa: BLE001 - the status still answers when the replica does not
            copied = {"error": f"the replica cannot be read ({type(exc).__name__})"}
    return out | {"replica": {"configured": url is not None, "last_copy": copied},
                  "standby": {"connected": out["shared_lease"], "notice": None if out["shared_lease"] else NOT_CONNECTED},
                  "design": "docs/CONTINUITY.md"}


class SafeModeIn(BaseModel):
    on: bool
    reason: str = Field("", max_length=300)


@router.put("/v1/continuity/safe-mode")
async def continuity_safe_mode(body: SafeModeIn, request: Request, role: str = "finance_manager", by: str = "dashboard"):
    """Stop every automatic payment (scheduled run, payment on arrival, autopilot) until it is lifted, with the reason;
    a person can still approve and pay one request. For a dangerous incident, and after a takeover."""
    _fco(role)
    state = request.app.state
    try:
        out = await asyncio.to_thread(guard.set_safe_mode, state.org, body.on, body.reason, by)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(state.audit.append, "safe_mode_on" if body.on else "safe_mode_off", {"by": by, "reason": body.reason})
    return out


@router.post("/v1/continuity/replicate")
async def continuity_replicate(request: Request, role: str = "finance_manager", by: str = "dashboard"):
    """Copy every store to the replica now (the host's scheduler runs the same copy on a timer: continuity/replicate.py)."""
    _fco(role)
    url = replica_url()
    if url is None:
        raise HTTPException(409, NOT_CONNECTED)
    data_dir = Path(os.environ.get("VENTHORA_DATA_DIR") or Path(request.app.state.executor.store.path).parent)
    result = await asyncio.to_thread(replicate.run, data_dir, url)
    await asyncio.to_thread(request.app.state.audit.append, "stores_replicated", {"by": by, "stores": len(result), "ok": all(r["ok"] for r in result)})
    return {"stores": result, "ok": all(r["ok"] for r in result)}


@router.post("/v1/continuity/takeover")
async def continuity_takeover(request: Request, role: str = "finance_manager", by: str = "dashboard"):
    """Make this server the writer: take the lease, switch safe mode on, and lock every transfer Airwallex knows so
    nothing is paid twice (continuity/takeover.py). Refused while another server holds a live lease. On one server
    there is nothing to take over from, and it says so."""
    _fco(role)
    lease = None
    try:
        lease = guard.writer_lease()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"the writer lease cannot be read ({type(exc).__name__}): nothing was changed") from exc
    if lease is None:
        raise HTTPException(409, NOT_CONNECTED)
    state = request.app.state
    # the mail reader and the intake, where a deployment connects them: (since) -> messages, and (raw) -> a request
    reread, ingest = getattr(state, "reread_mail", None), getattr(state, "ingest_mail", None)
    if reread is None or ingest is None:
        reread = None

    async def no_mail(since):
        return []

    async def no_ingest(raw):
        return None

    copied = None
    if replica_url():
        last = await asyncio.to_thread(replicate.last_run, replica_url())
        copied = (last or {}).get("run_at")
    known = {q.message_id for q in await asyncio.to_thread(state.intake.store.list, 5000) if getattr(q, "message_id", None)}
    report = await takeover.take_over(lease=lease, org=state.org, bank=state.executor.bank, store=state.executor.store,
                                      fetch_since=reread or no_mail, known_message_ids=known, ingest=ingest if reread else no_ingest,
                                      copied_at=copied, by=by)
    if report.get("took_over") and reread is None:
        report["look_at"].insert(0, "the mailbox was not read again here: run Email Sync → Check now for the last 24 hours")
    await asyncio.to_thread(state.audit.append, "takeover", {"by": by, "took_over": report.get("took_over"),
                                                             "locked": len((report.get("payments") or {}).get("locked", []))})
    return report
