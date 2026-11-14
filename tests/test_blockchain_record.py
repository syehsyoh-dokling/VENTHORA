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
    assert set(heads) == {"engagement", "pr"}  # contracts stay on by default; POs switched off
    assert heads["pr"]["records"] == 1 and heads["pr"]["digest"]  # only the PR from USD 1,000 up
    before = heads["pr"]["digest"]
    org._put("pr", {"_id": "PR-2", "pr_number": "PR-2", "estimated_minor": 500_001}, "t")  # an approved PR changed afterwards
    assert br.document_heads(org)["pr"]["digest"] != before


def test_the_page_shows_cost_products_and_refuses_unknown_choices():
    org = Org()
    v = br.view(org)
    assert v["cost"]["subscription"] is None and 0 < v["cost"]["per_month_usd"] < 1
    assert {p["status"] for p in v["products"]} == {"active", "planned"}
    assert v["provider"]["name"] == "Metal"
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
