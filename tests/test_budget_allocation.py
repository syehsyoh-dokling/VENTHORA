# ruff: noqa: E501
"""Budget allocation: periods follow the fiscal year and the cut-off, the most specific allocation governs a charge
code, No limit lifts the cap, and the same check refuses a payment at execution when its allocation is used up."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from venthora import budget_allocation as ba


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_periods_follow_the_fiscal_year():
    assert ba.period_of("2026-10", "monthly") == "2026-10"
    assert ba.period_of("2026-10", "quarterly") == "2026-Q4"
    assert ba.period_of("2026-10", "quarterly", start=10) == "2026-Q1"  # Q1 = Oct-Dec when the year starts in October
    assert ba.period_of("2027-01", "quarterly", start=10) == "2026-Q2"  # Q2 follows: Jan-Mar
    assert ba.period_of("2027-09", "annual", start=10) == "2026"
    assert ba.period_of("2026-07", "half_year") == "2026-H2" and ba.period_of("2026-07", "no_limit") == "ALL"
    assert ba.months_of("2026-Q2", start=10) == ["2027-01", "2027-02", "2027-03"]
    assert ba.months_of("2026", start=4)[0] == "2026-04" and ba.months_of("2026", start=4)[-1] == "2027-03"
    assert [q["months"] for q in ba.quarters(10)][:2] == [["Oct", "Nov", "Dec"], ["Jan", "Feb", "Mar"]]


def test_sync_schedule():
    pol = {"daily_sync_time": "23:59", "monthly_sync_day": 31, "sync_enabled": True,
           "last_daily_sync_at": "2026-10-29T17:00:00+00:00", "last_monthly_sync_at": "2026-09-30T17:00:00+00:00"}
    now = dt.datetime(2026, 10, 30, 17, 30, tzinfo=dt.UTC)  # 00:30 on 31 Oct in Jakarta
    assert ba.last_scheduled("daily", pol, now, "Asia/Jakarta").isoformat() == "2026-10-30T23:59:00+07:00"
    assert ba.due("daily", pol, now, "Asia/Jakarta")
    assert not ba.due("monthly", pol, now, "Asia/Jakarta")  # 31 Oct 23:59 has not come yet
    assert ba.next_scheduled("monthly", pol | {"monthly_sync_day": 31}, dt.datetime(2026, 11, 5, tzinfo=dt.UTC), "UTC").day == 30  # clamped
    assert not ba.due("daily", pol | {"last_daily_sync_at": None}, now, "Asia/Jakarta")  # first run is the next scheduled one


def _alloc(client, **body):
    r = client.post("/v1/org/allocations", json={"currency": "USD", "line": "Operations"} | body)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_most_specific_allocation_governs(client):
    assert client.get("/v1/budget-allocation/policy").json()["enforcement"] == "no_limit"  # the default: every unit may pay
    client.put("/v1/budget-allocation/policy", json={"enforcement": "per_allocation", "period_type": "monthly", "fiscal_year_start_month": 1, "cutoff_day": 31, "cutoff_time": "23:59", "timezone": "UTC"})
    month = dt.datetime.now(dt.UTC).strftime("%Y-%m")
    _alloc(client, period=month, unit_id="co-us", include_children=True, amount_minor=100_000)
    _alloc(client, period=month, unit_id="br-us-west", amount_minor=20_000, include_children=False)
    _alloc(client, period=month, basis="project", project_code="BRAND26", amount_minor=0, no_limit=True, line="Brand")

    def check(code, amt):
        return client.post("/v1/budget-allocation/check", json={"account_code": code, "amount_minor": amt, "currency": "USD"}).json()

    assert check("US-200.OPS.6700", 90_000)["ok"]  # the country office's own budget
    r = check("US-210.OPS.6700", 30_000)  # the branch has its own, smaller allocation
    assert not r["ok"] and "br-us-west" in r["allocation"] and "200.00 USD left" in r["reason"]
    assert check("US-210.OPS.6700", 20_000)["ok"]
    r = check("US-200.BRAND26.6600", 9_000_000)
    assert r["ok"] and "No limit" in r["reason"]  # the project-based allocation wins over the office
    r = check("UK-300.OPS.6700", 100)  # no monthly allocation for the UK: its own annual one governs (each has its period)
    assert r["ok"] and r["alloc_id"] == "a-uk"
    client.delete("/v1/org/allocations/a-uk")
    assert check("UK-300.OPS.6700", 100)["alloc_id"] == "a-hq"  # nothing for the UK: the head office's covers the units under it
    assert client.delete("/v1/org/allocations/a-hq").status_code == 200
    r = check("UK-300.OPS.6700", 100)
    assert not r["ok"] and "no budget allocation covers" in r["reason"], (r.get("alloc_id"), r.get("reason"))
    assert not check("XX-1.OPS.6700", 100)["ok"]  # an unknown charge code is never paid

    client.put("/v1/budget-allocation/policy", json={"enforcement": "no_limit", "period_type": "monthly", "fiscal_year_start_month": 1, "cutoff_day": 31, "cutoff_time": "23:59", "timezone": "UTC"})
    assert check("UK-300.OPS.6700", 10_000_000)["ok"]  # No limit: every unit pays from the balance


def test_allocations_are_validated(client):
    def post(**b):
        return client.post("/v1/org/allocations", json={"currency": "USD", "line": "Ops", "period": "2026"} | b)

    assert post(unit_id="co-us").status_code == 409  # neither an amount nor No limit
    assert post(basis="project", amount_minor=100).status_code == 409  # a project basis needs its project
    assert post(unit_id="co-uk", project_code="BRAND26", amount_minor=100).status_code == 409  # BRAND26 is the US office's
    assert post(unit_id="hq", project_code="BRAND26", amount_minor=100).status_code == 200  # HQ is above it
    r = post(unit_id="co-us", amount_minor=100, include_children=False, line="Other")
    assert r.status_code == 409 and "one allocation governs a scope" in r.json()["detail"]  # a-us already governs co-us in 2026
    assert post(period="2026-H1", unit_id="co-de", amount_minor=100).status_code == 200
    assert post(period="ALL", unit_id="co-de", amount_minor=0, no_limit=True).status_code == 200


def test_policy_round_trip_and_status(client):
    r = client.put("/v1/budget-allocation/policy", json={"period_type": "quarterly", "fiscal_year_start_month": 10, "cutoff_day": 25,
                                                         "cutoff_time": "18:00", "timezone": "Asia/Jakarta", "daily_sync_time": "23:59", "monthly_sync_day": 31})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["quarters"][0]["months"] == ["Oct", "Nov", "Dec"] and p["cutoff_time"] == "18:00" and p["monthly_sync_day"] == 31
    assert p["active_period"].endswith(("Q1", "Q2", "Q3", "Q4")) and p["next_sync"]["daily"]
    assert client.get("/v1/org/standards").json()["fiscal_year_start_month"] == 10
    assert client.get("/v1/reporting/config").json()["cutoff_time"] == "18:00"
    assert client.put("/v1/budget-allocation/policy?role=auditor", json={}).status_code == 403
    assert client.put("/v1/budget-allocation/policy", json={"timezone": "Mars/Base"}).status_code == 422
    st = client.get("/v1/budget-allocation/status?period=2026").json()
    a = next(x for x in st["allocations"] if x["alloc_id"] == "a-us")
    assert a["spent_minor"] == 0 and a["remaining_minor"] == a["amount_minor"]
    s = client.post("/v1/budget-allocation/sync?kind=daily").json()
    assert s["kind"] == "daily" and client.get("/v1/budget-allocation/policy").json()["last_daily_sync_at"] == s["at"]


def test_execution_refuses_a_payment_beyond_its_allocation(tmp_path):
    """The gate inside the executor, on the spend the ledger already holds."""
    from venthora.budget_allocation import Budget
    from venthora.execution.store import ExecutionStore
    from venthora.organisation import OrgStore

    org = OrgStore(str(tmp_path / "o.db"))
    store = ExecutionStore(str(tmp_path / "e.db"))
    month = "2026-10"
    with store._connect() as c:
        c.execute("INSERT INTO spend_ledger (transfer_id, obligation_id, book_id, line, currency, amount_minor, base_cost_minor, paid_at,"
                  " reporting_period, recorded_at, account_code) VALUES ('t1','o1','b','l','USD',200000000,200000000,'2026-10-01',?, '2026-10-01','US-200.OPS.6700')", (month,))
    from venthora.budget_allocation import BudgetPolicy

    org.save_budget_policy(BudgetPolicy(enforcement="per_allocation"), "test")
    b = Budget(org, store)
    usage = {a["alloc_id"]: a for a in b.usage("2026")}
    assert usage["a-us"]["spent_minor"] == 200_000_000 and usage["a-hq"]["spent_minor"] == 0  # counted where it was governed
    items = [{"obligation_id": "x", "account_code": "US-200.OPS.6700", "currency": "USD", "amount_minor": 30_000_000, "base_cost_minor": 30_000_000},
             {"obligation_id": "y", "account_code": "US-200.OPS.6700", "currency": "USD", "amount_minor": 20_000_000, "base_cost_minor": 20_000_000}]
    v = b.check(items, month)
    assert v[0]["ok"] and not v[1]["ok"]  # 2.4M - 2.0M = 0.4M left: the first fits, the two together do not


def test_execute_refuses_before_any_money_moves(tmp_path, monkeypatch):
    import asyncio

    from test_execution import WALLET, harness

    from conftest import scenario
    from venthora.domain.models import TreasuryState
    from venthora.execution.models import ExecutionRequest
    from venthora.organisation import Allocation, OrgStore
    from venthora.testing.fake_airwallex import FakeAirwallex

    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "sandbox-key")
    org = OrgStore(str(tmp_path / "o.db"))
    from venthora.budget_allocation import BudgetPolicy

    org.save_budget_policy(BudgetPolicy(enforcement="per_allocation"), "test")  # the default is No limit
    a = org.get("allocation", "a-uk") | {"amount_minor": 100}  # UK office: USD 1.00 left for the whole year
    org.save("allocation", Allocation.model_validate(a), "test")
    fake = FakeAirwallex(balances=WALLET)

    async def go():
        agent, executor = harness(fake, tmp_path)
        executor.org = org
        try:
            s = scenario("kit01_initial")
            for o in s["obligations"]:
                o["account_code"] = "UK-300.OPS.6100"
            state = TreasuryState.model_validate(s)
            plan = await agent.decide(state)
            return await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash))
        finally:
            await agent.close()

    result = asyncio.run(go())
    assert result.status == "REFUSED" and "budget allocation" in result.message and "1.00 USD left" in result.message
    assert fake.transfers == {} and fake.conversions == {}


def test_each_allocation_has_its_own_period_and_a_project_its_duration(client):
    from venthora.budget_allocation import covers

    assert covers({"period": "2026-Q4"}, "2026-11") and not covers({"period": "2026-Q3"}, "2026-11")
    assert covers({"period": "ALL", "start_date": "2026-10-01", "end_date": "2027-03-31"}, "2027-03")
    assert not covers({"period": "ALL", "start_date": "2026-10-01", "end_date": "2027-03-31"}, "2027-04")
    client.put("/v1/budget-allocation/policy", json={"enforcement": "per_allocation", "period_type": "annual", "fiscal_year_start_month": 1, "cutoff_day": 31, "cutoff_time": "23:59", "timezone": "UTC"})
    month = dt.datetime.now(dt.UTC).strftime("%Y-%m")
    _alloc(client, period=month, unit_id="br-us-west", amount_minor=10_000, include_children=False)  # the branch, per month
    today = dt.date.today()
    _alloc(client, basis="project", project_code="BRAND26", period="ALL", amount_minor=50_000, line="Brand",
           start_date=(today - dt.timedelta(days=40)).isoformat(), end_date=(today + dt.timedelta(days=40)).isoformat())

    def check(code, amt):
        return client.post("/v1/budget-allocation/check", json={"account_code": code, "amount_minor": amt, "currency": "USD"}).json()

    r = check("US-210.OPS.6700", 20_000)  # the branch's monthly limit governs, not the head office's annual one
    assert not r["ok"] and r["period"] == month and "100.00 USD left" in r["reason"]
    r = check("US-200.BRAND26.6600", 60_000)  # the project's budget for its duration
    assert not r["ok"] and "to" in r["period"] and "500.00 USD left" in r["reason"]
    every = client.get("/v1/budget-allocation/status?every=true").json()["allocations"]
    kinds = {a["alloc_id"]: a["period_type"] for a in every}
    assert "duration" in kinds.values() and "monthly" in kinds.values() and "annual" in kinds.values()
