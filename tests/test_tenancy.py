# ruff: noqa: E501
"""Book and tenant segregation: whose book, which payees, whose signature, which policy."""
import asyncio

import pytest

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.tenancy import TENANTS_FILE, attach, load_registry, resolve
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("tenancy")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_tenancy_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_shipped_registry_resolves_the_demo_and_sandbox_books():
    reg = load_registry(TENANTS_FILE)
    assert resolve(reg, "kit01_initial").config.tenant == "acme-group"
    assert resolve(reg, "tenant-real-123").config.policy_overrides == {"max_single_payment_minor": 50000}
    lab = resolve(reg, "fx-real-000000")
    assert lab.known and lab.config.tenant == "sandbox-lab" and lab.matched_by == "default"
    assert resolve(reg | {"strict": True}, "fx-real-000000").known is False


def test_attach_applies_the_book_overrides():
    from venthora.domain.models import TreasuryState
    case = next(c for c in CASES if c["id"] == "T01")
    base = TreasuryState.model_validate({k: v for k, v in case["input"].items() if k != "tenancy"} | {"book_id": "ops-us"})
    reg = next(c for c in CASES if c["id"] == "U01")["input"]["registry"]
    s = attach(base, reg)
    assert s.tenancy.config.tenant == "acme" and s.policy.max_single_payment_minor == 50_000
    assert base.policy.max_single_payment_minor is None
