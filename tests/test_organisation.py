# ruff: noqa: E501
"""Organisation master data: one unit tree, people in units, allocations checked against what a parent received."""
from fastapi.testclient import TestClient


def test_seed_tree_and_allocations():
    from venthora.api import app
    with TestClient(app) as client:
        units = client.get("/v1/org/units").json()
        kinds = {u["kind"] for u in units}
        assert kinds == {"HQ", "COUNTRY_OFFICE", "BRANCH_OFFICE", "PROJECT_FINANCE"}
        users = client.get("/v1/org/users").json()
        assert len(users) == 7 and [u["name"] for u in users if u["role"] == "super_admin"] == ["Rosa Lindqvist"]  # the FCO at HQ
        tree = {r["unit_id"]: r for r in client.get("/v1/org/allocation-tree?period=2026").json()}
        assert tree["hq"]["passed_down"] == {"USD": 510_000_000} and tree["hq"]["over_allocated"] == {}
        assert client.get("/v1/org/standards").json()["base_currency"] == "USD"


def test_tree_rules():
    from venthora.api import app
    with TestClient(app) as client:
        r = client.post("/v1/org/units", json={"name": "Second HQ", "kind": "HQ"})
        assert r.status_code == 409 and "already a Head Quarter" in r.json()["detail"]
        r = client.post("/v1/org/units", json={"name": "Branch under HQ", "kind": "BRANCH_OFFICE", "parent_id": "hq"})
        assert r.status_code == 409
        r = client.post("/v1/org/units", json={"name": "Singapore Country Office", "kind": "COUNTRY_OFFICE", "parent_id": "hq",
                                               "country": "SG", "currency": "sgd"})
        assert r.status_code == 200 and r.json()["currency"] == "SGD"
        assert client.delete("/v1/org/units/co-us").status_code == 409  # has a branch, people and an allocation
        assert client.post("/v1/org/units", json={"name": "x", "kind": "HQ", "currency": "US"}).status_code == 422


def test_users_allocations_prices_and_roles():
    from venthora.api import app
    with TestClient(app) as client:
        r = client.post("/v1/org/users", json={"name": "Dup", "email": "DANA.FINCH@acme.example", "role": "auditor", "unit_id": "hq"})
        assert r.status_code == 409
        r = client.post("/v1/org/users?role=auditor", json={"name": "New", "email": "new@acme.example", "role": "auditor", "unit_id": "hq"})
        assert r.status_code == 403
        r = client.post("/v1/org/allocations", json={"period": "2026", "unit_id": "co-uk", "line": "Extra", "amount_minor": 500_000_000})
        assert r.status_code == 409  # one allocation governs a scope in a period: a top-up edits it
        uk = next(a for a in client.get("/v1/org/allocations").json() if a["alloc_id"] == "a-uk")
        r = client.post("/v1/org/allocations", json=uk | {"amount_minor": uk["amount_minor"] + 500_000_000})
        assert r.status_code == 200
        tree = {x["unit_id"]: x for x in client.get("/v1/org/allocation-tree?period=2026").json()}
        assert tree["hq"]["over_allocated"] == {"USD": 410_000_000}
        r = client.post("/v1/org/prices", json={"item": "Courier", "category": "Logistics", "unit": "parcel", "price_minor": 1200,
                                                "currency": "EUR", "unit_id": "nowhere"})
        assert r.status_code == 409
        s = client.get("/v1/org/standards").json() | {"fiscal_year_start_month": 4}
        assert client.put("/v1/org/standards", json=s).json()["fiscal_year_start_month"] == 4
        assert client.put("/v1/org/standards?role=auditor", json=s).status_code == 403
