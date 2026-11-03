# ruff: noqa: E501
"""The standing mandate: seven categories, each with its policy; document checks on every request; every limit in
one list; saved once, every Operations run starts from it; an API key can never change it."""
import pytest
from fastapi.testclient import TestClient

from venthora import live, mandate


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_every_rule_has_a_category_and_every_category_a_policy():
    assert {m["key"] for m in live.MANDATES} == set(mandate.RULE_CATEGORY)
    assert {c["key"] for c in mandate.CATEGORIES} >= set(mandate.RULE_CATEGORY.values())
    assert all(c["policy"] for c in mandate.CATEGORIES)


def test_the_page_groups_checks_rules_and_limits(client):
    d = client.get("/v1/mandate").json()
    cats = {c["key"]: c for c in d["categories"]}
    assert list(cats) == ["documents", "payees", "authority", "liquidity", "escalation", "approval", "control"]
    checks = {c["key"]: c for c in cats["documents"]["checks"]}
    assert {"sender", "po_open", "po_room", "totals", "duplicates"} - {"duplicates"} <= set(checks)
    assert "po_exceeded" in checks["po_room"]["flags"] and checks["po_room"]["on"] is True
    assert {r["key"] for r in cats["documents"]["rules"]} == {"three_way", "tolerance_floor", "duplicates", "evidence"}
    keys = {x["key"] for x in d["limits"]}
    assert {"allocation_minor", "reserve_floor_minor", "pay_single", "outflow_daily", "cooling", "three_way", "incident_wait"} <= keys
    assert all(x["unit"] for x in d["limits"])  # only rules with a number are in the limits list


def test_saving_the_standing_mandate_changes_what_a_run_starts_from(client):
    r = client.put("/v1/mandate", json={"values": {"pay_single": 250_000, "cooling": 96}, "enabled": {"convert_fx": False}, "book": {"reserve_floor_minor": 2_000_000}})
    assert r.status_code == 200, r.text
    setup = client.get("/api/live/setup").json()
    rows = {m["key"]: m for m in setup["mandates"]}
    assert rows["pay_single"]["value"] == 250_000 and rows["cooling"]["value"] == 96 and rows["convert_fx"]["enabled"] is False
    assert setup["reserve_floor_minor"] == 2_000_000
    state, granted = live.apply_config(*_state_and_cfg(setup))
    assert state.policy.max_single_payment_minor == 250_000 and state.policy.autonomous_conversion_limit_minor == 0
    assert state.policy.payee_cooling_hours == 96


def _state_and_cfg(setup):
    import datetime as dt
    import json

    from venthora.config import ROOT
    from venthora.domain.models import TreasuryState

    raw = json.loads((ROOT / "data" / "scenarios" / "demo_live.json").read_text(encoding="utf-8"))
    return TreasuryState.model_validate(raw["state"]), setup, dt.date.today()


def test_bad_values_and_locked_switches_are_refused(client):
    assert client.put("/v1/mandate", json={"enabled": {"duplicates": False}}).status_code == 422
    assert client.put("/v1/mandate", json={"values": {"three_way": 20_000}}).status_code == 422
    assert client.put("/v1/mandate", json={"book": {"full_confidence_bp": 4000, "zero_limit_below_bp": 5000}}).status_code == 422
    assert client.put("/v1/mandate?role=auditor", json={}).status_code == 403


def test_an_api_key_can_never_change_the_mandate(client):
    from venthora.apiaccess import NEVER_BY_KEY

    assert "/v1/mandate" in NEVER_BY_KEY
