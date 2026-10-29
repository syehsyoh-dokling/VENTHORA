"""The demo's review cases reach their expected outcome through the inbound mail webhook, before anyone looks."""
import datetime as dt

from fastapi.testclient import TestClient

from venthora.testing.review_cases import cases, judge, postmark


def test_every_case_lands_where_expected(monkeypatch):
    from venthora.api import app
    monkeypatch.setenv("VENTHORA_INBOUND_EMAIL_SECRET", "s3cret-for-tests")
    stamp = "900001"  # numeric, like the live runner's HHMMSS stamp (an invoice number is letters, a dash, digits)
    with TestClient(app) as client:
        got = {}
        for case in cases(stamp, dt.date(2026, 10, 24)):
            r = client.post("/webhooks/email", json=postmark(case, stamp), auth=("postmark", "s3cret-for-tests"))
            assert r.status_code == 200, (case["id"], r.text)
            got[case["id"]] = r.json()["intake_id"]
        queue = {x["intake_id"]: x for x in client.get("/v1/intake").json()}
        problems = {c["id"]: judge(c, queue[got[c["id"]]]) for c in cases(stamp, dt.date(2026, 10, 24))}
        reasons = {c["id"]: queue[got[c["id"]]]["reasons"] for c in cases(stamp, dt.date(2026, 10, 24)) if problems[c["id"]]}
        assert {k: v for k, v in problems.items() if v} == {}, reasons
        assert queue[got["ready-lumen"]]["installment"] == "partial" and queue[got["ready-lumen"]]["attachments"][0]["read"]
        assert queue[got["expense"]]["approvers"] == ["u-omar"]  # Amira is the UK lead herself; the CFO approves
