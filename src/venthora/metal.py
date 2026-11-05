# ruff: noqa: E501
"""Platform Setup -> Metal: VENTHORA's proof of record anchored on Metal Testnet (docs.metalntwx.com, EVM, chain 63825).

VENTHORA keeps two kinds of hash chain: the audit log (every decision and execution) and one chain per contract
(Reporting -> Contract blockchain). Anchoring writes their latest hashes on Metal in one transaction: the digest of
{audit head, every contract head} goes in the transaction's data, from VENTHORA's own testnet address to itself, value 0
(Metal refuses any native value). Anyone can read the transaction on the explorer and recompute the digest from
VENTHORA's records: a past decision, invoice or payment changed after the anchor no longer matches what is on chain.

Everything is done by the agent itself, with test tokens only (verified on Metal Testnet, 6 Nov 2026):
  1. the testnet key: created here, kept in the write-only secrets store; only the address is public;
  2. gas: Metal pays gas in a stablecoin (USDZ, a MIP20 token), not a native coin; the faucet's API tops the address
     up to 100 USDZ (no captcha, no sign-in);
  3. the RPC: every request needs an endpoint. VENTHORA buys its own over x402 (the bare RPC answers 402; VENTHORA
     signs an EIP-3009 transfer of 1 USDZ and gets an endpoint URL owned by its address), or an admin pastes one made
     in the Metal console. The URL is a credential: kept in the secrets store, shown masked;
  4. the anchor: an EIP-1559 transaction, about 23,000 gas at a gas price of 1 base unit, final in about a second.
Money still moves on Airwallex.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import time

import httpx

from . import secret_values

NETWORK = "Metal Testnet"
RPC_BASE = "https://rpc-testnet.metalntwx.com/v1"
CHAIN_ID = 63825
EXPLORER = "https://testnet.metalntwx.com"
FAUCET = "https://faucet.metalntwx.com"
GAS_TOKEN = {"symbol": "USDZ", "address": "0x000000000000000000000000000000000000B5d5", "decimals": 18}
PRIVATE, ADDRESS, ENDPOINT = "metal.private_key", "metal.address", "metal.rpc_endpoint"
TAG = b"VENTHORA-ANCHOR-1:"
LEGACY_CHAIN_IDS = {381932}  # Metal Blockchain's "Tahoe": a different project, used by mistake before 6 Nov 2026


class MetalError(RuntimeError):
    pass


def _mask(url: str | None) -> str | None:
    return f"{url[:len(RPC_BASE) + 4]}…{url[-4:]}" if url else None


def endpoint() -> str | None:
    return secret_values.get(ENDPOINT) or None


def settings(org) -> dict:
    rec = org.get("metal", "settings") or {}
    if int(rec.get("chain_id") or CHAIN_ID) in LEGACY_CHAIN_IDS:
        rec = {}
    return {"network": NETWORK, "rpc": _mask(endpoint()), "rpc_set": bool(endpoint()), "chain_id": CHAIN_ID,
            "explorer": rec.get("explorer") or EXPLORER, "faucet": FAUCET, "gas_token": GAS_TOKEN}


def save_settings(org, body: dict, by: str) -> dict:
    """An admin pastes an endpoint made in the Metal console (console-testnet.metalntwx.com → Endpoints)."""
    url = str(body.get("rpc") or "").strip()
    if url:
        if not url.startswith(RPC_BASE + "/") or len(url) <= len(RPC_BASE) + 8:
            raise ValueError(f"paste the endpoint URL from the Metal console: {RPC_BASE}/<endpoint id>")
        secret_values.put(ENDPOINT, url, by)
    org._put("metal", {"_id": "settings", "chain_id": CHAIN_ID, "explorer": EXPLORER, "updated_by": by}, by)
    return settings(org)


def address() -> str | None:
    return secret_values.get(ADDRESS) or None


def generate(by: str) -> dict:
    """A new testnet key: the private key goes to the secrets store, the address is public."""
    from eth_account import Account

    acct = Account.create()
    secret_values.put(PRIVATE, acct.key.hex(), by)
    secret_values.put(ADDRESS, acct.address, by)
    return {"address": acct.address}


async def _post(url: str, client: httpx.AsyncClient | None, **kw) -> httpx.Response:
    try:
        if client is not None:
            return await client.post(url, timeout=30, **kw)
        async with httpx.AsyncClient(timeout=30) as c:
            return await c.post(url, **kw)
    except httpx.HTTPError as exc:
        raise MetalError(f"{type(exc).__name__}: {exc}") from exc


async def rpc(url: str | None, method: str, params: list, client: httpx.AsyncClient | None = None):
    if not url:
        raise MetalError("no RPC endpoint yet: let VENTHORA buy one over x402 (1 USDZ), or paste one from the Metal console")
    res = await _post(url, client, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if res.status_code == 402:
        raise MetalError("the RPC endpoint has no credit left: buy a new credit pack (1 USDZ over x402)")
    try:
        res.raise_for_status()
        out = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MetalError(f"{type(exc).__name__}: {exc}") from exc
    if out.get("error"):
        raise MetalError(str(out["error"].get("message") or out["error"]))
    return out.get("result")


async def fund(client: httpx.AsyncClient | None = None) -> dict:
    """Top VENTHORA's address up to 100 test USDZ from the faucet (its API needs no captcha or sign-in)."""
    addr = address()
    if not addr:
        raise MetalError("create the testnet key first")
    res = await _post(f"{FAUCET}/api/fund", client, json={"address": addr, "symbol": GAS_TOKEN["symbol"]})
    body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
    if res.status_code == 409:
        return {"funded": False, "note": "already holds 100 USDZ or more"}
    if res.status_code == 429:
        raise MetalError(f"the faucet's limit is reached; try again in {body.get('retryAfter', 60)} s")
    if res.status_code != 200:
        raise MetalError(f"the faucet answered {res.status_code}: {str(body or res.text)[:200]}")
    return {"funded": True, "amount": int(body.get("amount") or 0) / 10 ** GAS_TOKEN["decimals"], "tx_hash": body.get("txHash")}


def _pay_header(offer: dict, key: str, addr: str) -> tuple[str, dict]:
    """The x402 v2 PAYMENT-SIGNATURE header for the offer's exact/EIP-3009 requirement, signed with VENTHORA's key."""
    from eth_account import Account

    req = next((a for a in offer.get("accepts") or [] if a.get("scheme") == "exact" and (a.get("extra") or {}).get("assetTransferMethod") == "eip3009"), None)
    if req is None:
        raise MetalError("the RPC's x402 offer has no exact EIP-3009 option VENTHORA can pay")
    chain = int(str(req["network"]).split(":")[1])
    if chain != CHAIN_ID or req["asset"].lower() != GAS_TOKEN["address"].lower():
        raise MetalError(f"unexpected x402 offer: network {req['network']}, asset {req['asset']}")
    if int(req["amount"]) > 5 * 10 ** GAS_TOKEN["decimals"]:
        raise MetalError(f"the RPC asks {int(req['amount']) / 1e18:g} USDZ; VENTHORA pays at most 5")
    auth = {"from": addr, "to": req["payTo"], "value": str(req["amount"]), "validAfter": "0",
            "validBefore": str(int(time.time()) + int(req.get("maxTimeoutSeconds") or 60)), "nonce": "0x" + os.urandom(32).hex()}
    typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                        {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                       "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                                     {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                                     {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
             "primaryType": "TransferWithAuthorization",
             "domain": {"name": req["extra"]["name"], "version": req["extra"]["version"], "chainId": chain, "verifyingContract": req["asset"]},
             "message": {"from": addr, "to": req["payTo"], "value": int(auth["value"]), "validAfter": 0, "validBefore": int(auth["validBefore"]),
                         "nonce": bytes.fromhex(auth["nonce"][2:])}}
    sig = Account.sign_typed_data(key, full_message=typed).signature.hex()
    payment = {"x402Version": offer.get("x402Version", 2), "resource": offer.get("resource"), "accepted": req,
               "payload": {"authorization": auth, "signature": sig if sig.startswith("0x") else "0x" + sig}}
    if offer.get("extensions"):
        payment["extensions"] = offer["extensions"]
    return base64.b64encode(json.dumps(payment, separators=(",", ":")).encode()).decode(), req


async def buy_endpoint(by: str, client: httpx.AsyncClient | None = None) -> dict:
    """Buy VENTHORA's own RPC endpoint over x402: the bare RPC answers 402 with a 1 USDZ offer; VENTHORA signs an
    EIP-3009 transfer with its key and retries; the answer carries the new endpoint URL (kept as a secret)."""
    key, addr = secret_values.get(PRIVATE), address()
    if not key or not addr:
        raise MetalError("create the testnet key first")
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    first = await _post(RPC_BASE, client, json=body)
    if first.status_code != 402 or not first.headers.get("payment-required"):
        raise MetalError(f"the RPC did not offer x402 (HTTP {first.status_code})")
    offer = json.loads(base64.b64decode(first.headers["payment-required"]))
    header, req = _pay_header(offer, key, addr)
    paid = await _post(RPC_BASE, client, json=body, headers={"PAYMENT-SIGNATURE": header})
    url = paid.headers.get("rpc-aas-endpoint")
    if paid.status_code != 200 or not url:
        why = paid.headers.get("payment-response") or paid.text[:200]
        raise MetalError(f"the x402 payment was not accepted (HTTP {paid.status_code}): {why}")
    receipt = json.loads(base64.b64decode(paid.headers["payment-response"])) if paid.headers.get("payment-response") else {}
    secret_values.put(ENDPOINT, url, by)
    return {"endpoint": _mask(url), "paid": int(req["amount"]) / 10 ** GAS_TOKEN["decimals"], "token": GAS_TOKEN["symbol"],
            "payment_tx": receipt.get("transaction"), "explorer": f"{EXPLORER}/tx/{receipt['transaction']}" if receipt.get("transaction") else None}


async def status(org, client: httpx.AsyncClient | None = None) -> dict:
    st = settings(org)
    addr = address()
    out = {"settings": st, "address": addr, "faucet": FAUCET, "key": secret_values.info(PRIVATE), "network": None, "balance_wei": None,
           "balance": None, "error": None}
    if not endpoint():
        out["error"] = "no RPC endpoint yet: buy one over x402 (1 USDZ) or paste one from the Metal console"
        return out
    try:
        url = endpoint()
        cid = int(await rpc(url, "eth_chainId", [], client), 16)
        block = int(await rpc(url, "eth_blockNumber", [], client), 16)
        out["network"] = {"chain_id": cid, "block": block, "matches": cid == CHAIN_ID, "name": NETWORK}
        if addr:
            out["balance_wei"] = int(await rpc(url, "eth_getBalance", [addr, "latest"], client), 16)  # Metal answers in the gas token (USDZ)
            out["balance"] = out["balance_wei"] / 10 ** GAS_TOKEN["decimals"]
    except MetalError as exc:
        out["error"] = str(exc)
    return out


def digest(heads: dict) -> str:
    """The SHA-256 of the heads, canonical JSON (sorted keys): anyone with the records can recompute it."""
    return hashlib.sha256(json.dumps(heads, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def anchor(org, heads: dict, by: str, client: httpx.AsyncClient | None = None) -> dict:
    from eth_account import Account

    st, addr, key, url = settings(org), address(), secret_values.get(PRIVATE), endpoint()
    if not key or not addr:
        raise MetalError("create the testnet key first")
    d = digest(heads)
    data = "0x" + (TAG + bytes.fromhex(d)).hex()
    nonce = int(await rpc(url, "eth_getTransactionCount", [addr, "pending"], client), 16)
    price = max(int(await rpc(url, "eth_gasPrice", [], client), 16), 1)
    gas = int(int(await rpc(url, "eth_estimateGas", [{"from": addr, "to": addr, "value": "0x0", "data": data}], client), 16) * 1.2) + 1000
    fee = price * 2  # Metal: effective price = min(maxFee, maxPriority); the base fee is zero
    bal = int(await rpc(url, "eth_getBalance", [addr, "latest"], client), 16)
    if bal < gas * fee:
        raise MetalError("the testnet address has no USDZ for gas: fund it from the faucet first")
    tx = {"type": 2, "chainId": CHAIN_ID, "nonce": nonce, "to": addr, "value": 0, "data": data, "gas": gas,
          "maxFeePerGas": fee, "maxPriorityFeePerGas": fee}
    signed = Account.sign_transaction(tx, key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = await rpc(url, "eth_sendRawTransaction", ["0x" + bytes(raw).hex().removeprefix("0x")], client)
    rec = {"_id": tx_hash, "tx_hash": tx_hash, "digest": d, "heads": heads, "chain_id": CHAIN_ID, "network": NETWORK, "from": addr, "nonce": nonce,
           "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "by": by, "explorer": f"{st['explorer']}/tx/{tx_hash}"}
    org._put("metal_anchor", rec, by)
    return rec


def anchors(org) -> list[dict]:
    """Anchors on Metal Testnet (ones sent to the old, wrong network are left out)."""
    return sorted((a for a in org.list("metal_anchor") if a.get("chain_id") not in LEGACY_CHAIN_IDS), key=lambda r: r.get("at", ""), reverse=True)
