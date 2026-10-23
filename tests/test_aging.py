# ruff: noqa: E501
"""Invoice aging and dunning: buckets, overdue escalation, late interest, reminders from the inbox — mirrored in Rego."""
import asyncio
import datetime as dt

import pytest

from venthora.aging import aging_reasons, bucket_of, interest
from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("aging")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_aging_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_buckets_and_interest():
    assert [bucket_of(d) for d in (0, 1, 30, 31, 60, 61, 90, 91)] == ["current", "1-30", "1-30", "31-60", "31-60", "61-90", "61-90", "90+"]
    assert interest(1_000_000, 800, 45) == 9863 and interest(1_000_000, 800, 0) == 0 and interest(1_000_000, 0, 45) == 0


def test_reasons_from_rows():
    f = {"rows": [{"days_overdue": 31, "dunning_level": 3, "paying": False}], "exposure_minor": 1001,
         "overdue_escalation_days": 30, "dunning_escalation_level": 3, "max_late_interest_exposure_minor": 1000}
    assert aging_reasons(f) == ["final_notice_unpaid", "late_interest_exposure_above_limit", "overdue_obligation_unpaid"]
    assert aging_reasons(f | {"rows": [{"days_overdue": 31, "dunning_level": 3, "paying": True}], "exposure_minor": 0}) == []


def test_a_final_notice_raises_the_book_obligation_without_a_second_payable(tmp_path):
    """to_state: the duplicate's dunning level travels to the existing obligation; the obligation count is unchanged."""
    import json

    from venthora.config import ROOT
    from venthora.domain.models import TreasuryState

    intake = Intake(IntakeStore(str(tmp_path / "intake.db")))
    base = TreasuryState.model_validate(json.loads((ROOT / "data" / "scenarios" / "kit01_initial.json").read_text(encoding="utf-8"))["state"])
    case = next(c for c in CASES if c["id"] == "D04")
    req = intake.ingest(case["input"].encode("utf-8"), book_fingerprints={o.invoice_fingerprint: o.obligation_id for o in base.obligations if o.invoice_fingerprint})
    assert req.status == "DUPLICATE" and req.dunning_level == 3
    state = intake.to_state(base, as_of=dt.date(2026, 10, 24), requests=[req])
    assert len(state.obligations) == len(base.obligations)
    assert next(o for o in state.obligations if o.obligation_id == "SAAS-USD").dunning_level == 3
    assert state.as_of == "2026-10-24"
