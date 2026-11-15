# ruff: noqa: E501
"""Blockchain Record: the Finance Director says yes or no, picks the documents (all, or from a value up), and each
chosen kind gets its own digest in the next anchor; nothing goes on the record while the answer is no."""
from venthora import blockchain_record as br


class Org:
    def __init__(self):
        self.rec = {}

    def get(self, kind, _id):
        return self.rec.get((kind, _id))

    def list(self, kind):
        return [v for (k, _), v in self.rec.items() if k == kind]

    def _put(self, kind, rec, by):
        self.rec[(kind, rec["_id"])] = rec


def test_nothing_is_recorded_until_the_company_says_yes_then_only_what_it_chose():
    org = Org()
    for n, v in (("PR-1", 50_000), ("PR-2", 500_000)):
        org._put("pr", {"_id": n, "pr_number": n, "estimated_minor": v}, "t")
    assert br.document_heads(org) == {}
    br.save(org, {"interested": True, "documents": {"pr": {"on": True, "min_minor": 100_000}, "po": {"on": False}}}, "fd")
    heads = br.document_heads(org)
    assert set(heads) == {"engagement", "pr", "project"}  # contracts and projects stay on by default; POs switched off
    assert heads["pr"]["records"] == 1 and heads["pr"]["digest"]  # only the PR from USD 1,000 up
    before = heads["pr"]["digest"]
    org._put("pr", {"_id": "PR-2", "pr_number": "PR-2", "estimated_minor": 500_001}, "t")  # an approved PR changed afterwards
    assert br.document_heads(org)["pr"]["digest"] != before


def test_projects_all_or_only_the_ones_chosen_and_no_redundant_kinds():
    org = Org()
    for code in ("TPK", "JKT", "OPS"):
        org._put("project", {"_id": code, "code": code, "name": f"Project {code}"}, "t")
    br.save(org, {"interested": True, "documents": {"project": {"on": True}}}, "fd")
    assert br.document_heads(org)["project"]["records"] == 3
    br.save(org, {"documents": {"project": {"on": True, "only": ["tpk", "JKT"]}}}, "fd")
    assert br.document_heads(org)["project"]["records"] == 2
    assert [p["code"] for p in br.view(org)["projects"]] == ["JKT", "OPS", "TPK"]
    assert not {"payee", "fund_request", "ta"} & set(br.DOC_LABEL)  # already proven elsewhere, or not worth a record


def test_the_page_shows_cost_products_and_refuses_unknown_choices():
    org = Org()
    v = br.view(org)
    assert v["cost"]["subscription"] is None and 0 < v["cost"]["per_month_usd"] < 1
    assert {p["status"] for p in v["products"]} == {"active", "planned"}
    assert v["provider"]["name"] == "Alta"
    for bad in ({"product": "nft_mint"}, {"documents": {"invoice_scan": {"on": True}}}, {"documents": {"pr": {"on": True, "min_minor": -1}}}):
        try:
            br.save(org, bad, "fd")
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass


def test_the_api_saves_and_the_anchor_carries_the_documents():
    from fastapi.testclient import TestClient

    from venthora.api import _anchor_heads, app

    with TestClient(app) as c:
        r = c.put("/v1/blockchain-record?role=super_admin&by=fd", json={"interested": True, "documents": {"po": {"on": True, "min_minor": 0}}})
        assert r.status_code == 200, r.text
        assert r.json()["settings"]["interested"]
        assert "documents" in _anchor_heads()
