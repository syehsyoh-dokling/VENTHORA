# ruff: noqa: E501
"""Visa Supplier Matching: the X-Pay token covers the query string with its parameters sorted by name (the only form
Visa accepts), a match decides the payment rail (Visa virtual card with high or medium confidence, else bank
transfer), and vendors' answers are kept 30 days; staff accounts are never sent to Visa."""
import asyncio
import hashlib
import hmac
import urllib.parse

import httpx

from venthora import secret_values, visa_sms, visa_vic


class Org:
    def __init__(self):
        self.d = {}

    def get(self, k, i):
        return self.d.get((k, i))

    def _put(self, k, r, by):
        self.d[(k, r["_id"])] = r
        return r


def _keys(monkeypatch):
    monkeypatch.setattr(secret_values, "get", lambda k, env=None: {visa_vic.API_KEY: "KEY123", visa_vic.SHARED_SECRET: "s3cr3t"}.get(k, ""))


def _visa(seen, answer):
    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.query.decode()
        ts, digest = req.headers["x-pay-token"].split(":")[1:]
        expected = hmac.new(b"s3cr3t", f"{ts}v1/search{q}{{}}".encode(), hashlib.sha256).hexdigest()
        params = urllib.parse.parse_qsl(q)
        seen.append({"signed": digest == expected, "sorted": [k for k, _ in params] == sorted(k for k, _ in params), "params": dict(params),
                     "path": req.url.path, "body": req.content.decode(), "raw": q})
        return httpx.Response(201, json=answer(dict(params)))
    return handler


def test_search_signs_the_sorted_query_and_reads_the_match(monkeypatch):
    _keys(monkeypatch)
    seen = []
    ans = lambda p: {"matchStatus": "Yes", "matchConfidence": "High", "matchDetails": {"mcc": "5734", "l2": "Y", "l3s": "", "l3li": "", "fleetInd": ""},  # noqa: E731
                     "status": {"statusCode": "SMSAPI000"}}
    m = asyncio.run(visa_sms.search(name="Cloudloom Software LLC", country="US", city="San Francisco", postal="94105",
                                    client=httpx.AsyncClient(transport=httpx.MockTransport(_visa(seen, ans)))))
    s = seen[0]
    assert s["signed"] and s["sorted"] and s["path"] == "/visasuppliermatchingservice/v1/search" and s["body"] == "{}"
    assert s["params"]["supplierCountryCode"] == "840" and s["params"]["apikey"] == "KEY123"  # ISO numeric, as Visa asks
    assert "Cloudloom%20Software%20LLC" in s["raw"] and "+" not in s["raw"]  # %20, not +: Visa refuses a token over +
    assert m["ok"] and m["accepts_visa"] and m["confidence"] == "High" and m["mcc"] == "5734" and m["level2"] and not m["level3"]
    assert visa_sms.rail(m)["rail"] == "card"


def test_the_rail_is_a_transfer_unless_visa_is_confident():
    assert visa_sms.rail({"ok": True, "accepts_visa": True, "confidence": "Low"})["rail"] == "transfer"
    assert visa_sms.rail({"ok": True, "accepts_visa": False, "confidence": None})["rail"] == "transfer"
    assert visa_sms.rail({"ok": False})["rail"] == "transfer"
    assert visa_sms.rail({"ok": True, "accepts_visa": True, "confidence": "Medium"})["rail"] == "card"
    assert asyncio.run(visa_sms.search(name="X", country="ZZ"))["ok"] is False  # an unknown country is never sent


def test_vendors_are_checked_once_per_month_and_staff_never(monkeypatch):
    _keys(monkeypatch)
    seen = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_visa(seen, lambda p: {"matchStatus": "No", "matchDetails": {}})))
    payees = {"ben-a": {"name": "Alpha Ltd", "budget_line": "suppliers"}, "emp-x": {"name": "Staff", "kind": "employee", "budget_line": "payroll"}}
    bens = {"ben-a": {"payload": {"beneficiary": {"address": {"country_code": "GB", "city": "London", "postcode": "E1 6AN"}}}}}
    org = Org()
    rows = asyncio.run(visa_sms.check_all(org, payees, beneficiaries=bens, client=client))
    assert [r["vendor_id"] for r in rows] == ["ben-a"] and rows[0]["rail"]["rail"] == "transfer"
    assert seen[0]["params"]["supplierCountryCode"] == "826" and seen[0]["params"]["supplierCity"] == "London"
    again = asyncio.run(visa_sms.check_all(org, payees, beneficiaries=bens, client=client))
    assert len(seen) == 1 and again[0]["cached"] is True  # kept, not asked again
    asyncio.run(visa_sms.check_all(org, payees, beneficiaries=bens, fresh=True, client=client))
    assert len(seen) == 2
