# ruff: noqa: E501
"""An email from a person of the organisation asking to buy something becomes a purchase request (PR), and the
procurement process starts on it.

1. Only a registered person (People & roles) can raise a PR by email; anyone else's email stays ordinary intake.
2. The AI model (local or hosted, Settings → AI model) reads the email into the PR's fields: what, how many, brand,
   specifications, estimated price, currency, needed by, charge code, justification. Labelled lines in the email
   ("Quantity: 10") win over the model; without a model the labelled lines alone are used.
3. The PR is saved SUBMITTED with today as its request date (the requester's own charge code when the email names none),
   the Request for Quotation draft is kept on it, the candidate vendors are listed, and the RFQ is queued to them in
   the outbox, each with its own form link. Nothing is paid: the award and the PO are a person's decision.
"""
from __future__ import annotations

import re
from datetime import date

KEYWORDS = ("purchase request", "procurement request", "request to buy", "please procure", "please purchase", "pengadaan",
            "permintaan pembelian", "rfq for", "request for quotation")
LABELS = {"item": "title", "title": "title", "barang": "title", "quantity": "quantity", "qty": "quantity", "jumlah": "quantity",
          "unit": "unit", "satuan": "unit", "brand": "brand", "merek": "brand", "estimate": "estimate", "estimated price": "estimate",
          "budget": "estimate", "harga": "estimate", "currency": "currency", "mata uang": "currency", "needed by": "needed_by",
          "dibutuhkan": "needed_by", "tanggal dibutuhkan": "needed_by", "charge code": "charge_code", "kode biaya": "charge_code",
          "justification": "justification", "alasan": "justification"}
PROMPT = """Read this email from a staff member asking the company to buy something, and fill the purchase request.
Answer with one JSON object only:
{{"title": "what is to be bought, short", "quantity": number or null, "unit": "pcs/box/licence/... or empty",
"brand": "a brand only if the email requires one, else empty", "specs": ["one specification per item, as written"],
"estimate": number (the estimated TOTAL price) or null, "currency": "ISO code or empty", "needed_by": "YYYY-MM-DD or empty",
"charge_code": "the charge code if the email gives one, else empty", "justification": "why, one sentence"}}
Use only what the email says; leave a field empty rather than guess.

Subject: {subject}
Email:
{body}"""


def looks_like(subject: str, body: str) -> bool:
    t = f"{subject}\n{body}".lower()
    return any(k in t for k in KEYWORDS)


def _labelled(body: str) -> dict:
    out, specs = {}, []
    for line in body.splitlines():
        m = re.match(r"^\s*[-*]?\s*([A-Za-z][A-Za-z ]{1,24})\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if key in ("spec", "specs", "specification", "spesifikasi"):
            specs += [s.strip() for s in re.split(r";|\|", val) if s.strip()]
        elif key in LABELS:
            out[LABELS[key]] = val
    if specs:
        out["specs"] = specs
    return out


def _num(v) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.,]", "", str(v))
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    elif s.count(",") == 1 and len(s.split(",")[1]) != 3:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


async def read(model, subject: str, body: str) -> tuple[dict, str]:
    """The PR fields from the email, and what read it (model + labels, or labels only)."""
    fields, basis = {}, "labelled lines"
    if model is not None and getattr(model, "configured", False):
        from .evidence.llm import LLMUnavailable, first_json_object

        try:
            got = first_json_object(await model.complete([{"role": "user", "content": PROMPT.format(subject=subject, body=body[:6000])}], max_tokens=700))
            fields = {k: v for k, v in got.items() if v not in (None, "", [])}
            basis = f"{getattr(model, 'model', 'AI model')} + labelled lines"
        except (LLMUnavailable, ValueError) as exc:
            basis = f"labelled lines (the model could not read it: {str(exc)[:120]})"
    fields |= _labelled(body)
    if not fields.get("title"):
        fields["title"] = re.sub(r"^(re|fw|fwd)\s*:\s*", "", subject, flags=re.I).strip()[:200] or "Purchase request"
    return fields, basis


def to_pr(fields: dict, person: dict, org) -> dict:
    """The PR record for OrgStore (validated there)."""
    from .organisation import PurchaseRequest

    est = _num(fields.get("estimate"))
    std = (org.get("standards", "standards") or {})
    ccy = (fields.get("currency") or std.get("base_currency") or "USD").upper()[:3]
    needed = fields.get("needed_by") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fields.get("needed_by") or "")) else None
    rec = {"title": str(fields["title"])[:200], "requested_by": person["user_id"], "currency": ccy,
           "estimated_minor": int(round((est or 0) * 100)) or 1, "charge_code": fields.get("charge_code") or person.get("charge_code") or "",
           "needed_by": needed, "requested_date": date.today().isoformat(), "justification": str(fields.get("justification") or "")[:500],
           "quantity": _num(fields.get("quantity")), "unit": str(fields.get("unit") or "")[:20], "brand": str(fields.get("brand") or "")[:80],
           "specs": [str(s)[:200] for s in (fields.get("specs") or [])][:30], "status": "SUBMITTED"}
    if est is None:  # no price named: kept as a draft for the requester to complete; nothing is sourced on it
        rec |= {"status": "DRAFT", "justification": ("estimate missing in the email; " + rec["justification"])[:500]}
    return PurchaseRequest.model_validate(rec).model_dump()
