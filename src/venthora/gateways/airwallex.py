# ruff: noqa: E501
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


ISSUING_PATHS = ("/api/v1/issuing/", "/api/v1/simulation/issuing/")


class AirwallexError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.fields: list[str] = []  # the fields a schema refusal names (details.errors[].source)


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
    mid_rate: Decimal | None = None  # the mid-market rate of the pair (the measure of the exchange's real cost)
    currency_pair: str | None = None  # XY = Y per X, as Airwallex quotes it (EURUSD, USDIDR)

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
    failure_type: str | None = None  # Airwallex's typed failure code (e.g. BENEFICIARY_BANK_RETURNED)
    source_amount_minor: int | None = None  # what left the source wallet before the fee (differs when paid through)

    @property
    def funded_minor(self) -> int:
        """The amount taken from the source wallet for the payout itself, fee excluded, in source_currency."""
        return self.transfer_amount_minor if self.source_amount_minor is None else self.source_amount_minor

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TRANSFER_STATUSES


def _conversion(body: dict) -> ConversionResult:
    return ConversionResult(
        conversion_id=body["conversion_id"], status=body["status"], sell_currency=body["sell_currency"],
        buy_currency=body["buy_currency"], sell_amount_minor=to_minor(body["sell_amount"]),
        buy_amount_minor=to_minor(body["buy_amount"]), client_rate=Decimal(str(body["client_rate"])),
        quote_id=body.get("quote_id"), raw=body)


# Airwallex failure codes -> typed failure (docs: "Simulate transfer status transition", failure_type table).
# Observed on the real sandbox (23 Oct 2026): a transfer failed with BENEFICIARY_BANK_RETURNED is returned as
# status CANCELLED with failure = {"code": "90802", "details": {"type": "INCORRECT_ROUTING"}} and no top-level
# failure_type; the transition response itself carries failure_type = the *detail* type. The code is the
# authoritative field, the detail type only a fallback.
FAILURE_CODES = {
    "90101": "INVALID_ACCOUNT_NAME_OR_NUMBER", "90201": "INVALID_BANK_OR_BRANCH_CODE", "90202": "INVALID_SWIFT_BIC_CODE",
    "90203": "INVALID_CORRESPONDENT_BANK_INFORMATION", "90204": "INVALID_BANK_INFORMATION",
    "90301": "BENEFICIARY_NAME_MISMATCH", "90302": "ACCOUNT_CURRENCY_MISMATCH", "90401": "INVALID_BENEFICIARY_DETAILS",
    "90402": "INVALID_SPECIAL_CHARACTER", "90501": "CHANNEL_POLICY", "90502": "TM_SUSPENDED", "90701": "ACCOUNT_CLOSED",
    "90702": "ACCOUNT_INACTIVE_OR_DORMANT", "90703": "ACCOUNT_UNDER_RESTRICTION", "90801": "BENEFICIARY_REQUESTED",
    "90802": "BENEFICIARY_BANK_RETURNED", "90901": "INBOUND_BENFICIARY_VALIDATION_ERROR", "90902": "INBOUND_ORDER_ERROR",
    "90903": "INBOUND_2B_CARD_NOT_SUPPORTED", "90904": "INBOUND_COMPANY_REGISTRATION_NUMBER_ERROR",
    "91001": "RECALL_REQUESTED", "91002": "CLIENT_REQUESTED", "91101": "INVALID_PAYMENT_PURPOSE", "91201": "CARD_ISSUER_ERROR",
    "91401": "SYSTEM_ERROR", "91402": "CHANNEL_TIMEOUT", "99902": "OTHER",
}


def failure_type_of(body: dict) -> str | None:
    """The typed failure of a transfer body: failure.code mapped through the table, else the typed fields."""
    failure = body.get("failure") or {}
    code = str(failure.get("code") or "").strip()
    if code in FAILURE_CODES:
        return FAILURE_CODES[code]
    reason = str(body.get("failure_reason") or "")  # the guide: "90802: Beneficiary bank returned"
    if reason[:5] in FAILURE_CODES:
        return FAILURE_CODES[reason[:5]]
    return body.get("failure_type") or (failure.get("details") or {}).get("type") or None


def _transfer(body: dict) -> TransferResult:
    amount = to_minor(body.get("transfer_amount", 0))
    failure = body.get("failure") or {}
    return TransferResult(
        transfer_id=body["id"], status=body["status"], source_currency=body.get("source_currency", ""),
        transfer_currency=body.get("transfer_currency", ""), transfer_amount_minor=amount,
        amount_payer_pays_minor=to_minor(body.get("amount_payer_pays", body.get("source_amount", 0))),
        fee_minor=to_minor(body.get("fee_amount", 0) or 0),
        failure_reason=body.get("failure_reason") or failure.get("message")
        or (f"{failure['code']}: {(failure.get('details') or {}).get('type', '')}".strip(": ") if failure.get("code") else None)
        or body.get("failure_type"),
        raw=body, failure_type=failure_type_of(body),
        source_amount_minor=to_minor(body["source_amount"]) if body.get("source_amount") not in (None, "") else None)


class Airwallex:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None,
                 max_attempts: int = 3, backoff_s: float = 0.5, credentials: tuple[str, str] | None = None):
        self.base_url = base_url or env("AIRWALLEX_BASE_URL", SANDBOX_URL)
        self._fixed = credentials  # a candidate connection under test; None = the connected account, else .env
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=15.0)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s

    def _creds(self) -> tuple[str, str]:
        if self._fixed is not None:
            return self._fixed
        from ..airwallex_settings import credentials

        return credentials()

    def forget_token(self) -> None:
        """The connected account changed: log in again on the next call."""
        self._token, self._expires_at = None, 0.0

    @property
    def configured(self) -> bool:
        cid, key = self._creds()
        return bool(cid and key)

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
                cid, key = self._creds()
                res = await self._client.post("/api/v1/authentication/login", headers={"x-client-id": cid, "x-api-key": key})
                if res.status_code >= 400:
                    raise AirwallexError("authentication failed", status=res.status_code)
                body = res.json()
                self._token = body["token"]
                self._expires_at = _parse_time(body["expires_at"]).timestamp()
        return {"Authorization": f"Bearer {self._token}"}

    def _acting(self) -> str | None:
        if self._fixed is not None:
            return None
        from ..airwallex_settings import acting_for

        return acting_for()

    async def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None, once: bool = False,
                       own: bool = False):
        """Send with retries. Safe for writes because callers always pass a fixed request_id; once=True for a write that
        has none (opening an account): a lost answer is reported, never sent again, so nothing is created twice. When
        VENTHORA works for an account the platform opened, every call carries x-on-behalf-of with that account's ID
        (the platform's own credentials, no key of the account's); own=True is a call about the platform's own accounts."""
        last: Exception | None = None
        obo = None if own else self._acting()
        if not own and self._fixed is None and path.startswith(ISSUING_PATHS):
            from ..airwallex_settings import card_account_for

            obo = card_account_for() or obo  # cards live on the card-issuing account (Platform Setup → Airwallex Cards)
        if not own and obo is None and self._fixed is None:
            from ..airwallex_settings import linked, tenant

            if tenant() is not None and not linked():  # SaaS: the platform's own account is the operator's, never a company's
                raise AirwallexError("this company has no Airwallex account yet: the FCO opens a new one or connects the one "
                                     "it already has in Platform Setup → Airwallex Global Account", status=409, code="no_company_account")
        for attempt in range(self._max_attempts):
            try:
                res = await self._client.request(method, path, json=json, params=params,
                                                 headers=await self._auth_headers() | ({"x-on-behalf-of": obo} if obo else {}))
            except httpx.TransportError as exc:
                last = AirwallexError(f"transport error: {type(exc).__name__}")
                if once:
                    raise last from exc
            else:
                if res.status_code == 401 and attempt == 0:
                    self._token = None
                    continue
                if res.status_code in RETRYABLE_STATUS:
                    last = AirwallexError(f"HTTP {res.status_code}", status=res.status_code)
                    if once:
                        raise last
                elif res.status_code >= 400:
                    try:
                        err = res.json()
                    except ValueError:
                        err = {}
                    msg = err.get("message", f"HTTP {res.status_code}")
                    fields = [f"{e.get('source')} ({e.get('code')})" for e in ((err.get("details") or {}).get("errors") or []) if e.get("source")]
                    if fields:  # a schema refusal names the fields; the message alone only says "refer to the errors object"
                        msg = f"{msg} Fields: {', '.join(fields[:12])}"
                    exc = AirwallexError(msg, status=res.status_code, code=err.get("code"))
                    exc.fields = [e.get("source") for e in ((err.get("details") or {}).get("errors") or []) if e.get("source")]
                    raise exc
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

    async def balances_detail(self) -> dict[str, dict[str, int]]:
        """Per currency, in minor units: available (spendable now), pending (arrived, not yet settled), reserved (held
        for payouts in progress) and total, as Airwallex reports them."""
        rows = await self._request("GET", "/api/v1/balances/current")
        return {r["currency"]: {k: to_minor(r.get(f"{k}_amount", 0)) for k in ("available", "pending", "reserved", "total")}
                for r in rows if r.get("account_type", "cash") == "cash"}

    async def list_transfers(self, pages: int = 2, page_size: int = 100) -> list[dict]:
        """The most recent transfers with their Airwallex status (newest first, as the API returns them)."""
        out: list[dict] = []
        seen: set = set()
        for page in range(pages):
            body = await self._request("GET", "/api/v1/transfers", params={"page_num": page, "page_size": page_size})
            items = body.get("items", []) if isinstance(body, dict) else body
            new = [t for t in items if t.get("id") not in seen]
            # seen on the sandbox: page_num 0 and 1 return the same page, which counted every transfer twice
            if not new:
                break
            seen |= {t.get("id") for t in new}
            out += new
            if len(items) < page_size or not (isinstance(body, dict) and body.get("has_more", True)):
                break
        return out

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

    async def list_deposits(self) -> list[dict]:
        """Incoming deposits (authoritative status, e.g. SETTLED), newest last as returned."""
        body = await self._request("GET", "/api/v1/deposits")
        return body.get("items", body) if isinstance(body, dict) else body

    async def get_deposit(self, deposit_id: str) -> dict:
        return await self._request("GET", f"/api/v1/deposits/{deposit_id}")

    async def list_global_accounts(self) -> list[dict]:
        body = await self._request("GET", "/api/v1/global_accounts")
        return body.get("items", body) if isinstance(body, dict) else body

    async def create_global_account(self, *, request_id: str, country_code: str, currency: str,
                                    nick_name: str = "VENTHORA receipts") -> dict:
        """Create a Global Account (guide: only when no suitable one exists)."""
        return await self._request("POST", "/api/v1/global_accounts/create", json={
            "request_id": request_id, "country_code": country_code, "nick_name": nick_name,
            "required_features": [{"currency": currency, "transfer_method": "LOCAL"}]})

    # -- accounts opened through this platform (Airwallex connected accounts) ---------------------------------

    # SaaS: the platform holds every company's connected account; a company's VENTHORA sees and acts on its own only.
    @staticmethod
    def _mine(account_id: str) -> None:
        from ..airwallex_settings import owns

        if not owns(account_id):
            raise AirwallexError("not found: that account belongs to no account this company opened", status=404, code="not_this_company")

    @staticmethod
    def _no_platform_wallet() -> None:
        from ..airwallex_settings import tenant

        if tenant() is not None:
            raise AirwallexError("not available on SaaS: the platform's wallet is the operator's, never a company's", status=409,
                                 code="platform_wallet")

    async def list_accounts(self) -> list[dict]:
        from ..airwallex_settings import owns

        body = await self._request("GET", "/api/v1/accounts", own=True)
        items = body.get("items", body) if isinstance(body, dict) else body
        return [a for a in items if owns(str(a.get("id") or ""))]

    async def create_account(self, body: dict) -> dict:
        """Open an account under this platform (status CREATED); Airwallex then verifies it after submit. On SaaS the
        new account is recorded as this company's, so only this company sees it."""
        from ..airwallex_settings import AirwallexStore, tenant

        acct = await self._request("POST", "/api/v1/accounts/create", json=body, once=True, own=True)
        if tenant() is not None and acct.get("id"):
            d = acct.get("account_details") or {}
            name = (d.get("business_details") or {}).get("business_name") or " ".join(
                filter(None, [(d.get("individual_details") or {}).get("first_name"), (d.get("individual_details") or {}).get("last_name")]))
            AirwallexStore().add_own(acct["id"], name or acct["id"], "VENTHORA")
        return acct

    async def get_account(self, account_id: str) -> dict:
        self._mine(account_id)
        return await self._request("GET", f"/api/v1/accounts/{account_id}", own=True)

    async def submit_account(self, account_id: str) -> dict:
        """Hand the account to Airwallex's verification (KYC / KYB); status becomes SUBMITTED."""
        self._mine(account_id)
        return await self._request("POST", f"/api/v1/accounts/{account_id}/submit", json={}, own=True)

    async def update_account(self, account_id: str, body: dict) -> dict:
        """Add the KYB / KYC details Airwallex needs before the account can be submitted."""
        self._mine(account_id)
        return await self._request("POST", f"/api/v1/accounts/{account_id}/update", json=body, own=True)

    async def industry_categories(self) -> list[dict]:
        body = await self._request("GET", "/api/v1/reference/industry_categories", own=True)
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
                     usage=body.get("usage", "SINGLE_USE"), raw=body,
                     mid_rate=Decimal(str(body["mid_rate"])) if body.get("mid_rate") not in (None, "") else None,
                     currency_pair=body.get("currency_pair"))

    async def create_conversion(self, *, request_id: str, quote: Quote) -> ConversionResult:
        return _conversion(await self._request("POST", "/api/v1/fx/conversions/create", json={
            "request_id": request_id, "quote_id": quote.quote_id, "sell_currency": quote.sell_currency,
            "buy_currency": quote.buy_currency, "buy_amount": to_major(quote.buy_amount_minor)}))

    async def beneficiary_schema(self, payload: dict) -> dict:
        """POST /beneficiaries/schema for this corridor (guide: check the schema before building a payload)."""
        return await self._request("POST", "/api/v1/beneficiaries/schema", json={
            "beneficiary": payload["beneficiary"], "transfer_methods": payload["transfer_methods"]})

    async def validate_beneficiary(self, payload: dict) -> None:
        """POST /beneficiaries/validate (guide: validate over REST, not a dry-run flag on create)."""
        await self._request("POST", "/api/v1/beneficiaries/validate", json={
            "beneficiary": payload["beneficiary"], "transfer_methods": payload["transfer_methods"]})

    async def verify_account(self, body: dict) -> dict:
        """POST /beneficiaries/verify_account: does the account exist, and does its holder's name match (bank-side
        Confirmation / Verification of Payee; UK, SEPA countries and Viet Nam). Answers {code, message, details}."""
        return await self._request("POST", "/api/v1/beneficiaries/verify_account", json=body)

    async def create_beneficiary(self, payload: dict) -> str:
        """`payload` = {"beneficiary": {...}, "transfer_methods": [...], "nickname": ...}. Returns id."""
        body = await self._request("POST", "/api/v1/beneficiaries/create", json=payload)
        return body["id"]

    async def create_transfer(self, *, request_id: str, beneficiary_id: str, currency: str, amount_minor: int,
                              transfer_method: str, reference: str, reason: str, source_currency: str | None = None) -> TransferResult:
        """Pay `amount_minor` of `currency`; funded from `source_currency` (default: the same currency). A different
        source means Airwallex converts inside the transfer (a currency it cannot fund a transfer from, e.g. IDR)."""
        return _transfer(await self._request("POST", "/api/v1/transfers/create", json={
            "request_id": request_id, "beneficiary_id": beneficiary_id,
            "source_currency": source_currency or currency, "transfer_currency": currency, "transfer_amount": to_major(amount_minor),
            "transfer_method": transfer_method, "reference": reference[:140], "reason": reason,
            "fee_paid_by": "PAYER"}))

    # -- Issuing: cardholders, virtual cards, remote authorization, card transactions ------------------------

    async def issuing_config(self) -> dict:
        return await self._request("GET", "/api/v1/issuing/config")

    async def update_issuing_config(self, body: dict) -> dict:
        """POST /issuing/config/update (remote_auth: url, enabled, default_action, version; the shared secret is
        returned only here, once)."""
        return await self._request("POST", "/api/v1/issuing/config/update", json=body, once=True)


    async def create_cardholder(self, body: dict) -> dict:
        return await self._request("POST", "/api/v1/issuing/cardholders/create", json=body, once=True)

    async def list_cards(self) -> list[dict]:
        body = await self._request("GET", "/api/v1/issuing/cards", params={"page_num": 0, "page_size": 100})
        return body.get("items", []) if isinstance(body, dict) else body

    async def create_card(self, body: dict) -> dict:
        """POST /issuing/cards/create; the body carries its own request_id, so a retry never issues a second card."""
        return await self._request("POST", "/api/v1/issuing/cards/create", json=body)

    async def get_card(self, card_id: str) -> dict:
        return await self._request("GET", f"/api/v1/issuing/cards/{card_id}")

    async def fund_connected_account(self, *, account_id: str, amount_minor: int, currency: str, reference: str, request_id: str,
                                     reason: str = "travel") -> dict:
        """Connected Account Transfers: the platform's wallet pays a connected account's wallet, same currency, no fee
        (internal; it may be held by transaction monitoring before SETTLED)."""
        self._no_platform_wallet()
        self._mine(account_id)
        return await self._request("POST", "/api/v1/connected_account_transfers/create", own=True, json={
            "amount": f"{Decimal(amount_minor) / 100:.2f}", "currency": currency, "destination": account_id, "reason": reason,
            "reference": reference[:140], "request_id": request_id})

    async def charge_connected_account(self, *, account_id: str, amount_minor: int, currency: str, reference: str, request_id: str,
                                       reason: str = "travel") -> dict:
        """Charges: a connected account's wallet pays the platform's wallet back, same currency (internal)."""
        self._no_platform_wallet()
        self._mine(account_id)
        return await self._request("POST", "/api/v1/charges/create", own=True, json={
            "amount": f"{Decimal(amount_minor) / 100:.2f}", "currency": currency, "source": account_id, "reason": reason,
            "reference": reference[:140], "request_id": request_id})

    async def balances_of(self, account_id: str) -> dict[str, int]:
        """Available balances (minor units) of a connected account, read on its behalf."""
        body = await self._request_for(account_id, "GET", "/api/v1/balances/current")
        items = body if isinstance(body, list) else body.get("items", [])
        return {x["currency"]: round(float(x.get("available_amount") or 0) * 100) for x in items if x.get("available_amount")}

    async def _request_for(self, account_id: str, method: str, path: str, **kw):
        self._mine(account_id)
        old = self._acting
        self._acting = lambda: account_id
        try:
            return await self._request(method, path, **kw)
        finally:
            self._acting = old

    async def card_details(self, card_id: str) -> dict:
        """GET /issuing/cards/{id}/details: the card number, CVV and expiry (sensitive: never stored or shown by VENTHORA;
        used only to simulate a charge typed in by card number on the sandbox)."""
        return await self._request("GET", f"/api/v1/issuing/cards/{card_id}/details")

    async def card_limits(self, card_id: str) -> dict:
        """GET /issuing/cards/{id}/limits: the card's spending limits as Airwallex enforces them, with what remains
        (currency, limits: [{amount, interval, remaining}])."""
        return await self._request("GET", f"/api/v1/issuing/cards/{card_id}/limits")

    async def set_card_status(self, card_id: str, status: str) -> dict:
        """Freeze (INACTIVE), unfreeze (ACTIVE) or close (CLOSED) a card on Airwallex (guide: POST
        /issuing/cards/{id}/update with card_status, to show an agent revoking its own authority mid-run)."""
        if status not in ("ACTIVE", "INACTIVE", "CLOSED"):
            raise ValueError("card_status is ACTIVE, INACTIVE or CLOSED")
        return await self._request("POST", f"/api/v1/issuing/cards/{card_id}/update", json={"card_status": status})

    async def update_card_controls(self, card_id: str, controls: dict) -> dict:
        """POST /issuing/cards/{id}/update with new authorization_controls (a staff wallet's card: its limits follow
        the wallet's balance)."""
        return await self._request("POST", f"/api/v1/issuing/cards/{card_id}/update", json={"authorization_controls": controls})

    async def card_transactions(self, card_id: str | None = None) -> list[dict]:
        params = {"page_num": 0, "page_size": 100} | ({"card_id": card_id} if card_id else {})
        body = await self._request("GET", "/api/v1/issuing/transactions", params=params)
        return body.get("items", []) if isinstance(body, dict) else body

    async def simulate_card_authorization(self, body: dict) -> dict:
        """Sandbox: a merchant charges an issued card (card_id, transaction_amount, transaction_currency,
        merchant_category_code, merchant_info; single_phase to capture at once)."""
        return await self._request("POST", "/api/v1/simulation/issuing/create", json=body, once=True)


    # -- sandbox simulation --------------------------------------------------

    # -- webhook subscriptions (API-created ones are not listed in the web app) -----------------------
    async def list_webhooks(self) -> list[dict]:
        body = await self._request("GET", "/api/v1/webhooks")
        return body.get("items", []) if isinstance(body, dict) else body

    async def create_webhook(self, *, request_id: str, url: str, events: list[str], version: str) -> dict:
        return await self._request("POST", "/api/v1/webhooks/create",
                                   json={"request_id": request_id, "url": url, "events": events, "version": version})

    async def update_webhook(self, webhook_id: str, url: str) -> dict:
        return await self._request("POST", f"/api/v1/webhooks/{webhook_id}/update", json={"url": url})

    def _require_sandbox(self) -> None:
        if not self.sandbox:
            raise AirwallexError("simulation endpoints are sandbox-only")

    async def simulate_deposit(self, *, global_account_id: str, amount_minor: int, payer_name: str) -> dict:
        self._require_sandbox()
        return await self._request("POST", "/api/v1/simulation/deposit/create", json={
            "global_account_id": global_account_id, "amount": to_major(amount_minor), "payer_name": payer_name})

    async def simulate_account_status(self, account_id: str, next_status: str = "ACTIVE") -> dict:
        """Sandbox only: Airwallex's verification of a SUBMITTED account, decided at once (ACTIVE or SUSPENDED)."""
        self._require_sandbox()
        self._mine(account_id)
        return await self._request("POST", f"/api/v1/simulation/accounts/{account_id}/update_status",
                                   json={"next_status": next_status}, own=True)

    async def simulate_transfer_transition(self, transfer_id: str, next_status: str,
                                           failure_type: str | None = None) -> TransferResult:
        self._require_sandbox()
        body = {"next_status": next_status} | ({"failure_type": failure_type} if failure_type else {})
        return _transfer(await self._request("POST", f"/api/v1/simulation/transfers/{transfer_id}/transition", json=body))
