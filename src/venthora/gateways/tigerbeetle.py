"""TigerBeetle ledger: read the internal cash account (optional)."""
from __future__ import annotations

import asyncio

from ..config import env


class TigerBeetleLedger:
    def __init__(self):
        self._manager = None
        self._client = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(env("TB_ADDRESS") and env("TB_ACCOUNT_ID"))

    async def _get_client(self):
        async with self._lock:
            if self._client is None:
                import tigerbeetle as tb

                self._manager = tb.ClientAsync(cluster_id=int(env("TB_CLUSTER_ID", "0")),
                                               replica_addresses=env("TB_ADDRESS"))
                self._client = await self._manager.__aenter__()
        return self._client

    async def close(self) -> None:
        if self._manager is not None:
            await self._manager.__aexit__(None, None, None)
            self._manager = self._client = None

    async def balance_minor(self) -> int:
        client = await self._get_client()
        accounts = await client.lookup_accounts([int(env("TB_ACCOUNT_ID"))])
        if len(accounts) != 1:
            raise LookupError("TigerBeetle account not found")
        a = accounts[0]
        # Credit-normal cash account: spendable = posted credits - posted debits - pending debits.
        return int(a.credits_posted - a.debits_posted - a.debits_pending)
