"""Open Policy Agent client. Undefined or malformed decisions fail closed."""
from __future__ import annotations

import httpx

from ..config import env


class OPA:
    def __init__(self):
        self.url = env("OPA_URL").rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def decide(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=2.0) as client:
            res = await client.post(f"{self.url}/v1/data/venthora/treasury/decision", json={"input": payload})
            res.raise_for_status()
            result = res.json().get("result")
        if not isinstance(result, dict) or type(result.get("allow")) is not bool:
            raise ValueError("OPA decision undefined")
        return {"allow": result["allow"], "requires_approval": sorted(result.get("requires_approval", []))}
