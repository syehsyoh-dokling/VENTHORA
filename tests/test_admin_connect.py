# ruff: noqa: E501
"""User management: the admin team (super admin = the FCO at HQ; HQ, country, branch and project-finance admins each
within their own reach), connecting an Airwallex account from the page (tested before it is kept, never shown back),
opening Global Accounts; Settings: a key vault per AI provider."""
import pytest
from fastapi.testclient import TestClient

from venthora.gateways.airwallex import Airwallex, AirwallexError


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _user(client, who, role_q, **body):
    return client.post(f"/v1/org/users?role={role_q}&by={who}", json={"approval_limit_minor": 0} | body)


def test_the_admin_team_and_its_reach(client):
    # the super admin (FCO) is HQ's; an admin level must sit on a unit of that level
    assert _user(client, "Rosa%20Lindqvist", "super_admin", name="Second FCO", email="fco2@acme.example", role="super_admin", unit_id="hq").status_code == 409
    r = _user(client, "Rosa%20Lindqvist", "super_admin", name="Kai Brandt", email="kai.brandt@acme.example", role="country_admin", unit_id="hq")
    assert r.status_code == 409 and "country office" in r.json()["detail"]
    assert _user(client, "Rosa%20Lindqvist", "super_admin", name="Kai Brandt", email="kai.brandt@acme.example", role="country_admin", unit_id="co-uk").status_code == 200
    # the UK country admin manages the UK office and its branch, nothing else, and gives only roles below its own
    ok = _user(client, "Kai%20Brandt", "country_admin", name="Nia Evans", email="nia.evans@acme.example", role="branch_admin", unit_id="br-uk-manchester")
    assert ok.status_code == 200
    r = _user(client, "Kai%20Brandt", "country_admin", name="Al Moss", email="al.moss@acme.example", role="requester", unit_id="co-us")
    assert r.status_code == 403 and "outside your reach" in r.json()["detail"]
    r = _user(client, "Kai%20Brandt", "country_admin", name="Al Moss", email="al.moss@acme.example", role="cfo", unit_id="co-uk")
    assert r.status_code == 403 and "may give" in r.json()["detail"]
    r = client.post("/v1/org/units?role=country_admin&by=Kai%20Brandt", json={"name": "Leeds Branch", "kind": "BRANCH_OFFICE", "parent_id": "co-uk",
                                                                             "country": "GB", "currency": "GBP", "cost_centre": "UK-320"})
    assert r.status_code == 200
    assert client.put("/v1/org/standards?role=country_admin&by=Kai%20Brandt", json=client.get("/v1/org/standards").json()).status_code == 403
    assert client.post("/v1/org/users?role=country_admin&by=Nobody", json={}).status_code == 403  # the role must be really theirs
    # the branch admin reaches only its branch
    r = _user(client, "Nia%20Evans", "branch_admin", name="Ola Reed", email="ola.reed@acme.example", role="requester", unit_id="br-uk-manchester")
    assert r.status_code == 200
    assert _user(client, "Nia%20Evans", "branch_admin", name="Ola Reed2", email="ola2@acme.example", role="requester", unit_id="co-uk").status_code == 403


def test_connect_an_airwallex_account(client, monkeypatch):
    calls = []

    async def balances(self):
        cid, key = self._creds()
        calls.append(cid)
        if key != "good-key-1234567":
            raise AirwallexError("authentication failed", status=401)
        return {"USD": {"available": 500000, "pending": 0, "reserved": 0, "total": 500000}}

    async def gas(self):
        return [{"id": "ga1", "nick_name": "Receipts", "status": "ACTIVE", "country_code": "US", "account_number": "8888001",
                 "account_routing": [{"type": "aba", "value": "026073150"}], "supported_features": [{"currency": "USD", "transfer_method": "LOCAL"}]}]

    async def create(self, *, request_id, country_code, currency, nick_name="x"):
        return {"id": "ga2", "nick_name": nick_name, "status": "ACTIVE", "country_code": country_code,
                "supported_features": [{"currency": currency, "transfer_method": "LOCAL"}]}
    monkeypatch.setattr(Airwallex, "balances_detail", balances)
    monkeypatch.setattr(Airwallex, "list_global_accounts", gas)
    monkeypatch.setattr(Airwallex, "create_global_account", create)
    assert client.get("/v1/airwallex/connection").json()["connected"] is False
    r = client.put("/v1/airwallex/connection", json={"client_id": "client-abcd1234", "api_key": "wrong-key-1234567"})
    assert r.status_code == 422 and "refused" in r.json()["detail"]
    assert client.put("/v1/airwallex/connection?role=treasury_analyst", json={"client_id": "client-abcd1234", "api_key": "x" * 10}).status_code == 403
    assert client.put("/v1/airwallex/connection", json={"client_id": "client-abcd1234", "api_key": "k" * 10, "environment": "production"}).status_code == 422
    r = client.put("/v1/airwallex/connection", json={"client_id": "client-abcd1234", "api_key": "good-key-1234567", "account_name": "Acme Group"})
    c = r.json()
    assert r.status_code == 200 and c["connected"] and c["source"] == "settings" and c["client_id_hint"] == "…1234" and c["currencies"] == ["USD"]
    assert "good-key" not in r.text and "client-abcd1234" not in r.text  # never shown back
    assert calls[-1] == "client-abcd1234"  # the running client now uses the connected account
    ga = client.get("/v1/airwallex/global-accounts").json()
    assert ga["items"][0]["currencies"] == ["USD"] and ga["items"][0]["routing"] == [{"type": "aba", "value": "026073150"}]
    assert any(o["currency"] == "GBP" for o in ga["options"])
    opened = client.post("/v1/airwallex/global-accounts", json={"currency": "gbp", "country_code": "gb", "nick_name": "UK receipts"}).json()
    assert opened["currencies"] == ["GBP"] and opened["country_code"] == "GB"
    assert client.delete("/v1/airwallex/connection").json()["source"] in ("env", "none")


def test_a_key_vault_per_ai_provider(client):
    assert client.put("/v1/settings/ai/keys/anthropic", json={"api_key": "sk-ant-aaaaaaaa1111", "model": "claude-sonnet-5-5"}).status_code == 200
    assert client.put("/v1/settings/ai/keys/openai", json={"api_key": "sk-oa-bbbbbbbb2222", "model": "gpt-x"}).status_code == 200
    r = client.put("/v1/settings/ai/keys/custom", json={"api_key": "zzzzzzzz3333"})
    assert r.status_code == 422 and "base URL" in r.json()["detail"]
    s = client.get("/v1/settings/ai").json()
    assert s["keys"]["anthropic"] == {"key_set": True, "key_hint": "…1111", "model": "claude-sonnet-5-5", "base_url": ""}
    assert "aaaaaaaa1111" not in str(s) and s["mode"] == "rules"  # saving a key does not switch the model in use
    # switching to a provider takes its key from the vault: no key in the request
    r = client.put("/v1/settings/ai", json={"mode": "provider", "provider": {"name": "anthropic", "model": "claude-sonnet-5-5"}, "consent_external": True})
    assert r.status_code == 200 and r.json()["provider"]["key_hint"] == "…1111" and r.json()["active"] == "Anthropic · claude-sonnet-5-5"
    r = client.put("/v1/settings/ai", json={"mode": "provider", "provider": {"name": "openai", "model": "gpt-x"}, "consent_external": True})
    assert r.json()["provider"]["key_hint"] == "…2222" and r.json()["keys"]["anthropic"]["key_set"]  # the other key is kept
    assert client.delete("/v1/settings/ai/keys/openai").status_code == 409  # in use
    assert "anthropic" not in client.delete("/v1/settings/ai/keys/anthropic").json()["keys"]
    assert {"deepseek", "nvidia", "dashscope", "custom"} <= set(s["providers"])


def test_open_an_airwallex_account_from_the_dashboard(client, monkeypatch):
    sent = []

    async def create(self, body):
        sent.append(body)
        if body["account_details"]["legal_entity_type"] == "INDIVIDUAL":
            raise AirwallexError("This account cannot onboard individual accounts", status=400, code="invalid_argument")
        return {"id": "acct_1", "status": "CREATED", "account_details": body["account_details"], "primary_contact": body["primary_contact"]}

    async def listing(self):
        return [{"id": "acct_1", "status": "CREATED", "account_details": sent[0]["account_details"], "primary_contact": sent[0]["primary_contact"]}] if sent else []

    async def submit(self, account_id):
        return {"id": account_id, "status": "SUBMITTED", "account_details": sent[0]["account_details"]}
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "c")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "k")
    monkeypatch.setattr(Airwallex, "create_account", create)
    monkeypatch.setattr(Airwallex, "list_accounts", listing)
    monkeypatch.setattr(Airwallex, "submit_account", submit)
    base = {"legal_entity_type": "BUSINESS", "business_name": "Venthora Demo Trading Pte Ltd", "country_code": "sg", "email": "ops@venthora-demo.example"}
    r = client.post("/v1/airwallex/accounts", json=base)
    assert r.status_code == 422 and "terms" in r.json()["detail"] and not sent  # no agreement, nothing sent
    ok = base | {"agreed_to_terms_and_conditions": True, "agreed_to_data_usage": True}
    a = client.post("/v1/airwallex/accounts", json=ok).json()
    assert (a["id"], a["status"], a["name"], a["country"]) == ("acct_1", "CREATED", "Venthora Demo Trading Pte Ltd", "SG")
    assert sent[0] == {"account_details": {"legal_entity_type": "BUSINESS", "business_details": {"business_name": "Venthora Demo Trading Pte Ltd",
                                                                                                 "registration_address": {"country_code": "SG"}}},
                       "primary_contact": {"email": "ops@venthora-demo.example"},
                       "customer_agreements": {"agreed_to_terms_and_conditions": True, "agreed_to_data_usage": True}}
    assert client.get("/v1/airwallex/accounts").json()["items"][0]["status"] == "CREATED"
    assert client.post("/v1/airwallex/accounts/acct_1/submit").json()["status"] == "SUBMITTED"
    r = client.post("/v1/airwallex/accounts", json=ok | {"legal_entity_type": "INDIVIDUAL", "first_name": "Ana", "last_name": "Lim"})
    assert r.status_code == 502 and "cannot onboard individual accounts" in r.json()["detail"] and "enable it" in r.json()["detail"]
    assert client.post("/v1/airwallex/accounts?role=treasury_analyst", json=ok).status_code == 403
