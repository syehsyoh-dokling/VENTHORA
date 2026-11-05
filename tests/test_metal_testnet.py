# ruff: noqa: E501
"""Metal Testnet (chain 63825): VENTHORA buys its own RPC endpoint over x402 (an EIP-3009 transfer of 1 USDZ signed
with its key), tops its address up with test USDZ from the faucet, and anchors with an EIP-1559 transaction of value 0.
The old, wrong network (Metal Blockchain's Tahoe, 381932) is never used."""
import asyncio
import base64
import json

import httpx
import pytest

from venthora import metal, secret_values


class Org:
    def __init__(self):
        self.d = {}

    def get(self, k, i):
        return self.d.get((k, i))

    def _put(self, k, r, by):
        self.d[(k, r["_id"])] = r
        return r

    def list(self, k):
        return [v for (kk, _), v in self.d.items() if kk == k]


@pytest.fixture
def keys(monkeypatch):
    from eth_account import Account

    acct = Account.create()
    store = {metal.PRIVATE: acct.key.hex(), metal.ADDRESS: acct.address}
    monkeypatch.setattr(secret_values, "get", lambda k, env=None: store.get(k, ""))
    monkeypatch.setattr(secret_values, "put", lambda k, v, by: store.__setitem__(k, v))
    return acct, store


OFFER = {"x402Version": 2, "resource": {"url": metal.RPC_BASE}, "accepts": [{"scheme": "exact", "network": "eip155:63825", "amount": str(10**18),
         "asset": metal.GAS_TOKEN["address"], "payTo": "0x69257753706e96218cACf3A78D392e71459DB7D5", "maxTimeoutSeconds": 60,
         "extra": {"assetTransferMethod": "eip3009", "name": "USD Zero", "version": "1", "decimals": 18, "symbol": "USDZ"}}]}


def test_venthora_buys_its_own_rpc_endpoint_over_x402(keys):
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    acct, store = keys
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        sig = req.headers.get("payment-signature")
        seen.append(sig)
        if not sig:
            return httpx.Response(402, headers={"payment-required": base64.b64encode(json.dumps(OFFER).encode()).decode()}, json={})
        receipt = base64.b64encode(json.dumps({"success": True, "transaction": "0x" + "cd" * 32}).encode()).decode()
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0xf951"},
                              headers={"rpc-aas-endpoint": metal.RPC_BASE + "/ep-secret-1234", "payment-response": receipt})

    out = asyncio.run(metal.buy_endpoint("Rosa (super_admin)", httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert out["paid"] == 1 and out["token"] == "USDZ" and out["payment_tx"] == "0x" + "cd" * 32
    assert store[metal.ENDPOINT] == metal.RPC_BASE + "/ep-secret-1234" and "ep-secret" not in out["endpoint"]  # kept secret, shown masked
    pay = json.loads(base64.b64decode(seen[1]))
    a = pay["payload"]["authorization"]
    assert pay["x402Version"] == 2 and pay["accepted"]["network"] == "eip155:63825" and a["from"] == acct.address and a["value"] == str(10**18)
    typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"},
                                        {"name": "verifyingContract", "type": "address"}],
                       "TransferWithAuthorization": [{"name": n, "type": t} for n, t in (("from", "address"), ("to", "address"), ("value", "uint256"),
                                                                                        ("validAfter", "uint256"), ("validBefore", "uint256"), ("nonce", "bytes32"))]},
             "primaryType": "TransferWithAuthorization", "domain": {"name": "USD Zero", "version": "1", "chainId": 63825, "verifyingContract": metal.GAS_TOKEN["address"]},
             "message": {"from": a["from"], "to": a["to"], "value": int(a["value"]), "validAfter": 0, "validBefore": int(a["validBefore"]), "nonce": bytes.fromhex(a["nonce"][2:])}}
    assert Account.recover_message(encode_typed_data(full_message=typed), signature=pay["payload"]["signature"]) == acct.address


def test_an_x402_offer_on_another_network_or_too_dear_is_refused(keys):
    for bad in ({"network": "eip155:1"}, {"amount": str(50 * 10**18)}):
        offer = json.loads(json.dumps(OFFER))
        offer["accepts"][0] |= bad

        def handler(req, offer=offer):
            return httpx.Response(402, headers={"payment-required": base64.b64encode(json.dumps(offer).encode()).decode()}, json={})

        with pytest.raises(metal.MetalError):
            asyncio.run(metal.buy_endpoint("x", httpx.AsyncClient(transport=httpx.MockTransport(handler))))


def test_faucet_and_settings(keys):
    acct, store = keys

    def handler(req):
        body = json.loads(req.content)
        assert str(req.url) == metal.FAUCET + "/api/fund" and body == {"address": acct.address, "symbol": "USDZ"}
        return httpx.Response(200, json={"amount": str(100 * 10**18), "symbol": "USDZ", "txHash": "0x" + "ef" * 32})

    out = asyncio.run(metal.fund(httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert out == {"funded": True, "amount": 100, "tx_hash": "0x" + "ef" * 32}
    org = Org()
    org._put("metal", {"_id": "settings", "rpc": "https://tahoe.metalblockchain.org/ext/bc/C/rpc", "chain_id": 381932}, "old")
    st = metal.settings(org)
    assert st["chain_id"] == 63825 and st["explorer"] == "https://testnet.metalntwx.com" and st["rpc_set"] is False  # the old network is ignored
    with pytest.raises(ValueError):
        metal.save_settings(org, {"rpc": "https://tahoe.metalblockchain.org/ext/bc/C/rpc"}, "x")
    assert metal.save_settings(org, {"rpc": metal.RPC_BASE + "/abc123def456"}, "x")["rpc_set"] is True
    org._put("metal_anchor", {"_id": "old", "chain_id": 381932, "at": "2026-11-01"}, "x")
    assert metal.anchors(org) == []
