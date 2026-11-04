"""Pay what is inside the mandate now, hold only the exceptions: one exception no longer holds the whole book."""
import asyncio

from test_execution import FINANCE_MANAGER, WALLET, harness, paid, sandbox_credentials  # noqa: F401 (the fixture)

from conftest import scenario
from venthora import autonomy
from venthora.domain.models import TreasuryState
from venthora.execution.models import ExecutionRequest
from venthora.testing.fake_airwallex import FakeAirwallex


def _run(fake, tmp_path, name):
    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            state = TreasuryState.model_validate(scenario(name))
            s = await autonomy.split(agent, state)
            auto = None
            if s.auto_plan is not None:
                auto = await executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash))
            rest = None
            if s.remaining_state is not None:
                plan = await agent.decide(s.remaining_state)
                if any(d.action in autonomy.PAYING for d in plan.obligations):
                    rest = await executor.execute(ExecutionRequest(state=s.remaining_state, plan_hash=plan.plan_hash,
                                                                   approval=FINANCE_MANAGER))
            return s, auto, rest
        finally:
            await agent.close()
    return asyncio.run(go())


def test_an_approved_plan_is_autonomous_as_a_whole(tmp_path):
    s, auto, rest = _run(FakeAirwallex(balances=WALLET), tmp_path, "kit01_initial")
    assert s.full.decision == "APPROVED" and s.auto_plan is s.full and s.held == [] and s.remaining_state is None
    assert auto.status == "COMPLETED" and rest is None


def test_one_exception_no_longer_holds_the_rest(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    s, auto, rest = _run(fake, tmp_path, "kit01_receipt_delayed")
    assert s.full.decision == "REQUIRES_APPROVAL"
    full_pays = {d.obligation_id for d in s.full.obligations if d.action in autonomy.PAYING}
    auto_pays = {d.obligation_id for d in s.auto_plan.obligations if d.action in autonomy.PAYING}
    assert s.auto_plan.decision == "APPROVED" and auto_pays and auto_pays <= full_pays  # only what the whole plan pays
    assert s.held and all(h["reasons"] for h in s.held)
    assert {h["obligation_id"] for h in s.held} | auto_pays == full_pays
    # executed without anyone, then the remainder with an approval: nothing is planned twice, nothing paid twice
    assert auto.status == "COMPLETED", auto.message
    assert not ({o.obligation_id for o in s.remaining_state.obligations} & auto_pays)
    assert rest is None or rest.status in ("COMPLETED", "REFUSED"), rest.message
    assert len(paid(fake)) == len(set(paid(fake)))


def test_a_rejected_copy_is_never_paid_when_its_original_is_held(tmp_path):
    async def go():
        agent, _ = harness(FakeAirwallex(balances=WALLET), tmp_path)
        try:
            state = TreasuryState.model_validate(scenario("kit01_receipt_delayed"))
            return await autonomy.split(agent, state)
        finally:
            await agent.close()
    s = asyncio.run(go())
    rejected = {d.obligation_id for d in s.full.obligations if d.action == "REJECT"}
    assert rejected and not (rejected & {d.obligation_id for d in s.auto_plan.obligations if d.action in autonomy.PAYING})


def test_a_blocked_plan_executes_nothing(tmp_path):
    def mutate(st):
        st["actor_role"] = "viewer"
    async def go():
        agent, _ = harness(FakeAirwallex(balances=WALLET), tmp_path)
        try:
            raw = scenario("kit01_initial")
            mutate(raw)
            return await autonomy.split(agent, TreasuryState.model_validate(raw))
        finally:
            await agent.close()
    s = asyncio.run(go())
    assert s.full.decision == "BLOCKED" and s.auto_plan is None and s.held
