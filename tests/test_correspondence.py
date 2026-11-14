"""Review form → correspondence: what was sent to the vendor and when, where the vendor's answer stands, the vendor's
other submissions that were refused again, and the notices sent to the FCO with their text."""
from types import SimpleNamespace as NS

from venthora import correspondence as co


def req(i, at, status="NEEDS_REVIEW", inv="INV-9", **kw):
    base = dict(intake_id=i, received_at=at, status=status, invoice_no=inv, invoice_fingerprint=f"v1:{inv}", counterparty_id="v1", sender="ar@v1.example",
                duplicate_of=None, subject=f"Invoice {inv}", amount_minor=1000, currency="USD", dunning_level=0, reasons=["no PO quoted"], reviewer=None,
                review_note=None, payment=None)
    return NS(**(base | kw))


def msg(i, ref, at, kind="vendor_notice", status="sent", body="Please quote the PO."):
    return {"_id": i, "kind": kind, "ref": ref, "to": ["ar@v1.example"], "subject": "s", "body": body, "status": status, "created_at": at,
            "sent_at": at if status == "sent" else None, "created_by": "Fitri", "error": None, "attempts": 1}


def test_nothing_sent_then_waiting_then_refused_again_then_accepted():
    a = req("a", "2026-11-10T08:00:00")
    assert co.build(a, [a], [])["feedback"]["state"] == "none"
    out = [msg("m1", "REQ-INV-9", "2026-11-10T09:00:00"), msg("f1", "needs_review:a", "2026-11-10T08:05:00", kind="fco_notice", body="Dear FCO"),
           msg("x", "REQ-OTHER", "2026-11-10T09:00:00")]
    d = co.build(a, [a], out)
    assert d["feedback"]["state"] == "waiting" and [m["id"] for m in d["vendor_messages"]] == ["m1"]
    assert [m["id"] for m in d["fco_messages"]] == ["f1"] and d["fco_messages"][0]["body"] == "Dear FCO"
    b = req("b", "2026-11-11T08:00:00", status="DUPLICATE", duplicate_of="a", reasons=["already in the queue"])
    d = co.build(a, [a, b, req("z", "2026-11-11T08:00:00", inv="INV-OTHER")], out)
    assert d["feedback"]["state"] == "refused_again" and "duplicate" in d["feedback"]["text"]
    assert [s["intake_id"] for s in d["submissions"]] == ["b"] and d["refused"] == 1
    c = req("c", "2026-11-12T08:00:00", status="VERIFIED", reasons=["PO matched"])
    assert co.build(a, [a, b, c], out)["feedback"]["state"] == "accepted"


def test_a_message_that_bounced_is_not_counted_as_received():
    a = req("a", "2026-11-10T08:00:00")
    d = co.build(a, [a], [msg("m1", "REQ-INV-9", "2026-11-10T09:00:00", status="bounced")])
    assert d["feedback"]["state"] == "undelivered"
