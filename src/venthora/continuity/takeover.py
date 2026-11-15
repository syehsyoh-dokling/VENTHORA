# ruff: noqa: E501
"""What a standby does when it takes over, in the order it must be done.

    1. take the writer lease        refused while the primary's lease is live: nothing else happens
    2. safe mode ON                 before anything is read or caught up, so no payer can start in the middle
    3. catch up from Airwallex      every transfer the bank knows is locked in the payout register, paid or in flight;
                                    after this, a plan cannot pay any of them again
    4. re-read the mailbox          the last 24 hours; a message already in the queue is known by its Message-ID
    5. report                       how old the copied data was, what was caught up, what a person must look at

Safe mode stays on. A person reads the report, compares the book with the wallet, and lifts it. The standby never
decides by itself that it is safe to pay.

Each step takes what it needs as arguments (the bank client, the payout register, the mail reader), so the order and
the rules can be read here and tested without a bank or a mailbox.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from . import guard


def _iso(epoch: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=epoch)).isoformat(timespec="seconds")

# Airwallex transfer statuses that mean the money did not and will not leave: the invoice may be paid again
NOT_PAID = {"FAILED", "CANCELLED", "REJECTED"}
PAID = {"PAID", "SUCCEEDED", "SENT"}
MAIL_HOURS = 24


def payout_key(reference: str) -> tuple[str, str] | None:
    """(payout key, obligation id) from a transfer's reference. VENTHORA writes every transfer's reference as
    "<obligation id> <invoice fingerprint>" (execution/executor.py), and locks an invoice by its fingerprint, or by
    the obligation id when it has none: the same key the executor uses, rebuilt from what the bank holds."""
    parts = str(reference or "").split()
    if not parts:
        return None
    obligation, fingerprint = parts[0], (parts[1] if len(parts) > 1 else "")
    return (f"invoice:{fingerprint}" if fingerprint else f"obligation:{obligation}"), obligation


async def catch_up_payments(bank, store, *, pages: int = 20) -> dict:
    """Lock every transfer Airwallex knows that this server's payout register does not. A transfer that failed or was
    cancelled locks nothing. Returns what was found; `unreadable` are transfers whose reference VENTHORA did not
    write (made by hand at the bank): a person looks at those."""
    transfers = await bank.list_transfers(pages=pages)
    locked, already, not_paid, unreadable = [], 0, 0, []
    for t in transfers:
        status = str(t.get("status") or "").upper()
        if status in NOT_PAID:
            not_paid += 1
            continue
        key = payout_key(t.get("reference"))
        if key is None:
            unreadable.append({"transfer_id": t.get("id"), "status": status})
            continue
        payout, obligation = key
        known = store.payout(payout)
        if known and known.get("status") in ("PAID", "IN_FLIGHT"):
            already += 1
            continue
        execution = f"takeover:{t.get('id')}"
        store.claim_payout(payout, obligation, execution)  # IN_FLIGHT: the duplicate lock now refuses a second payout
        if status in PAID:
            store.settle_payout(payout, "PAID", t.get("id"))
        locked.append({"payout": payout, "obligation_id": obligation, "transfer_id": t.get("id"), "status": status})
    return {"transfers": len(transfers), "locked": locked, "already_known": already, "not_paid": not_paid, "unreadable": unreadable}


async def reread_mail(fetch_since, known_message_ids: set[str], ingest, *, hours: int = MAIL_HOURS, now: float | None = None) -> dict:
    """Read the mailbox again for the last `hours`. `fetch_since(epoch)` returns messages as {message_id, raw};
    `ingest(raw)` puts one through the normal intake (sender check, document checks, duplicate check). A message
    already in the queue is skipped by its Message-ID, so re-reading never makes a second request for one email."""
    since = (now or time.time()) - hours * 3600
    messages = await fetch_since(since)
    new, skipped, failed = [], 0, []
    for m in messages:
        mid = str(m.get("message_id") or "").strip()
        if mid and mid in known_message_ids:
            skipped += 1
            continue
        try:
            await ingest(m["raw"])
            new.append(mid or "(no Message-ID)")
        except Exception as exc:  # noqa: BLE001 - one unreadable email never stops the others
            failed.append({"message_id": mid, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return {"since": _iso(since), "read": len(messages), "new": new, "already_in_queue": skipped, "failed": failed}


async def take_over(*, lease, org, bank, store, fetch_since, known_message_ids: set[str], ingest, copied_at: float | None,
                    by: str = "VENTHORA (takeover)", now: float | None = None) -> dict:
    """The whole sequence. Returns the report a person reads before lifting safe mode."""
    now = now or time.time()
    if lease is not None and not lease.acquire():
        holder = lease.holder() or {}
        return {"took_over": False, "reason": f"{holder.get('holder')} still holds the writer lease: it is alive, nothing was changed"}
    guard.set_safe_mode(org, True, "this server took over from another: check the book against the wallet, then lift", by)
    payments = await catch_up_payments(bank, store)
    mail = await reread_mail(fetch_since, known_message_ids, ingest, now=now)
    age_min = None if copied_at is None else round((now - copied_at) / 60)
    look_at = []
    if copied_at is None:
        look_at.append("no copy of the data was found: this server started from its own stores")
    elif age_min > 60:
        look_at.append(f"the copied data is {age_min} minutes old: approvals, call-backs and settings made since are not here")
    if payments["unreadable"]:
        look_at.append(f"{len(payments['unreadable'])} transfer(s) at the bank carry a reference VENTHORA did not write: match them by hand")
    if mail["failed"]:
        look_at.append(f"{len(mail['failed'])} email(s) could not be read again")
    look_at.append("compare the book balance with the Airwallex wallet (Agent plan), then lift safe mode")
    return {"took_over": True, "node": getattr(lease, "node", None), "at": _iso(now),
            "data_age_minutes": age_min, "payments": payments, "mail": mail, "safe_mode": guard.safe_mode(org), "look_at": look_at}
