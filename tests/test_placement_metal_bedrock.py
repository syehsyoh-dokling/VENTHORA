# ruff: noqa: E501
"""Cash placement (idle cash above what must stay; proposed, approved by another person, never above the idle
amount), Metal anchoring (the digest of the chain heads in a signed testnet transaction), Amazon Bedrock as a model
(Converse with a Bedrock API key), and the journal export for an accounting system."""
import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from venthora import cash_placement, metal, secret_values
from venthora.evidence.llm import BedrockModel


class Org:
    def __init__(self):
        self.recs = {}

    def get(self, kind, rid):
        return copy.deepcopy(self.recs.get(kind, {}).get(rid))

    def list(self, kind):
        return list(self.recs.get(kind, {}).values())

    def _put(self, kind, rec, by):
        self.recs.setdefault(kind, {})[rec["_id"]] = copy.deepcopy(rec)


def test_idle_cash_is_what_is_left_after_reserve_bills_commitments_and_buffer():
    pol = cash_placement.DEFAULT | {"buffer_pct": 10, "min_amount_minor": 100_000}
    c = cash_placement.compute(available_minor=10_000_000, reserve_floor_minor=1_000_000, bills_minor=500_000, commitments_minor=2_500_000, pol=pol)
    assert c["buffer_minor"] == 400_000 and c["needed_minor"] == 4_400_000 and c["idle_minor"] == 5_600_000 and c["place_minor"] == 5_600_000
    short = cash_placement.compute(available_minor=3_000_000, reserve_floor_minor=1_000_000, bills_minor=500_000, commitments_minor=2_500_000, pol=pol)
    assert short["idle_minor"] < 0 and short["place_minor"] == 0 and "nothing idle" in short["why"]
    small = cash_placement.compute(available_minor=4_450_000, reserve_floor_minor=1_000_000, bills_minor=500_000, commitments_minor=2_500_000, pol=pol)
    assert small["idle_minor"] == 50_000 and small["place_minor"] == 0 and "below the minimum" in small["why"]


def test_a_placement_never_exceeds_idle_cash_and_needs_a_second_person():
    org = Org()
    basis = {"idle_minor": 500_000}
    with pytest.raises(ValueError, match="above the idle cash"):
        cash_placement.instruct(org, amount_minor=600_000, currency="USD", destination="Term deposit", note="", by="Dana Finch (finance_manager)", basis=basis)
    rec = cash_placement.instruct(org, amount_minor=400_000, currency="USD", destination="Term deposit", note="", by="Dana Finch (finance_manager)", basis=basis)
    with pytest.raises(ValueError, match="cannot approve"):
        cash_placement.decide(org, rec["_id"], "APPROVED", "Dana Finch (finance_manager)")
    with pytest.raises(ValueError, match="cannot become done"):
        cash_placement.decide(org, rec["_id"], "DONE", "Omar Reyes (cfo)")
    assert cash_placement.decide(org, rec["_id"], "APPROVED", "Omar Reyes (cfo)")["status"] == "APPROVED"
    assert cash_placement.decide(org, rec["_id"], "DONE", "Omar Reyes (cfo)")["status"] == "DONE"


def test_an_anchor_is_a_signed_testnet_transaction_carrying_the_digest(monkeypatch):
    from eth_account import Account

    acct = Account.create()
    store = {metal.PRIVATE: acct.key.hex(), metal.ADDRESS: acct.address}
    monkeypatch.setattr(secret_values, "get", lambda k: store.get(k, ""))
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        m = body["method"]
        res = {"eth_getTransactionCount": "0x3", "eth_gasPrice": "0x5d21dba00", "eth_estimateGas": "0x5f5e", "eth_getBalance": hex(10**18)}.get(m)
        if m == "eth_sendRawTransaction":
            sent.append(body["params"][0])
            res = "0x" + "ab" * 32
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": res})

    heads = {"audit": {"head": "f" * 64, "events": 9, "verified": True}, "contracts": {"PO-1": "a" * 64}}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    org = Org()
    rec = asyncio.run(metal.anchor(org, heads, "Dana Finch (finance_manager)", client))
    assert rec["tx_hash"] == "0x" + "ab" * 32 and rec["digest"] == metal.digest(heads) and rec["explorer"].endswith(rec["tx_hash"])
    assert Account.recover_transaction(sent[0]) == acct.address  # signed by VENTHORA's key
    assert (metal.TAG + bytes.fromhex(rec["digest"])).hex() in sent[0]  # the digest is in the data
    assert metal.anchors(org)[0]["tx_hash"] == rec["tx_hash"]


def test_no_anchor_without_gas(monkeypatch):
    from eth_account import Account

    acct = Account.create()
    monkeypatch.setattr(secret_values, "get", lambda k: {metal.PRIVATE: acct.key.hex(), metal.ADDRESS: acct.address}.get(k, ""))

    def handler(req):
        m = json.loads(req.content)["method"]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x0" if m == "eth_getBalance" else "0x1"})

    with pytest.raises(metal.MetalError, match="faucet"):
        asyncio.run(metal.anchor(Org(), {"audit": {}, "contracts": {}}, "x", httpx.AsyncClient(transport=httpx.MockTransport(handler))))


def test_bedrock_converse_with_a_bearer_api_key():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"output": {"message": {"role": "assistant", "content": [{"text": "PAY"}]}}})

    m = BedrockModel(api_key="bedrock-key-123", model="amazon.nova-pro-v1:0", endpoint="https://bedrock-runtime.us-east-1.amazonaws.com",
                     client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = asyncio.run(m.complete([{"role": "system", "content": "be brief"}, {"role": "user", "content": "pay?"}], max_tokens=50))
    assert out == "PAY" and seen["auth"] == "Bearer bedrock-key-123" and seen["url"].endswith("/model/amazon.nova-pro-v1%3A0/converse")
    assert seen["body"]["system"] == [{"text": "be brief"}] and seen["body"]["messages"] == [{"role": "user", "content": [{"text": "pay?"}]}]


def test_the_journal_exports_as_debit_and_credit_lines():
    from venthora.api import app

    with TestClient(app) as c:
        r = c.get("/v1/ledger/export.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        head, *rows = r.text.strip().splitlines()
        assert head == "Date,Journal entry,Book,Entry type,Reference,Execution,Account code,Account name,Currency,Debit,Credit"
        for line in rows:
            cells = line.split(",")
            assert bool(cells[-2]) != bool(cells[-1])  # each line is a debit or a credit, never both
