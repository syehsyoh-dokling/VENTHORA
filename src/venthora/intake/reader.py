# ruff: noqa: E501
"""The model reads an invoice or a claim; code decides what it may keep.

Invoices arrive in every layout, so a language model (Qwen2.5-0.5B-Instruct by default, any OpenAI-compatible
endpoint) is asked for the amount due, the invoice number and the due date, each with the exact words it read
them from. Nothing the model says is trusted:

* the quote must occur verbatim in the email or its PDF text (whitespace aside), or the field is dropped
* the value must be in the quote: the amount's digits, the invoice number's characters, the due date's day
* the currency must be one the intake knows; dates must parse and fall within two years of the email

What survives becomes a proposal. The rules still read every field; the model only fills what the rules missed,
and a field both read differently is flagged for a person (see Intake.ingest). Without MODEL_ENDPOINT the reader
does nothing and the rules alone decide, exactly as before.
"""
from __future__ import annotations

import datetime as dt
import re

from ..evidence.llm import ChatModel, LLMUnavailable, first_json_object
from .models import Quote

PROMPT = """You read one email (with any PDF attachment text) that asks a company to pay money.
Return only a JSON object with these keys, each null when the text does not state it:
  "amount": {"value": "<number as written>", "currency": "USD|EUR|GBP", "quote": "<exact words from the text>"}
  "invoice_no": {"value": "<invoice or claim reference>", "quote": "<exact words from the text>"}
  "due_date": {"value": "YYYY-MM-DD", "quote": "<exact words from the text>"}
"amount" is the total to be paid (the amount due), not a line item or a rate.
Every "quote" must be copied exactly from the text below. Do not guess."""

CURRENCY = {"USD": "USD", "US$": "USD", "$": "USD", "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _find(quote: str, text: str) -> tuple[int, int] | None:
    """Where the quote occurs in the text, whitespace-insensitive."""
    q = _norm(quote)
    if len(q) < 3:
        return None
    pattern = r"\s+".join(re.escape(part) for part in q.split(" "))
    m = re.search(pattern, text)
    return (m.start(), m.end()) if m else None


def _minor(value: str) -> int | None:
    digits = re.sub(r"[^\d.,]", "", value or "")
    if not digits:
        return None
    if "," in digits and "." in digits and digits.rfind(",") > digits.rfind("."):  # 1.234,56
        digits = digits.replace(".", "").replace(",", ".")
    else:
        digits = digits.replace(",", "")
    try:
        minor = int(round(float(digits) * 100))
    except ValueError:
        return None
    return minor if minor > 0 else None


def ground(reply: dict, text: str, received: dt.date) -> dict[str, Quote]:
    """Keep only what the text itself proves; the reason each field was dropped is not needed by the caller."""
    out: dict[str, Quote] = {}
    a = reply.get("amount")
    if isinstance(a, dict) and isinstance(a.get("quote"), str):
        where = _find(a["quote"], text)
        minor, cur = _minor(str(a.get("value") or "")), CURRENCY.get(str(a.get("currency") or "").upper())
        if where and minor and cur:
            quoted = text[where[0]:where[1]]
            if any(_minor(n) == minor for n in re.findall(r"\d[\d.,]*\d|\d", quoted)):
                out["amount"] = Quote(field="amount", value=f"{cur} {minor}", quote=quoted, start=where[0], end=where[1])
    inv = reply.get("invoice_no")
    if isinstance(inv, dict) and isinstance(inv.get("quote"), str) and isinstance(inv.get("value"), str):
        where = _find(inv["quote"], text)
        value = inv["value"].strip().upper()
        if where and 2 <= len(value) <= 40 and value in text[where[0]:where[1]].upper():
            out["invoice_no"] = Quote(field="invoice_no", value=value, quote=text[where[0]:where[1]], start=where[0], end=where[1])
    due = reply.get("due_date")
    if isinstance(due, dict) and isinstance(due.get("quote"), str) and isinstance(due.get("value"), str):
        where = _find(due["quote"], text)
        try:
            d = dt.date.fromisoformat(due["value"].strip())
        except ValueError:
            d = None
        if where and d and abs((d - received).days) <= 730 and re.search(rf"\b0?{d.day}\b", text[where[0]:where[1]]):
            out["due_date"] = Quote(field="due_date", value=d.isoformat(), quote=text[where[0]:where[1]], start=where[0], end=where[1])
    return out


async def read(text: str, subject: str, received: dt.date, model: ChatModel | None = None) -> dict[str, Quote]:
    """The model's grounded proposals, or {} when no model is configured or it fails."""
    if model is None:
        from ..ai_settings import active_model  # Settings → AI model, else .env

        model = active_model()
    if not model.configured:
        return {}
    doc = f"Subject: {subject}\n\n{text}"[:6000]
    try:
        reply = await model.complete([{"role": "system", "content": PROMPT}, {"role": "user", "content": doc}], max_tokens=300)
        return ground(first_json_object(reply), text, received)
    except (LLMUnavailable, ValueError):
        return {}
