# ruff: noqa: E501
"""Visa Foreign Exchange Rates: numeric ISO codes, the X-Pay signature over the path after the product and the body,
one retry when the sandbox answers 500, and Visa's conversionRate returned as is."""
import asyncio
import hashlib
import hmac
import json

import httpx

from venthora import secret_values, visa_vic


def test_the_fx_call_is_signed_and_retried_once(monkeypatch):
    monkeypatch.setattr(secret_values, "get", lambda k, env=None: {visa_vic.API_KEY: "KEY123", visa_vic.SHARED_SECRET: "s3cr3t"}.get(k, ""))
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        ts, digest = req.headers["x-pay-token"].split(":")[1:]
        expected = hmac.new(b"s3cr3t", f"{ts}v2/foreignexchangerates{req.url.query.decode()}{body}".encode(), hashlib.sha256).hexdigest()
        calls.append((json.loads(body), digest == expected, req.url.path))
        if len(calls) == 1:
            return httpx.Response(500, json={"status": "INTERNAL_SERVER_ERROR", "code": "NPPS003"})
        return httpx.Response(200, json={"conversionRate": "1.626567", "destinationAmount": "162.66"})

    r = asyncio.run(visa_vic.fx_rate("GBP", "USD", "100", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert r["ok"] and r["conversion_rate"] == "1.626567" and len(calls) == 2
    sent, signed, path = calls[-1]
    assert sent == {"rateProductCode": "A", "sourceCurrencyCode": "826", "destinationCurrencyCode": "840", "sourceAmount": "100"}
    assert signed and path == "/forexrates/v2/foreignexchangerates"
    assert asyncio.run(visa_vic.fx_rate("GBP", "XXX"))["ok"] is False
