# ruff: noqa: E501
"""A staff advance approved by email: allocated first, paid at 23:59 company time, then the card and its link.

1. Two emails approve an advance:
     - the FCO's own email naming the person ("Cash advance for Dana Finch");
     - a registered person's email that forwards the FCO's approval: the forwarded part is from the FCO's registered
       address and says it is approved. A forwarded text can be typed by anyone, so this is not the last word: the
       FCO is told at once (step 2) and can cancel until the payment time.
   Numbers. None of these needs a purchase request or a purchase order. A cash advance, petty cash or slush fund
   email that states its request number keeps it; one that states none is numbered by VENTHORA. A travel advance
   carries a Travel Authorisation (Travel Clearance) number the same way: the email's own, or one VENTHORA makes; when
   that number is on file it must be approved and for that person. A travel advance goes on the person's own staff
   card, the card the person added to Airi (card_program.py); the others follow the card program's purposes.
2. Allocation. The amount is set aside when it is green: the money above the reserve floor, less what falls due
   inside the planning horizon (72 hours unless the FCO chose another), covers it. The person and the FCO get one
   email: allocated, paid at 23:59 company time, in so many hours. When it is not green nothing is promised: both are
   told why, and the request waits for a person.
3. At 23:59 the scheduler pays what was allocated: the card is created (or the staff card opened) and funded, and the
   person gets the second email with the link to the card page. Safe mode (continuity/guard.py) stops this run like
   every other automatic payment.
4. The card link shows the card (masked number, what it may spend, until when). The full card number is never held by
   VENTHORA. A person who turned on the second factor (an authenticator app such as Google Authenticator) is asked
   for its code before the page shows anything: no SMS and no WhatsApp message is sent by VENTHORA.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
from zoneinfo import ZoneInfo

FORWARD_MARK = re.compile(r"(-{2,}\s*forwarded message\s*-{2,}|begin forwarded message|-{3,}\s*original message\s*-{3,}|^\s*>?\s*from:\s)", re.I | re.M)
FROM_LINE = re.compile(r"^\s*>?\s*(?:from|dari)\s*:\s*(.+)$", re.I | re.M)
ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
APPROVED = re.compile(r"\b(approved|i approve|approve this|ok to pay|go ahead|disetujui|setuju|saya setujui)\b", re.I)
REFUSED = re.compile(r"\b(not approved|do not approve|cannot approve|rejected|declined|tidak disetujui|tidak setuju|ditolak)\b", re.I)
TA_LABEL = re.compile(r"\b(?:travel\s+(?:authori[sz]ation|clearance)|ta|tc)\s*(?:no\.?|number|nomor|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9/-]{2,30})\b", re.I)
TA_SHAPE = re.compile(r"\b((?:TA|TC)[-/][A-Z0-9][A-Z0-9/-]{1,27})\b")
PAY_HOUR, PAY_MINUTE = 23, 59
MAX_ATTEMPTS = 3


def forwarded_approval(body: str, fco_emails: set[str]) -> dict | None:
    """The FCO's approval inside a forwarded email: {"from": the FCO's address, "quote": the approving line}. None when
    the email forwards nothing from the FCO, or what the FCO wrote does not approve."""
    text = body or ""
    mark = FORWARD_MARK.search(text)
    if mark is None:
        return None
    part = text[mark.start():]
    wanted = {e.lower() for e in fco_emails}
    sender = next((a.lower() for line in FROM_LINE.findall(part) for a in ADDRESS.findall(line) if a.lower() in wanted), None)
    if sender is None:
        return None
    said = part[part.lower().find(sender):]  # what follows the FCO's From line
    if REFUSED.search(said):
        return None
    hit = APPROVED.search(said)
    if hit is None:
        return None
    line = next((ln.strip(" >\t") for ln in said.splitlines() if hit.group(0).lower() in ln.lower()), hit.group(0))
    return {"from": sender, "quote": line[:200]}


def ta_number(subject: str, body: str) -> str | None:
    """The Travel Authorisation (Travel Clearance) number an email carries, upper case; None when it carries none."""
    text = f"{subject}\n{body}"
    m = TA_SHAPE.search(text.upper())
    if m:
        return m.group(1)
    m = TA_LABEL.search(text)
    if m and any(ch.isdigit() for ch in m.group(1)):
        return m.group(1).upper()
    return None


REQUEST_NO = re.compile(r"\b(?:cash\s+advance|travel\s+advance|advance|petty\s+cash|slush\s+fund|request|reference|ref)\s*(?:request\s*)?"
                        r"(?:no\.?|number|nomor|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9/-]{2,30})\b", re.I)


def request_number(subject: str, body: str) -> str | None:
    """The advance, petty cash or slush fund request number an email states ("Advance request no: CA-2026-014"), upper
    case; None when it states none and VENTHORA numbers the request itself. No purchase request or purchase order
    number is needed for any of these."""
    for m in REQUEST_NO.finditer(f"{subject}\n{body}"):
        n = m.group(1).upper()
        if any(ch.isdigit() for ch in n) and not TA_SHAPE.fullmatch(n):
            return n
    return None


def next_ta(taken: set[str], year: int) -> str:
    """A travel authorisation number of VENTHORA's own, for a travel advance whose email states none."""
    return next(f"TA-{year}-{n:04d}" for n in range(1, 100_000) if f"TA-{year}-{n:04d}" not in taken)


def check_ta(ta: dict | None, number: str, person: dict, amount_minor: int, currency: str) -> str | None:
    """Why the travel authorisation on file does not cover the advance; None when it does. A number that is not on
    file is the email's own reference: it is kept with the advance, and the FCO's approval is what authorises it."""
    if ta is None:
        return None
    if ta.get("status") != "APPROVED":
        return f"travel authorisation {number} is {str(ta.get('status')).lower()}, not approved"
    if ta.get("traveller") != person["user_id"]:
        return f"travel authorisation {number} is not for {person['name']}"
    if ta.get("estimated_minor") and ta.get("currency") == currency and amount_minor > ta["estimated_minor"]:
        return f"the advance is above the {currency} {ta['estimated_minor'] / 100:,.2f} travel authorisation {number} estimates"
    return None


def pay_at(now: dt.datetime, tz: str) -> dt.datetime:
    """The next 23:59 in the company's time zone, in UTC."""
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - an unknown zone name: UTC
        zone = dt.UTC
    local = now.astimezone(zone)
    at = local.replace(hour=PAY_HOUR, minute=PAY_MINUTE, second=0, microsecond=0)
    if at <= local:
        at += dt.timedelta(days=1)
    return at.astimezone(dt.UTC)


def time_left(now: dt.datetime, at: dt.datetime) -> str:
    minutes = max(int((at - now).total_seconds() // 60), 0)
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def green(*, cost_base_minor: int, base_balance_minor: int, floor_minor: int, due_base_minor: int, horizon_hours: int, base: str = "USD") -> tuple[bool, str]:
    """(green, why): the money above the reserve floor, less what falls due inside the horizon, covers the advance."""
    free = base_balance_minor - floor_minor - due_base_minor
    money = lambda v: f"{base} {v / 100:,.2f}"  # noqa: E731
    if free >= cost_base_minor:
        return True, (f"{money(free)} is free above the {money(floor_minor)} reserve floor after the {money(due_base_minor)} due in the next "
                      f"{horizon_hours} hours; the advance needs {money(cost_base_minor)}")
    return False, (f"only {money(max(free, 0))} is free above the {money(floor_minor)} reserve floor after the {money(due_base_minor)} due in the next "
                   f"{horizon_hours} hours; the advance needs {money(cost_base_minor)}")


def allocation(*, now: dt.datetime, tz: str, ok: bool, why: str, route: str, approval: str) -> dict:
    at = pay_at(now, tz)
    return {"status": "ALLOCATED" if ok else "WAITING", "green": ok, "why": why, "route": route, "approval": approval,
            "at": now.isoformat(timespec="seconds"), "pay_at": at.isoformat(timespec="seconds") if ok else None, "timezone": tz, "attempts": 0}


def due(fr: dict, now: dt.datetime) -> bool:
    a = fr.get("allocation") or {}
    return (fr.get("status") == "APPROVED" and a.get("status") == "ALLOCATED" and bool(a.get("pay_at"))
            and dt.datetime.fromisoformat(a["pay_at"]) <= now and a.get("attempts", 0) < MAX_ATTEMPTS)


ROUTES = {"staff_card": "your staff card (the card you added to Airi)", "virtual_card": "a virtual Visa card issued for this advance"}


def _money(fr: dict) -> str:
    return f"{fr['currency']} {fr['amount_minor'] / 100:,.2f}"


def _local(at_iso: str, tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone, tz = dt.UTC, "UTC"
    return dt.datetime.fromisoformat(at_iso).astimezone(zone).strftime("%d %b %Y %H:%M") + f" ({tz})"


def allocated_notice(fr: dict, person: dict, kind_label: str, now: dt.datetime) -> tuple[str, str]:
    """The first email, to the person and the FCO: the money is set aside and when it is paid."""
    a = fr["allocation"]
    at = dt.datetime.fromisoformat(a["pay_at"])
    ta = f"Travel authorisation: {fr['ta_number']}\n" if fr.get("ta_number") else ""
    return (f"{kind_label} {fr['request_number']}: allocated, paid at {PAY_HOUR}:{PAY_MINUTE:02d} (in {time_left(now, at)})",
            f"Dear {person.get('name', '')},\n\n{kind_label} {fr['request_number']} is approved and its money is set aside.\n\n"
            f"Amount: {_money(fr)}\nFor: {person.get('name', '')}\nPurpose: {fr.get('purpose', '')}\n{ta}"
            f"Approved by: {fr.get('decided_by', '')}\nPaid at: {_local(a['pay_at'], a['timezone'])}, in {time_left(now, at)}\n"
            f"Paid to: {ROUTES.get(a['route'], a['route'])}\n\n"
            "When the money is on the card you get a second email with the link to the card.\n\n"
            "To the FCO: if this advance should not be paid, cancel it in VENTHORA (Cash advance) before the payment time.\n\nFinance team")


def waiting_notice(fr: dict, person: dict, kind_label: str) -> tuple[str, str]:
    a = fr["allocation"]
    return (f"{kind_label} {fr['request_number']}: approved, not yet allocated",
            f"Dear {person.get('name', '')},\n\n{kind_label} {fr['request_number']} ({_money(fr)}, {fr.get('purpose', '')}) is approved, but its money "
            f"could not be set aside now: {a['why']}.\n\nNothing is promised yet. Finance will pay it from VENTHORA (Cash advance) once the money is "
            "there, and you will be told.\n\nFinance team")


def funded_notice(fr: dict, person: dict, kind_label: str, card: dict, link: str, second_factor: bool) -> tuple[str, str]:
    """The second email, to the person: the money is on the card, and the link to it."""
    last4 = str(card.get("masked") or "")[-4:]
    until = (fr.get("card_window") or {}).get("until") or fr.get("settle_by")
    gate = ("The link asks for the code from your authenticator app (the second factor you turned on)." if second_factor else
            "Turn on the second factor in VENTHORA (an authenticator app such as Google Authenticator) and the link will ask for its code; "
            "VENTHORA sends no SMS or WhatsApp code.")
    return (f"{kind_label} {fr['request_number']}: the money is on your card",
            f"Dear {person.get('name', '')},\n\n{_money(fr)} for {kind_label.lower()} {fr['request_number']} ({fr.get('purpose', '')}) is now on "
            f"{ROUTES.get(fr.get('paid_by') or '', 'your card')}, ending {last4}.\n\nYour card: {link}\n\n"
            f"It can spend {_money(fr)} in total" + (f", until {str(until)[:10]}" if until else "") + ", wherever Visa is accepted, online or added to your "
            f"phone's wallet. Keep every receipt: what you do not spend never leaves the company.\n\n{gate}\n"
            "The full card number is never sent by email.\n\nFinance team")


def make_link(secret: bytes, number: str) -> str:
    """The card link's token: the request number and a signature. It names one advance and is useless for any other."""
    ref = base64.urlsafe_b64encode(number.encode()).decode().rstrip("=")
    sig = hmac.new(secret, f"card-link|{number}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ref}.{sig}"


def read_link(secret: bytes, token: str) -> str | None:
    """The request number a genuine token names; None for anything else."""
    ref, _, sig = (token or "").partition(".")
    try:
        number = base64.urlsafe_b64decode(ref + "=" * (-len(ref) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    good = hmac.new(secret, f"card-link|{number}".encode(), hashlib.sha256).hexdigest()[:32]
    return number if sig and hmac.compare_digest(sig, good) else None
