# ruff: noqa: E501
"""The test log shown on Demo Day: a sentence that says what happened, no noise, no link token."""
from venthora import testlog


def test_the_steps_of_the_new_flows_get_their_own_sentence():
    line = testlog.own_line
    assert line("POST", "/v1/intake/emails", {}, {"flags": ["x", "fund_request:CA-2026-0003"]}, 200) == "Email · became fund request CA-2026-0003"
    assert line("POST", "/v1/intake/emails", {}, {"flags": ["purchase_request:PR-UJI-0001"]}, 200) == "Email · became purchase request PR-UJI-0001"
    assert line("POST", "/v1/intake/emails", {}, {"flags": ["fund_request_rejected: not an organisation email"]}, 200).startswith("Email · fund request rejected")
    assert line("POST", "/v1/intake/emails", {}, {"flags": ["po_unknown"]}, 200) is None  # an ordinary invoice: the general description
    got = line("POST", "/v1/procurement/PR-1/demo-replies", {}, {"submitted": [1, 2, 3, 4, 5], "decision": {"action": "award", "vendor": "Ergo", "po_id": "PO-2026-0206"}}, 200)
    assert got == "Procurement · PR-1: 5 demo vendor(s) sent a quotation; awarded to Ergo · PO-2026-0206"
    assert line("PUT", "/v1/procurement/settings", {}, {"po_threshold_minor": 50_000, "po_threshold_currency": "USD"}, 200) == "Procurement · PO threshold set to USD 500.00"
    assert line("PUT", "/v1/continuity/safe-mode", {}, {"on": True, "reason": "wrong account"}, 200) == "Continuity · safe mode ON: wrong account"
    assert line("POST", "/v1/fund-requests/CA-1/cancel", {}, {"status": "CANCELLED"}, 200) == "Cash advance · CA-1 cancelled before it was paid"
    assert line("POST", "/v1/fund-requests/CA-1/cancel", {}, {"detail": "no"}, 403) is None


def test_noise_is_left_out_and_link_tokens_are_never_written():
    assert "/webhooks/ses" in testlog.SKIP
    assert testlog.tab_of("PUT", "/v1/continuity/safe-mode") == "Continuity" and testlog.tab_of("POST", "/card-link/abc") == "Advances & cards"
    assert testlog.channel_of("/card-link/abc.def", {}, None) == "card link"

    class Org:
        def __init__(self):
            self.rows = []

        def get(self, kind, rid):
            return {"_id": "session", "active": True, "run": "r", "scenario": "s"} if rid == "session" else None

        def list(self, kind):
            return []

        def _put(self, kind, rec, by):
            self.rows.append(rec)

    org = Org()
    testlog.record(org, method="POST", path="/card-link/Q0EtMjAyNi0wMDAx.0123456789abcdef", query="", status=200, ms=3, role=None, by=None, channel="card link", body=b"", ctype="text/html")
    assert org.rows and org.rows[0]["path"] == "/card-link/••••" and "0123456789abcdef" not in str(org.rows[0])
