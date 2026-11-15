# ruff: noqa: E501
"""The standing mandate: seven categories, each with its policy; document checks on every request, each on hold, warn
or off; every limit in one list; one role holds the authority to change it; a change that weakens a critical control
needs an acknowledged warning; every Autopilot Console run starts from it; an API key can never change it."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from venthora import live, mandate
from venthora.testing.review_cases import cases, postmark

FCO = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _put(client, body, who=FCO):
    """Save, acknowledging whatever warnings the server raises (as the person does in the dialog)."""
    r = client.put(f"/v1/mandate?{who}", json=body)
    if r.status_code == 409:
        r = client.put(f"/v1/mandate?{who}", json=body | {"acknowledged": [w["key"] for w in r.json()["detail"]["warnings"]]})
    return r


def test_every_rule_has_a_category_and_every_category_a_policy():
    assert {m["key"] for m in live.MANDATES} == set(mandate.RULE_CATEGORY)
    assert {c["key"] for c in mandate.CATEGORIES} >= set(mandate.RULE_CATEGORY.values())
    assert all(c["policy"] for c in mandate.CATEGORIES)
    assert all(c["risk"] for c in mandate.CHECKS)
    assert set(mandate.SWITCHABLE_CRITICAL) == live.SWITCHABLE_LOCKED


def test_the_page_groups_checks_rules_and_limits(client):
    d = client.get("/v1/mandate?role=super_admin").json()
    cats = {c["key"]: c for c in d["categories"]}
    assert list(cats) == ["documents", "payees", "authority", "liquidity", "escalation", "approval", "control"]
    checks = {c["key"]: c for c in cats["documents"]["checks"]}
    assert {"sender", "po_open", "po_room", "totals"} <= set(checks)
    assert "po_exceeded" in checks["po_room"]["flags"] and checks["po_room"]["mode"] == "hold"
    assert {r["key"] for r in cats["documents"]["rules"]} == {"three_way", "tolerance_floor", "duplicates", "evidence"}
    keys = {x["key"] for x in d["limits"]}
    assert {"allocation_minor", "reserve_floor_minor", "pay_single", "outflow_daily", "cooling", "three_way", "incident_wait", "duplicates", "recon"} <= keys
    assert all(x["unit"] or x.get("switch_only") for x in d["limits"])
    assert d["authority"]["role"] == "super_admin" and d["authority"]["you_may_change"] is True
    assert client.get("/v1/mandate?role=finance_manager").json()["authority"]["you_may_change"] is False


def test_saving_the_standing_mandate_changes_what_a_run_starts_from(client):
    r = _put(client, {"values": {"pay_single": 250_000, "cooling": 96}, "enabled": {"convert_fx": False}, "book": {"reserve_floor_minor": 2_000_000}})
    assert r.status_code == 200, r.text
    setup = client.get("/api/live/setup").json()
    rows = {m["key"]: m for m in setup["mandates"]}
    assert rows["pay_single"]["value"] == 250_000 and rows["cooling"]["value"] == 96 and rows["convert_fx"]["enabled"] is False
    assert setup["reserve_floor_minor"] == 2_000_000
    state, granted = live.apply_config(*_state_and_cfg(setup))
    assert state.policy.max_single_payment_minor == 250_000 and state.policy.autonomous_conversion_limit_minor == 0
    assert state.policy.payee_cooling_hours == 96


def _state_and_cfg(setup):
    import json

    from venthora.config import seed_file
    from venthora.domain.models import TreasuryState

    raw = json.loads(seed_file("scenarios/demo_live.json").read_text(encoding="utf-8"))
    return TreasuryState.model_validate(raw["state"]), setup, dt.date.today()


def test_only_the_mandate_authority_changes_it_and_may_hand_it_on(client):
    r = client.put("/v1/mandate?role=finance_manager&by=Dana", json={"values": {"pay_single": 100}})
    assert r.status_code == 403 and "FCO (Super admin)" in r.json()["detail"]
    assert _put(client, {"authority_role": "cfo"}).status_code == 200
    assert client.put(f"/v1/mandate?{FCO}", json={}).status_code == 403  # handed on: the FCO no longer holds it
    assert _put(client, {"authority_role": "super_admin"}, who="role=cfo&by=Omar").status_code == 200


def test_a_critical_change_needs_an_acknowledged_warning(client):
    r = client.put(f"/v1/mandate?{FCO}", json={"checks": {"bank_change": "warn", "pressure": "off"}, "values": {"pay_single": 5_000_000}, "enabled": {"recon": False}})
    assert r.status_code == 409
    keys = {w["key"] for w in r.json()["detail"]["warnings"]}
    assert keys == {"check:bank_change", "value:pay_single", "rule:recon"}  # pressure is not critical
    assert all(w["risk"] for w in r.json()["detail"]["warnings"])
    r = client.put(f"/v1/mandate?{FCO}", json={"checks": {"bank_change": "warn"}, "values": {"pay_single": 5_000_000}, "enabled": {"recon": False},
                                                "acknowledged": sorted(keys)})
    assert r.status_code == 200 and r.json()["checks"]["bank_change"] == "warn"
    # tightening is never questioned
    assert client.put(f"/v1/mandate?{FCO}", json={"checks": {"bank_change": "hold"}, "values": {"pay_single": 100_000}}).status_code == 200


def test_bad_values_are_refused_and_the_evidence_rule_is_built_in(client):
    assert client.put(f"/v1/mandate?{FCO}", json={"values": {"three_way": 20_000}}).status_code == 422
    assert client.put(f"/v1/mandate?{FCO}", json={"book": {"full_confidence_bp": 4000, "zero_limit_below_bp": 5000}}).status_code == 422
    assert client.put(f"/v1/mandate?{FCO}", json={"checks": {"totals": "maybe"}}).status_code == 422
    r = client.put(f"/v1/mandate?{FCO}", json={"enabled": {"evidence": False}})
    assert r.status_code == 422 and "Built into" in r.json()["detail"]


def test_the_intake_follows_each_checks_mode(client, monkeypatch):
    monkeypatch.setenv("VENTHORA_INBOUND_EMAIL_SECRET", "s3cret-for-tests")
    def send(case_id, n):  # a fresh stamp each time: a new invoice number, not a duplicate
        stamp = f"90000{n}"
        c = next(x for x in cases(stamp, dt.date(2026, 10, 24)) if x["id"] == case_id)
        r = client.post("/webhooks/email", json=postmark(c, stamp), auth=("postmark", "s3cret-for-tests"))
        return next(x for x in client.get("/v1/intake").json() if x["intake_id"] == r.json()["intake_id"])

    assert send("urgent", 1)["status"] == "NEEDS_REVIEW"
    assert _put(client, {"checks": {"pressure": "warn", "po_room": "warn"}}).status_code == 200
    got = send("urgent", 2)
    assert got["status"] == "VERIFIED" and "urgency_pressure" in got["flags"]
    got = send("po-exceeded", 3)
    assert got["status"] == "VERIFIED" and "po_exceeded" in got["flags"]
    assert any("Flag only in the mandate" in x for x in got["reasons"])
    assert _put(client, {"checks": {"po_room": "off"}}).status_code == 200
    got = send("po-exceeded", 4)
    assert "po_exceeded" not in got["flags"]


def test_an_api_key_can_never_change_the_mandate(client):
    from venthora.apiaccess import NEVER_BY_KEY

    assert "/v1/mandate" in NEVER_BY_KEY
