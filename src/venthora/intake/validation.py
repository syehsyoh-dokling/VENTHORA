# ruff: noqa: E501
"""Document validation: the finance checks an invoice, a claim or a bank-detail letter must pass before it is payable.

Settings → Document validation holds the rules (organisation store, kind ``doc_rules``):

* required documents  per request kind, the documents that must come with it (invoice, tax invoice, delivery
                      note, receipt, bank letter ...); every ticked one is mandatory.
* standard checks     each Off, Warn (flagged, still payable) or Required (a failure holds the request for a person):
                      the vendor's identity (registered mailbox, name, tax ID, bank account against the registered
                      one), the dates (invoice date not after the submission date, not too old, due date after it,
                      PDF created and modified no later than the submission), the arithmetic, the PO currency, the
                      stamp (present, and its size on the page within the set range in millimetres), the signature,
                      the file types and whether a PDF was edited after it was created.

Local Python does the reading: the PDF text layer, the position and size of every image placed on a page (a stamp
is a near-square image in coloured ink, a signature a wide stroke), the PDF's own dates and producer, and the
email. A language model (Settings → AI model, e.g. Qwen) adds what rules miss: which document each attachment
is, the invoice date, and anything an auditor would question. Every model answer must quote the document
verbatim or it is dropped, and a model can only raise a warning, never pass a check the rules failed.
"""
from __future__ import annotations

import datetime as dt
import io
import re
from email import policy
from email.parser import BytesParser
from typing import Literal

from pydantic import BaseModel, Field

from .documents import MAX_PDF_BYTES, MAX_PDF_PAGES
from .models import Check

Mode = Literal["off", "warn", "required"]
KINDS = ("supplier_invoice", "contractor_claim", "expense_claim", "bank_change")
KIND_LABEL = {"supplier_invoice": "Supplier invoice", "contractor_claim": "Contractor claim", "expense_claim": "Staff expense claim",
              "bank_change": "Bank-detail change"}

# key -> (label, words that name it, in English and Indonesian)
DOC_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "invoice": ("Invoice", ("invoice", "faktur", "tagihan", "bill no", "rechnung")),
    "tax_invoice": ("Tax invoice (VAT / faktur pajak)", ("tax invoice", "faktur pajak", "vat invoice", "e-faktur", "gst invoice")),
    "receipt": ("Receipt (kwitansi)", ("receipt", "kwitansi", "struk", "paid in full", "nota")),
    "purchase_order": ("Purchase order copy", ("purchase order", "p.o. no", "po number", "surat pesanan")),
    "delivery_note": ("Delivery note (surat jalan)", ("delivery note", "surat jalan", "packing list", "delivery order", "proof of delivery")),
    "goods_receipt": ("Goods received note / handover (BAST)", ("goods received", "goods receipt", "berita acara", "bast", "handover", "acceptance certificate")),
    "quotation": ("Quotation", ("quotation", "penawaran", "price quote", "proforma")),
    "contract": ("Contract / agreement (SPK)", ("contract", "agreement", "perjanjian", "kontrak", "surat perintah kerja", "statement of work")),
    "timesheet": ("Timesheet", ("timesheet", "time sheet", "hours worked", "daftar hadir")),
    "statement": ("Statement of account", ("statement of account", "account statement", "rekening koran")),
    "bank_letter": ("Bank account letter", ("bank reference", "bank confirmation", "account confirmation", "surat keterangan rekening", "bank letter")),
    "tax_registration": ("Tax registration (NPWP / VAT / W-9)", ("npwp", "w-9", "vat registration", "tax registration", "tax identification")),
    "business_licence": ("Business licence (NIB / company registration)", ("nib", "business licence", "business license", "certificate of incorporation", "company registration", "siup")),
    "travel_document": ("Ticket, boarding pass or hotel folio", ("boarding pass", "e-ticket", "itinerary", "hotel folio", "booking confirmation")),
    "expense_report": ("Expense report", ("expense report", "expense claim form", "laporan biaya", "reimbursement form")),
}

# key -> (label, default mode, what it checks)
CHECKS: dict[str, tuple[str, Mode, str]] = {
    "required_documents": ("Required documents attached", "required", "every document ticked for this kind of request is attached"),
    "sender_registered": ("Sender is a registered mailbox", "required", "the email comes from a mailbox the vendor (or the staff member) registered"),
    "vendor_name_on_document": ("Vendor name on the document", "warn", "the registered vendor name appears in the document"),
    "addressed_to_company": ("Addressed to the company", "warn", "the company's name (Finance standards) appears as the customer"),
    "tax_id_present": ("Vendor tax ID present", "off", "an NPWP, VAT, TIN or EIN number appears"),
    "bank_matches_registered": ("Bank account matches the registered one", "required", "any account number or IBAN on the document is the vendor's registered account"),
    "invoice_number_present": ("Invoice number present", "required", "an invoice or claim reference was read"),
    "invoice_date_present": ("Invoice date present", "warn", "the issue date was read"),
    "invoice_date_not_after_submission": ("Invoice date not after submission", "required", "the document is not dated after the day it was submitted"),
    "invoice_not_too_old": ("Invoice not older than the limit", "warn", "the invoice date is within the maximum age"),
    "due_date_after_invoice_date": ("Due date on or after invoice date", "warn", "the due date does not precede the issue date"),
    "totals_add_up": ("Subtotal + tax = total", "required", "the document's own arithmetic adds up"),
    "currency_matches_po": ("Currency matches the PO", "required", "the invoice currency is the purchase order's"),
    "attachments_readable": ("Attachments readable", "warn", "every PDF has a text layer (a scan is listed, not read)"),
    "file_types_allowed": ("File types allowed", "required", "attachments are of the allowed types and within the size limit"),
    "pdf_dates_not_after_submission": ("PDF not created after submission", "required", "the PDF's own creation and modification dates are not after the submission"),
    "pdf_not_edited": ("PDF not edited after it was issued", "warn", "modified more than a day after creation, or produced by an editing tool"),
    "stamp_present": ("Company stamp present", "off", "a stamp image is placed on a page"),
    "stamp_size": ("Stamp size within range", "warn", "the stamp's size on the page is within the set millimetres"),
    "signature_present": ("Signature present", "off", "a signature image is placed on a page"),
    "model_review": ("AI review", "warn", "the model's findings, each quoted from the document"),
}
EDITORS = ("photoshop", "gimp", "ilovepdf", "smallpdf", "sejda", "pdfescape", "foxit phantom", "nitro", "pdf-xchange editor",
           "acrobat pro", "pdfelement", "canva", "inkscape", "word", "libreoffice writer")


def _default_required() -> dict[str, list[str]]:
    return {"supplier_invoice": ["invoice"], "contractor_claim": ["invoice"], "expense_claim": ["receipt"], "bank_change": ["bank_letter"]}


class DocRules(BaseModel):
    enabled: bool = True
    required_documents: dict[str, list[str]] = Field(default_factory=_default_required)
    checks: dict[str, Mode] = Field(default_factory=lambda: {k: v[1] for k, v in CHECKS.items()})
    max_invoice_age_days: int = Field(default=180, ge=1, le=3650)
    stamp_min_mm: float = Field(default=25, ge=5, le=150)
    stamp_max_mm: float = Field(default=50, ge=5, le=150)
    signature_min_mm: float = Field(default=15, ge=3, le=150)
    allowed_file_types: list[str] = Field(default_factory=lambda: ["pdf", "jpg", "jpeg", "png"])
    max_attachment_mb: float = Field(default=10, gt=0, le=35)
    date_order: Literal["dmy", "mdy"] = Field(default="dmy", description="how 03/04/2026 is read: 3 April (dmy) or 4 March (mdy)")
    invoice_in_body_ok: bool = Field(default=True, description="an invoice written in the email body counts as the invoice document")

    def mode(self, check: str) -> Mode:
        return self.checks.get(check, CHECKS[check][1] if check in CHECKS else "off")


# -- reading the email's documents -------------------------------------------------------------------------

def _pdf_date(raw) -> dt.datetime | None:
    m = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", str(raw or ""))
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) if x else dflt for x, dflt in zip(m.groups(), (0, 1, 1, 0, 0, 0), strict=True))
    try:
        return dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.UTC)
    except ValueError:
        return None


def _ink(img) -> dict:
    """Share of ink pixels (not near white), their mean colour and how coloured it is."""
    im = img.convert("RGB")
    im.thumbnail((96, 96))
    raw = im.tobytes()
    px = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    ink = [p for p in px if min(p) < 200]
    if not ink:
        return {"share": 0.0, "rgb": (255, 255, 255), "saturation": 0}
    r, g, b = (sum(p[i] for p in ink) / len(ink) for i in range(3))
    return {"share": len(ink) / len(px), "rgb": (round(r), round(g), round(b)), "saturation": round(max(r, g, b) - min(r, g, b))}


def classify_mark(w_mm: float, h_mm: float, ink: dict) -> str:
    """stamp: near-square, coloured ink (blue, red, purple, green), partly filled; signature: wide, thin stroke."""
    aspect = w_mm / h_mm if h_mm else 0
    if ink["share"] <= 0.005:
        return "blank"
    r, g, b = ink["rgb"]
    coloured = ink["saturation"] >= 40 and not (r > 150 and g > 150 and b < 100)  # not yellow highlighter
    if 0.7 <= aspect <= 1.45 and coloured and ink["share"] <= 0.7:
        return "stamp"
    if aspect >= 1.8 and ink["share"] <= 0.35:
        return "signature"
    return "image"


def pdf_marks(data: bytes) -> tuple[list[dict], dict]:
    """Every image placed on the PDF's pages with its size in millimetres and what it looks like; the PDF's info."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    meta = {k.lstrip("/"): str(v) for k, v in (reader.metadata or {}).items()}
    marks: list[dict] = []
    for n, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1):
        placed: list[tuple[str, list]] = []
        try:
            page.extract_text(visitor_operand_before=lambda op, args, cm, tm, _p=placed: _p.append((str(args[0]), list(cm))) if op == b"Do" and args else None)
            images = {im.name.rsplit(".", 1)[0]: im for im in page.images}
        except Exception:  # noqa: BLE001 - an unusual page is skipped, never fatal
            continue
        for name, cm in placed:
            im = images.get(name.lstrip("/"))
            if im is None:
                continue
            w_mm = (cm[0] ** 2 + cm[1] ** 2) ** 0.5 / 72 * 25.4
            h_mm = (cm[2] ** 2 + cm[3] ** 2) ** 0.5 / 72 * 25.4
            try:
                ink = _ink(im.image)
            except Exception:  # noqa: BLE001 - an image Pillow cannot decode is sized but not classified
                ink = {"share": 0.0, "rgb": (0, 0, 0), "saturation": 0}
            marks.append({"page": n, "w_mm": round(w_mm, 1), "h_mm": round(h_mm, 1), "kind": classify_mark(w_mm, h_mm, ink), "ink": ink})
    return marks, meta


def read_documents(raw: bytes) -> list[dict]:
    """Each attachment: name, type, size, text (PDF), the marks placed on its pages and its PDF info."""
    from .documents import pdf_text

    msg = BytesParser(policy=policy.default).parsebytes(raw)
    out = []
    for part in msg.iter_attachments():
        name = (part.get_filename() or "attachment")[:200]
        data = part.get_payload(decode=True) or b""
        ctype = part.get_content_type()
        doc = {"name": name, "content_type": ctype, "bytes": len(data), "ext": name.rsplit(".", 1)[-1].lower() if "." in name else "",
               "text": "", "marks": [], "meta": {}, "problem": None}
        if ctype == "application/pdf" or doc["ext"] == "pdf":
            doc["text"], _, doc["problem"] = pdf_text(data)
            if len(data) <= MAX_PDF_BYTES:
                try:
                    doc["marks"], doc["meta"] = pdf_marks(data)
                except Exception as exc:  # noqa: BLE001
                    doc["problem"] = doc["problem"] or f"unreadable PDF ({type(exc).__name__})"
        elif ctype.startswith("image/"):
            try:
                from PIL import Image

                img = Image.open(io.BytesIO(data))
                dpi = (img.info.get("dpi") or (0, 0))[0]
                w_mm, h_mm = (img.width / dpi * 25.4, img.height / dpi * 25.4) if dpi else (0.0, 0.0)
                ink = _ink(img)
                doc["marks"] = [{"page": 1, "w_mm": round(w_mm, 1), "h_mm": round(h_mm, 1), "scaled": bool(dpi),
                                 "kind": classify_mark(img.width, img.height, ink), "ink": ink}]
            except Exception as exc:  # noqa: BLE001
                doc["problem"] = f"unreadable image ({type(exc).__name__})"
        out.append(doc)
    return out


# -- what the text says -----------------------------------------------------------------------------------
MONTHS = {**{m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)},
          "mei": 5, "agu": 8, "agt": 8, "okt": 10, "des": 12, "mrt": 3}
DATE = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,10}\.?\s+\d{4}|[A-Za-z]{3,10}\.?\s+\d{1,2},?\s+\d{4})"
INVOICE_DATE_RE = re.compile(r"\b(?:invoice date|date of invoice|date of issue|issue date|issued on|issued|tanggal faktur|tanggal invoice|tgl\.? faktur|tanggal|dated|date)\b\s*[:#-]?\s*" + DATE, re.IGNORECASE)
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?)\b")
ACCOUNT_RE = re.compile(r"\b(?:account\s*(?:no\.?|number|#)|acct\.?\s*(?:no\.?)?|a/c(?:\s*no\.?)?|no\.?\s*rek(?:ening)?\.?|nomor rekening|rekening)\s*[:#.]?\s*([0-9][0-9 -]{5,24}[0-9])", re.IGNORECASE)
TAX_ID_RE = re.compile(r"\b(?:npwp|vat(?:\s*(?:no\.?|number|id|reg(?:istration)?))?|tax\s*id|tin|ein|ust-?idnr\.?|gst\s*no\.?)\s*[:#.]?\s*([A-Z]{0,3}[0-9][0-9 .\-]{6,22}[0-9])", re.IGNORECASE)


def parse_date(raw: str, order: str = "dmy") -> dt.date | None:
    raw = raw.strip().rstrip(".,")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return dt.date.fromisoformat(raw)
        m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", raw)
        if m:
            a, b, y = (int(x) for x in m.groups())
            y += 2000 if y < 100 else 0
            d, mo = (a, b) if order == "dmy" else (b, a)
            return dt.date(y, mo, d)
        words = re.findall(r"[A-Za-z]+|\d+", raw)
        mon = next((MONTHS[w[:3].lower()] for w in words if w[:3].lower() in MONTHS), None)
        nums = [int(w) for w in words if w.isdigit()]
        if mon and len(nums) >= 2:
            day, year = (nums[0], nums[1]) if nums[1] > 31 else (nums[1], nums[0])
            return dt.date(year, mon, day)
    except ValueError:
        return None
    return None


def _norm_acct(s: str) -> str:
    return re.sub(r"[\s-]", "", s).upper()


def _name_tokens(name: str) -> list[str]:
    stop = {"inc", "ltd", "llc", "gmbh", "ag", "pt", "cv", "tbk", "plc", "co", "corp", "company", "limited", "the", "and", "ou", "sa", "bv"}
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in stop and len(t) > 2]


def detect_types(docs: list[dict], body: str, body_ok: bool = True) -> dict[str, dict]:
    """type -> {where, quote}: an attachment whose name or text names the document (the body counts for an invoice)."""
    found: dict[str, dict] = {}
    body = body.split("\n\n[Attachment: ")[0]  # the request text carries the PDFs; the body is what precedes them
    sources = [(d["name"], (d["name"] + "\n" + d["text"][:4000]).lower()) for d in docs] + ([("email body", body[:4000].lower())] if body_ok else [])
    for key, (_, words) in DOC_TYPES.items():
        for where, text in sources:
            if where == "email body" and key not in ("invoice", "expense_report"):
                continue  # other documents must be attached, not mentioned
            w = next((w for w in words if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text)), None)
            if w:
                found[key] = {"where": where, "quote": w, "source": "rules"}
                break
    return found


# -- the model's part ---------------------------------------------------------------------------------------
PROMPT = """You check documents sent to a company's finance team before a payment. You get the email and each attachment's text.
Return only a JSON object:
  "documents": [{"attachment": "<file name>", "types": ["<type key>"], "quote": "<exact words from that attachment that show it>"}]
  "invoice_date": {"value": "YYYY-MM-DD", "quote": "<exact words from the text>"} or null
  "findings": [{"issue": "<what a finance auditor would question, one short sentence>", "quote": "<exact words from the text>"}]
Type keys: {types}
Only report what the text shows. Every quote must be copied exactly from the text. Do not guess. At most 5 findings."""


def _find(quote: str, text: str) -> bool:
    q = re.sub(r"\s+", " ", quote or "").strip()
    return len(q) >= 3 and re.search(r"\s+".join(re.escape(p) for p in q.split(" ")), text, re.IGNORECASE) is not None


async def model_assist(model, subject: str, body: str, docs: list[dict], received: dt.date) -> dict:
    """The model's grounded reading: document types per attachment, the invoice date, and findings. {} without a model."""
    if model is None or not getattr(model, "configured", False):
        return {}
    from ..evidence.llm import LLMUnavailable, first_json_object

    parts = [f"Subject: {subject}\n\n{body[:2500]}"] + [f"[Attachment: {d['name']}]\n{d['text'][:2500]}" for d in docs if d["text"]]
    prompt = PROMPT.replace("{types}", ", ".join(f"{k} ({v[0]})" for k, v in DOC_TYPES.items()))
    try:
        reply = first_json_object(await model.complete([{"role": "system", "content": prompt}, {"role": "user", "content": "\n\n".join(parts)[:7000]}], max_tokens=500))
    except (LLMUnavailable, ValueError) as exc:
        return {"error": f"the AI model did not answer ({type(exc).__name__})"}
    texts = {d["name"]: d["text"] for d in docs}
    out: dict = {"types": {}, "invoice_date": None, "findings": []}
    for item in reply.get("documents") or []:
        if not isinstance(item, dict):
            continue
        where, quote = str(item.get("attachment") or ""), str(item.get("quote") or "")
        if where in texts and _find(quote, texts[where]):
            for t in item.get("types") or []:
                if t in DOC_TYPES:
                    out["types"].setdefault(t, {"where": where, "quote": quote[:160], "source": "model"})
    alltext = body + "\n" + "\n".join(texts.values())
    d = reply.get("invoice_date")
    if isinstance(d, dict) and _find(str(d.get("quote") or ""), alltext):
        try:
            day = dt.date.fromisoformat(str(d.get("value")))
            if abs((day - received).days) <= 3650 and re.search(rf"\b0?{day.day}\b", str(d["quote"])):
                out["invoice_date"] = {"value": day, "quote": str(d["quote"])[:160]}
        except ValueError:
            pass
    for f in (reply.get("findings") or [])[:5]:
        if isinstance(f, dict) and str(f.get("issue") or "").strip() and _find(str(f.get("quote") or ""), alltext):
            out["findings"].append({"issue": str(f["issue"])[:200], "quote": str(f["quote"])[:160]})
    return out


# -- the checks ---------------------------------------------------------------------------------------------

def validate(req, docs: list[dict], rules: DocRules, *, company: str = "", vendor: dict | None = None,
             registered_bank: dict | None = None, po: dict | None = None, model: dict | None = None) -> list[Check]:
    """Every check for one intake request. req is an IntakeRequest; vendor its registered record; registered_bank
    the bank details VENTHORA pays it to; po the purchase order it quotes."""
    if not rules.enabled or req.kind not in KINDS:
        return []
    model = model or {}
    received = dt.date.fromisoformat(req.received_at[:10])
    text = req.text
    out: list[Check] = []

    def add(key, status, detail, evidence=None, source="rules"):
        mode = rules.mode(key)
        if mode == "off":
            return
        out.append(Check(check=key, label=CHECKS[key][0], mode=mode, status=status, detail=detail, evidence=evidence, source=source))

    # required documents
    types = detect_types(docs, text, rules.invoice_in_body_ok)
    for k, v in model.get("types", {}).items():
        types.setdefault(k, v)
    need = rules.required_documents.get(req.kind, [])
    missing = [k for k in need if k not in types]
    if need:
        got = ", ".join(f"{DOC_TYPES[k][0]} ({types[k]['where']})" for k in need if k in types)
        add("required_documents", "fail" if missing else "pass",
            ("missing: " + ", ".join(DOC_TYPES[k][0] for k in missing) + (f"; found {got}" if got else "")) if missing else f"found {got}",
            source="rules+model" if any(types[k]["source"] == "model" for k in need if k in types) else "rules")

    # identity
    trusted = req.trust in ("registered", "staff", "system_of_record")
    add("sender_registered", "pass" if trusted else "fail",
        f"{req.sender} is the registered mailbox of {req.counterparty_name}" if trusted else f"{req.sender} is not a registered mailbox ({req.trust.replace('_', ' ')})")
    if req.kind != "expense_claim":
        if vendor and vendor.get("name"):
            toks = _name_tokens(vendor["name"])
            hit = [t for t in toks if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)]
            ok = bool(toks) and len(hit) >= max(1, (len(toks) + 1) // 2)
            add("vendor_name_on_document", "pass" if ok else "fail", f"{vendor['name']} {'appears' if ok else 'does not appear'} in the document",
                evidence=", ".join(hit) or None)
        else:
            add("vendor_name_on_document", "skip", "no registered vendor to compare with")
        if company:
            toks = _name_tokens(company)
            ok = any(re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE) for t in toks)
            add("addressed_to_company", "pass" if ok else "fail", f"{company} {'appears' if ok else 'does not appear'} as the customer")
        m = TAX_ID_RE.search(text)
        add("tax_id_present", "pass" if m else "fail", f"tax ID {m.group(1).strip()}" if m else "no NPWP, VAT, TIN or EIN number found",
            evidence=m.group(0) if m else None)
        # bank account on the document against the registered one
        on_doc = {_norm_acct(x) for x in IBAN_RE.findall(text)} | {_norm_acct(x) for x in ACCOUNT_RE.findall(text)}
        on_doc = {x for x in on_doc if len(x) >= 6}
        reg = {_norm_acct(str((registered_bank or {}).get(k) or "")) for k in ("account_number", "iban")}  # never the routing or sort code
        reg = {x for x in reg if len(x) >= 6}
        if req.kind == "bank_change":
            add("bank_matches_registered", "skip", "this request changes the bank details; the call-back decides")
        elif not on_doc:
            add("bank_matches_registered", "pass", "no account number on the document; VENTHORA pays the registered account")
        elif not reg:
            add("bank_matches_registered", "fail", "the document names an account, but no registered account is on file to compare with",
                evidence=", ".join(sorted(on_doc)))
        else:
            other = sorted(x for x in on_doc if x not in reg and not any(x.endswith(r) or r.endswith(x) for r in reg))
            add("bank_matches_registered", "fail" if other else "pass",
                f"the document asks for payment to {', '.join('•••' + x[-4:] for x in other)}, not the registered account •••{sorted(reg)[0][-4:]}" if other
                else "the account on the document is the registered one", evidence=", ".join(other) or None)

    # numbers and dates
    if req.kind != "bank_change":
        add("invoice_number_present", "pass" if req.invoice_no else "fail", f"reference {req.invoice_no}" if req.invoice_no else "no invoice or claim reference read")
        inv_date, quote, src = None, None, "rules"
        m = INVOICE_DATE_RE.search(text)
        if m:
            inv_date, quote = parse_date(m.group(1), rules.date_order), m.group(0)
        if inv_date is None and model.get("invoice_date"):
            inv_date, quote, src = model["invoice_date"]["value"], model["invoice_date"]["quote"], "model"
        if req.kind == "expense_claim" and inv_date is None and req.expense_date:
            inv_date, quote = dt.date.fromisoformat(req.expense_date), f"expense date {req.expense_date}"
        add("invoice_date_present", "pass" if inv_date else "fail", f"dated {inv_date.isoformat()}" if inv_date else "no issue date read", quote, src)
        if inv_date:
            later = inv_date > received
            add("invoice_date_not_after_submission", "fail" if later else "pass",
                f"dated {inv_date.isoformat()}, after it was submitted on {received.isoformat()}" if later else f"dated {inv_date.isoformat()}, submitted {received.isoformat()}", quote, src)
            age = (received - inv_date).days
            if age >= 0:
                add("invoice_not_too_old", "fail" if age > rules.max_invoice_age_days else "pass",
                    f"{age} days old (limit {rules.max_invoice_age_days})", quote, src)
            if req.due_date:
                due = dt.date.fromisoformat(req.due_date)
                add("due_date_after_invoice_date", "fail" if due < inv_date else "pass", f"due {due.isoformat()}, issued {inv_date.isoformat()}")
        else:
            add("invoice_date_not_after_submission", "skip", "no issue date to compare")
        add("totals_add_up", "fail" if "totals_do_not_add_up" in req.flags else "pass",
            "the subtotal and tax do not add up to the total" if "totals_do_not_add_up" in req.flags else "the arithmetic adds up (or the document shows only a total)")
        if po:
            ok = not req.currency or req.currency == po.get("currency")
            add("currency_matches_po", "pass" if ok else "fail", f"invoice in {req.currency}, PO {po.get('po_id')} in {po.get('currency')}")

    # the files themselves
    if docs:
        unread = [d["name"] for d in docs if d["ext"] == "pdf" and not d["text"]]
        add("attachments_readable", "fail" if unread else "pass", ("not read: " + ", ".join(unread) + " (" + "; ".join(d["problem"] or "no text" for d in docs if d["name"] in unread) + ")") if unread else "every PDF has a text layer")
        bad = [f"{d['name']} ({d['ext'] or d['content_type']})" for d in docs if d["ext"] not in rules.allowed_file_types]
        big = [f"{d['name']} ({d['bytes'] / 1024 / 1024:.1f} MB)" for d in docs if d["bytes"] > rules.max_attachment_mb * 1024 * 1024]
        add("file_types_allowed", "fail" if bad or big else "pass", "; ".join(filter(None, ["not allowed: " + ", ".join(bad) if bad else "", "too large: " + ", ".join(big) if big else ""])) or "all attachments allowed")
        submitted = dt.datetime.fromisoformat(req.received_at.replace("Z", "+00:00"))
        late, edited = [], []
        for d in docs:
            made, mod = _pdf_date(d["meta"].get("CreationDate")), _pdf_date(d["meta"].get("ModDate"))
            for label, t in (("created", made), ("modified", mod)):
                if t and t > submitted + dt.timedelta(hours=1):
                    late.append(f"{d['name']} {label} {t.date().isoformat()}")
            tool = (d["meta"].get("Producer", "") + " " + d["meta"].get("Creator", "")).lower()
            if made and mod and (mod - made) > dt.timedelta(days=1):
                edited.append(f"{d['name']} modified {(mod - made).days} days after it was created")
            elif any(e in tool for e in EDITORS):
                edited.append(f"{d['name']} produced by {tool.strip()[:60]}")
        pdfs = [d for d in docs if d["ext"] == "pdf"]
        if pdfs:
            add("pdf_dates_not_after_submission", "fail" if late else "pass", "; ".join(late) if late else "no PDF is dated after the submission")
            add("pdf_not_edited", "fail" if edited else "pass", "; ".join(edited) if edited else "no sign of editing after issue")
        marks = [m | {"doc": d["name"]} for d in docs for m in d["marks"]]
        stamps = [m for m in marks if m["kind"] == "stamp"]
        sigs = [m for m in marks if m["kind"] == "signature"]
        add("stamp_present", "pass" if stamps else "fail", f"{len(stamps)} stamp(s): " + ", ".join(f"{m['doc']} p{m['page']}" for m in stamps) if stamps else "no stamp image on any page")
        if stamps:
            sized = [m for m in stamps if m["w_mm"]]
            wrong = [m for m in sized if not rules.stamp_min_mm <= max(m["w_mm"], m["h_mm"]) <= rules.stamp_max_mm]
            add("stamp_size", "fail" if wrong else ("pass" if sized else "skip"),
                ("; ".join(f"{m['doc']} p{m['page']}: {m['w_mm']} x {m['h_mm']} mm" for m in wrong) + f" (allowed {rules.stamp_min_mm:g}-{rules.stamp_max_mm:g} mm)") if wrong
                else ", ".join(f"{m['w_mm']} x {m['h_mm']} mm" for m in sized) + f" (allowed {rules.stamp_min_mm:g}-{rules.stamp_max_mm:g} mm)" if sized else "image without a page scale")
        ok_sig = [m for m in sigs if not m["w_mm"] or m["w_mm"] >= rules.signature_min_mm]
        add("signature_present", "pass" if ok_sig else "fail", f"{len(ok_sig)} signature(s)" if ok_sig else "no signature image on any page")
    else:
        add("attachments_readable", "skip", "no attachment")
        add("stamp_present", "fail" if req.kind != "expense_claim" else "skip", "no attachment, so no stamp")
        add("signature_present", "fail" if req.kind != "expense_claim" else "skip", "no attachment, so no signature")

    # the model's review
    if "findings" in model:
        f = model["findings"]
        add("model_review", "warn" if f else "pass", "; ".join(x["issue"] for x in f) if f else "the model raised nothing it could quote",
            evidence=" | ".join(x["quote"] for x in f) or None, source="model")
    else:
        add("model_review", "skip", model.get("error") or "no AI model configured (Settings → AI model)", source="model")
    return out


def apply(req, checks: list[Check]):
    """Fold the checks into the request: a failed Required check holds a VERIFIED request for a person (NEEDS_REVIEW);
    a failed Warn check, or a model finding, is flagged. Never makes a request more payable than it was."""
    flags, reasons, status = list(req.flags), list(req.reasons), req.status
    for c in checks:
        if c.status not in ("fail", "warn"):
            continue
        flags.append(f"doc:{c.check}")
        if c.mode == "required" and c.status == "fail":
            reasons.append(f"document check failed: {c.label}: {c.detail}")
            if status == "VERIFIED":
                status = "NEEDS_REVIEW"
    return req.model_copy(update={"checks": checks, "flags": flags, "reasons": reasons, "status": status})
