"""Airwallex sandbox client: the only code that talks to the bank.

Credentials stay server-side and the model never sees them. Every method is
typed and converts at the boundary: the API speaks major units ("4012.85"),
VENTHORA speaks integer minor units (401285). Every money-moving call takes a
caller-supplied `request_id` that is persisted *before* the call, so a retry
after a timeout reuses it and Airwallex de-duplicates instead of paying twice.

Endpoints follow https://airwallexdev.com/guide and the Airwallex API reference.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import httpx

from ..config import env

SANDBOX_URL = "https://api.sandbox.airwallex.com"
PRODUCTION_URL = "https://api.airwallex.com"
TERMINAL_TRANSFER_STATUSES = {"PAID", "CANCELLED", "FAILED"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class AirwallexError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def to_major(amount_minor: int) -> str:
    return f"{Decimal(amount_minor) / 100:.2f}"


def to_minor(amount_major) -> int:
    return int((Decimal(str(amount_major)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    if len(value) > 5 and value[-5] in "+-" and value[-3] != ":":  # +1100 -> +11:00
        value = f"{value[:-2]}:{value[-2:]}"
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Quote:
    quote_id: str
    sell_currency: str
    buy_currency: str
    sell_amount_minor: int
    buy_amount_minor: int
    client_rate: Decimal
    valid_to_at: datetime | None
    usage: str
    raw: dict

    def expired(self, now: datetime | None = None) -> bool:
        return self.valid_to_at is not None and (now or datetime.now(UTC)) >= self.valid_to_at


@dataclass(frozen=True)
class ConversionResult:
    conversion_id: str
    status: str
    sell_currency: str
    buy_currency: str
    sell_amount_minor: int
    buy_amount_minor: int
    client_rate: Decimal
    quote_id: str | None
    raw: dict


@dataclass(frozen=True)
class TransferResult:
    transfer_id: str
    status: str
    source_currency: str
    transfer_currency: str
    transfer_amount_minor: int
    amount_payer_pays_minor: int
    fee_minor: int
    failure_reason: str | None
    raw: dict

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TRANSFER_STATUSES


def _conversion(body: dict) -> ConversionResult:
    return ConversionResult(
        conversion_id=body["conversion_id"], status=body["status"], sell_currency=body["sell_currency"],
        buy_currency=body["buy_currency"], sell_amount_minor=to_minor(body["sell_amount"]),
        buy_amount_minor=to_minor(body["buy_amount"]), client_rate=Decimal(str(body["client_rate"])),
        quote_id=body.get("quote_id"), raw=body)


def _transfer(body: dict) -> TransferResult:
    amount = to_minor(body.get("transfer_amount", 0))
    return TransferResult(
        transfer_id=body["id"], status=body["status"], source_currency=body.get("source_currency", ""),
        transfer_currency=body.get("transfer_currency", ""), transfer_amount_minor=amount,
        amount_payer_pays_minor=to_minor(body.get("amount_payer_pays", body.get("source_amount", 0))),
        fee_minor=to_minor(body.get("fee_amount", 0) or 0),
        failure_reason=body.get("failure_reason") or body.get("failure_type"), raw=body)


class Airwallex:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None,
                 max_attempts: int = 3, backoff_s: float = 0.5):
        self.base_url = base_url or env("AIRWALLEX_BASE_URL", SANDBOX_URL)
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=15.0)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s

    @property
    def configured(self) -> bool:
        return bool(env("AIRWALLEX_CLIENT_ID") and env("AIRWALLEX_API_KEY"))

    @property
    def sandbox(self) -> bool:
        return self.base_url.rstrip("/") != PRODUCTION_URL

    async def close(self) -> None:
        await self._client.aclose()

    # -- transport ---------------------------------------------------------

    async def _auth_headers(self) -> dict[str, str]:
        if not self.configured:
            raise AirwallexError("Airwallex credentials not configured")
        async with self._lock:
            now = datetime.now(UTC).timestamp()
            if not self._token or now >= self._expires_at - 60:
                res = await self._client.post("/api/v1/authentication/login", headers={
                    "x-client-id": env("AIRWALLEX_CLIENT_ID"), "x-api-key": env("AIRWALLEX_API_KEY")})
                if res.status_code >= 400:
                    raise AirwallexError("authentication failed", status=res.status_code)
                body = res.json()
                self._token = body["token"]
                self._expires_at = _parse_time(body["expires_at"]).timestamp()
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None):
        """Send with retries. Safe for writes because callers always pass a fixed request_id."""
        last: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                res = await self._client.request(method, path, json=json, params=params,
                                                 headers=await self._auth_headers())
            except httpx.TransportError as exc:
                last = AirwallexError(f"transport error: {type(exc).__name__}")
            else:
                if res.status_code == 401 and attempt == 0:
                    self._token = None
                    continue
                if res.status_code in RETRYABLE_STATUS:
                    last = AirwallexError(f"HTTP {res.status_code}", status=res.status_code)
                elif res.status_code >= 400:
                    try:
                        err = res.json()
                    except ValueError:
                        err = {}
                    raise AirwallexError(err.get("message", f"HTTP {res.status_code}"),
                                         status=res.status_code, code=err.get("code"))
                else:
                    return res.json()
            await asyncio.sleep(self._backoff_s * (2 ** attempt))
        raise last or AirwallexError("request failed")

    # -- read tools ----------------------------------------------------------

    async def balances_minor(self) -> dict[str, int]:
        """Available cash per currency (account_type=cash), in minor units."""
        rows = await self._request("GET", "/api/v1/balances/current")
        return {r["currency"]: to_minor(r.get("available_amount", 0))
                for r in rows if r.get("account_type", "cash") == "cash"}

    async def rate_to_base(self, currency: str, base: str) -> Decimal:
        """Indicative base units per 1 unit of `currency` (not a quote)."""
        body = await self._request("GET", "/api/v1/fx/rates/current",
                                   params={"buy_currency": currency, "sell_currency": base})
        rate, pair = Decimal(str(body["rate"])), body.get("currency_pair", f"{currency}{base}")
        if pair == f"{currency}{base}":
            return rate
        if pair == f"{base}{currency}":
            return 1 / rate
        raise AirwallexError(f"unexpected currency pair {pair}")

    async def get_transfer(self, transfer_id: str) -> TransferResult:
        return _transfer(await self._request("GET", f"/api/v1/transfers/{transfer_id}"))

    async def get_conversion(self, conversion_id: str) -> ConversionResult:
        return _conversion(await self._request("GET", f"/api/v1/fx/conversions/{conversion_id}"))

    async def find_transfer(self, request_id: str) -> TransferResult | None:
        """Recover a transfer whose create response was lost (lookup by our request_id)."""
        body = await self._request("GET", "/api/v1/transfers", params={"request_id": request_id})
        items = body.get("items", []) if isinstance(body, dict) else body
        return _transfer(items[0]) if items else None

    async def find_conversion(self, request_id: str) -> ConversionResult | None:
        body = await self._request("GET", "/api/v1/fx/conversions", params={"request_id": request_id})
        items = body.get("items", []) if isinstance(body, dict) else body
        return _conversion(items[0]) if items else None

    async def list_global_accounts(self) -> list[dict]:
        body = await self._request("GET", "/api/v1/global_accounts")
        return body.get("items", body) if isinstance(body, dict) else body

    # -- write tools ---------------------------------------------------------

    async def create_quote(self, *, sell_currency: str, buy_currency: str, buy_amount_minor: int,
                           validity: str = "MIN_15") -> Quote:
        body = await self._request("POST", "/api/v1/fx/quotes/create", json={
            "sell_currency": sell_currency, "buy_currency": buy_currency,
            "buy_amount": to_major(buy_amount_minor), "validity": validity})
        return Quote(quote_id=body["quote_id"], sell_currency=body["sell_currency"], buy_currency=body["buy_currency"],
                     sell_amount_minor=to_minor(body["sell_amount"]), buy_amount_minor=to_minor(body["buy_amount"]),
                     client_rate=Decimal(str(body["client_rate"])), valid_to_at=_parse_time(body.get("valid_to_at")),
                     usage=body.get("usage", "SINGLE_USE"), raw=body)

    async def create_conversion(self, *, request_id: str, quote: Quote) -> ConversionResult:
        return _conversion(await self._request("POST", "/api/v1/fx/conversions/create", json={
            "request_id": request_id, "quote_id": quote.quote_id, "sell_currency": quote.sell_currency,
            "buy_currency": quote.buy_currency, "buy_amount": to_major(quote.buy_amount_minor)}))

    async def create_beneficiary(self, payload: dict) -> str:
        """`payload` = {"beneficiary": {...}, "transfer_methods": [...], "nickname": ...}. Returns id."""
        body = await self._request("POST", "/api/v1/beneficiaries/create", json=payload)
        return body["id"]

    async def create_transfer(self, *, request_id: str, beneficiary_id: str, currency: str, amount_minor: int,
                              transfer_method: str, reference: str, reason: str) -> TransferResult:
        return _transfer(await self._request("POST", "/api/v1/transfers/create", json={
            "request_id": request_id, "beneficiary_id": beneficiary_id,
            "source_currency": currency, "transfer_currency": currency, "transfer_amount": to_major(amount_minor),
            "transfer_method": transfer_method, "reference": reference[:140], "reason": reason,
            "fee_paid_by": "PAYER"}))

    # -- sandbox simulation --------------------------------------------------

    def _require_sandbox(self) -> None:
        if not self.sandbox:
            raise AirwallexError("simulation endpoints are sandbox-only")

    async def simulate_deposit(self, *, global_account_id: str, amount_minor: int, payer_name: str) -> dict:
        self._require_sandbox()
        return await self._request("POST", "/api/v1/simulation/deposit/create", json={
            "global_account_id": global_account_id, "amount": to_major(amount_minor), "payer_name": payer_name})

    async def simulate_transfer_transition(self, transfer_id: str, next_status: str,
                                           failure_type: str | None = None) -> TransferResult:
        self._require_sandbox()
        body = {"next_status": next_status} | ({"failure_type": failure_type} if failure_type else {})
        return _transfer(await self._request("POST", f"/api/v1/simulation/transfers/{transfer_id}/transition", json=body))
