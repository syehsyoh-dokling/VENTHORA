# ruff: noqa: E501
"""Finance standards cover every kind of organisation: generic fields have working defaults, sector fields (NGO,
government, listed, cooperative, Sharia) start at None, and only known options are saved."""
import pytest
from fastapi.testclient import TestClient

from venthora import finance_standards as fs


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_sector_fields_start_at_none_and_generic_ones_have_a_default():
    for f in fs.FIELDS:
        if "sectors" in f:  # sector-specific: None first, None by default
            assert f["kind"] in ("select", "multi", "percent"), f["key"]
            if f["kind"] == "select":
                assert f["options"][0] == fs.NONE and f["default"] == "none", f["key"]
            else:
                assert not f["default"], f["key"]
        elif f.get("store") != "top":
            assert "default" in f, f["key"]
    assert {"isak_335", "asc_958", "charities_sorp", "inpag"} <= {o[0] for o in fs.BY_KEY["npo_standard"]["options"]}
    assert {"ifrs", "ifrs_sme", "us_gaap", "sak_id", "sak_ep", "sak_emkm", "ipsas_accrual", "sap_id", "gasb"} <= {o[0] for o in fs.BY_KEY["reporting_framework"]["options"]}
    assert {"ngo", "central_government", "listed_company", "sme", "cooperative", "state_owned"} <= set(fs.ENTITY_TYPES)


def test_the_catalog_and_defaults_are_served(client):
    cat = client.get("/v1/org/standards/catalog").json()
    keys = {f["key"] for f in cat["fields"]}
    assert {"company_name", "require_po", "entity_type", "donor_rules", "budget_execution", "icfr_regime", "sharia_standard"} <= keys
    assert {f["key"] for f in cat["fields"] if f["enforced"]} >= {"require_po", "expense_categories", "base_currency"}
    s = client.get("/v1/org/standards").json()
    assert s["policies"]["npo_standard"] == "none" and s["policies"]["reporting_framework"] == "ifrs" and s["policies"]["donor_rules"] == []


def test_an_ngo_saves_its_rules_and_a_wrong_option_is_refused(client):
    s = client.get("/v1/org/standards").json()
    ngo = s | {"policies": s["policies"] | {"entity_type": "ngo", "npo_standard": "isak_335", "donor_rules": ["un_hact", "us_2cfr200"],
                                             "indirect_cost_recovery": "de_minimis", "indirect_cost_rate_bp": 1500}}
    out = client.put("/v1/org/standards", json=ngo).json()
    assert out["policies"]["npo_standard"] == "isak_335" and out["policies"]["donor_rules"] == ["us_2cfr200", "un_hact"]
    assert out["require_po"] == s["require_po"] and client.get("/v1/org/standards").json()["policies"]["indirect_cost_rate_bp"] == 1500
    bad = client.put("/v1/org/standards", json=s | {"policies": {"npo_standard": "made_up"}})
    assert bad.status_code == 422
    assert client.put("/v1/org/standards", json=s | {"policies": {"no_such_rule": 1}}).status_code == 422
