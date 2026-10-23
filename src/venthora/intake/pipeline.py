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

from ..config import ROOT, data_path
from ..domain.models import TreasuryState
from .models import IntakeRequest, Quote, ReviewRequest

PAYEES_FILE = ROOT / "data" / "payees.json"
FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.id", "outlook.com", "hotmail.com",
            "live.com", "icloud.com", "proton.me", "protonmail.com"}

CURRENCIES = {"USD": "USD", "US$": "USD", "$": "USD", "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP"}
AMOUNT_RE = re.compile(r"(?P<cur>USD|EUR|GBP|US\$|\$|€|£)\s?(?P<amt>\d{1,3}(?:[,.]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
                       r"|(?P<amt2>\d{1,3}(?:[,.]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?(?P<cur2>USD|EUR|GBP)\b")
INVOICE_RE = re.compile(r"\b(?:invoice|inv\.?|bill|statement|claim|timesheet)\s*(?:no\.?|number|#|ref\.?|:)?\s*"
                        r"(?P<no>[A-Z]{1,6}[-/]?\d{2,}(?:[-/]\d{1,6})*)", re.IGNORECASE)
INVOICE_BARE_RE = re.compile(r"\b(?P<no>[A-Z]{2,6}-\d{2,}(?:-\d{1,6})?)\b")
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
    def __init__(self, path: Path = PAYEES_FILE):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.payees: dict[str, dict] = data["payees"]
        self.payers: dict[str, dict] = data["payers"]

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
    text = re.sub(r"[ \t]+", " ", text).strip()
    try:
        received = parsedate_to_datetime(msg.get("Date")).astimezone(dt.UTC).isoformat() if msg.get("Date") else _now()
    except (TypeError, ValueError):
        received = _now()
    return {"message_id": (msg.get("Message-ID") or f"<{hashlib.sha256(raw).hexdigest()[:24]}@local>").strip(),
            "received_at": received, "sender": addr.lower(), "display_name": display.strip(),
            "reply_to": reply[0][1].lower() if reply and reply[0][1] else None,
            "subject": (msg.get("Subject") or "").strip(), "text": text[:20_000]}


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


def _invoice(text: str, subject: str) -> Quote | None:
    for src, offset in ((subject, -1), (text, 0)):
        m = INVOICE_RE.search(src) or INVOICE_BARE_RE.search(src)
        if m:
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
                 reviewer_roles: tuple[str, ...] = ("finance_manager", "ap_clerk")):
        self.store = store or IntakeStore()
        self.registry = registry or Registry()
        self.reviewer_roles = reviewer_roles

    def ingest(self, raw: bytes, *, book_fingerprints: dict[str, str] | None = None, save: bool = True) -> IntakeRequest:
        """Triage one raw email. save=False (dry run) triages without touching the queue."""
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

        hit = reg.by_contact(parsed["sender"])
        claimed = reg.claimed(parsed["display_name"], subject, text)
        if hit and parsed["reply_to"] and parsed["reply_to"] != parsed["sender"] and not reg.by_contact(parsed["reply_to"]):
            trust = "reply_to_mismatch"
            reasons.append(f"Reply-To {parsed['reply_to']} is not a registered mailbox of {hit[1]['name']}")
        elif hit:
            trust = "registered"
        else:
            trust = "unknown_sender"
            reasons.append(f"{parsed['sender']} is not a registered mailbox" + (f" of {claimed[1]['name']}, which the message "
                                                                                 "claims to be" if claimed else ""))
        cid, rec, role = hit if hit else (None, None, None)

        bank = _phrases(text, BANK_CHANGE, "bank_change")
        urgency = _phrases(text + " " + subject, URGENCY, "urgency")
        remit = _phrases(text, REMITTANCE, "remittance")
        claim = _phrases(text + " " + subject, CLAIM, "claim")
        amount = _amount(text)
        inv = _invoice(text, subject)
        due = _due(text, received)
        quotes += bank + urgency + remit + claim[:2]
        if amount:
            quotes.append(amount[0])
        if inv:
            quotes.append(inv)
        if due:
            quotes.append(due)
        if bank:
            flags.append("payee_bank_details_changed")
        if urgency:
            flags.append("urgency_pressure")

        is_payer = role == "payer" or (claimed and claimed[2] == "payer" and not hit)
        if bank:
            kind = "bank_change"
        elif is_payer and remit:
            kind = "remittance_notice"
        elif amount and (claim or inv):
            kind = "contractor_claim" if (rec or {}).get("payout_method") and ((rec or {}).get("default_priority") == 7
                                                                              or "hours" in text.lower()) else "supplier_invoice"
        else:
            kind = "unknown"

        counterparty_rec = rec or (claimed[1] if claimed else None)
        fingerprint = _fingerprint(rec, inv.value if inv else None, cid) if kind != "remittance_notice" else None
        known = dict(book_fingerprints or {}) | self.store.fingerprints()
        duplicate_of = known.get(fingerprint) if fingerprint else None

        if duplicate_of:
            status = "DUPLICATE"
            reasons.append(f"invoice {inv.value} is already in the book or the queue ({duplicate_of}); never paid twice")
        elif trust != "registered" and (kind in ("supplier_invoice", "contractor_claim", "bank_change") or amount):
            status = "QUARANTINED"
            if bank:
                reasons.append("asks to redirect a payment to new bank details from an unverified mailbox")
            if urgency:
                reasons.append("pressure language: " + ", ".join(q.value for q in urgency))
        elif trust != "registered":
            status = "NEEDS_REVIEW"
        elif kind == "bank_change":
            status = "NEEDS_REVIEW"
            reasons.append("registered sender, but bank details may only change after an out-of-band call-back")
        elif kind == "remittance_notice":
            status = "VERIFIED" if amount else "NEEDS_REVIEW"
        elif kind in ("supplier_invoice", "contractor_claim") and amount and inv:
            status = "VERIFIED"
            reasons.append(f"registered mailbox of {rec['name']}; amount and invoice quoted verbatim")
            if urgency:
                status = "NEEDS_REVIEW"
                reasons.append("pressure language from a registered sender; a person confirms before it is payable")
        else:
            status = "NEEDS_REVIEW"
            reasons.append("could not read an amount and an invoice reference from the text")

        req = IntakeRequest(
            intake_id=str(uuid4()), message_id=parsed["message_id"], received_at=parsed["received_at"],
            sender=parsed["sender"], sender_domain=parsed["sender"].rsplit("@", 1)[-1], display_name=parsed["display_name"],
            reply_to=parsed["reply_to"], subject=subject, kind=kind, trust=trust, counterparty_id=cid,
            counterparty_name=rec["name"] if rec else None, claimed_name=claimed[1]["name"] if claimed else None,
            amount_minor=amount[1] if amount else None, currency=amount[2] if amount else None,
            invoice_no=inv.value if inv else None, invoice_fingerprint=fingerprint,
            due_date=due.value if due else None, flags=flags, quotes=quotes, status=status, reasons=reasons,
            duplicate_of=duplicate_of, text_sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)
        _ = counterparty_rec
        if save:
            self.store.save(req)
        return req

    def ingest_dir(self, folder: Path, *, book_fingerprints: dict[str, str] | None = None) -> list[IntakeRequest]:
        return [self.ingest(p.read_bytes(), book_fingerprints=book_fingerprints)
                for p in sorted(folder.glob("*.eml"))]

    def review(self, intake_id: str, review: ReviewRequest) -> IntakeRequest:
        req = self.store.get(intake_id)
        if req is None:
            raise KeyError(intake_id)
        if review.role not in self.reviewer_roles:
            raise PermissionError(f"role {review.role} may not review intake; allowed: {', '.join(self.reviewer_roles)}")
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
            if r.status in ("DUPLICATE", "REJECTED") or r.kind in ("unknown", "remittance_notice"):
                data["evidence"].append({"ref": ref, "subject_id": r.duplicate_of or r.intake_id,
                                         "source": f"intake:email:{r.sender}", "status": "rejected", "summary": summary})
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
                "invoice_fingerprint": r.invoice_fingerprint,
                "policy_flags": [f for f in r.flags if f == "payee_bank_details_changed"]})
            data["evidence"].append({"ref": ref, "subject_id": oid, "source": f"intake:email:{r.sender}",
                                     "status": "verified" if r.status == "VERIFIED" else "pending", "summary": summary})
        data["obligations"] = data["obligations"][:12]
        return TreasuryState.model_validate(data)
