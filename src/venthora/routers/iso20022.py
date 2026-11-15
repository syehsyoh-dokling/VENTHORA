# ruff: noqa: E501
"""ISO 20022 endpoints (iso20022.py): the day's payments as pain.001, a bank statement (camt.053) reconciled."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .. import iso20022

router = APIRouter()
MAX_STATEMENT_BYTES = 15 * 1024 * 1024


def payments(state, day: str | None = None) -> list[dict]:
    """VENTHORA's paid payments in the shape iso20022 works on; one day (UTC) or all of them."""
    from ..masterdata import bank_from_payload

    bens = state.executor.beneficiaries
    out = []
    for r in state.executor.store.paid_rows():
        paid_on = str(r.get("paid_at") or "")[:10]
        if day and paid_on != day:
            continue
        ben = bens.get(r.get("beneficiary_id") or "") or {}
        who = ((ben.get("payload") or {}).get("beneficiary") or {})
        name = who.get("company_name") or " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or r.get("beneficiary_id") or ""
        out.append({"end_to_end_id": r["obligation_id"], "amount_minor": r["amount_minor"], "currency": r["currency"], "execution_date": paid_on,
                    "creditor_name": name, "creditor_country": (who.get("address") or {}).get("country_code"),
                    "bank": bank_from_payload(ben) or {}, "remittance": r.get("invoice_fingerprint") or r["obligation_id"]})
    return out


@router.get("/v1/iso20022/pain.001")
async def pain001(request: Request, date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """The payments VENTHORA made on one day (UTC, today by default) as an ISO 20022 Customer Credit Transfer
    Initiation (pain.001.001.09): the file a bank or an auditor reads. Each payment's EndToEndId is its obligation id.
    A record of what was paid through Airwallex; downloading it moves no money."""
    from .. import airwallex_settings

    state = request.app.state
    day = date or datetime.now(UTC).date().isoformat()
    rows = await asyncio.to_thread(payments, state, day)
    if not rows:
        raise HTTPException(404, f"no payment was made on {day}")
    company = (airwallex_settings.tenant() or {}).get("name") or (state.org.get("unit", "hq") or {}).get("name") or "VENTHORA"
    xml = iso20022.pain001(rows, debtor_name=company, message_id=f"VENTHORA-{day}")
    return Response(xml, media_type="application/xml", headers={"Content-Disposition": f'attachment; filename="pain.001-{day}.xml"'})


@router.post("/v1/iso20022/camt.053")
async def camt053(request: Request):
    """Read a bank statement (ISO 20022 camt.053, the XML as the body) and reconcile its debits with the payments
    VENTHORA made, by EndToEndId, amount and currency: matched, a different amount, on the statement only, and paid but
    not on the statement. Nothing is changed by reading it."""
    body = await request.body()
    if not body or len(body) > MAX_STATEMENT_BYTES:
        raise HTTPException(413, "the statement is empty or larger than 15 MB")
    try:
        entries = iso20022.camt053(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    paid = await asyncio.to_thread(payments, request.app.state)
    days = {e["booked"] for e in entries if e["booked"]}
    in_range = [p for p in paid if not days or min(days) <= p["execution_date"] <= max(days)]
    return {"entries": len(entries), "from": min(days) if days else None, "to": max(days) if days else None,
            "reconciliation": iso20022.reconcile(entries, in_range)}
