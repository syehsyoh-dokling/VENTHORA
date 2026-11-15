# ruff: noqa: E501
"""With sign-in on, the person who is signed in is the person who acts: a role or a name written into the body of a
request changes nothing, and only a finance role runs a plan on the bank."""
import pytest
from fastapi.testclient import TestClient

from conftest import scenario
from venthora import auth

PASSWORD = "Treasury2026ok"


@pytest.fixture
def signed(monkeypatch):
    from venthora.api import app

    monkeypatch.setenv("VENTHORA_LOGIN", "1")
    monkeypatch.setattr(auth, "_fails", {})
    with TestClient(app) as c:
        def sign_in(role):
            user = next(u for u in app.state.org.list("user") if u["role"] == role and u.get("active", True))
            auth.AuthStore().set_password(user["user_id"], PASSWORD)
            assert c.post("/v1/auth/login", json={"email": user["email"], "password": PASSWORD}).status_code == 200
            return user

        c.sign_in = sign_in
        yield c


def test_a_role_written_in_the_body_is_replaced_by_the_signed_in_persons(signed):
    ines = signed.sign_in("auditor")  # may read; decides nothing
    claimed = {"reviewer": "Dana Finch", "role": "finance_manager", "scenario_id": "kit01_initial", "as_of": "2026-11-30", "note": "x"}
    r = signed.post("/v1/budgets/2026-11/close", json=claimed)
    assert r.status_code == 403 and "role auditor may not close a period" in r.json()["detail"]
    r = signed.post("/v1/budgets/2026-11/suppliers/grant", json={"approver": "Dana Finch", "role": "finance_manager", "extra_minor": 100, "note": "x"})
    assert r.status_code == 403 and "auditor" in r.json()["detail"]
    payee = signed.get("/v1/payees").json()[0]["beneficiary_id"]
    r = signed.post(f"/v1/payees/{payee}/callback", json={"by": "Dana Finch", "role": "super_admin", "called_number": "+1 555 0100", "outcome": "confirmed"})
    assert r.status_code == 403 and "role auditor may not record a call-back" in r.json()["detail"]
    r = signed.post("/v1/intake/no-such-request/review", json={"decision": "verify", "reviewer": "Dana Finch", "role": "finance_manager"})
    assert r.status_code in (403, 404)  # never accepted under the claimed name
    assert ines["name"] == "Ines Duarte"


def test_only_a_finance_role_runs_a_plan_on_the_bank(signed):
    signed.sign_in("auditor")
    body = {"state": scenario("kit01_initial"), "plan_hash": "0" * 64}
    for path in ("/v1/executions", "/v1/executions/autonomous"):
        r = signed.post(path, json=body if path.endswith("executions") else {"state": body["state"]})
        assert r.status_code == 403 and "role auditor may not execute a plan" in r.json()["detail"], path
    assert signed.post("/v1/executions/abc/resume").status_code == 403
    signed.sign_in("finance_manager")
    r = signed.post("/v1/executions", json=body)
    assert r.status_code != 403  # a finance manager reaches the executor, which then holds the plan to its own checks


def test_the_reserve_floor_of_the_mandate_cannot_be_lowered_by_the_caller():
    from venthora.api import _bound_execution, app
    from venthora.domain.models import TreasuryState
    from venthora.execution.models import ExecutionRequest

    with TestClient(app):
        from venthora import mandate

        floor = (mandate.standing(app.state.org).get("book") or {}).get("reserve_floor_minor")
        state = TreasuryState.model_validate(scenario("kit01_initial"))
        low = state.model_copy(update={"policy": state.policy.model_copy(update={"reserve_floor_minor": 0})})
        out = _bound_execution(ExecutionRequest(state=low, plan_hash="0" * 64), "finance_manager")
        assert out.state.policy.reserve_floor_minor == (int(floor) if floor is not None else 0)
        with pytest.raises(Exception, match="may not execute"):
            _bound_execution(ExecutionRequest(state=low, plan_hash="0" * 64), "requester")
