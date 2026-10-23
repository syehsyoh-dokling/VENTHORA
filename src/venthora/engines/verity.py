"""Verity: which obligations are backed by evidence we can trust?"""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from ..domain.models import Obligation, StageResult, TreasuryState


@dataclass
class VerityResult:
    eligible: dict[str, Obligation]
    rejected: dict[str, str]
    stage: StageResult


async def verity(state: TreasuryState) -> VerityResult:
    started = perf_counter()
    evidence = {e.ref: e for e in state.evidence}
    eligible: dict[str, Obligation] = {}
    rejected: dict[str, str] = {}
    seen_fingerprints: dict[str, str] = {}

    for o in sorted(state.obligations, key=lambda x: (-x.priority, x.due_in_hours, x.obligation_id)):
        doc = evidence.get(o.evidence_ref)
        if o.record_kind != "payable":
            rejected[o.obligation_id] = "already settled; not a payable"
        elif o.invoice_fingerprint and o.invoice_fingerprint in seen_fingerprints:
            rejected[o.obligation_id] = f"duplicate of {seen_fingerprints[o.invoice_fingerprint]} (same invoice fingerprint)"
        elif doc is None:
            rejected[o.obligation_id] = f"no evidence document {o.evidence_ref}"
        elif doc.subject_id != o.obligation_id:
            rejected[o.obligation_id] = f"evidence {doc.ref} refers to {doc.subject_id}"
        elif doc.status != "verified":
            rejected[o.obligation_id] = f"evidence {doc.ref} is {doc.status}"
        else:
            eligible[o.obligation_id] = o
            if o.invoice_fingerprint:
                seen_fingerprints[o.invoice_fingerprint] = o.obligation_id

    summary = f"{len(eligible)} obligations verified, {len(rejected)} rejected"
    return VerityResult(eligible, rejected, StageResult(
        engine="Verity · evidence", status="WARN" if rejected else "PASS", summary=summary,
        details={"eligible": list(eligible), "rejected": rejected},
        duration_ms=(perf_counter() - started) * 1000))
