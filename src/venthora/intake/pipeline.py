# ruff: noqa: E501
"""Email intake: parse -> verify the sender -> extract grounded fields -> classify -> queue -> state.

Trust is decided by code, from the counterparty registry (data/payees.json):
  registered        the exact mailbox the counterparty registered at onboarding
  reply_to_mismatch registered mailbox, but Reply-To points elsewhere (classic payment-diversion pattern)
  unknown_sender    anything else, whatever the display name says (gmail/yahoo/outlook are not identities)

Status is decided by code, never by the text of the email:
  VERIFIED      registered sender, one amount and currency found, no bank-change claim -> payable
  NEEDS_REVIEW  registered sender but something is missing or a bank change is claimed -> visible, not payable
  QUARANTINED   unknown sender, or reply-to mismatch, asking for money or a bank change -> visible, not payable
  DUPLICATE     same invoice as one already in the book or the queue -> never paid twice
A person can move NEEDS_REVIEW / QUARANTINED to VERIFIED or REJECTED (role-checked); the review is recorded.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

from .. import doa, mandate
from ..config import ROOT, data_path
from ..domain.models import TreasuryState
from . import claims, documents
from .models import IntakeRequest, Quote, ReviewRequest

PAYEES_FILE = ROOT / "data" / "payees.json"
FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.id", "outlook.com", "hotmail.com",
            "live.com", "icloud.com", "proton.me", "protonmail.com"}

CURRENCIES = {"USD": "USD", "US$": "USD", "$": "USD", "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP"}
AMOUNT_RE = re.compile(r"(?P<cur>USD|EUR|GBP|US\$|\$|€|£)\s?(?P<amt>\d{1,3}(?:[,.]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
                       r"|(?P<amt2>\d{1,3}(?:[,.]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?(?P<cur2>USD|EUR|GBP)\b")
INVOICE_RE = re.compile(r"\b(?:invoice|inv\.?|bill|statement|claim|timesheet)\s*(?:no\.?|number|#|ref\.?|:)?\s*"
                        r"(?P<no>[A-Z]{1,6}[-/]?(?:[A-Z]{1,3}\d{2,}|\d{2,}[A-Z]{0,3})(?:[-/][A-Z0-9]{1,8})*)", re.IGNORECASE)
PO_BEFORE_RE = re.compile(r"(?:purchase order|\bp\.?o\.?)\s*(?:no\.?|number|#)?\s*[:#]?\s*$")
# LD-4410, LD-F025811, INV/2026/X/017, INV-2026-A17: letters, then digits (a short letter run may sit on either side)
INVOICE_BARE_RE = re.compile(r"(?<![-/A-Za-z0-9])(?P<no>[A-Z]{2,6}[-/](?:[A-Z]{1,3}\d{2,}|\d{2,}[A-Z]{0,3})(?:[-/][A-Z0-9]{1,8})?)\b")
DUE_RE = re.compile(r"\b(?:due|payable|pay(?:ment)?\s+(?:by|before|on)|by)\s+(?:on\s+|by\s+)?(?P<date>"
                    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?:\s+\d{4})?"
                    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?"
                    r"|\d{4}-\d{2}-\d{2})", re.IGNORECASE)
ANY_DATE_RE = re.compile(r"\b(?P<date>\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}"
                         r"|\d{4}-\d{2}-\d{2})", re.IGNORECASE)
NET_RE = re.compile(r"\b(?:net|within)\s+(?P<days>\d{1,3})\s*(?:days?)?\b", re.IGNORECASE)
BANK_CHANGE = ("new bank", "new account", "changed bank", "updated bank", "updated payment details", "updated account",
               "new payment details", "disregard the account", "different account", "account details are updated",
               "changed our bank", "bank details have changed")
URGENCY = ("urgent", "immediately", "right away", "today without fail", "asap", "as soon as possible", "before end of day",
           "do not call", "don't call", "confidential", "ceo", "managing director has approved")
REMITTANCE = ("remittance", "will transfer", "will pay", "we will settle", "payment has been scheduled", "we have scheduled",
              "will be released", "has been released", "released the payment", "we are paying")
CLAIM = ("please pay", "kindly pay", "payment for", "please settle", "please find attached", "invoice", "amount due",
         "please remit", "please transfer", "hours worked", "timesheet", "please process")
EXPENSE = ("expense claim", "reimbursement", "reimburse", "expenses", "out of pocket", "receipt attached", "per diem")
# Dunning: a payee reminding us. Level 3 = final notice, 2 = second reminder, 1 = reminder; the highest phrase found wins.
DUNNING = {3: ("final notice", "final reminder", "last reminder", "legal action", "debt collection", "collections agency",
               "suspend your account", "suspend service", "suspend deliveries"),
           2: ("second reminder", "2nd reminder", "still outstanding", "remains unpaid", "remains outstanding", "past due",
               "second notice"),
           1: ("reminder", "overdue", "not yet received", "has not been received", "payment is due", "outstanding invoice")}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake (
  intake_id TEXT PRIMARY KEY, message_id TEXT UNIQUE, text_sha256 TEXT, received_at TEXT, sender TEXT,
  status TEXT, invoice_fingerprint TEXT, record_json TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS intake_reviews (
  review_id TEXT PRIMARY KEY, intake_id TEXT, decision TEXT, reviewer TEXT, role TEXT, note TEXT, created_at TEXT);
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


# -- registry ----------------------------------------------------------------------------------

class Registry:
    """data/payees.json, with the vendors and customers synced or uploaded into the master data on top (masterdata.py):
    a master record wins over the file record with the same id; re-read whenever the master data changes."""

    def __init__(self, path: Path = PAYEES_FILE, master=None):
        data = json.loads(path.read_text(encoding="utf-8"))
        self._file_payees: dict[str, dict] = data["payees"]
        self._file_payers: dict[str, dict] = data["payers"]
        if master is None:
            from ..masterdata import MasterStore

            master = MasterStore()
        self._master, self._seen, self._merged = master, None, (self._file_payees, self._file_payers)

    def _sync(self) -> tuple[dict, dict]:
        v = self._master.version()
        if v != self._seen:
            from ..masterdata import registry_entries

            payees, payers = registry_entries(self._master)
            self._merged, self._seen = (self._file_payees | payees, self._file_payers | payers), v
        return self._merged

    @property
    def payees(self) -> dict[str, dict]:
        return self._sync()[0]

    @property
    def payers(self) -> dict[str, dict]:
        return self._sync()[1]

    def by_contact(self, address: str) -> tuple[str, dict, str] | None:
        address = address.lower()
        for role, book in (("payee", self.payees), ("payer", self.payers)):
            for cid, rec in book.items():
                if address in {c.lower() for c in rec.get("contacts", [])}:
                    return cid, rec, role
                domain = address.rsplit("@", 1)[-1]
                if domain not in FREEMAIL and domain in {d.lower() for d in rec.get("domains", [])}:
                    return cid, rec, role
        return None

    def claimed(self, *texts: str) -> tuple[str, dict, str] | None:
        """Which counterparty does the display name / text claim to be? (Never trusted; used to name the claim.)"""
        blob = " ".join(t.lower() for t in texts if t)
        best = None
        for role, book in (("payee", self.payees), ("payer", self.payers)):
            for cid, rec in book.items():
                if rec.get("kind") == "employee":  # a staff member's own account is paid through payroll or their claims, never named as a vendor
                    continue
                for name in [rec["name"], *rec.get("aliases", [])]:
                    if name.lower() in blob and (best is None or len(name) > len(best[3])):
                        best = (cid, rec, role, name)
        return best[:3] if best else None


# -- parsing and extraction ----------------------------------------------------------------------

def parse_eml(raw: bytes) -> dict:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    froms = getaddresses([msg.get("From", "")])
    display, addr = (froms[0] if froms else ("", ""))
    reply = getaddresses([msg.get("Reply-To", "")])
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body is not None else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text).strip()[:20_000]
    files = documents.attachments_of(msg)
    for f in files:  # a read PDF joins the document, so every value it supplies is quoted from it like the body
        if f["read"]:
            text += f"\n\n[Attachment: {f['name']}]\n{f['text']}"
    try:
        received = parsedate_to_datetime(msg.get("Date")).astimezone(dt.UTC).isoformat() if msg.get("Date") else _now()
    except (TypeError, ValueError):
        received = _now()
    return {"message_id": (msg.get("Message-ID") or f"<{hashlib.sha256(raw).hexdigest()[:24]}@local>").strip(),
            "received_at": received, "sender": addr.lower(), "display_name": display.strip(),
            "reply_to": reply[0][1].lower() if reply and reply[0][1] else None,
            "subject": (msg.get("Subject") or "").strip(), "text": text[:40_000],
            "attachments": [{k: v for k, v in f.items() if k != "text"} for f in files],
            "authentication": documents.authentication(msg)}


def _amount(text: str) -> tuple[Quote, int, str] | None:
    hits = []
    for m in AMOUNT_RE.finditer(text):
        cur = CURRENCIES[(m.group("cur") or m.group("cur2")).upper() if (m.group("cur") or m.group("cur2")) not in ("$", "€", "£")
                         else (m.group("cur") or m.group("cur2"))]
        amt = (m.group("amt") or m.group("amt2")).replace(",", "")
        if amt.count(".") > 1:  # 1.500.000 style
            amt = amt.replace(".", "")
        minor = int(round(float(amt) * 100))
        if minor > 0:
            q = Quote(field="amount", value=f"{cur} {minor}", quote=m.group(0), start=m.start(), end=m.end())
            hits.append((q, minor, cur))
    if not hits:
        return None
    # the largest amount in the message is the amount due (totals exceed line items and hourly rates)
    return max(hits, key=lambda h: h[1])


def _labelled_amount(sums: dict, text: str) -> tuple[Quote, int, str] | None:
    """The amount the document labels as its total or amount due (not merely the largest number on it)."""
    total = sums.get("total")
    if not total:
        return None
    cur = None
    m = AMOUNT_RE.search(total["quote"])
    if m:
        sym = m.group("cur") or m.group("cur2")
        cur = CURRENCIES[sym.upper() if sym not in ("$", "€", "£") else sym]
    if cur is None:  # "Total 1,250.00" with the currency stated elsewhere in the document
        anywhere = _amount(text)
        cur = anywhere[2] if anywhere else None
    if cur is None:
        return None
    q = Quote(field="amount", value=f"{cur} {total['minor']}", quote=total["quote"], start=total["start"], end=total["end"])
    return q, total["minor"], cur


def _invoice(text: str, subject: str) -> Quote | None:
    for src, offset in ((subject, -1), (text, 0)):
        for m in [*INVOICE_RE.finditer(src), *INVOICE_BARE_RE.finditer(src)]:
            # a purchase order number is not the invoice number (seen in tests: "PO-2026-0101" read as the invoice,
            # which would make every later invoice on the same PO look like a duplicate)
            before = src[max(0, m.start() - 20):m.start()].lower()
            if m.group("no").upper().startswith("PO-") or PO_BEFORE_RE.search(before):
                continue
            return Quote(field="invoice_no", value=m.group("no").upper(), quote=m.group(0),
                         start=m.start() if offset == 0 else -1, end=m.end() if offset == 0 else -1)
    return None


def _due(text: str, received: dt.date) -> Quote | None:
    m = DUE_RE.search(text)
    if m:
        raw = m.group("date")
        d = _parse_date(raw, received)
        if d:
            return Quote(field="due_date", value=d.isoformat(), quote=m.group(0), start=m.start(), end=m.end())
    m = NET_RE.search(text)
    if m:
        d = received + dt.timedelta(days=int(m.group("days")))
        return Quote(field="due_date", value=d.isoformat(), quote=m.group(0), start=m.start(), end=m.end())
    m = ANY_DATE_RE.search(text)  # e.g. "scheduled for release on 27 October 2026"
    if m:
        d = _parse_date(m.group("date"), received)
        if d and d >= received:
            return Quote(field="due_date", value=d.isoformat(), quote=m.group(0), start=m.start(), end=m.end())
    return None


def _parse_date(raw: str, received: dt.date) -> dt.date | None:
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return dt.date.fromisoformat(raw)
    parts = re.findall(r"[A-Za-z]+|\d+", raw)
    nums = [int(p) for p in parts if p.isdigit()]
    mon = next((MONTHS[p[:3].lower()] for p in parts if p[:3].lower() in MONTHS), None)
    if mon is None or not nums:
        return None
    day = nums[0]
    year = nums[1] if len(nums) > 1 else received.year
    try:
        d = dt.date(year, mon, day)
    except ValueError:
        return None
    if len(nums) == 1 and d < received - dt.timedelta(days=90):
        d = dt.date(year + 1, mon, day)
    return d


def _phrases(text: str, phrases, field: str) -> list[Quote]:
    low = text.lower()
    out = []
    for p in phrases:
        i = low.find(p)
        if i >= 0:
            out.append(Quote(field=field, value=p, quote=text[i:i + len(p)], start=i, end=i + len(p)))
    return out


def _fingerprint(rec: dict | None, invoice_no: str | None, counterparty_id: str | None) -> str | None:
    if not invoice_no:
        return None
    prefix = (rec or {}).get("invoice_prefix") or counterparty_id or "unknown"
    return f"{prefix}:{invoice_no}"


# -- the pipeline --------------------------------------------------------------------------------

class IntakeStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_INTAKE_DB", "data/venthora_intake.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        return conn

    def by_message(self, message_id: str) -> IntakeRequest | None:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT record_json FROM intake WHERE message_id = ?", (message_id,)).fetchone()
        return IntakeRequest.model_validate_json(row["record_json"]) if row else None

    def get(self, intake_id: str) -> IntakeRequest | None:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT record_json FROM intake WHERE intake_id = ?", (intake_id,)).fetchone()
        return IntakeRequest.model_validate_json(row["record_json"]) if row else None

    def list(self, limit: int = 200) -> list[IntakeRequest]:
        with self._lock, self._connect() as c:
            rows = c.execute("SELECT record_json FROM intake ORDER BY received_at DESC, created_at DESC LIMIT ?",
                             (limit,)).fetchall()
        return [IntakeRequest.model_validate_json(r["record_json"]) for r in rows]

    def fingerprints(self) -> dict[str, str]:
        with self._lock, self._connect() as c:
            rows = c.execute("SELECT invoice_fingerprint, intake_id FROM intake WHERE invoice_fingerprint IS NOT NULL"
                             " AND status != 'DUPLICATE' ORDER BY created_at").fetchall()
        return {r["invoice_fingerprint"]: r["intake_id"] for r in rows}

    def save(self, req: IntakeRequest) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO intake VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(intake_id) DO UPDATE SET"
                      " status = excluded.status, record_json = excluded.record_json, updated_at = excluded.updated_at",
                      (req.intake_id, req.message_id, req.text_sha256, req.received_at, req.sender, req.status,
                       req.invoice_fingerprint, req.model_dump_json(), _now(), _now()))

    def review(self, intake_id: str, review: ReviewRequest) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO intake_reviews VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (str(uuid4()), intake_id, review.decision, review.reviewer, review.role, review.note, _now()))


class Intake:
    def __init__(self, store: IntakeStore | None = None, registry: Registry | None = None,
                 reviewer_roles: tuple[str, ...] = ("finance_manager", "ap_clerk", "super_admin"), org=None, payments=None):
        self.store = store or IntakeStore()
        self.registry = registry or Registry()
        self.reviewer_roles = reviewer_roles
        self.org = org  # User management, Finance standards, Unit prices, purchase orders; None = none of those checks
        self.payments = payments  # the execution store's spend ledger: what each PO has really paid

    def _modes(self) -> dict[str, str]:
        return mandate.check_modes(self.org)

    def _flag(self, flags: list[str], flag: str, check: str) -> None:
        if self._modes()[check] != "off":
            flags.append(flag)

    def ingest(self, raw: bytes, *, book_fingerprints: dict[str, str] | None = None, save: bool = True,
               proposals: dict[str, Quote] | None = None, retriage_of: str | None = None, channel: str = "email") -> IntakeRequest:
        """Triage one raw email. save=False (dry run) triages without touching the queue. `proposals` are fields
        the model read and the reader already matched verbatim to the text (see intake/reader.py)."""
        parsed = parse_eml(raw)
        existing = self.store.by_message(parsed["message_id"]) if save else None
        if existing:
            return existing
        text, subject = parsed["text"], parsed["subject"]
        received = dt.date.fromisoformat(parsed["received_at"][:10])
        reg = self.registry
        quotes: list[Quote] = []
        flags: list[str] = []
        reasons: list[str] = []
        auth = parsed["authentication"]
        mode = self._modes()  # the standing mandate: each check holds, only warns, or is off
        sender_holds = mode["sender"] == "hold"

        hit = reg.by_contact(parsed["sender"])
        staff = claims.employee(self.org, parsed["sender"]) if not hit else None
        claimed = reg.claimed(parsed["display_name"], subject, text)
        if (hit or staff) and documents.spoofed(auth):
            trust = "failed_authentication"
            reasons.append("the receiving server could not authenticate the sender ("
                           + ", ".join(f"{k} {v}" for k, v in sorted(auth.items())) + "); the From address may be forged")
        elif hit and parsed["reply_to"] and parsed["reply_to"] != parsed["sender"] and not reg.by_contact(parsed["reply_to"]):
            trust = "reply_to_mismatch"
            reasons.append(f"Reply-To {parsed['reply_to']} is not a registered mailbox of {hit[1]['name']}")
        elif staff and parsed["reply_to"] and parsed["reply_to"] != parsed["sender"]:
            trust = "reply_to_mismatch"
            reasons.append(f"Reply-To {parsed['reply_to']} is not {staff['name']}'s mailbox")
        elif hit:
            trust = "registered"
        elif staff:
            trust = "staff"
        else:
            trust = "unknown_sender"
            reasons.append(f"{parsed['sender']} is not a registered mailbox" + (f" of {claimed[1]['name']}, which the message "
                                                                                 "claims to be" if claimed else ""))
        cid, rec, role = hit if hit else (None, None, None)

        bank = _phrases(text, BANK_CHANGE, "bank_change")
        urgency = _phrases(text + " " + subject, URGENCY, "urgency")
        remit = _phrases(text, REMITTANCE, "remittance")
        claim = _phrases(text + " " + subject, CLAIM + EXPENSE, "claim")
        sums = documents.totals(text)
        amount = _labelled_amount(sums, text) or _amount(text)
        inv = _invoice(text, subject)
        due = _due(text, received)
        if sums.get("adds_up") is False and mode["totals"] != "off":
            flags.append("totals_do_not_add_up")
            reasons.append("the subtotal and tax do not add up to the total: " + "; ".join(
                f"{k} {sums[k]['minor'] / 100:,.2f}" for k in ("subtotal", "tax", "total") if k in sums))

        # The model's reading fills what the rules missed; when both read a field and disagree, a person decides.
        reader = "rules"
        for field, mine in (("amount", amount[0] if amount else None), ("invoice_no", inv), ("due_date", due)):
            theirs = (proposals or {}).get(field)
            if theirs is None:
                continue
            reader = "model+rules"
            if mine is None:
                if field == "amount":
                    cur, minor = theirs.value.split(" ")
                    amount = (theirs, int(minor), cur)
                elif field == "invoice_no":
                    inv = theirs
                else:
                    due = theirs
            elif mine.value != theirs.value and mode["readers"] != "off":
                flags.append("reader_disagreement")
                reasons.append(f"the rules read {field} as {mine.value!r}, the model as {theirs.value!r} (\"{theirs.quote}\")")
        if mode["bank_change"] == "off":
            bank = []
        if mode["pressure"] == "off":
            urgency = []
        quotes += bank + urgency + remit + claim[:2]
        for q in (amount[0] if amount else None, inv, due):
            if q:
                quotes.append(q)
        if bank:
            flags.append("payee_bank_details_changed")
        if urgency:
            flags.append("urgency_pressure")
        dunning_level = 0
        for level in (3, 2, 1):
            found = _phrases(text + " " + subject, DUNNING[level], "dunning")
            if found:
                dunning_level = level
                quotes += found[:1]
                flags.append(f"dunning_notice_L{level}")
                break

        is_payer = role == "payer" or (claimed and claimed[2] == "payer" and not hit)
        # Experts and consultants under the delegation of authority: who may send the request, what it must carry
        consult = doa.consultant_mandate(self.org)
        forwarded_by = (consult or {}).get("forwarders", {}).get(parsed["sender"].lower()) if staff else None
        office = doa.office_mandate(self.org)
        cash = doa.cash_mandate(self.org)
        cash_by = cash["requester_name"] if cash and staff and parsed["sender"].lower() == cash["requester_email"] and amount else None
        if cash_by:  # a cash advance instructed by the one person the delegation names: the payee is the person named
            named = next((u for u in self.org.list("user") if u.get("active", True) and u["email"].lower() != parsed["sender"].lower()
                          and u["name"].lower() in text.lower()), None)
            if named is not None:
                staff = named
                flags += ["doa_cash_advance", "doa_forwarded"]
                reasons.append(f"cash advance to {named['name']}, instructed by {cash_by}, whom the delegation of authority names for cash advances")
            else:
                cash_by = None
        custom = doa.custom_for(self.org, parsed["sender"], text + " " + subject) if staff and not cash_by else None
        custom_taken = False
        travel = doa.travel_mandate(self.org)
        travel_by = (travel or {}).get("forwarders", {}).get(parsed["sender"].lower()) if staff and channel != "dashboard" else None
        if travel_by and doa.TRAVEL_WORDS.search(text + " " + subject):
            named = next((u for u in self.org.list("user") if u.get("active", True) and u["email"].lower() != parsed["sender"].lower()
                          and u["name"].lower() in text.lower()), None)
            if named is not None:
                staff = named
                reasons.append(f"travel claim of {named['name']}, forwarded by {travel_by}, whom the delegation of authority names as a sender for travel")
        office_by = (office or {}).get("forwarders", {}).get(parsed["sender"].lower()) if staff and not forwarded_by else None
        if bank and mode["bank_change"] == "hold":
            kind = "bank_change"
        elif forwarded_by and amount and claimed and claimed[2] == "payee":
            # a consultant's request forwarded by someone the delegation names: paid to the consultant's own account
            kind, (cid, rec) = "contractor_claim", claimed[:2]
            reasons.append(f"forwarded by {forwarded_by}, whom the delegation of authority names as a sender for consultants")
            flags.append("doa_forwarded")
        elif custom and amount and claimed and claimed[2] == "payee":
            kind, (cid, rec) = "supplier_invoice", claimed[:2]
            flags.append("doa_forwarded")
            custom_taken = True
            reasons.append(f"{custom['title']} ({custom['charge_code']}), asked for by {staff['name']}, whom the delegation of authority names for it")
        elif office_by and amount and claimed and claimed[2] == "payee":
            # a vendor's bill forwarded by someone the delegation names for office and operations
            kind, (cid, rec) = "supplier_invoice", claimed[:2]
            reasons.append(f"forwarded by {office_by}, whom the delegation of authority names as a sender for office and operations")
            flags.append("doa_forwarded")
        elif staff and amount:
            kind = "expense_claim"
        elif is_payer and remit:
            kind = "remittance_notice"
        elif amount and (claim or inv):
            kind = "contractor_claim" if (rec or {}).get("payout_method") and ((rec or {}).get("default_priority") == 7
                                                                              or "hours" in text.lower()) else "supplier_invoice"
        else:
            kind = "unknown"

        extra: dict = {}
        po_hold = False
        if kind in ("supplier_invoice", "contractor_claim") and self.org is not None:
            ignore_exceed = kind == "contractor_claim" and bool(consult and consult["ignore_exceed_claim"])
            po_hold = self._purchase_order(text, subject, cid, amount, quotes, flags, reasons, extra, ignore_exceed=ignore_exceed)
            if custom_taken and rec:
                missing = doa.custom_missing(text + " " + subject, rec.get("name"), amount, custom["requirements"])
                cur = amount[2] if amount else None
                if cur not in custom["currencies"]:
                    missing.append(f"a payout currency the category allows ({', '.join(custom['currencies'])})")
                if not missing:
                    if "po_missing" in flags:
                        flags.remove("po_missing")
                        reasons[:] = [r for r in reasons if "quotes no purchase order" not in r]
                    reasons.append(f"{custom['title']}: everything the delegation requires is on the request; paid to the vendor's registered account")
                    po_hold = False
                else:
                    po_hold = True
                    reasons.append(f"{custom['title']}: not paid on the agent's own authority: missing " + "; ".join(m.lower() for m in missing))
            elif kind == "supplier_invoice" and "po_missing" in flags and office is not None and rec:
                # a bill without a purchase order is office and operations spend: the delegation decides it
                sender_ok = bool(office_by) or ("vendor" in office["senders"] and trust == "registered")
                missing = doa.office_missing(text + " " + subject, rec.get("name"), amount, office["requirements"])
                if sender_ok and not missing:
                    flags.remove("po_missing")
                    reasons[:] = [r for r in reasons if "quotes no purchase order" not in r]
                    reasons.append("office and operations bill without a purchase order: everything the delegation requires is on it")
                    po_hold = False
                else:
                    reasons.append("office and operations: not paid on the agent's own authority: "
                                   + ("the sender is not one the delegation accepts" if not sender_ok else "missing " + "; ".join(m.lower() for m in missing)))
        travel_verdict = None
        if custom_taken:  # booked to the category's charge code
            extra["account_code"] = custom["charge_code"]
        if kind == "expense_claim":
            extra = self._expense(staff, text, subject, received, amount, quotes, flags, reasons, inv)
            cid, fingerprint = f"staff:{staff['user_id']}", extra.pop("fingerprint")
            if travel is not None and "doa_cash_advance" not in flags and (str(extra.get("category") or "").lower() in ("travel", "transport") or doa.TRAVEL_WORDS.search(text + " " + subject)):
                travel_verdict = self._travel(travel, staff, text, received, amount, channel, travel_by, flags, reasons)
        else:
            fingerprint = _fingerprint(rec, inv.value if inv else None, cid) if kind != "remittance_notice" else None
        known = dict(book_fingerprints or {}) | self.store.fingerprints()
        if retriage_of:  # read again (e.g. after its PO was pulled in): the request is not a duplicate of itself
            known = {k: v for k, v in known.items() if v != retriage_of}
        duplicate_of = known.get(fingerprint) if fingerprint else None
        if duplicate_of and not mandate.rule_on(self.org, "duplicates"):
            # the mandate authority switched the duplicate rule off: a person decides; the executor's payout lock
            # still never sends a second payout for the same invoice
            flags.append("possible_duplicate")
            reasons.append(f"repeats a request already in the book or the queue ({duplicate_of}); the duplicate rule is off, a person decides")
            duplicate_of = None
            force_review = True
        else:
            force_review = False
        # with the sender check on warn or off, a registered counterparty whose Reply-To points elsewhere is treated as
        # registered, and an unknown or unauthenticated sender is held for review instead of quarantined (never paid)
        trusted = trust in ("registered", "staff") or (not sender_holds and trust == "reply_to_mismatch")
        if not sender_holds and trust != "registered":
            reasons.append(f"sender check is set to {mandate.MODE_LABEL[mode['sender']]} in the mandate: {trust.replace('_', ' ')} is not quarantined")

        if trust == "failed_authentication" and sender_holds:
            # a forged sender is the alarm even when it copies an invoice already in the book (seen live: a spoofed
            # resend of a real invoice number was filed as a mere duplicate)
            status = "QUARANTINED"
            if duplicate_of:
                reasons.append(f"it also copies a request already in the book or the queue ({duplicate_of})")
        elif duplicate_of:
            status = "DUPLICATE"
            reasons.append(f"{'claim' if kind == 'expense_claim' else 'invoice ' + inv.value if inv else 'claim'} is already "
                           f"in the book or the queue ({duplicate_of}); never paid twice")
        elif not trusted and sender_holds and (kind in ("supplier_invoice", "contractor_claim", "bank_change") or amount):
            status = "QUARANTINED"
            if bank:
                reasons.append("asks to redirect a payment to new bank details from an unverified mailbox")
            if urgency:
                reasons.append("pressure language: " + ", ".join(q.value for q in urgency))
        elif not trusted:
            status = "NEEDS_REVIEW"
        elif kind == "bank_change":
            status = "NEEDS_REVIEW"
            reasons.append("registered sender, but bank details may only change after an out-of-band call-back")
        elif kind == "expense_claim" and "doa_cash_advance" in flags:
            account = self.registry.payees.get(staff.get("payee_id") or "")
            cur = amount[2] if amount else None
            if account and cur in cash["currencies"] and (not account.get("currency") or account["currency"] == cur):
                status = "VERIFIED"
                reasons.append(f"cash advance inside the delegation: paid to {staff['name']}'s registered account, booked as an advance until settled")
            else:
                status = "NEEDS_REVIEW"
                reasons.append(f"cash advance waits for a person: {staff['name']} has no registered bank account in {cur}, or {cur} is not allowed")
        elif kind == "expense_claim" and travel_verdict in ("auto", "never"):
            # travel under the delegation of authority: complete and inside the rates is paid; outside it is rejected
            status = "VERIFIED" if travel_verdict == "auto" else "REJECTED"
        elif kind == "expense_claim":
            status = "NEEDS_REVIEW"  # always: a person approves spending; the agent never approves its own
        elif kind == "remittance_notice":
            status = "VERIFIED" if amount else "NEEDS_REVIEW"
        elif kind in ("supplier_invoice", "contractor_claim") and amount and inv:
            status = "VERIFIED"
            reasons.append(f"registered mailbox of {rec['name']}; amount and invoice quoted verbatim")
            if urgency and mode["pressure"] == "hold":
                status = "NEEDS_REVIEW"
                reasons.append("pressure language from a registered sender; a person confirms before it is payable")
            if bank:  # bank check on warn: the request is not held, and the payout still goes to the registered account
                reasons.append("mentions new bank details; the bank-change check is set to Flag only in the mandate: "
                               "paid, if at all, to the bank details already registered")
            if po_hold or any(f in flags and mode[k] == "hold" for f, k in (("totals_do_not_add_up", "totals"), ("reader_disagreement", "readers"))):
                status = "NEEDS_REVIEW"
            if force_review:
                status = "NEEDS_REVIEW"
        else:
            status = "NEEDS_REVIEW"
            reasons.append("could not read an amount and an invoice reference from the text")
        if kind == "contractor_claim" and self.org is not None and status not in ("QUARANTINED", "DUPLICATE"):
            if consult is None:
                if status == "VERIFIED":
                    status = "NEEDS_REVIEW"
                    reasons.append("experts and consultants are not delegated to the agent: a person decides")
            elif not (forwarded_by or ("consultant" in consult["senders"] and trust == "registered")):
                status = "REJECTED"
                allowed = ", ".join(doa.CONSULTANT_SENDERS[x].lower() for x in consult["senders"])
                reasons.append(f"rejected under the delegation of authority: a consultant's request is paid only on an email from {allowed}")
        warned = sorted({mandate.FLAG_CHECK[f] for f in flags if mandate.FLAG_CHECK.get(f) and mode[mandate.FLAG_CHECK[f]] == "warn"})
        if warned:
            reasons.append("set to Flag only in the mandate, flagged but not held: " + ", ".join(warned))

        req = IntakeRequest(
            intake_id=str(uuid4()), message_id=parsed["message_id"], received_at=parsed["received_at"],
            sender=parsed["sender"], sender_domain=parsed["sender"].rsplit("@", 1)[-1], display_name=parsed["display_name"],
            reply_to=parsed["reply_to"], subject=subject, kind=kind, trust=trust, counterparty_id=cid,
            counterparty_name=rec["name"] if rec else (staff["name"] if staff else None),
            claimed_name=claimed[1]["name"] if claimed else None,
            amount_minor=amount[1] if amount else None, currency=amount[2] if amount else None,
            invoice_no=inv.value if inv else None, invoice_fingerprint=fingerprint,
            due_date=due.value if due else None, flags=flags, quotes=quotes, status=status, reasons=reasons,
            dunning_level=dunning_level, attachments=parsed["attachments"], authentication=auth, reader=reader,
            duplicate_of=duplicate_of, text_sha256=hashlib.sha256(text.encode()).hexdigest(), text=text, **extra)
        if save:
            self.store.save(req)
        return req

    def _purchase_order(self, text, subject, cid, amount, quotes, flags, reasons, extra, ignore_exceed: bool = False,
                        exclude: str | None = None) -> bool:
        """Match the invoice to the PO it quotes; True when a person must look first."""
        from .. import procurement

        mode = self._modes()
        found = procurement.find_po(text, subject)
        if found is None:
            if self.org.standards().get("require_po", True) and mode["po_open"] != "off":
                flags.append("po_missing")
                reasons.append("the invoice quotes no purchase order; the finance standards pay supplier invoices "
                               "only against an open PO")
                return mode["po_open"] == "hold"
            return False
        po_id, quoted, start, end = found
        quotes.append(Quote(field="po_number", value=po_id, quote=quoted, start=start, end=end))
        extra["po_number"] = po_id
        paid = {r["invoice_fingerprint"] for r in (self.payments.po_payments(po_id) if self.payments is not None else [])}
        pending: dict[str, int] = {}
        for r in self.store.list(500):  # accepted or waiting, not yet paid, a valid draw: they hold part of the PO
            if r.po_number == po_id and r.intake_id != exclude and procurement.holds(r, paid):
                pending[r.installment or r.intake_id] = pending.get(r.installment or r.intake_id, 0) + r.amount_minor
        m = procurement.match(self.org, self.payments, po_id, supplier_id=cid, currency=amount[2] if amount else None,
                              amount_minor=amount[1] if amount else None, pending=pending)
        if ignore_exceed and "po_exceeded" in m["flags"]:  # the delegation ignores claims above the contract value
            m = m | {"flags": [f for f in m["flags"] if f != "po_exceeded"]}
            m["ok"] = m["ok"] or not m["flags"]
            reasons.append("the claim is not checked against the contract value: the delegation ignores exceeding claims")
        check = {f: mandate.FLAG_CHECK.get(f, "po_open") for f in m["flags"]}
        flags += [f for f in m["flags"] if mode[check[f]] != "off"]
        reasons += m["reasons"]
        if m["po"] is not None:
            extra["account_code"] = m["account_code"]
            if m["installment"]:
                extra["installment"] = m["installment"]
        return not m["ok"] and (not m["flags"] or any(mode[check[f]] == "hold" for f in m["flags"]))

    def ingest_record(self, bill: dict, *, source: str) -> IntakeRequest:
        """An open bill from the company's own system (sync or upload) into the same queue as emailed invoices, through
        the same checks: the vendor must be known, the PO must match, and an invoice that also arrived by email (or by
        an earlier sync) is a duplicate, so it is paid once. The source system is the evidence; each field is quoted
        as the record it came from."""
        message_id = f"<{source}:{bill['bill_id']}>"
        existing = self.store.by_message(message_id)
        if existing:
            return existing
        reg = self.registry
        rec = reg.payees.get(bill["vendor_id"])
        cite = f"{source} record {bill['bill_id']}"
        quotes = [Quote(field=k, value=str(bill[k]), quote=f"{cite}: {k}={bill[k]}", start=-1, end=-1)
                  for k in ("invoice_no", "amount_minor", "currency", "due_date", "po_number") if bill.get(k)]
        flags: list[str] = []
        reasons: list[str] = [f"from the company's own system ({source})"]
        extra: dict = {}
        if rec is None:
            reasons.append(f"vendor {bill['vendor_id']} is not in the vendor list; sync or upload the vendor first")
        amount = (Quote(field="amount", value=f"{bill['currency']} {bill['amount_minor']}", quote=f"{cite}", start=-1, end=-1),
                  bill["amount_minor"], bill["currency"])
        po_hold = False
        if rec is not None and self.org is not None:
            text = f"PO {bill['po_number']}" if bill.get("po_number") else ""
            po_hold = self._purchase_order(text, "", bill["vendor_id"], amount, [], flags, reasons, extra)
        fingerprint = _fingerprint(rec, bill["invoice_no"].upper(), bill["vendor_id"])
        duplicate_of = self.store.fingerprints().get(fingerprint)
        if duplicate_of:
            status = "DUPLICATE"
            reasons.append(f"invoice {bill['invoice_no']} is already in the queue ({duplicate_of}); never paid twice")
        elif rec is None or po_hold:
            status = "NEEDS_REVIEW"
        else:
            status = "VERIFIED"
        req = IntakeRequest(
            intake_id=str(uuid4()), message_id=message_id, received_at=_now(), sender=source, sender_domain=source.split(":")[0],
            display_name=source, reply_to=None, subject=f"Open bill {bill['invoice_no']}", kind="supplier_invoice",
            trust="system_of_record", counterparty_id=bill["vendor_id"] if rec else None, counterparty_name=rec["name"] if rec else None,
            claimed_name=None, amount_minor=bill["amount_minor"], currency=bill["currency"], invoice_no=bill["invoice_no"].upper(),
            invoice_fingerprint=fingerprint, due_date=bill["due_date"], flags=flags, quotes=quotes, status=status, reasons=reasons,
            duplicate_of=duplicate_of, text_sha256=hashlib.sha256(json.dumps(bill, sort_keys=True).encode()).hexdigest(),
            text=json.dumps(bill, sort_keys=True), **extra)
        self.store.save(req)
        return req

    # what a person may correct on a request in Pending payment, and the checks that are run again on it
    CORRECTABLE = ("counterparty_id", "invoice_no", "amount_minor", "currency", "due_date", "po_number", "account_code", "category")
    RECHECKED = {"po_missing", "po_unknown", "po_not_open", "po_supplier_mismatch", "po_currency_mismatch", "po_exceeded", "po_term_mismatch",
                 "po_expired", "payee_unknown", "payee_currency_mismatch", "duplicate_invoice"}

    def recheck(self, intake_id: str, changes: dict, *, by: str) -> tuple[IntakeRequest, list[str]]:
        """A person corrects a request in Pending payment: the corrected fields replace the read ones and the checks
        are run again on them (the payee is registered and paid in that currency, the PO is open and has room, the
        invoice is not already in the queue). Returns the request and what still stops it; an empty list means it
        passes and waits only for an approval. The original reading stays in the reasons."""
        req = self.store.get(intake_id)
        if req is None:
            raise KeyError(intake_id)
        if req.payment:
            raise PermissionError("this request is already paid; it no longer changes")
        if req.status == "REJECTED":
            raise PermissionError("a rejected request is not paid; raise a new payment order instead")
        upd = {k: changes[k] for k in self.CORRECTABLE if k in changes}
        for k in ("invoice_no", "po_number", "currency", "account_code"):
            if upd.get(k):
                upd[k] = str(upd[k]).strip().upper()
        if "po_number" in upd and not upd["po_number"]:
            upd["po_number"] = None
        if upd.get("amount_minor") is not None and (not isinstance(upd["amount_minor"], int) or upd["amount_minor"] <= 0):
            raise ValueError("the amount is above zero")
        if upd.get("due_date"):
            dt.date.fromisoformat(upd["due_date"])
        changed = [f"{k} {getattr(req, k)!r} → {v!r}" for k, v in upd.items() if getattr(req, k) != v]
        r = req.model_copy(update=upd)
        reg = self.registry
        flags = [f for f in r.flags if f not in self.RECHECKED]
        problems, reasons, extra = [], [], {}
        rec = reg.payees.get(r.counterparty_id or "") if r.kind != "remittance_notice" else reg.payers.get(r.counterparty_id or "")
        if rec is None:
            flags.append("payee_unknown")
            problems.append("the payee is not a registered beneficiary: choose one, or register it in Users & Beneficiaries")
        elif rec.get("currency") and r.currency and rec["currency"] != r.currency and r.kind != "expense_claim":
            flags.append("payee_currency_mismatch")
            problems.append(f"{rec['name']} is paid in {rec['currency']}, the request is in {r.currency}")
        if not r.amount_minor or not r.currency:
            problems.append("the amount and its currency are needed")
        if r.kind in ("supplier_invoice", "contractor_claim") and self.org is not None and rec is not None and r.amount_minor:
            quotes: list[Quote] = []
            amount = (Quote(field="amount", value=f"{r.currency} {r.amount_minor}", quote="corrected by a person", start=-1, end=-1),
                      r.amount_minor, r.currency)
            text = f"PO {r.po_number}" if r.po_number else ""
            hold = self._purchase_order(text, "", r.counterparty_id, amount, quotes, flags, reasons, extra, exclude=r.intake_id)
            if not r.po_number:
                extra |= {"po_number": None, "installment": None}
            if hold:
                problems += [x for x in reasons if "matched as its next payment" not in x]
        fp = _fingerprint(rec, (r.invoice_no or "").upper() or None, r.counterparty_id)
        dup = self.store.fingerprints().get(fp) if fp else None
        dup = dup if dup and dup != r.intake_id else None
        if dup:
            flags.append("duplicate_invoice")
            problems.append(f"invoice {r.invoice_no} is already in the queue ({dup}); it is never paid twice")
        note = f"corrected by {by}: " + ("; ".join(changed) if changed else "no field changed") + " · checks run again: " +             ("all pass, waiting for an approval" if not problems else "; ".join(problems))
        status = "NEEDS_REVIEW" if r.status in ("VERIFIED", "DUPLICATE", "QUARANTINED") else r.status
        if r.status == "QUARANTINED" and not problems:
            status = "QUARANTINED"  # a quarantine (an announced bank change) is released by its own review, not by a correction
        out = r.model_copy(update={"flags": list(dict.fromkeys(flags)), "status": status, "duplicate_of": dup,
                                   "counterparty_name": (rec or {}).get("name", r.counterparty_name),
                                   "invoice_fingerprint": fp, "reasons": [note] + [x for x in reasons if x not in problems] + req.reasons,
                                   **{k: v for k, v in extra.items() if k in ("po_number", "installment", "account_code")}})
        self.store.save(out)
        return out, problems

    def _po_terms(self, r: IntakeRequest) -> dict:
        """What the plan needs from the PO: its total and what it already paid (the three-way match), the term, the
        account code, and the unit's payment limit with the people who may approve above it."""
        if not r.po_number or self.org is None:
            return {}
        from .. import procurement

        st = procurement.status(self.org, self.payments, r.po_number)
        if st is None:
            return {}
        out = {"po_number": r.po_number, "account_code": st["po"]["account_code"], "installment": r.installment,
               "contract_total_minor": st["po"]["total_minor"], "paid_so_far_minor": st["paid_minor"]}
        unit = procurement.unit_of(self.org, st["po"]["account_code"])
        if unit is not None:
            limit, _ = self.org.payment_limit(unit["unit_id"])
            base = self.org.standards()["base_currency"]
            # approval limits are set in the base currency; another currency is not compared against them
            need = (r.amount_minor or 0) if (r.currency or base) == base else 0
            out |= {"unit_payment_limit_minor": limit,
                    "unit_approvers": [p["name"] for p in self.org.approvers_for(unit["unit_id"], need)][:20]}
        return out

    def _travel(self, travel, staff, text, received, amount, channel, travel_by, flags, reasons) -> str:
        """A travel claim against the delegation: channel, sender, places, rates. Returns auto, approve or never."""
        flags.append("doa_travel")
        if travel["channel"] == "dashboard" and channel != "dashboard":
            reasons.append("rejected under the delegation of authority: travel requests are accepted from the dashboard only")
            return "never"
        if channel != "dashboard" and not (travel_by or "traveller" in travel["senders"]):
            allowed = ", ".join(doa.TRAVEL_SENDERS[x].lower() for x in travel["senders"])
            reasons.append(f"rejected under the delegation of authority: a travel claim is accepted only by email from {allowed}")
            return "never"
        dates = [d for d in (_parse_date(m.group("date"), received) for m in ANY_DATE_RE.finditer(text)) if d]
        j = doa.judge_travel(text, travel, amount[1] if amount else None, amount[2] if amount else None, dates)
        trip = f"{j['origin'] or 'origin not named'} to {j['destination'] or 'destination not named'}"
        if j["nights"] is not None:
            trip += f", {j['nights']} night(s), {j['days']} day(s)"
        if j["allowed_minor"] is not None:
            trip += f"; the rates allow {j['currency']} {j['allowed_minor'] / 100:,.2f}"
        reasons.append(f"travel: {trip}")
        if j["verdict"] == "never":
            reasons.append("rejected under the delegation of authority: " + "; ".join(j["reasons"]))
        elif j["verdict"] == "approve":
            reasons.append("travel waits for a person: " + "; ".join(j["reasons"]))
        else:
            account = self.registry.payees.get(staff.get("payee_id") or "")
            if not account:
                reasons.append(f"travel is inside the delegation, but {staff['name']} has no registered bank account (People and roles); a person pays it")
                return "approve"
            cur = amount[2] if amount else None
            if cur not in travel["currencies"] or (account.get("currency") and account["currency"] != cur):
                reasons.append(f"travel is inside the delegation, but {cur} is not an allowed payout currency or {staff['name']}'s account receives "
                               f"{account.get('currency')}; a person pays it")
                return "approve"
            reasons.append("travel is inside the delegation: the agent pays it to the traveller's registered account")
        return j["verdict"]

    def _expense(self, staff, text, subject, received, amount, quotes, flags, reasons, inv) -> dict:
        """Check a staff claim against Finance standards and Unit prices, and name its approvers."""
        std = self.org.standards()
        _, minor, cur = amount
        chain = claims.unit_chain(self.org, staff["unit_id"])
        cat = claims.category(text, subject, std["expense_categories"])
        spent = claims.expense_date(text, received, ANY_DATE_RE, _parse_date)
        for q in (cat, spent):
            if q:
                quotes.append(q)
        if cat is None:
            self._flag(flags, "category_unknown", "claims")
            reasons.append("no expense category from the finance standards is named: " + ", ".join(std["expense_categories"]))
        if spent is None:
            self._flag(flags, "expense_date_missing", "claims")
            reasons.append("no date of the expense was found")
        elif (received - dt.date.fromisoformat(spent.value)).days > std["expense_claim_deadline_days"]:
            self._flag(flags, "claim_past_deadline", "claims")
            reasons.append(f"spent {spent.value}, claimed {received.isoformat()}: more than the "
                           f"{std['expense_claim_deadline_days']} days the finance standards allow")
        if cat is not None:
            cap = claims.unit_price_cap(self.org, chain, cat.value, cur, text)
            if cap:
                limit, why, qty = cap
                quotes.append(qty)
                if minor > limit:
                    self._flag(flags, "above_unit_price", "unit_price")
                    reasons.append(f"{cur} {minor / 100:,.2f} claimed, unit prices allow {cur} {limit / 100:,.2f} ({why})")
        people, note = claims.approvers(self.org, staff, chain, minor, cur, std["base_currency"])
        if people:
            reasons.append("route to " + ", ".join(f"{p['name']} ({p['role'].replace('_', ' ')})" for p in people[:3])
                           + "; the claimant never approves their own claim")
        else:
            self._flag(flags, "no_approver", "approver")
            reasons.append("no active approver in the claimant's unit chain has a limit that covers this amount")
        if note:
            reasons.append(note)
        ref = inv.value if inv else f"{cur}{minor}-{spent.value if spent else received.isoformat()}"
        return {"unit_id": staff["unit_id"], "category": cat.value if cat else None,
                "expense_date": spent.value if spent else None, "approvers": [p["user_id"] for p in people],
                "fingerprint": f"staff:{staff['user_id']}:{ref}"}

    def ingest_dir(self, folder: Path, *, book_fingerprints: dict[str, str] | None = None) -> list[IntakeRequest]:
        return [self.ingest(p.read_bytes(), book_fingerprints=book_fingerprints)
                for p in sorted(folder.glob("*.eml"))]

    def review(self, intake_id: str, review: ReviewRequest) -> IntakeRequest:
        req = self.store.get(intake_id)
        if req is None:
            raise KeyError(intake_id)
        if req.kind == "expense_claim":
            # a staff claim is approved by a finance role, never by the person who spent the money
            if review.decision == "verify" and review.role not in claims.APPROVER_ROLES:
                allowed = ", ".join(claims.APPROVER_ROLES)
                raise PermissionError(f"role {review.role} may not approve expense claims; allowed: {allowed}")
            if review.reviewer.strip().lower() == (req.counterparty_name or "").strip().lower():
                raise PermissionError("the claimant cannot review their own claim")
        elif review.role not in self.reviewer_roles:
            raise PermissionError(f"role {review.role} may not review intake; allowed: {', '.join(self.reviewer_roles)}")
        if "internal_order" in req.flags and review.decision == "verify" and \
                review.reviewer.strip().lower() == (req.display_name or "").strip().lower():
            raise PermissionError("the person who asked for this payment cannot approve it")
        if req.status == "DUPLICATE":
            raise PermissionError("a duplicate invoice cannot be verified; the original is already in the book")
        if review.decision == "verify" and req.counterparty_id is None:
            raise PermissionError("cannot verify a request from a sender who is not registered for any counterparty; "
                                  "register the mailbox first")
        req = req.model_copy(update={"status": "VERIFIED" if review.decision == "verify" else "REJECTED",
                                     "reviewer": f"{review.reviewer} ({review.role})", "review_note": review.note})
        self.store.save(req)
        self.store.review(intake_id, review)
        return req

    # -- into the treasury loop ------------------------------------------------------------------

    def to_state(self, base: TreasuryState, *, as_of: dt.date, requests: list[IntakeRequest] | None = None) -> TreasuryState:
        """Add the queue to a book. VERIFIED claims become payable obligations with verified evidence;
        NEEDS_REVIEW and QUARANTINED claims become obligations with *pending* evidence, so they are
        visible in the plan but Verity never lets them be paid; DUPLICATE and REJECTED are recorded as
        evidence only. A VERIFIED remittance becomes a receivable with the payer's history."""
        data = base.model_dump()
        if not data.get("as_of"):
            data["as_of"] = as_of.isoformat()  # aging and the period cut-off reason from the same date
        reqs = requests if requests is not None else self.store.list()
        existing_ids = {o["obligation_id"] for o in data["obligations"]}
        for r in sorted(reqs, key=lambda x: x.received_at):
            ref = f"intake:{r.intake_id[:8]}"
            summary = (f"{r.kind} from {r.sender} ({r.trust}); " + "; ".join(r.reasons))[:300]
            if r.kind == "remittance_notice" and r.counterparty_id and r.status == "VERIFIED":
                payer = self.registry.payers[r.counterparty_id]
                hours = max(0, int((dt.date.fromisoformat(r.due_date) - as_of).total_seconds() // 3600)) if r.due_date else 24
                rid = f"RCV-{r.counterparty_id.upper()}-{(r.invoice_no or r.intake_id[:4]).upper()}"
                if rid not in {x["receivable_id"] for x in data["receivables"]}:
                    data["receivables"].append({"receivable_id": rid, "payer": payer["name"], "amount_minor": r.amount_minor,
                                                "currency": r.currency, "expected_in_hours": min(hours, 2160),
                                                "confidence_bp": 9000, "evidence_ref": ref,
                                                "payer_history": payer.get("history")})
                    data["evidence"].append({"ref": ref, "subject_id": rid, "source": f"intake:email:{r.sender}",
                                             "status": "verified", "summary": summary})
                continue
            traveller = None
            if r.kind == "expense_claim" and r.status == "VERIFIED" and ({"doa_travel", "doa_cash_advance"} & set(r.flags)) and self.org is not None:
                u = self.org.get("user", (r.counterparty_id or "").removeprefix("staff:"))
                traveller = self.registry.payees.get((u or {}).get("payee_id") or "")
            if r.kind == "expense_claim" and traveller:
                # a travel claim the delegation of authority lets the agent pay: to the traveller's own registered account
                oid = f"{'ADVANCE' if 'doa_cash_advance' in r.flags else 'TRAVEL'}-{r.intake_id[:8].upper()}"
                if oid not in existing_ids:
                    existing_ids.add(oid)
                    data["obligations"].append({
                        "obligation_id": oid, "amount_minor": r.amount_minor, "currency": r.currency, "priority": 6, "due_in_hours": 24,
                        "beneficiary_id": next(cid for cid, p in self.registry.payees.items() if p is traveller), "payee_name": traveller["name"],
                        "payout_method": traveller.get("payout_method", "LOCAL"), "evidence_ref": ref, "invoice_fingerprint": r.invoice_fingerprint,
                        "budget_line": traveller.get("budget_line")})
                    data["evidence"].append({"ref": ref, "subject_id": oid, "source": f"intake:email:{r.sender}", "status": "verified", "summary": summary})
                continue
            if r.kind == "expense_claim":
                # recorded, never paid by the agent yet: a reimbursement needs the employee's bank details registered
                # as a verified payee first, and that registration is a person's job
                status = {"VERIFIED": "verified", "NEEDS_REVIEW": "pending"}.get(r.status, "rejected")
                data["evidence"].append({"ref": ref, "subject_id": r.intake_id, "source": f"intake:email:{r.sender}",
                                         "status": status, "summary": summary})
                continue
            if r.status in ("DUPLICATE", "REJECTED") or r.kind in ("unknown", "remittance_notice"):
                data["evidence"].append({"ref": ref, "subject_id": r.duplicate_of or r.intake_id,
                                         "source": f"intake:email:{r.sender}", "status": "rejected", "summary": summary})
                # Dunning: a reminder for an invoice already in the book raises that obligation's dunning level and never
                # creates a second payable.
                if r.status == "DUPLICATE" and r.dunning_level:
                    for o in data["obligations"]:
                        same_invoice = r.invoice_fingerprint and o.get("invoice_fingerprint") == r.invoice_fingerprint
                        if o["obligation_id"] == r.duplicate_of or same_invoice:
                            o["dunning_level"] = max(int(o.get("dunning_level") or 0), r.dunning_level)
                continue
            rec = self.registry.payees.get(r.counterparty_id or "") or {}
            if not rec:  # unknown sender: still shown, pending, never payable (no beneficiary to pay)
                claimed = next((cid for cid, p in self.registry.payees.items() if p["name"] == r.claimed_name), None)
                rec = self.registry.payees.get(claimed or "", {})
            if not rec or not r.amount_minor:
                data["evidence"].append({"ref": ref, "subject_id": r.intake_id, "source": f"intake:email:{r.sender}",
                                         "status": "pending", "summary": summary})
                continue
            oid = f"REQ-{(r.invoice_no or r.intake_id[:6]).upper()}"
            if oid in existing_ids:
                oid = f"{oid}-{r.intake_id[:4]}"
            existing_ids.add(oid)
            due_hours = 72
            if r.due_date:
                due_hours = max(0, min(720, int((dt.date.fromisoformat(r.due_date) - as_of).days * 24)))
            data["obligations"].append({
                "obligation_id": oid, "amount_minor": r.amount_minor, "currency": r.currency or rec["currency"],
                "priority": rec.get("default_priority", 5), "due_in_hours": due_hours,
                "beneficiary_id": next(cid for cid, p in self.registry.payees.items() if p is rec),
                "payee_name": rec["name"], "payout_method": rec.get("payout_method", "LOCAL"), "evidence_ref": ref,
                "invoice_fingerprint": r.invoice_fingerprint, "budget_line": rec.get("budget_line"),
                "policy_flags": [f for f in r.flags if f == "payee_bank_details_changed" and self._modes()["bank_change"] == "hold"],
                "due_date": r.due_date, "dunning_level": r.dunning_level} | self._po_terms(r))
            data["evidence"].append({"ref": ref, "subject_id": oid, "source": f"intake:email:{r.sender}",
                                     "status": "verified" if r.status == "VERIFIED" else "pending", "summary": summary})
        # Receivables the company's own system holds (synced or uploaded): expected receipts, calibrated by the payer's
        # history of keeping payment promises; never cash until Airwallex reports the deposit settled.
        known = {x["receivable_id"] for x in data["receivables"]}
        for ar in self.registry._master.list("receivable"):
            rid = f"AR-{ar['receivable_id']}"[:80]
            payer = self.registry.payers.get(ar["customer_id"])
            if rid in known or payer is None or ar["currency"] not in ("USD", "EUR", "GBP"):
                continue
            hours = max(0, min(2160, int((dt.date.fromisoformat(ar["due_date"]) - as_of).days * 24)))
            ref = f"master:{ar['_source']}:{ar['receivable_id']}"
            data["receivables"].append({"receivable_id": rid, "payer": payer["name"], "amount_minor": ar["amount_minor"],
                                        "currency": ar["currency"], "expected_in_hours": hours, "confidence_bp": 8000,
                                        "evidence_ref": ref, "payer_history": payer.get("history")})
            data["evidence"].append({"ref": ref, "subject_id": rid, "source": ar["_source"], "status": "verified",
                                     "summary": f"open customer invoice {ar['invoice_no']} in the company's system, due {ar['due_date']}"})
        data["obligations"] = data["obligations"][:12]
        from .. import priority_sop

        # the FCO's Standard Payment Priority Policy gives each obligation its priority (once saved)
        return priority_sop.apply(TreasuryState.model_validate(data), self.org, self.registry.payees)
