# ruff: noqa: E501
"""Platform Setup → Airwallex Cards: the company's card program, set once by the Finance Director.

* Purposes: what a staff card may be used for (cash advance, travel advance, petty cash, slush fund, entertainment,
  online transport, office and operation expenses, subscriptions, ...). An approved advance whose purpose is ticked
  goes on the requester's own staff card; one that is not ticked is paid as before (its own card or a transfer).
* Each staff has a virtual card: one Visa virtual card per person, issued once and kept inactive. Airwallex, as the
  issuer, sets the card number; VENTHORA labels each card with the person's employee number so it is recognised.
* The card limit per charge is the Finance Director's "pay by virtual card up to" in the mandate (USD 5,000 until
  changed; "no limit" leaves only the mandate's single payout limit).
* Auto-activated N days after the advance is approved, auto-deactivated M days later (0 = at the advance's
  settle-by date): outside that window, or once the approved amount is spent, VENTHORA declines the charge at remote
  authorization and the card is set inactive on Airwallex.
* Merchants: anywhere on the Visa network, or only selected merchants (merchant category codes and names; the
  ticked purposes bring their usual categories); any other merchant is declined.
"""
from __future__ import annotations

import datetime as dt

KIND, ID = "card_program", "program"
# key, label, the merchant categories (MCC) the purpose usually needs (used when only selected merchants are allowed)
PURPOSES: list[tuple[str, str, list[str]]] = [
    ("cash_advance", "Cash advance", []),
    ("travel_advance", "Travel advance", ["3000", "4511", "4111", "4112", "4121", "4131", "4722", "7011", "7512"]),
    ("petty_cash", "Petty cash", []),
    ("slush_fund", "Slush fund", []),
    ("entertainment", "Entertainment & services", ["5812", "5813", "5814", "7832", "7999"]),
    ("online_transport", "Online transportation", ["4121", "4111", "4789"]),
    ("office_operation", "Office & operation expense", ["5111", "5943", "5044", "5045", "5734", "4814", "4900"]),
    ("subscriptions", "Software & subscriptions", ["5734", "5817", "5818", "7372"]),
    ("fuel_vehicle", "Fuel & vehicle", ["5541", "5542", "7523", "7538"]),
    ("training_events", "Training & events", ["8299", "7399", "8220"]),
]
LABEL = {k: lbl for k, lbl, _ in PURPOSES}
MCC = {k: m for k, _, m in PURPOSES}
LIMIT_CHOICES = [10_000, 50_000, 100_000, 300_000, 500_000, 1_000_000, None]  # USD minor; None = no limit of its own
NO_LIMIT = 10**13  # what "no limit" is saved as in the mandate (its maximum)
DEFAULT = {"staff_cards": False, "purposes": ["cash_advance", "travel_advance", "petty_cash", "slush_fund"],
           "activate_after_days": 0, "deactivate_after_days": 0, "merchant_mode": "all", "merchants": []}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def get(org) -> dict:
    rec = org.get(KIND, ID) or {}
    return DEFAULT | {k: v for k, v in rec.items() if not k.startswith("_") and k in DEFAULT}


def validate(body: dict) -> dict:
    p = get_defaults() | {k: body[k] for k in DEFAULT if k in body}
    p["staff_cards"] = bool(p["staff_cards"])
    bad = [x for x in p["purposes"] if x not in LABEL]
    if bad:
        raise ValueError(f"unknown purpose {bad[0]}")
    p["purposes"] = [k for k, *_ in PURPOSES if k in set(p["purposes"])]
    for k in ("activate_after_days", "deactivate_after_days"):
        p[k] = int(p[k] or 0)
        if not 0 <= p[k] <= 365:
            raise ValueError(f"{k.replace('_', ' ')} is 0 to 365 days")
    if p["merchant_mode"] not in ("all", "selected"):
        raise ValueError("merchants are all or selected")
    rows = []
    for m in p["merchants"] or []:
        code, name = str(m.get("mcc") or "").strip(), str(m.get("name") or "").strip()
        if code and not (code.isdigit() and len(code) == 4):
            raise ValueError(f"a merchant category code has 4 digits ({code})")
        if code or name:
            rows.append({"mcc": code, "name": name[:60]})
    p["merchants"] = rows
    if p["merchant_mode"] == "selected" and not rows and not any(MCC[k] for k in p["purposes"]):
        raise ValueError("name at least one merchant (category code or name) when only selected merchants are allowed")
    return p


def get_defaults() -> dict:
    return dict(DEFAULT)


def save(org, body: dict, by: str) -> dict:
    p = validate(get(org) | {k: v for k, v in body.items() if k in DEFAULT})  # what is not sent stays as it was
    org._put(KIND, {"_id": ID} | p, by)
    return p


def purpose_of(fr: dict) -> str:
    """The program purpose an approved fund request falls under."""
    text = f"{fr.get('purpose', '')} {fr.get('activity', '')}".lower()
    if fr.get("kind") == "cash_advance":
        return "travel_advance" if any(w in text for w in ("travel", "trip", "flight", "hotel", "transport")) else "cash_advance"
    if fr.get("kind") == "petty_cash":
        return "slush_fund" if "slush" in text else "petty_cash"
    return "office_operation"


def applies(prog: dict, fr: dict) -> bool:
    return bool(prog["staff_cards"]) and purpose_of(fr) in prog["purposes"]


def label(user: dict) -> str:
    emp = user.get("employee_id") or user.get("employee_number") or user.get("user_id") or ""
    return f"{user.get('name') or emp} · {emp}".strip(" ·")


def staff_card(org, user_id: str) -> dict | None:
    return next((c for c in org.list("card") if c.get("kind") == "staff" and c.get("user_id") == user_id and c.get("status") != "CLOSED"), None)


def window(fr_approved_at: str | None, settle_by: str | None, prog: dict, now: dt.datetime | None = None) -> tuple[str, str | None]:
    """(active from, active until) for an advance: N days after approval, for M days (0 = until its settle-by date)."""
    start = dt.datetime.fromisoformat(fr_approved_at) if fr_approved_at else (now or _now())
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    start = start + dt.timedelta(days=prog["activate_after_days"])
    if prog["deactivate_after_days"]:
        until = (start + dt.timedelta(days=prog["deactivate_after_days"])).isoformat(timespec="seconds")
    else:
        until = f"{settle_by}T23:59:59+00:00" if settle_by else None
    return start.isoformat(timespec="seconds"), until


def attach(card: dict, *, ref: str, amount_minor: int, start: str, until: str | None, purpose: str) -> dict:
    """An approved advance put on the person's staff card: what it may spend, and when."""
    allow = [a for a in card.get("allowances") or [] if a["ref"] != ref]
    allow.append({"ref": ref, "amount_minor": amount_minor, "from": start, "until": until, "purpose": purpose})
    return card | {"allowances": allow}


def _live(a: dict, now: dt.datetime) -> bool:
    start = dt.datetime.fromisoformat(a["from"])
    return start <= now and (not a.get("until") or now <= dt.datetime.fromisoformat(a["until"]))


def state(card: dict, spent_since, now: dt.datetime | None = None) -> dict:
    """Whether the staff card may spend now, and how much: the advances whose window is open, less what the card has
    been authorized since the oldest of them. spent_since(iso) -> minor authorized on this card since that time."""
    now = now or _now()
    live = [a for a in card.get("allowances") or [] if _live(a, now)]
    if not live:
        return {"active": False, "remaining_minor": 0, "advances": []}
    since = min(a["from"] for a in live)
    remaining = sum(a["amount_minor"] for a in live) - spent_since(since)
    return {"active": remaining > 0, "remaining_minor": max(remaining, 0), "advances": [a["ref"] for a in live], "since": since}


def allowed_codes(prog: dict) -> set[str] | None:
    """The merchant categories a card may pay; None = anywhere on the Visa network."""
    if prog["merchant_mode"] != "selected":
        return None
    return {m["mcc"] for m in prog["merchants"] if m["mcc"]} | {c for k in prog["purposes"] for c in MCC[k]}


def merchant_ok(prog: dict, merchant_name: str, mcc: str, matches) -> tuple[bool, str]:
    """Only selected merchants: the charge's category code is allowed, or its name is a selected merchant."""
    codes = allowed_codes(prog)
    if codes is None:
        return True, "any merchant on the Visa network"
    if mcc and str(mcc) in codes:
        return True, f"merchant category {mcc} allowed"
    if any(m["name"] and matches(m["name"], merchant_name) for m in prog["merchants"]):
        return True, "a selected merchant"
    return False, f"merchant '{merchant_name or '-'}' ({mcc or 'no category'}) is not one of the selected merchants"


def public(prog: dict) -> dict:
    return prog | {"choices": {"purposes": [{"key": k, "label": lbl, "mcc": m} for k, lbl, m in PURPOSES],
                               "limits": LIMIT_CHOICES, "no_limit": NO_LIMIT}}
