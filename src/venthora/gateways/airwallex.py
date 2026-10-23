"""Airwallex sandbox client.

Credentials stay server-side; the model never sees them. Only typed methods are
exposed. Implemented: authentication and current balances. Next (build phase):
FX quote, conversion, transfer and simulation calls, each callable only with a
ProofCore-approved plan and a persisted request_id.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import httpx

from ..config import env

SANDBOX_URL = "https://api-demo.airwallex.com"


class AirwallexError(RuntimeError):
    pass


class Airwallex:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None):
        self.base_url = base_url or env("AIRWALLEX_BASE_URL", SANDBOX_URL)
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=8.0)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(env("AIRWALLEX_CLIENT_ID") and env("AIRWALLEX_API_KEY"))

    async def close(self) -> None:
        await self._client.aclose()

    async def _auth_headers(self) -> dict[str, str]:
        if not self.configured:
            raise AirwallexError("Airwallex credentials not configured")
        async with self._lock:
            now = datetime.now(UTC).timestamp()
            if not self._token or now >= self._expires_at - 60:
                res = await self._client.post("/api/v1/authentication/login", headers={
                    "x-client-id": env("AIRWALLEX_CLIENT_ID"), "x-api-key": env("AIRWALLEX_API_KEY")})
                res.raise_for_status()
                body = res.json()
                self._token = body["token"]
                self._expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp()
        return {"Authorization": f"Bearer {self._token}"}

    async def balances_minor(self) -> dict[str, int]:
        """Available cash per currency, in minor units (2-decimal currencies)."""
        res = await self._client.get("/api/v1/balances/current", headers=await self._auth_headers())
        res.raise_for_status()
        out: dict[str, int] = {}
        for row in res.json():
            amount = Decimal(str(row.get("available_amount", "0")))
            out[row["currency"]] = int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return out
