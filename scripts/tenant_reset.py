# ruff: noqa: E501
"""Empty one company's data for a fresh recording, after a full backup; keeps its sign-ins, credentials and connections.

  python scripts/tenant_reset.py nusantara-trading-pte-ltd                 # dry run: lists what would go and what stays
  python scripts/tenant_reset.py nusantara-trading-pte-ltd --yes           # backup, then empty (stop the platform first)
  python scripts/tenant_reset.py nusantara-trading-pte-ltd --yes --airwallex disconnect
  python scripts/tenant_reset.py --restore data/saas/backups/<folder>      # put a backup back (stop the platform first)

Kept: sign-ins and sessions, every stored credential (Airwallex, AI keys, mailbox and ERP secrets), the AI model
choice, the mailbox and its sender, the ERP connector, the email policy, the people (they sign in), the company
profile. Emptied: invoices and requests, payments, executions, ledger and journal, payees, vendors, customers, bills,
budget lines, POs, PRs, the audit chain, webhooks, AI usage, test logs, simulations, T:0 posting notes, the FCO's
settings (mandate, priority policy, standards, period, document rules) so they are filled again on camera.
--airwallex disconnect also drops the link to the Airwallex account this company acts for (the next "Open new
account" creates a fresh one, with a zero balance); the account itself stays at Airwallex.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TENANTS = ROOT / "data" / "saas" / "tenants"
BACKUPS = ROOT / "data" / "saas" / "backups"

KEEP_ORG_KINDS = {"user", "company_profile", "meta", "mailbox", "outbox_smtp", "email_addresses", "mail_oauth_state"}
KEEP_TABLES = {  # db -> tables kept whole; every other table of the db is emptied
    "secrets.db": {"airwallex", "acting", "card_account", "company_link", "own_account", "auth_password", "auth_invite", "auth_session", "secret_values"},
    "ai.db": {"settings", "ai_price", "sqlite_sequence"},
    "master.db": {"api_keys", "connectors", "email_policy", "email_inboxes", "sqlite_sequence"},
    "org.db": set(),  # by kind, below
}


def plan(tdir: Path, airwallex: str) -> list[tuple[str, str, int, str]]:
    """[(db, table or kind, rows, 'empty' | 'keep')]"""
    out = []
    for db in sorted(tdir.glob("*.db")):
        c = sqlite3.connect(db)
        tabs = [t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tabs:
            n = c.execute(f"SELECT count(*) FROM [{t}]").fetchone()[0]
            if db.name == "org.db" and t == "records":
                for kind, k in c.execute("SELECT kind, count(*) FROM records GROUP BY kind"):
                    out.append((db.name, f"records:{kind}", k, "keep" if kind in KEEP_ORG_KINDS else "empty"))
                continue
            keep = t in KEEP_TABLES.get(db.name, set())
            if db.name == "secrets.db" and t in ("acting", "own_account") and airwallex == "disconnect":
                keep = False
            if db.name == "secrets.db" and t == "secret_values":
                keep = True
            out.append((db.name, t, n, "keep" if keep else "empty"))
        c.close()
    return out


def run(tdir: Path, rows, stamp: str) -> Path:
    dest = BACKUPS / f"{tdir.name}-{stamp}"
    shutil.copytree(tdir, dest)
    for db in sorted({r[0] for r in rows}):
        c = sqlite3.connect(tdir / db)
        for _, what, _, act in [r for r in rows if r[0] == db]:
            if act != "empty":
                continue
            if what.startswith("records:"):
                c.execute("DELETE FROM records WHERE kind = ?", (what.split(":", 1)[1],))
            elif what != "sqlite_sequence":
                c.execute(f"DELETE FROM [{what}]")
        c.commit()
        c.execute("VACUUM")
        c.close()
    log = tdir / "server.log"
    if log.exists():
        log.write_text("", encoding="utf-8")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tenant", nargs="?")
    ap.add_argument("--yes", action="store_true", help="really empty it (a backup is made first)")
    ap.add_argument("--airwallex", choices=("keep", "disconnect"), default="keep")
    ap.add_argument("--restore", help="a backup folder to put back")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.restore:
        src = Path(a.restore).resolve()
        name = src.name.rsplit("-", 2)[0]
        tdir = TENANTS / name
        if not src.is_dir() or not tdir.is_dir():
            print("no such backup or tenant")
            return 1
        safety = BACKUPS / f"{name}-before-restore-{dt.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copytree(tdir, safety)
        shutil.rmtree(tdir)
        shutil.copytree(src, tdir)
        print(f"restored {src.name} -> {tdir} (what was there is kept in {safety.name})")
        return 0
    tdir = TENANTS / (a.tenant or "")
    if not a.tenant or not tdir.is_dir():
        print("tenants:", ", ".join(p.name for p in TENANTS.iterdir() if p.is_dir()))
        return 1
    rows = plan(tdir, a.airwallex)
    for act in ("empty", "keep"):
        print(f"\n{act.upper()}:")
        for db, what, n, x in rows:
            if x == act and (n or act == "keep"):
                print(f"  {db:<13} {what:<32} {n:>5}")
    if not a.yes:
        print("\ndry run: nothing changed (add --yes to back up and empty)")
        return 0
    dest = run(tdir, rows, dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    print(f"\nbacked up to {dest}\nemptied {sum(1 for r in rows if r[3] == 'empty' and r[2])} tables / kinds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
