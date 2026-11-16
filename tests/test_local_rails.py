# ruff: noqa: E501
"""Local Rails: only what Airwallex, Visa and Alta do not cover in Indonesia; Xendit and Midtrans products the
Finance Director turns on or off; Midtrans keys sandbox only and write-only; notifications verified by signature."""
import hashlib

import httpx
import pytest

from venthora import access, secret_values
from venthora import local_rails as lr


class Org:
    def __init__(self):
        self.rec = {}

    def get(self, kind, _id):
        return self.rec.get((kind, _id))

    def _put(self, kind, rec, by):
        self.rec[(kind, rec["_id"])] = rec


@pytest.fixture(autouse=True)
def _secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("VENTHORA_SECRETS_DB", str(tmp_path / "s.db"))
    for k in ("MIDTRANS_SERVER_KEY", "MIDTRANS_CLIENT_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_every_gap_points_at_a_product_and_nothing_already_covered_is_offered():
    keys = {(pv, p["key"]) for pv, d in lr.PROVIDERS.items() for p in d["products"]}
    assert {(g["provider"], g["product"]) for g in lr.GAPS} == keys
    labels = " ".join(p["label"].lower() for d in lr.PROVIDERS.values() for p in d["products"])
    for covered in ("bank transfer", "card", "conversion", "fx"):
        assert covered not in labels


def test_active_products_on_by_default_and_choices_kept():
    org = Org()
    assert lr.enabled(org, "xendit", "ewallet_payout") and lr.enabled(org, "xendit", "name_check")
    assert not lr.enabled(org, "midtrans", "qris")
    lr.save_products(org, {"xendit": {"ewallet_payout": False}, "midtrans": {"qris": True}}, "fd")
    assert not lr.enabled(org, "xendit", "ewallet_payout") and lr.enabled(org, "midtrans", "qris")
    with pytest.raises(ValueError):
        lr.save_products(org, {"midtrans": {"bank_transfer": True}}, "fd")


def test_midtrans_keys_are_sandbox_only_and_never_shown():
    org = Org()
    with pytest.raises(ValueError):
        lr.save_midtrans(org, "sandbox", "dev")  # no server key yet
    with pytest.raises(ValueError):
        lr.save_midtrans(org, "sandbox", "dev", server_key="Mid-server-production")
    s = lr.save_midtrans(org, "sandbox", "dev", merchant_id="g123456", server_key="SB-Mid-server-abcdef123", client_key="SB-Mid-client-xyz98765")
    assert s["mode"] == "sandbox" and s["merchant_id"] == "G123456"
    assert s["server_key"]["set"] and "abcdef" not in str(s)


def test_connection_test_reads_404_as_a_valid_key_and_401_as_refused():
    secret_values.put(lr.MT_SERVER, "SB-Mid-server-abc", "dev")
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status_code": "404", "status_message": "Transaction doesn't exist."})))
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"status_code": "401"})))
    assert lr.midtrans_test(ok)["ok"] is True
    assert lr.midtrans_test(bad)["ok"] is False


def test_notification_signature():
    secret_values.put(lr.MT_SERVER, "SB-Mid-server-abc", "dev")
    n = {"order_id": "INV-1", "status_code": "200", "gross_amount": "150000.00"}
    n["signature_key"] = hashlib.sha512(b"INV-1200150000.00SB-Mid-server-abc").hexdigest()
    assert lr.midtrans_signature_ok(n)
    assert not lr.midtrans_signature_ok(n | {"gross_amount": "1.00"})


def test_a_notification_signed_with_another_merchant_key_is_read_back_from_midtrans():
    secret_values.put(lr.MT_SERVER, "SB-Mid-server-abc", "dev")
    n = {"order_id": "INV-1", "status_code": "200", "gross_amount": "15000.00", "transaction_status": "settlement",
         "signature_key": hashlib.sha512(b"INV-120015000.00SB-Mid-server-OTHER").hexdigest()}
    seen = []

    def midtrans(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"status_code": "200", "order_id": "INV-1", "transaction_status": "settlement", "gross_amount": "15000.00"})

    v = lr.midtrans_verify(n, httpx.Client(transport=httpx.MockTransport(midtrans)))
    assert v == {"ok": True, "how": "read back from Midtrans", "reason": None} and seen[0].endswith("/v2/INV-1/status")
    other = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status_code": "201", "order_id": "INV-1", "transaction_status": "pending", "gross_amount": "15000.00"})))
    assert not lr.midtrans_verify(n, other)["ok"]  # the body says settled, Midtrans says pending: refused
    missing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"status_code": "404"})))
    assert "does not exist" in lr.midtrans_verify(n, missing)["reason"]
    assert "order id" in lr.midtrans_verify(n | {"order_id": "../../v2/charge"})["reason"]  # never a path of the caller's choosing
    good = n | {"signature_key": hashlib.sha512(b"INV-120015000.00SB-Mid-server-abc").hexdigest()}
    assert lr.midtrans_verify(good)["how"] == "signature"  # no call to Midtrans needed


def test_keys_are_the_developers_products_the_fcos():
    assert access.check("super_admin", "PUT", "/v1/providers/midtrans")[0] in (403, 404)
    assert access.check("developer", "PUT", "/v1/local-rails/products")[0] == 403
    assert access.check("super_admin", "PUT", "/v1/local-rails/products") is None
