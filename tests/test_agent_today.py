# ruff: noqa: E501
"""Agent plan -> Today's plan in one paragraph and two tables, a person's reject / pending, the payment detail with a
notice to the vendor, and the transaction log of every payment, step by step with date and time."""
import pytest
from fastapi.testclient import TestClient

AMIRA = "role=country_finance_lead&by=Amira%20Shah"
DANA = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _orders(c):
    for inv, amt in (("LDS-8101", 120_000), ("LDS-8102", 90_000)):
        r = c.post(f"/v1/intake/payment-orders?{AMIRA}", json={"vendor_id": "ben-lumen-design", "invoice_no": inv, "amount_minor": amt, "currency": "USD",
                                                               "due_date": "2026-12-01", "po_number": "PO-2026-0101", "description": "phase"})
        assert r.status_code == 200, r.text


def test_today_says_what_is_due_ready_and_waiting(client):
    _orders(client)
    t = client.get("/v1/agent-plan").json()["today"]
    assert t["horizon_hours"] == 72 and t["base_currency"] == "USD"
    ids = {r["obligation_id"] for r in t["ready"] + t["others"]}
    assert {"REQ-LDS-8101", "REQ-LDS-8102"} <= ids
    assert all(r["document_check"] == "completed" and r["smoke_test"] == "succeeded" for r in t["ready"])
    assert all(r["why"] for r in t["others"])
    assert t["ready_count"] + t["manual_count"] == t["count"] and t["available_minor"] >= 0 and isinstance(t["enough"], bool)
    assert t["next_pay_at"] is None  # no bank in the tests: the ready part is paid by hand
    assert t["count"] == sum(c["count"] for c in t["categories"])  # what is due inside the 72 hours, by category (these orders are due later)


def test_pending_to_a_new_date_and_reject_with_a_reason(client):
    _orders(client)
    assert client.post(f"/v1/agent-plan/pending?{DANA}", json={"obligation_id": "REQ-LDS-8101", "until": "2026-01-01"}).status_code == 422
    assert client.post(f"/v1/agent-plan/pending?{DANA}", json={"obligation_id": "REQ-LDS-8101", "until": "2099-12-30", "note": "wait for the delivery"}).status_code == 200
    t = client.get("/v1/agent-plan").json()["today"]
    assert "REQ-LDS-8101" not in {r["obligation_id"] for r in t["ready"] + t["others"]}  # out of today's plan until its date
    assert client.post("/v1/agent-plan/reject?role=auditor&by=Ines", json={"obligation_id": "REQ-LDS-8102", "reason": "wrong amount"}).status_code == 403
    assert client.post(f"/v1/agent-plan/reject?{DANA}", json={"obligation_id": "REQ-LDS-8102", "reason": "wrong amount"}).status_code == 200
    req = next(r for r in client.app.state.intake.store.list(50) if r.invoice_no == "LDS-8102")
    assert req.status == "REJECTED"


def test_the_log_tells_the_whole_story_and_the_vendor_can_be_notified(client):
    _orders(client)
    client.get("/v1/agent-plan")  # the plan's decision is recorded
    client.post(f"/v1/agent-plan/pending?{DANA}", json={"obligation_id": "REQ-LDS-8101", "until": "2099-12-30", "note": "wait"})
    log = client.get("/v1/transaction-log?obligation_id=REQ-LDS-8101").json()
    kinds = [e["kind"] for e in log["events"]]
    assert log["intake_id"] and kinds[:3] == ["received", "checked", "status"] and "decision" in kinds and "plan_pending" in kinds
    from venthora.agent_today import _when

    moments = [_when(e["at"]) for e in log["events"]]
    assert all(e["at"] for e in log["events"]) and moments == sorted(moments)
    index = client.get("/v1/transaction-log").json()
    assert any(x["obligation_id"] == "REQ-LDS-8101" and x["steps"] >= 5 for x in index)
    client.post(f"/v1/agent-plan/pending?{DANA}", json={"obligation_id": "REQ-LDS-8102", "until": "2099-12-30"})  # still in the plan? detail works either way
    d = client.get("/v1/agent-plan/payment/REQ-LDS-8101").json()
    assert d["events"] and "contacts" in d["vendor"]


def test_the_balance_is_set_against_everything_due_inside_the_policy_horizon(client):
    _orders(client)
    t = client.get("/v1/agent-plan").json()["today"]
    c = t["coverage"]
    assert c["hours"] == t["horizon_hours"] and len(c["days"]) == -(-c["hours"] // 24)  # the policy's horizon, never more
    assert c["spendable_minor"] == max(0, c["available_minor"] - c["reserve_floor_minor"])
    assert c["total_minor"] == sum(d["due_minor"] for d in c["days"]) == (c["days"][-1]["cumulative_minor"] if c["days"] else 0)
    assert c["enough"] == (c["total_minor"] <= c["spendable_minor"]) and (c["short_from"] is None) == c["enough"]
    assert sum(c["by_group"].values()) == c["total_minor"]
    x = t["executive"]  # the summary's figures: groups, the largest categories (three and the rest), the reasons counted
    assert sum(x[g]["count"] for g in ("ready", "approval", "blocked")) == t["count"] and len(x["categories"]) <= 4


def test_a_longer_horizon_in_the_policy_widens_the_view():
    from venthora.agent_today import coverage, reason_kind

    assert reason_kind("reject: evidence intake:1234 is pending") == "the documents are not verified yet"
    assert reason_kind("instalment 'partial' would take the invoice past its contract total") == "the amount goes above its purchase order or contract"
    assert reason_kind("something new") == "another check"

    class S:
        base_currency, receivables = "USD", []
    rows = [{"base_minor": 100, "due_in_hours": h, "ready": True} for h in (10, 50, 100)]
    assert coverage(S(), rows, 1_000, 0, 72)["count"] == 2 and coverage(S(), rows, 1_000, 0, 120)["count"] == 3
    edge = coverage(S(), [{"base_minor": 100, "due_in_hours": 0, "ready": True}, {"base_minor": 50, "due_in_hours": 72, "ready": True}], 1_000, 0, 72)
    assert edge["count"] == 2 and sum(d["due_minor"] for d in edge["days"]) == edge["total_minor"] == 150  # due exactly at the end counts
