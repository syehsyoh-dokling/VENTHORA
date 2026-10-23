"""In-memory Airwallex sandbox for tests (httpx.MockTransport handler).

Models the behaviors the executor relies on: bearer auth, cash balances in major
units, single-use quotes, request_id de-duplication (`duplicate_request_id`),
EUR 12.85 SWIFT fee, transfer lifecycle via the simulation endpoint, refunds on
failure, and lookup by request_id. Fault hooks let tests lose responses.
"""
from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

import httpx

SWIFT_FEE = {"EUR": Decimal("12.85")}


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class FakeAirwallex:
    def __init__(self, balances: dict[str, str] | None = None, rates: dict[str, str] | None = None):
        self.balances = {c: money(v) for c, v in (balances or {"USD": "20000", "EUR": "0", "GBP": "0"}).items()}
        self.rates = {k: Decimal(v) for k, v in (rates or {"EURUSD": "1.0850", "GBPUSD": "1.2700"}).items()}
        self.quote_markup = Decimal(0)
        self.quotes: dict[str, dict] = {}
        self.conversions: dict[str, dict] = {}  # by request_id
        self.transfers: dict[str, dict] = {}  # by id
        self.transfer_requests: dict[str, str] = {}  # request_id -> id
        self.beneficiaries: dict[str, dict] = {}
        self.deposits: list[dict] = []
        self.lose_response_once: set[str] = set()  # paths whose next response is "lost" after processing
        self.fail_on_paid: set[str] = set()  # transfer references (substring) that fail when moved to PAID
        self.calls: list[tuple[str, str]] = []

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _error(status: int, code: str, message: str = "") -> httpx.Response:
        return httpx.Response(status, json={"code": code, "message": message or code})

    def _rate(self, buy: str, sell: str) -> Decimal:
        """sell units per 1 buy unit."""
        if f"{buy}{sell}" in self.rates:
            return self.rates[f"{buy}{sell}"]
        return 1 / self.rates[f"{sell}{buy}"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if path == "/api/v1/authentication/login":
            return httpx.Response(201, json={"token": "tok", "expires_at": "2099-01-01T00:00:00+0000"})
        if request.headers.get("authorization") != "Bearer tok":
            return self._error(401, "credentials_invalid")
        body = json.loads(request.content) if request.content else {}
        response = self._route(method, path, body, dict(request.url.params))
        if path in self.lose_response_once:
            self.lose_response_once.discard(path)
            raise httpx.ConnectError("connection reset after the server processed the request")
        return response

    def _route(self, method, path, body, params) -> httpx.Response:
        if path == "/api/v1/balances/current":
            return httpx.Response(200, json=[{"account_type": "cash", "currency": c, "available_amount": float(v),
                                              "pending_amount": 0, "reserved_amount": 0, "total_amount": float(v)}
                                             for c, v in self.balances.items()])
        if path == "/api/v1/fx/rates/current":
            buy, sell = params["buy_currency"], params["sell_currency"]
            pair = f"{buy}{sell}" if f"{buy}{sell}" in self.rates else f"{sell}{buy}"
            return httpx.Response(200, json={"rate": float(self.rates[pair]), "currency_pair": pair})
        if path == "/api/v1/fx/quotes/create":
            return self._quote(body)
        if path == "/api/v1/fx/conversions/create":
            return self._convert(body)
        if path == "/api/v1/fx/conversions" and method == "GET":
            c = self.conversions.get(params.get("request_id"))
            return httpx.Response(200, json={"items": [c] if c else [], "has_more": False})
        if path == "/api/v1/beneficiaries/create":
            return self._beneficiary(body)
        if path == "/api/v1/transfers/create":
            return self._transfer(body)
        if path == "/api/v1/transfers" and method == "GET":
            tid = self.transfer_requests.get(params.get("request_id"))
            return httpx.Response(200, json={"items": [self.transfers[tid]] if tid else [], "has_more": False})
        if path.startswith("/api/v1/transfers/") and method == "GET":
            t = self.transfers.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=t) if t else self._error(404, "not_found")
        if path.startswith("/api/v1/simulation/transfers/") and path.endswith("/transition"):
            return self._transition(path.split("/")[-2], body)
        if path == "/api/v1/simulation/deposit/create":
            return self._deposit(body)
        if path == "/api/v1/global_accounts":
            return httpx.Response(200, json={"items": [{"id": "ga-usd-1", "currency": "USD"}], "has_more": False})
        return self._error(404, "invalid_endpoint", path)

    # -- endpoints -------------------------------------------------------------

    def _quote(self, b):
        buy, sell, amount = b["buy_currency"], b["sell_currency"], money(b["buy_amount"])
        rate = self._rate(buy, sell) + self.quote_markup
        qid = f"q-{uuid4()}"
        self.quotes[qid] = q = {"quote_id": qid, "buy_currency": buy, "sell_currency": sell, "buy_amount": float(amount),
                                "sell_amount": float(money(amount * rate)), "client_rate": float(rate),
                                "valid_from_at": "2026-10-23T00:00:00+0000", "valid_to_at": "2099-01-01T00:00:00+0000",
                                "validity": b["validity"], "usage": "SINGLE_USE", "used": False}
        return httpx.Response(201, json={k: v for k, v in q.items() if k != "used"})

    def _convert(self, b):
        rid = b["request_id"]
        if rid in self.conversions:
            return self._error(400, "duplicate_request_id")
        q = self.quotes.get(b.get("quote_id"))
        if q is None:
            return self._error(400, "invalid_argument", "unknown quote")
        if q["used"]:
            return self._error(400, "quote_already_used")
        sell = money(q["sell_amount"])
        if self.balances.get(q["sell_currency"], Decimal(0)) < sell:
            return self._error(400, "insufficient_funds")
        q["used"] = True
        self.balances[q["sell_currency"]] -= sell
        self.balances[q["buy_currency"]] = self.balances.get(q["buy_currency"], Decimal(0)) + money(q["buy_amount"])
        self.conversions[rid] = c = {
            "conversion_id": f"c-{uuid4()}", "request_id": rid, "quote_id": q["quote_id"], "status": "SETTLED",
            "buy_currency": q["buy_currency"], "buy_amount": str(money(q["buy_amount"])),
            "sell_currency": q["sell_currency"], "sell_amount": float(sell), "client_rate": q["client_rate"]}
        return httpx.Response(201, json=c)

    def _beneficiary(self, b):
        if not b.get("transfer_methods") or "beneficiary" not in b:
            return self._error(400, "001", "beneficiary and transfer_methods are required")
        bank = b["beneficiary"].get("bank_details", {})
        if bank.get("account_routing_type1") == "SORT_CODE":
            return self._error(400, "011")
        bid = f"b-{uuid4()}"
        self.beneficiaries[bid] = b
        return httpx.Response(201, json={"id": bid, **b})

    def _transfer(self, b):
        rid = b["request_id"]
        if not 10 <= len(rid) <= 50:
            return self._error(400, "018")
        if rid in self.transfer_requests:
            return self._error(400, "duplicate_request_id")
        if "payment_currency" in b or b.get("beneficiary_id") not in self.beneficiaries:
            return self._error(400, "001")
        ccy, amount = b["source_currency"], money(b["transfer_amount"])
        fee = SWIFT_FEE.get(ccy, Decimal(0)) if b["transfer_method"] == "SWIFT" else Decimal(0)
        if self.balances.get(ccy, Decimal(0)) < amount + fee:
            return self._error(400, "balance_insufficient")
        self.balances[ccy] -= amount + fee
        tid = f"t-{uuid4()}"
        self.transfer_requests[rid] = tid
        self.transfers[tid] = t = {
            "id": tid, "request_id": rid, "status": "PROCESSING", "source_currency": ccy, "transfer_currency": ccy,
            "transfer_amount": float(amount), "source_amount": float(amount), "fee_amount": float(fee),
            "fee_currency": ccy, "amount_payer_pays": float(amount + fee), "transfer_method": b["transfer_method"],
            "reference": b["reference"], "reason": b["reason"], "beneficiary_id": b["beneficiary_id"]}
        return httpx.Response(201, json=t)

    def _transition(self, tid, b):
        t = self.transfers.get(tid)
        if t is None:
            return self._error(404, "not_found")
        nxt = b["next_status"]
        if t["status"] in ("PAID", "CANCELLED"):
            return self._error(400, "invalid_argument", "transfer already final")
        if nxt == "PAID" and t["status"] != "SENT":
            return self._error(400, "invalid_argument", "simulate SENT before PAID")
        if nxt in ("FAILED", "CANCELLED") or (nxt == "PAID" and any(r in t["reference"] for r in self.fail_on_paid)):
            t.update(status="CANCELLED", failure_type=b.get("failure_type", "BENEFICIARY_BANK_RETURNED"),
                     failure_reason="simulation")
            self.balances[t["source_currency"]] += money(t["amount_payer_pays"])  # refund
        else:
            t["status"] = nxt
        return httpx.Response(200, json=t)

    def _deposit(self, b):
        amount = money(b["amount"])
        self.balances["USD"] += amount
        d = {"id": f"d-{uuid4()}", "status": "PENDING", "amount": float(amount), **b}
        self.deposits.append(d)
        return httpx.Response(201, json=d)
