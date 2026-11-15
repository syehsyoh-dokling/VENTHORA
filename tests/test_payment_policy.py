# ruff: noqa: E501
"""System Setting -> Standard Payment Priority Policy: the priority (FIFO in real time, or high priority first over a
planned horizon), the rules for a short balance, deemed approval and currency timing; a person's hold; notices."""
import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient

ADMIN = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _save(c, **policy):
    items = [{"key": r["key"], "level": r["level"]} for r in c.get("/v1/priority-sop").json()["items"]]
    return c.put(f"/v1/priority-sop?{ADMIN}", json={"items": items, "late_cost": True, "policy": policy})


def test_the_defaults_and_the_choices_are_offered(client):
    d = client.get("/v1/priority-sop").json()
    assert d["policy_saved"] is False and d["policy"]["mode"] == "priority" and d["policy"]["horizon_hours"] == 72
    assert d["horizons"] == [24, 48, 72, 96, 120] and set(d["modes"]) == {"fifo", "priority"}
    assert d["timing"] == {"deadline_hour_utc": 17, "deemed_before_h": 3, "fx_before_h": 2}


def test_saving_the_policy_sets_the_horizon_and_the_real_time_switch(client):
    from venthora import mandate

    r = _save(client, mode="priority", horizon_hours=48, deemed_approval=True, cascade=False, fx_at_plan=True)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["policy_saved"] and d["horizon_hours"] == 48 and d["policy"]["cascade"] is False and d["policy"]["deemed_approval"] is True
    assert mandate.pay_on_arrival(client.app.state.org) is False  # high priority first: a planned horizon, not real time
    assert client.get("/v1/agent-plan").json()["horizon_hours"] == 48
    assert _save(client, mode="fifo").json()["policy"]["mode"] == "fifo"
    assert mandate.pay_on_arrival(client.app.state.org) is True  # FIFO is real time


def test_a_bad_choice_is_refused_and_only_the_authority_saves(client):
    assert _save(client, horizon_hours=36).status_code == 422
    assert _save(client, mode="lifo").status_code == 422
    items = [{"key": r["key"], "level": r["level"]} for r in client.get("/v1/priority-sop").json()["items"]]
    assert client.put("/v1/priority-sop?role=finance_manager&by=Dana", json={"items": items, "policy": {"mode": "fifo"}}).status_code == 403


def test_the_horizon_reaches_the_plan():
    from conftest import scenario
    from venthora import priority_sop
    from venthora.domain.models import TreasuryState

    class Org:
        def __init__(self):
            self.rec = {}

        def get(self, kind, rid):
            return self.rec.get((kind, rid))

        def _put(self, kind, rec, by):
            self.rec[(kind, rec["_id"])] = rec

        def list(self, kind):
            return [v for (k, _), v in self.rec.items() if k == kind]

    org = Org()
    priority_sop.save(org, [{"key": k, "level": v[3]} for k, v in priority_sop.ITEMS.items()], "t", True, {"mode": "priority", "horizon_hours": 120})
    state = priority_sop.apply(TreasuryState.model_validate(scenario("kit01_initial")), org, {})
    assert state.policy.planning_horizon_hours == 120
    # the planned rate is kept once: later plans buy at it or better
    assert priority_sop.planned_rate(org, "X-1", "EUR", "1.08") == "1.08"
    assert priority_sop.planned_rate(org, "X-1", "EUR", "1.20") == "1.08"
    # a notice is written once while it is open
    priority_sop.notify(org, "k", "short", "body")
    priority_sop.notify(org, "k", "short again", "body")
    assert [n["subject"] for n in priority_sop.notices(org)] == ["short"]


def test_the_deadline_is_17_utc_on_the_due_date():
    from venthora import priority_sop

    now = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)
    assert priority_sop.hours_left({"due_date": "2026-11-05"}, now) == 5
    assert priority_sop.hours_left({"due_date": "2026-11-06"}, now) == 29
    assert priority_sop.hours_left({"due_in_hours": 10}, now) == 10


def test_a_hold_is_an_intervention_and_can_be_released(client):
    assert client.post("/v1/agent-plan/hold?role=auditor&by=Ines", json={"obligation_id": "REQ-1"}).status_code == 403
    r = client.post("/v1/agent-plan/hold?role=finance_manager&by=Dana%20Finch", json={"obligation_id": "REQ-1", "note": "checking the invoice"})
    assert r.status_code == 200
    assert [h["_id"] for h in client.get("/v1/priority-sop").json()["holds"]] == ["REQ-1"]
    assert client.delete("/v1/agent-plan/hold/REQ-1?role=finance_manager&by=Dana%20Finch").status_code == 200
    assert client.get("/v1/priority-sop").json()["holds"] == []


def test_notices_are_listed_and_closed(client):
    from venthora import priority_sop

    priority_sop.notify(client.app.state.org, "short-test", "The balance does not cover every payment", "REQ-1 waits", ["REQ-1"])
    assert [n["_id"] for n in client.get("/v1/notifications").json()] == ["short-test"]
    assert client.post(f"/v1/notifications/short-test/close?{ADMIN}").status_code == 200
    assert client.get("/v1/notifications").json() == []


def test_the_policy_run_does_nothing_without_the_policy_or_the_bank(client):
    from venthora.api import _policy_run

    assert "skipped" in asyncio.run(_policy_run())  # not saved yet
    _save(client, mode="priority")
    assert "skipped" in asyncio.run(_policy_run())  # no bank in the tests
