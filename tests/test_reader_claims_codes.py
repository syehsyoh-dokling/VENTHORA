# ruff: noqa: E501
"""A charge code (OFFICE.PROJECT.ACCOUNT) is never read as an invoice number, a staff claim keeps the charge code it
quotes, and a common word names the category (a taxi is travel), always quoted from the text."""
from venthora.intake import claims
from venthora.intake.pipeline import CHARGE_CODE_RE, _invoice


def test_a_charge_code_is_not_an_invoice_number():
    text = "I paid a taxi on 3 November 2026: USD 46.50. Charge code US-200.OPS.6400."
    assert _invoice(text, "Expense claim: taxi") is None
    assert CHARGE_CODE_RE.search(text).group("code") == "US-200.OPS.6400"
    assert _invoice("Invoice LD-78 for the October work, charge US-200.BRAND26.6600.", "Invoice LD-78").value == "LD-78"


def test_a_word_names_the_category_and_is_quoted():
    cats = ["Payroll", "Logistics", "Suppliers", "Contractors", "Software", "Travel", "Office", "Marketing"]
    q = claims.category("I paid a taxi to the client workshop.", "Expense claim", cats)
    assert q.value == "Travel" and q.quote == "taxi"
    assert claims.category("Hotel two nights", "", cats).value == "Travel"
    assert claims.category("Courier to the bank", "", cats).value == "Logistics"
    assert claims.category("Lunch with a friend", "", cats) is None  # nothing guessed
    assert claims.category("taxi", "", ["Payroll"]) is None  # only categories the standards have
    assert claims.category("Travel: taxi", "", cats).quote == "Travel"  # the category's own name wins
