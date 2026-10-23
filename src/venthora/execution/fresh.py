"""A fresh book: the same scenario under new identifiers, so a cycle can be repeated on the real sandbox.

The execution store remembers every invoice fingerprint it has paid (the duplicate lock) and carries the
managed budget per book. Re-running a demo cycle therefore needs new obligation ids, invoice fingerprints
and a new book id; the amounts, evidence and policy stay exactly the same. Budget lines keep their names,
so the period's mandate still applies (realized spend is per period, not per book).
"""
from __future__ import annotations

import datetime as dt

from ..domain.models import TreasuryState


def fresh_tag() -> str:
    return dt.datetime.now().strftime("%m%d%H%M%S")


def fresh_book(state: TreasuryState, tag: str) -> TreasuryState:
    data = state.model_dump()
    data["book_id"] = f"{data['book_id']}-{tag}"
    data["case_id"] = f"{data['case_id']}-{tag}"
    renamed = {}
    for o in data["obligations"]:
        new_id = f"{o['obligation_id']}-{tag}"
        renamed[o["obligation_id"]] = new_id
        o["obligation_id"] = new_id
        if o.get("invoice_fingerprint"):
            o["invoice_fingerprint"] = f"{o['invoice_fingerprint']}-{tag}"
    for e in data["evidence"]:
        if e["subject_id"] in renamed:
            e["subject_id"] = renamed[e["subject_id"]]
    return TreasuryState.model_validate(data)


def fresh_id(obligation_id: str, state: TreasuryState) -> str:
    """The fresh id of an obligation named by its original id (or the id itself when it already exists)."""
    ids = {o.obligation_id for o in state.obligations}
    if obligation_id in ids:
        return obligation_id
    return next((i for i in ids if i.startswith(obligation_id + "-")), obligation_id)
