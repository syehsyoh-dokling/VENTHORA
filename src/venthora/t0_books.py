# ruff: noqa: E501
"""VENTHORA's book ledger posted to T:0 as manual journal entries, then read back and verified.

The source is the internal double-entry journal (ledger.py; executor store `journal`): every entry is balanced per
currency, in integer minor units. Each one becomes one T:0 journal entry:

* the accounts map to T:0 GL accounts by account number (ACCOUNT_MAP; the entity's chart is read to find their ids);
* T:0 books in the entity's functional currency (USD for the sandbox): an amount in another currency is converted with
  the ECB reference rate of the entry's date (or the last one before it), and kept as the transaction amount when the
  entry has one currency; a rounding cent is put on the largest line so debits equal credits;
* ``idempotency_key`` = venthora-<company>-<entry id>, so a retry or a second run never books anything twice; VENTHORA
  also remembers what it posted (org kind ``t0_posting``) and skips it.

Verification reads every posted entry back (get_one_journal_entry) and compares: debits = credits, the number of lines,
each line's account, side and amount, and the reference in the memo. The verdict is kept with the posting and shown
in Books & ledger.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import httpx

from . import t0
from .config import env

KIND = "t0_posting"
# VENTHORA ledger account -> T:0 account number (the sandbox chart); names shown in the report
ACCOUNT_MAP = {"cash": ("11110", "Operating Checking Account #1"), "committed": ("11999", "Money in Transit"),
               "expense": ("63000", "General & Administrative Expenses"), "fees": ("63700", "Bank Charges & Fees"),
               "fx": ("71500", "Other Gain/Loss"), "funding": ("33000", "Additional Paid-In Capital")}
PAID_KINDS = ("commit", "settle", "refund")  # money that left (or came back to) the wallet for a payee
ALL_KINDS = ("open", "deposit", "grant", "convert", "commit", "settle", "refund", "adjust")


class T0Error(RuntimeError):
    pass


def _company() -> str:
    from .airwallex_settings import tenant

    return (tenant() or {}).get("tenant_id") or "local"


# -- FX: units of `ccy` per 1 EUR from the ECB file; USD per unit = rate[USD] / rate[ccy] ----------------------------
_RATES: list | None = None


def usd_rate(ccy: str, day: str) -> Decimal:
    global _RATES
    if ccy == "USD":
        return Decimal(1)
    if _RATES is None:
        from .data.ecb import load_rates

        _RATES = load_rates()
    row = None
    for d, r in _RATES:
        if d <= day:
            row = r
        else:
            break
    row = row or (_RATES[0][1] if _RATES else None)
    eur = {"EUR": Decimal(1)} | (row or {})
    if ccy not in eur or "USD" not in eur:
        raise T0Error(f"no ECB rate for {ccy} on {day}")
    return eur["USD"] / eur[ccy]


def body_for(entry: dict, accounts: dict[str, str], functional: str = "USD") -> dict:
    """The T:0 journal entry for one VENTHORA entry (entry: entry_id, book_id, at, kind, ref, lines[account, currency, amount])."""
    day = str(entry["at"])[:10]
    ccys = sorted({ln["currency"] for ln in entry["lines"]})
    lines = []
    for ln in entry["lines"]:
        amt = int(ln["amount"])
        if not amt:
            continue
        num, name = ACCOUNT_MAP[ln["account"]]
        rate = usd_rate(ln["currency"], day) if functional == "USD" else Decimal(1)
        func = int((Decimal(abs(amt)) * rate).to_integral_value(rounding=ROUND_HALF_UP))
        line = {"gl_account_id": accounts[num], "line_type": "debit" if amt > 0 else "credit", "amount_atoms": max(func, 1),
                "memo": f"{ln['account']} {ln['currency']} {abs(amt) / 100:,.2f}"}
        if len(ccys) == 1 and ccys[0] != functional:
            line["transaction_amount_atoms"] = abs(amt)
        lines.append(line)
    if len(lines) < 2:
        raise T0Error(f"entry {entry['entry_id']} has fewer than two lines")
    deb = sum(x["amount_atoms"] for x in lines if x["line_type"] == "debit")
    cre = sum(x["amount_atoms"] for x in lines if x["line_type"] == "credit")
    if deb != cre:  # conversion rounding: the difference goes on the largest line of the lighter side
        side = "debit" if deb < cre else "credit"
        biggest = max((x for x in lines if x["line_type"] == side), key=lambda x: x["amount_atoms"])
        biggest["amount_atoms"] += abs(deb - cre)
    body = {"entry_date": day, "currency": functional, "je_number": f"VT-{entry['entry_id']}"[:64],
            "memo": f"VENTHORA {entry['kind']} {entry.get('ref') or ''} · book {entry.get('book_id')} · entry {entry['entry_id']}"[:1000],
            "manual_entry_reason": "Posted by VENTHORA from its double-entry treasury ledger",
            "idempotency_key": f"venthora-{_company()}-{entry['entry_id']}"[:128], "lines": lines}
    if len(ccys) == 1 and ccys[0] != functional:
        body["transaction_currency"] = ccys[0]
    return body


class Client:
    """One MCP session to T:0 for a batch of calls."""

    def __init__(self):
        self.key, self.url = env("T0_API_KEY"), env("T0_MCP_URL") or t0.DEFAULT_URL
        if not self.key:
            raise T0Error("T0_API_KEY is not set on this server (Developer Console → T:0)")
        self.c: httpx.AsyncClient | None = None
        self.s: dict | None = None

    async def __aenter__(self):
        self.c = httpx.AsyncClient(timeout=60)
        self.s = await t0._session(self.c, self.url, self.key)
        return self

    async def __aexit__(self, *exc):
        await self.c.aclose()

    async def call(self, tool: str, args: dict | None = None) -> dict:
        out = await t0.call(self.c, self.url, self.s, tool, args or {})
        if not isinstance(out, dict):
            raise T0Error(f"T:0 gave no answer to {tool}")
        return out

    async def accounts(self) -> dict[str, str]:
        """account number -> external id, for the accounts ACCOUNT_MAP names."""
        found: dict[str, str] = {}
        for page in range(1, 6):
            r = await self.call("list_the_entity_s_chart_of_accounts", {"postable": True, "page_size": 100, "page_number": page})
            rows = (r.get("body") or {}).get("data") or [] if isinstance(r.get("body"), dict) else []
            for a in rows:
                if a.get("account_number"):
                    found[str(a["account_number"])] = a["external_id"]
            if len(rows) < 100:
                break
        missing = [n for n, _ in ACCOUNT_MAP.values() if n not in found]
        if missing:
            raise T0Error(f"T:0's chart has no postable account {', '.join(missing)}")
        return found


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _err(r: dict) -> str:
    b = r.get("body")
    if isinstance(b, dict):
        return str(b.get("detail") or b.get("message") or b.get("error") or json.dumps(b))[:300]
    return str(b)[:300]


async def post(org, entries: list[dict], kinds: tuple[str, ...] = PAID_KINDS, limit: int = 200, dry_run: bool = False) -> dict:
    """Post the entries not yet in T:0 (of the given kinds); returns what was posted, skipped and refused."""
    done = {r["_id"]: r for r in org.list(KIND)}
    todo = [e for e in entries if e["kind"] in kinds and not (done.get(e["entry_id"]) or {}).get("t0_id")][:limit]
    report = {"candidates": len(todo), "posted": [], "already": sum(1 for e in entries if (done.get(e["entry_id"]) or {}).get("t0_id")),
              "refused": [], "dry_run": dry_run}
    if not todo:
        return report
    async with Client() as cl:
        accts = await cl.accounts()
        for e in todo:
            try:
                body = body_for(e, accts)
            except (T0Error, KeyError) as exc:
                report["refused"].append({"entry_id": e["entry_id"], "error": str(exc)})
                continue
            if dry_run:
                report["posted"].append({"entry_id": e["entry_id"], "would_post": body})
                continue
            r = await cl.call("post_a_manual_journal_entry", {"body": body})
            b = r.get("body") if isinstance(r.get("body"), dict) else {}
            t0_id = (b.get("data") or b).get("external_id") or (b.get("data") or b).get("id") if isinstance(b, dict) else None
            ok = (r.get("status") or 0) < 300 and t0_id
            if not ok:  # T:0 already has it (same idempotency key): VENTHORA lost its note; keep the entry T:0 made
                import re

                m = re.search(r"already been processed; it created (je_[A-Za-z0-9_]+)", _err(r))
                if m:
                    t0_id, ok = m.group(1), True
            rec = {"_id": e["entry_id"], "entry_id": e["entry_id"], "kind": e["kind"], "ref": e.get("ref"), "book_id": e.get("book_id"),
                   "entry_date": body["entry_date"], "posted_at": _now(), "t0_id": t0_id if ok else None, "status": r.get("status"),
                   "error": None if ok else _err(r), "total_atoms": sum(x["amount_atoms"] for x in body["lines"] if x["line_type"] == "debit"),
                   "currency": body["currency"], "transaction_currency": body.get("transaction_currency"),
                   "sent_lines": [{k: x[k] for k in ("gl_account_id", "line_type", "amount_atoms")} for x in body["lines"]], "verify": None}
            org._put(KIND, rec, "VENTHORA (T:0 books)")
            (report["posted"] if ok else report["refused"]).append({"entry_id": e["entry_id"], "t0_id": rec["t0_id"], "error": rec["error"]})
    return report


async def verify(org, only_unverified: bool = False) -> dict:
    """Read every posted entry back from T:0 and compare it with what VENTHORA sent."""
    rows = [r for r in org.list(KIND) if r.get("t0_id") and not (only_unverified and (r.get("verify") or {}).get("ok"))]
    out = {"checked": 0, "passed": 0, "failed": []}
    if not rows:
        return out
    async with Client() as cl:
        for rec in rows:
            r = await cl.call("get_one_journal_entry", {"journal_entry_id": rec["t0_id"]})
            b = r.get("body") if isinstance(r.get("body"), dict) else {}
            je = b.get("data") or b
            problems: list[str] = []
            if (r.get("status") or 0) >= 300 or not je:
                problems.append(f"T:0 did not return the entry ({_err(r)})")
            else:
                got = je.get("lines") or []
                norm = sorted((str(x.get("account_id") or x.get("gl_account_id")), str(x.get("line_type")), int(x.get("amount_atoms") or 0)) for x in got)
                sent = sorted((x["gl_account_id"], x["line_type"], int(x["amount_atoms"])) for x in rec["sent_lines"])
                deb = sum(a for _, t, a in norm if t == "debit")
                cre = sum(a for _, t, a in norm if t == "credit")
                if deb != cre or (je.get("total_debit_atoms") is not None and je.get("total_debit_atoms") != je.get("total_credit_atoms")):
                    problems.append(f"debits {deb} ≠ credits {cre}")
                if deb != rec.get("total_atoms"):
                    problems.append(f"total {deb} in T:0, {rec.get('total_atoms')} sent")
                if (je.get("transaction_currency") or rec.get("currency")) != (rec.get("transaction_currency") or rec.get("currency")):
                    problems.append(f"transaction currency {je.get('transaction_currency')} ≠ {rec.get('transaction_currency')}")
                if len(norm) != len(sent):
                    problems.append(f"{len(norm)} lines in T:0, {len(sent)} sent")
                elif norm != sent:
                    problems.append("a line's account, side or amount differs from what was sent")
                if rec["entry_id"] not in str(je.get("memo") or "") + str(je.get("je_number") or ""):
                    problems.append("the VENTHORA reference is not on the entry")
                if str(je.get("entry_date") or "")[:10] != rec["entry_date"]:
                    problems.append(f"date {je.get('entry_date')} ≠ {rec['entry_date']}")
            rec["verify"] = {"ok": not problems, "problems": problems, "at": _now(), "t0_total_atoms": je.get("total_debit_atoms") if je else None,
                             "t0_status": je.get("status") if je else None,
                             "t0_lines": [f"{x.get('line_type')} {x.get('account_number')} {x.get('account_name')} {int(x.get('amount_atoms') or 0) / 100:,.2f}"
                                          for x in (je.get("lines") or [])] if je else []}
            org._put(KIND, rec, "VENTHORA (T:0 books)")
            out["checked"] += 1
            if problems:
                out["failed"].append({"entry_id": rec["entry_id"], "t0_id": rec["t0_id"], "problems": problems})
            else:
                out["passed"] += 1
    return out


def summary(org, entries: list[dict]) -> dict:
    rows = sorted(org.list(KIND), key=lambda r: r.get("posted_at") or "", reverse=True)
    posted = [r for r in rows if r.get("t0_id")]
    paid = [e for e in entries if e["kind"] in PAID_KINDS]
    return {"configured": bool(env("T0_API_KEY")), "map": {k: {"number": n, "name": nm} for k, (n, nm) in ACCOUNT_MAP.items()},
            "ledger_entries": len(entries), "paid_entries": len(paid), "posted": len(posted),
            "pending": sum(1 for e in paid if not any(r["entry_id"] == e["entry_id"] for r in posted)),
            "refused": sum(1 for r in rows if not r.get("t0_id")),
            "verified": sum(1 for r in posted if (r.get("verify") or {}).get("ok")),
            "verify_failed": sum(1 for r in posted if r.get("verify") and not r["verify"]["ok"]),
            "rows": rows[:200]}
