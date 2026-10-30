# ruff: noqa: E501
"""What arrives with an email, turned into text the intake can quote: PDF attachments, inbound-service payloads,
the receiving server's sender-authentication verdicts, and the invoice's own arithmetic.

* PDF       the text layer of an exported invoice; a scan without one is read by local OCR (RapidOCR, offline),
            and so is an attached image (JPG, PNG). OCR text is marked as such; what it cannot read is listed.
* Postmark  the inbound JSON (no own domain needed: every Postmark server has an inbound address) is rebuilt
            into a standard RFC 822 message, attachments included, so one parser serves every channel.
* SPF/DKIM  read from Authentication-Results / Received-SPF added by the receiving server. A registered mailbox
            whose mail fails both is treated as spoofed; no verdict at all (an uploaded .eml) changes nothing.
* totals    subtotal + tax must equal the total; the labelled total or amount due is the amount, not the
            largest number on the page.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
from email import policy
from email.message import EmailMessage

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_TEXT = 20_000
MAX_OCR_IMAGES = 6
_OCR = None


def _engine():
    """The OCR engine, loaded once; None when RapidOCR is not installed (the files are then listed, not read)."""
    global _OCR
    if _OCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            _OCR = RapidOCR()
        except Exception:  # noqa: BLE001 - OCR is optional
            _OCR = False
    return _OCR or None


# a label glued to an UPPER-case or digit value ("InvoiceNL-5009"); the label itself in any case
LABEL_GLUE = re.compile(r"\b(?i:invoice|inv|iban|date|due|amount|total|po|account|no\.?|number|faktur|tanggal|rekening)(?=[A-Z0-9])")
CCY_WORD = re.compile(r"(?i)\b(usd|eur|gbp|sgd|idr|aud|cad|chf|jpy|cny|hkd|myr|thb|php|inr|nzd)\b")


GLUED_PAIR = re.compile(r"(?i)\b(invoice|amount|total|due|issue|tax|account|bank|grand)(date|due|amount|no|number|total|name)\b")


def ocr_image(data: bytes) -> str:
    """Text read from one image, line by line top to bottom; labels OCR glued to their value are split again
    ("InvoiceNL-5009" -> "Invoice NL-5009", "date:2026" -> "date: 2026")."""
    eng = _engine()
    if eng is None or not data:
        return ""
    try:
        res, _ = eng(data)
    except Exception:  # noqa: BLE001 - an unreadable image is listed, never fatal
        return ""
    lines = [t for _, t, score in (res or []) if float(score) >= 0.5]
    text = "\n".join(lines)
    text = LABEL_GLUE.sub(lambda m: m.group(0) + " ", text)
    text = CCY_WORD.sub(lambda m: m.group(0).upper(), text)  # "UsD 300.00" -> "USD 300.00"
    text = GLUED_PAIR.sub(lambda m: f"{m.group(1)} {m.group(2)}", text)  # "Invoicedate" -> "Invoice date"
    return re.sub(r":(?=\S)", ": ", text)


def pdf_text(data: bytes) -> tuple[str, int, str | None]:
    """(text, pages, problem). Text is empty when the PDF has no text layer or cannot be read."""
    if len(data) > MAX_PDF_BYTES:
        return "", 0, f"larger than {MAX_PDF_BYTES // 1024 // 1024} MB"
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return "", len(reader.pages), "encrypted"
        pages = reader.pages[:MAX_PDF_PAGES]
        text = "\n".join((p.extract_text() or "") for p in pages)
        if not text.strip():  # a scan: read the page images
            images = [im.data for p in pages for im in p.images][:MAX_OCR_IMAGES]
            ocr = "\n".join(filter(None, (ocr_image(d) for d in images)))
            if ocr.strip():
                return ocr[:MAX_TEXT], len(pages), "scanned PDF; text read by OCR"
    except Exception as exc:  # noqa: BLE001 - a malformed attachment is reported, never fatal
        return "", 0, f"unreadable PDF ({type(exc).__name__})"
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return "", len(pages), "no text layer and no OCR text (a scan); not read"
    return text[:MAX_TEXT], len(pages), None


def attachments_of(msg) -> list[dict]:
    """Every attachment with its hash; PDFs also with their text."""
    out = []
    for part in msg.iter_attachments():
        name = part.get_filename() or "attachment"
        data = part.get_payload(decode=True) or b""
        ctype = part.get_content_type()
        item = {"name": name[:200], "content_type": ctype, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "pages": 0, "read": False, "problem": None, "text": ""}
        if ctype == "application/pdf" or name.lower().endswith(".pdf"):
            item["text"], item["pages"], item["problem"] = pdf_text(data)
            item["read"] = bool(item["text"])
        elif ctype.startswith("image/") or name.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "tif", "tiff", "bmp", "webp"):
            item["text"] = ocr_image(data) if len(data) <= MAX_PDF_BYTES else ""
            item["pages"], item["read"] = 1, bool(item["text"])
            item["problem"] = "image; text read by OCR" if item["text"] else "image; no text could be read"
        else:
            item["problem"] = "not a PDF or an image; not read"
        out.append(item)
    return out


AUTH_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*(pass|fail|softfail|neutral|none|temperror|permerror|policy)\b", re.IGNORECASE)


def authentication(msg) -> dict[str, str]:
    """The receiving server's verdicts, e.g. {"spf": "pass", "dkim": "fail"}; empty when none were added."""
    found: dict[str, str] = {}
    for header in msg.get_all("Authentication-Results", []) or []:
        for mech, verdict in AUTH_RE.findall(str(header)):
            found.setdefault(mech.lower(), verdict.lower())
    for header in msg.get_all("Received-SPF", []) or []:
        m = re.match(r"\s*(pass|fail|softfail|neutral|none|temperror|permerror)\b", str(header), re.IGNORECASE)
        if m:
            found.setdefault("spf", m.group(1).lower())
    return found


def spoofed(auth: dict[str, str]) -> bool:
    """Both SPF and DKIM answered and neither passed (or DMARC failed): the From address is not proven."""
    if auth.get("dmarc") == "fail":
        return True
    spf, dkim = auth.get("spf"), auth.get("dkim")
    return spf is not None and dkim is not None and spf != "pass" and dkim != "pass"


def postmark_to_eml(payload: dict) -> bytes:
    """Rebuild Postmark's inbound JSON as an RFC 822 message (headers, text body, attachments)."""
    msg = EmailMessage(policy=policy.default)
    sender = payload.get("FromFull") or {}
    frm = payload.get("From") or sender.get("Email") or ""
    name = sender.get("Name") or ""
    msg["From"] = f'"{name}" <{sender.get("Email", frm)}>' if name else frm
    for key, field in (("To", "To"), ("Reply-To", "ReplyTo"), ("Subject", "Subject"), ("Date", "Date")):
        if payload.get(field):
            msg[key] = str(payload[field])
    msg["Message-ID"] = str(payload.get("MessageID") and f"<{payload['MessageID']}@inbound.postmarkapp.com>" or "")
    if payload.get("OriginalRecipient"):  # the inbound address it really reached (Settings → Email sources)
        msg["Delivered-To"] = str(payload["OriginalRecipient"])
    for h in payload.get("Headers") or []:
        if h.get("Name") in ("Authentication-Results", "Received-SPF", "X-Spam-Status"):
            msg[h["Name"]] = str(h.get("Value", ""))[:2000]
    body = payload.get("TextBody") or re.sub(r"<[^>]+>", " ", payload.get("HtmlBody") or "")
    msg.set_content(body or "")
    for a in payload.get("Attachments") or []:
        data = base64.b64decode(a.get("Content") or "", validate=False)
        maintype, _, subtype = (a.get("ContentType") or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=a.get("Name") or "attachment")
    return msg.as_bytes()


# -- the invoice's own arithmetic ---------------------------------------------------------------------------
NUM = r"(?:USD|EUR|GBP|US\$|\$|€|£)?\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?(?:USD|EUR|GBP)?"
LABELS = {
    "subtotal": re.compile(r"\b(?:sub-?total|net amount|amount before tax)\b[^\d\n]{0,20}" + NUM, re.IGNORECASE),
    "tax": re.compile(r"\b(?:tax|vat|gst|sales tax)\b(?:\s*\(?\d{1,2}(?:\.\d+)?\s?%\)?)?[^\d\n]{0,20}" + NUM, re.IGNORECASE),
    "total": re.compile(r"\b(?:total due|amount due|balance due|invoice total|grand total|total amount|total)\b[^\d\n]{0,20}" + NUM, re.IGNORECASE),
}


def _minor(raw: str) -> int:
    return int(round(float(raw.replace(",", "")) * 100))


def totals(text: str) -> dict:
    """The labelled subtotal, tax and total (minor units, with the quoted text) and whether they add up."""
    found: dict = {}
    for label, rx in LABELS.items():
        hits = [m for m in rx.finditer(text) if not (label == "total" and re.match(r"sub", m.group(0), re.IGNORECASE))]
        if hits:
            m = hits[-1]  # the last labelled line is the summary block
            raw = m.group(0)
            start = m.start() + len(raw) - len(raw.lstrip())
            quote = raw.strip()  # the offsets point at the stripped quote, so text[start:end] == quote
            found[label] = {"minor": _minor(m.group(1)), "quote": quote, "start": start, "end": start + len(quote)}
    sub, tax, tot = (found.get(k, {}).get("minor") for k in ("subtotal", "tax", "total"))
    found["adds_up"] = None if sub is None or tot is None else abs(sub + (tax or 0) - tot) <= 1
    return found
