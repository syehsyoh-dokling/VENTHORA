# ruff: noqa: E501
"""What an email is read as before money is promised on it: an approval that is taken back approves nothing, an
amount means the same whoever typed it, and an ordinary sentence is never a reference number."""
import pytest

from venthora import advance_flow, pr_email
from venthora.amounts import parse_amount

FCO = "rosa.lindqvist@acme.example"


def fwd(said):
    return f"Hi Finance, see below.\n\n---------- Forwarded message ---------\nFrom: Rosa Lindqvist <{FCO}>\nSubject: Re: cash advance\nTo: Lee Park\n\n{said}\n"


@pytest.mark.parametrize("said", [
    "Approved. Go ahead.", "Approved", "OK to pay, thanks.", "I approve this advance.", "Disetujui, silakan diproses.",
    "Approved.\n\nOn Mon, 9 Nov 2026 Lee Park wrote:\n> Is this not approved yet?"])
def test_a_plain_approval_approves(said):
    assert advance_flow.forwarded_approval(fwd(said), {FCO})["from"] == FCO


@pytest.mark.parametrize("said", [
    "This is not yet approved, send me the budget first.", "I can't approve this.", "I will not approve this without receipts.",
    "Once approved by the board we can talk.", "Let me think about it.\n\n> Please confirm this is approved", "Is this approved by Omar?",
    "Not approved, the budget is closed.", "Belum disetujui.", "Tidak disetujui.", "Approved only if the trip is confirmed.",
    "Let me think.\n\nOn Mon, 9 Nov 2026 Lee Park wrote:\nMy manager approved this already."])
def test_an_approval_that_is_taken_back_or_is_not_the_fcos_own_words_approves_nothing(said):
    assert advance_flow.forwarded_approval(fwd(said), {FCO}) is None


@pytest.mark.parametrize(("text", "value"), [
    ("USD 1,500", 1500.0), ("15,000", 15000.0), ("1.500,00", 1500.0), ("Rp 1.500.000", 1500000.0), ("2.500", 2500.0), ("600.00", 600.0),
    ("1,5", 1.5), ("1,234,567.89", 1234567.89), ("300", 300.0), ("0.75", 0.75), ("", None), ("about", None)])
def test_an_amount_means_the_same_whoever_typed_it(text, value):
    assert parse_amount(text) == value


def test_both_email_readers_use_the_one_amount_reader():
    from venthora import fund_email

    assert fund_email._num("USD 1,500") == pr_email._num("USD 1,500") == 1500.0


@pytest.mark.parametrize(("fn", "sentence", "want"), [
    (advance_flow.ta_number, "Please arrange TA/DA as usual", None),
    (advance_flow.ta_number, "Travel clearance 2026-11-20 was given by phone", None),
    (advance_flow.ta_number, "Our tc 2500 units arrived", None),
    (advance_flow.ta_number, "travel authorization USD600 requested", None),
    (advance_flow.ta_number, "Travel authorisation approved. TA no 12345 attached", "12345"),
    (advance_flow.ta_number, "Travel Authorisation: TA-2026-0031", "TA-2026-0031"),
    (advance_flow.ta_number, "Travel clearance no. TC/11/204 attached", "TC/11/204"),
    (advance_flow.request_number, "Reference number 4500 USD budget line", None),
    (advance_flow.request_number, "Advance request #600USD for Dana", None),
    (advance_flow.request_number, "Call me, ref # 555-1234", None),
    (advance_flow.request_number, "this request not2late please", None),
    (advance_flow.request_number, "Per purchase request no: PR-2026-0412 I need a cash advance", None),
    (advance_flow.request_number, "Advance request no: CA-2026-0144", "CA-2026-0144"),
    (advance_flow.request_number, "Petty cash number PC/11/007", "PC/11/007"),
    (pr_email.pr_number, "purchase request for PR/marketing team banners", None),
    (pr_email.pr_number, "request for quotation, PR-team will follow", None),
    (pr_email.pr_number, "We request number 250 chairs too", None),
    (pr_email.pr_number, "Please send the request no later than Friday. Purchase request no: 26/OPS/017", "26/OPS/017"),
    (pr_email.pr_number, "Purchase request PR-2026-0412", "PR-2026-0412")])
def test_an_ordinary_sentence_is_never_a_reference_number(fn, sentence, want):
    assert fn(sentence, "") == want
