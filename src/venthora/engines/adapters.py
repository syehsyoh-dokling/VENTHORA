"""Optional remote engines (e.g. the original Verity/Horizon/Theorem services).

A remote engine may only narrow what the local engine allows: Verity's answer
is intersected with local eligibility, Horizon's stress takes the maximum, and
Theorem's selection must be a subset of verified obligations. Any error fails
closed. Contracts: docs/ENGINE_CONTRACTS.md.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx

from ..config import env


def endpoint(name: str) -> str | None:
    url = env(f"VENTHORA_{name}_URL")
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"invalid VENTHORA_{name}_URL")
    return url


async def call(url: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=12.0) as client:
        res = await client.post(url, json=payload)
        res.raise_for_status()
        body = res.json()
    if not isinstance(body, dict):
        raise ValueError("engine response must be a JSON object")
    return body


def narrow_eligible(local: set[str], remote: dict) -> set[str]:
    ids = remote.get("eligible_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("invalid eligible_ids")
    return local & set(ids)


def widen_stress(local: int, remote: dict) -> int:
    value = remote.get("stress_minor")
    if type(value) is not int or not 0 <= value <= 10**13:
        raise ValueError("invalid stress_minor")
    return max(local, value)


def checked_selection(eligible: set[str], remote: dict) -> list[str]:
    ids = remote.get("selected_ids")
    if not isinstance(ids, list) or len(ids) != len(set(ids)) or not set(ids) <= eligible:
        raise ValueError("selection includes unverified or duplicate obligations")
    return ids
