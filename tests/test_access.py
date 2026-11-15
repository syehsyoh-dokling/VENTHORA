# ruff: noqa: E501
"""Menus by role (access.py), as the server enforces them: who may change what, and what a Developer may not read."""
import pytest

from venthora import access

FCO_SAYS = "only the FCO (Super admin)"


@pytest.fixture(autouse=True)
def self_hosted(monkeypatch):
    monkeypatch.delenv("VENTHORA_DEPLOYMENT", raising=False)


def refused(role, method, path):
    out = access.check(role, method, path)
    return None if out is None else out[0]


def test_fco_authorize_is_changed_by_the_fco_only():
    for path in ("/v1/mandate", "/v1/priority-sop", "/v1/org/standards", "/v1/doa/registers", "/v1/placement-providers/x"):
        assert refused("super_admin", "PUT", path) is None
        for role in ("finance_manager", "cfo", "hq_admin", "developer", "requester"):
            assert refused(role, "PUT", path) == 403, (role, path)
        assert FCO_SAYS in access.check("cfo", "POST", path)[1]
    assert refused("finance_manager", "GET", "/v1/mandate") is None  # a finance manager may read what it works under


def test_a_developer_never_reads_fco_authorize():
    for path in ("/v1/doa", "/v1/doa/registers/2026-11", "/v1/mandate", "/v1/priority-sop", "/v1/placement-providers"):
        assert refused("developer", "GET", path) == 403
    assert refused("developer", "GET", "/v1/org/standards") is None  # every screen shows the base currency


def test_the_money_part_of_platform_setup_is_the_fcos():
    for path in ("/v1/airwallex/accounts/open", "/v1/cards/card-1/freeze", "/v1/card-authorization", "/v1/controls/airi", "/v1/local-rails/products"):
        assert refused("super_admin", "POST", path) is None
        assert refused("finance_manager", "POST", path) == 403 and refused("developer", "POST", path) == 403
        assert refused("developer", "GET", path) is None  # a Developer may look at it


def test_the_technical_part_is_the_fcos_or_a_developers():
    for path in ("/v1/webhooks/register", "/v1/settings/ai/budget", "/v1/settings/email/inboxes", "/v1/developer/keys", "/v1/sources/connectors/c1"):
        assert refused("super_admin", "PUT", path) is None and refused("developer", "PUT", path) is None
        assert refused("finance_manager", "PUT", path) == 403


def test_the_developer_console_is_changed_by_a_developer_and_absent_on_saas(monkeypatch):
    for path in ("/v1/visa/tap/keys", "/v1/visa/tap/sign-test", "/v1/metal/key"):
        assert refused("developer", "POST", path) is None
        assert refused("super_admin", "POST", path) == 403  # the FCO views it, a Developer changes it
        assert refused("super_admin", "GET", path) is None
    assert refused("super_admin", "PUT", "/v1/airwallex/connection") == 403 and refused("developer", "PUT", "/v1/airwallex/connection") is None
    monkeypatch.setenv("VENTHORA_DEPLOYMENT", "saas")
    assert access.deployment() == "saas"
    assert refused("developer", "GET", "/v1/metal/key") == 404  # on SaaS the technical setup is the operator's
    assert refused("developer", "PUT", "/v1/airwallex/connection") == 404
    assert refused("super_admin", "GET", "/v1/airwallex/connection") is None  # its state is still read by the FCO's pages


def test_everything_else_is_left_to_the_endpoints_own_checks():
    assert refused("finance_manager", "POST", "/v1/beneficiaries") is None and refused("requester", "POST", "/v1/intake/requests") is None
    assert refused(None, "PUT", "/v1/mandate") is None  # an API key's call: its role is set by the key, checked elsewhere
