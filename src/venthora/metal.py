# ruff: noqa: E501
"""Platform Setup -> Metal: VENTHORA's proof of record anchored on Metal's public testnet (Tahoe C-Chain, EVM).

VENTHORA keeps two kinds of hash chain: the audit log (every decision and execution) and one chain per contract
(Reporting -> Contract blockchain). Anchoring writes their latest hashes on Metal in one transaction: the digest of
{audit head, every contract head} goes in the transaction's data, from VENTHORA's own testnet address to itself, value 0.
Anyone can then read the transaction on the explorer and recompute the digest from VENTHORA's records: a past decision,
invoice or payment changed after the anchor no longer matches what is on chain.

The testnet key is created here and kept in the write-only secrets store (never shown); only the address is public.
It needs test METAL for gas from Metal's faucet (a captcha, so a person claims it). Money still moves on Airwallex.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import httpx

from . import secret_values

RPC = "https://tahoe.metalblockchain.org/ext/bc/C/rpc"
CHAIN_ID = 381932
EXPLORER = "https://tahoe.metalscan.io"
FAUCET = "https://faucet.metalblockchain.org"
PRIVATE, ADDRESS = "metal.private_key", "metal.address"
TAG = b"VENTHORA-ANCHOR-1:"


class MetalError(RuntimeError):
    pass


def settings(org) -> dict:
    rec = org.get("metal", "settings") or {}
    return {"rpc": rec.get("rpc") or RPC, "chain_id": int(rec.get("chain_id") or CHAIN_ID), "explorer": rec.get("explorer") or EXPLORER}


def save_settings(org, body: dict, by: str) -> dict:
    rpc = str(body.get("rpc") or RPC).strip()
    if not rpc.startswith("https://"):
        raise ValueError("the RPC address must start with https://")
    org._put("metal", {"_id": "settings", "rpc": rpc, "chain_id": int(body.get("chain_id") or CHAIN_ID),
                       "explorer": str(body.get("explorer") or EXPLORER).strip(), "updated_by": by}, by)
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


async def rpc(url: str, method: str, params: list, client: httpx.AsyncClient | None = None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        if client is not None:
            res = await client.post(url, json=body, timeout=20)
        else:
            async with httpx.AsyncClient(timeout=20) as c:
                res = await c.post(url, json=body)
        res.raise_for_status()
        out = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MetalError(f"{type(exc).__name__}: {exc}") from exc
    if out.get("error"):
        raise MetalError(str(out["error"].get("message") or out["error"]))
    return out.get("result")


async def status(org, client: httpx.AsyncClient | None = None) -> dict:
    st = settings(org)
    addr = address()
    out = {"settings": st, "address": addr, "faucet": FAUCET, "key": secret_values.info(PRIVATE), "network": None, "balance_wei": None, "error": None}
    try:
        cid = int(await rpc(st["rpc"], "eth_chainId", [], client), 16)
        block = int(await rpc(st["rpc"], "eth_blockNumber", [], client), 16)
        out["network"] = {"chain_id": cid, "block": block, "matches": cid == st["chain_id"]}
        if addr:
            out["balance_wei"] = int(await rpc(st["rpc"], "eth_getBalance", [addr, "latest"], client), 16)
    except MetalError as exc:
        out["error"] = str(exc)
    return out


def digest(heads: dict) -> str:
    """The SHA-256 of the heads, canonical JSON (sorted keys): anyone with the records can recompute it."""
    return hashlib.sha256(json.dumps(heads, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def anchor(org, heads: dict, by: str, client: httpx.AsyncClient | None = None) -> dict:
    from eth_account import Account

    st, addr, key = settings(org), address(), secret_values.get(PRIVATE)
    if not key or not addr:
        raise MetalError("create the testnet key first")
    d = digest(heads)
    data = "0x" + (TAG + bytes.fromhex(d)).hex()
    nonce = int(await rpc(st["rpc"], "eth_getTransactionCount", [addr, "pending"], client), 16)
    price = int(await rpc(st["rpc"], "eth_gasPrice", [], client), 16)
    tx = {"to": addr, "value": 0, "data": data, "nonce": nonce, "chainId": st["chain_id"], "gasPrice": price}
    tx["gas"] = int(await rpc(st["rpc"], "eth_estimateGas", [{"from": addr, "to": addr, "value": "0x0", "data": data}], client), 16)
    bal = int(await rpc(st["rpc"], "eth_getBalance", [addr, "latest"], client), 16)
    if bal < tx["gas"] * price:
        raise MetalError("the testnet address has no test METAL for gas: claim some from the faucet first")
    signed = Account.sign_transaction(tx, key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = await rpc(st["rpc"], "eth_sendRawTransaction", ["0x" + bytes(raw).hex().removeprefix("0x")], client)
    rec = {"_id": tx_hash, "tx_hash": tx_hash, "digest": d, "heads": heads, "chain_id": st["chain_id"], "from": addr, "nonce": nonce,
           "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "by": by, "explorer": f"{st['explorer']}/tx/{tx_hash}"}
    org._put("metal_anchor", rec, by)
    return rec


def anchors(org) -> list[dict]:
    return sorted(org.list("metal_anchor"), key=lambda r: r.get("at", ""), reverse=True)
