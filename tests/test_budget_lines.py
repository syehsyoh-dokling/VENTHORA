# ruff: noqa: E501
"""Payment -> Budget allocation -> Budget lines: the amount per line, the share the agent pays on its own, the alert,
an approver at the alert, and a signed bypass, per month. The rule for a payment outside every line is a switch of
the standing mandate (DoA -> Treasury limits and controls); every budget the engine loads takes it."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from venthora.masterdata import MasterStore
from venthora.proofcore.budget import load_budget

FM = "role=finance_manager&by=Dana%20Finch"
FCO = "role=super_admin&by=Rosa%20Lindqvist"
LINES = [{"line": "suppliers", "amount_minor": 800_000, "auto_share_bp": 7_500, "alert_share_bp": 7_000, "require_approval_on_alert": True},
         {"line": "saas", "amount_minor": 150_000, "auto_share_bp": 10_000, "alert_share_bp": 9_000, "bypass": True, "bypass_approved_by": "Omar Reyes"}]


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_the_file_budget_shows_until_the_lines_are_set_here(client):
    d = client.get("/v1/budget-lines?period=2026-11").json()
    assert d["source"] == "data/budgets.json" and {ln["line"] for ln in d["lines"]} >= {"payroll", "suppliers", "contractors"}
    assert "2026-11" in d["periods"] and "contractors" in d["line_names"] and d["unbudgeted_requires_approval"] is True
    r = client.put(f"/v1/budget-lines/2026-11?{FM}", json={"lines": LINES})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"].startswith("dashboard:") and [ln["line"] for ln in d["lines"]] == ["saas", "suppliers"]
    b = load_budget("2026-11")
    by = {ln.line: ln for ln in b.lines}
    assert by["suppliers"].auto_share_bp == 7_500 and by["suppliers"].require_approval_on_alert and by["saas"].bypass_signed
    # saving again with one line less removes it
    assert [ln["line"] for ln in client.put(f"/v1/budget-lines/2026-11?{FM}", json={"lines": LINES[:1]}).json()["lines"]] == ["suppliers"]


def test_a_bypass_needs_its_signatory_and_lines_are_unique(client):
    r = client.put(f"/v1/budget-lines/2026-11?{FM}", json={"lines": [LINES[1] | {"bypass_approved_by": ""}]})
    assert r.status_code == 422 and "signed" in r.json()["detail"]
    assert client.put(f"/v1/budget-lines/2026-11?{FM}", json={"lines": [LINES[0], LINES[0]]}).status_code == 422
    assert client.put(f"/v1/budget-lines/2026-11?{FM}", json={"lines": [LINES[0] | {"alert_share_bp": 12_000}]}).status_code == 422
    assert client.put(f"/v1/budget-lines/2026-13?{FM}", json={"lines": LINES}).status_code == 422
    assert client.put("/v1/budget-lines/2026-11?role=auditor&by=Ines", json={"lines": LINES}).status_code == 403


def test_synced_budget_lines_are_read_only(client):
    MasterStore().upsert("budget_line", [{"period": "2026-12", "line": "suppliers", "amount_minor": 500_000}], source="sync:legacy-erp-demo")
    assert client.get("/v1/budget-lines?period=2026-12").json()["synced_from"]["connector_id"] == "legacy-erp-demo"
    r = client.put(f"/v1/budget-lines/2026-12?{FM}", json={"lines": LINES})
    assert r.status_code == 409 and "source system" in r.json()["detail"]


def test_the_unbudgeted_rule_is_a_standing_mandate_switch(client):
    row = next(x for x in client.get("/v1/mandate?role=super_admin").json()["limits"] if x["key"] == "unbudgeted_requires_approval")
    assert row["enabled"] is True and row["switch_only"] and row["critical"]
    body = {"enabled": {"unbudgeted_requires_approval": False}}
    r = client.put(f"/v1/mandate?{FCO}", json=body)
    assert r.status_code == 409 and r.json()["detail"]["warnings"][0]["key"] == "rule:unbudgeted_requires_approval"
    assert client.put(f"/v1/mandate?{FCO}", json=body | {"acknowledged": ["rule:unbudgeted_requires_approval"]}).status_code == 200
    assert load_budget("2026-11").unbudgeted_requires_approval is False
    assert client.get("/v1/budget-lines?period=2026-11").json()["unbudgeted_requires_approval"] is False
    assert client.get("/api/live/setup").json()["unbudgeted_requires_approval"] is False
    assert next(x for x in client.get("/v1/mandate?role=super_admin").json()["limits"] if x["key"] == "unbudgeted_requires_approval")["enabled"] is False
    # switching it back on needs no confirmation
    assert client.put(f"/v1/mandate?{FCO}", json={"enabled": {"unbudgeted_requires_approval": True}}).status_code == 200
    assert load_budget("2026-11").unbudgeted_requires_approval is True


def test_the_operations_run_form_starts_from_this_months_lines(client):
    month = dt.date.today().strftime("%Y-%m")
    assert client.put(f"/v1/budget-lines/{month}?{FM}", json={"lines": LINES}).status_code == 200
    form = client.get("/api/live/setup").json()
    assert {ln["line"]: ln["auto_share_bp"] for ln in form["budget"]} == {"saas": 10_000, "suppliers": 7_500}
    assert next(ln for ln in form["budget"] if ln["line"] == "saas")["bypass_approved_by"] == "Omar Reyes"
