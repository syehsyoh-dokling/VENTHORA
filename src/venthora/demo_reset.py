# ruff: noqa: E501
"""Staging-demo data reset, per category: for each kind of data (companies, staff, PO numbers, invoices, payments …) the
Super admin decides whether it goes back to a saved baseline, and at what UTC time each day. Visitors explore freely;
what is set to reset starts fresh, what is set to keep stays (so a demo can be staged as needed).

The platform owns it (it starts and stops each company's VENTHORA). Settings live in data/saas/demo_reset.json, the
baseline in data/saas/baseline/ (a full copy of each company's resettable files, made while they run). A reset of some
categories stops each company, replaces only those rows (or files) with the baseline's, and starts it again.
Never touched: secrets and sign-in passwords, AI usage and the AI budget, the webhook dedupe store, the mailbox and
email settings, the company profile and bank account. Companies registered after the baseline leave the registry when
"Companies" resets; their folders are moved to data/saas/removed/, never deleted.
"""
from __future__ import annotations

import json
from contextlib import closing
import shutil
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

NEVER = {"secrets.db", "ai.db", "webhooks.db", "server.log"}  # never copied into, nor restored from, the baseline
DEFAULT_TIME = "23:59"
_LOCK = threading.RLock()

# id -> label, what it covers (for the page), and targets: (file, table, kind column, kinds) or (file, None, None, None) for a whole file
CATEGORIES: list[dict] = [
    {"id": "companies", "label": "Companies", "covers": "Companies registered by visitors after the baseline (removed from the platform)", "targets": []},
    {"id": "staff", "label": "Staff & people", "covers": "Users and staff accounts",
     "targets": [("org.db", "records", "kind", ["user"])]},
    {"id": "structure", "label": "Units, projects & accounts", "covers": "Branches, units, country offices, projects and account codes",
     "targets": [("org.db", "records", "kind", ["unit", "project", "account"])]},
    {"id": "purchase_orders", "label": "PO numbers & purchase requests", "covers": "Purchase orders, their numbers and the instalments drawn on them",
     "targets": [("org.db", "records", "kind", ["po"]), ("execution.db", "po_draws", None, None)]},
    {"id": "invoices", "label": "Invoices & intake", "covers": "Emails and uploads read as invoices, and their reviews",
     "targets": [("intake.db", "intake", None, None), ("intake.db", "intake_reviews", None, None), ("master.db", "email_seen", None, None)]},
    {"id": "payments", "label": "Payments & ledger", "covers": "Payments, transfers, payouts, journal, spend ledger, FX costs, incidents, reconciliation, budget periods",
     "targets": [("execution.db", t, None, None) for t in ("executions", "operations", "payouts", "journal", "spend_ledger", "fx_costs", "observed_fees",
                                                         "incidents", "recon_cases", "used_quotes", "counted_deposits", "budget_grants", "budget_periods",
                                                         "budget_spend", "closing_rates")]},
    {"id": "payees", "label": "Vendors & payees", "covers": "Payees and their bank details, bank checks, vendor master data",
     "targets": [("execution.db", "payees", None, None), ("execution.db", "beneficiaries", None, None), ("execution.db", "payee_events", None, None),
                 ("org.db", "records", "kind", ["bank_check"]), ("master.db", "records", None, None), ("master.db", "runs", None, None)]},
    {"id": "cards", "label": "Cards", "covers": "Virtual cards and card authorization decisions",
     "targets": [("org.db", "records", "kind", ["card", "card_auth"])]},
    {"id": "policies", "label": "Policies & controls", "covers": "Delegation of Authority, Payment Priority Policy, finance standards, document rules, budget policy, Visa/Airi/Metal switches, prices",
     "targets": [("org.db", "records", "kind", ["standing_mandate", "priority_sop", "standards", "doc_rules", "budget_policy", "controls",
                                                "policy_slot", "placement_provider", "price"])]},
    {"id": "activity", "label": "Activity logs", "covers": "Decision log, test log, call-backs, notices to the FCO, approve links, outbox, simulator runs",
     "targets": [("org.db", "records", "kind", ["decision_log", "testlog", "testlog_session", "callback_log", "fco_notify", "approve_link",
                                                "outbox", "sim_run", "contract_seal", "rate_snapshot", "funding_check"])]},
    {"id": "audit", "label": "Audit chain", "covers": "The audit log (the whole hash chain, so it stays verifiable)",
     "targets": [("audit.db", "events", None, None)]},
    {"id": "api", "label": "API keys & requests", "covers": "Keys made for other applications and their request log",
     "targets": [("master.db", "api_keys", None, None), ("master.db", "api_requests", None, None)]},
    {"id": "reporting", "label": "Reporting periods", "covers": "Reporting period settings",
     "targets": [("reporting.json", None, None, None)]},
]
CAT = {c["id"]: c for c in CATEGORIES}
ALWAYS_KEPT = ["Secrets, API keys and sign-in passwords", "AI usage and the AI budget", "Webhook dedupe store",
               "Mailbox, email settings and connectors", "Company profile and bank account"]


def _paths(root: Path) -> tuple[Path, Path, Path]:
    """root: the platform's data folder (data/saas, the registry's root)."""
    return root / "demo_reset.json", root / "baseline", root / "removed"


def _load(root: Path) -> dict:
    try:
        return json.loads(_paths(root)[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _manifest(root: Path) -> dict:
    try:
        return json.loads((_paths(root)[1] / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def settings(root: Path) -> dict:
    s, m = _load(root), _manifest(root)
    per = s.get("categories") or {}
    cats = []
    for c in CATEGORIES:
        v = per.get(c["id"]) or {}
        cats.append({"id": c["id"], "label": c["label"], "covers": c["covers"], "reset": bool(v.get("reset", True)),
                     "time_utc": v.get("time_utc") or DEFAULT_TIME, "last_run_at": v.get("last_run_at"), "last_run_date": v.get("last_run_date")})
    return {"enabled": bool(s.get("enabled", False)), "categories": cats, "always_kept": ALWAYS_KEPT,
            "last_result": s.get("last_result"), "updated_by": s.get("updated_by"), "updated_at": s.get("updated_at"),
            "baseline_at": m.get("saved_at"), "baseline_by": m.get("saved_by"), "baseline_companies": [t["name"] for t in m.get("tenants", [])]}


def _write(root: Path, s: dict) -> None:
    cfg = _paths(root)[0]
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(s, indent=1), encoding="utf-8")


def _hhmm(v: str) -> str:
    hh, _, mm = (v or DEFAULT_TIME).strip().partition(":")
    if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        raise ValueError(f"time {v!r}: use HH:MM in UTC, e.g. 23:59")
    return f"{int(hh):02d}:{int(mm):02d}"


def configure(root: Path, enabled: bool, categories: list[dict], by: str) -> dict:
    """Master switch plus, per category, reset yes/no and its UTC time."""
    if enabled and _manifest(root).get("saved_at") is None:
        raise ValueError("save a baseline first: the reset puts the data back to it")
    with _LOCK:
        s = _load(root)
        per = s.get("categories") or {}
        for c in categories or []:
            cid = c.get("id")
            if cid not in CAT:
                raise ValueError(f"unknown category {cid!r}")
            old = per.get(cid) or {}
            per[cid] = old | {"reset": bool(c.get("reset", True)), "time_utc": _hhmm(str(c.get("time_utc") or DEFAULT_TIME))}
        s.update(enabled=bool(enabled), categories=per, updated_by=by, updated_at=datetime.now(UTC).isoformat(timespec="seconds"))
        _write(root, s)
    return settings(root)


def _copy_db(src: Path, dst: Path) -> None:
    """A consistent copy of a live SQLite file (the backup API waits for writers), or a plain copy for other files."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix == ".db":
        with closing(sqlite3.connect(src, timeout=30)) as s, closing(sqlite3.connect(dst, timeout=30)) as d:
            s.backup(d)
        for side in ("-wal", "-shm"):
            Path(str(dst) + side).unlink(missing_ok=True)
    else:
        shutil.copy2(src, dst)


def save_baseline(root: Path, tenants: list[dict], tenant_dir, by: str) -> dict:
    """Snapshot every company's resettable files as they are now (safe while the companies run)."""
    base = _paths(root)[1]
    with _LOCK:
        tmp = base.with_name("baseline.new")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for t in tenants:
            src_dir = tenant_dir(t["tenant_id"])
            for f in sorted(p for p in src_dir.iterdir() if p.is_file() and p.name not in NEVER and not p.name.endswith(("-wal", "-shm", "-journal"))):
                _copy_db(f, tmp / t["tenant_id"] / f.name)
        (tmp / "manifest.json").write_text(json.dumps({"saved_at": datetime.now(UTC).isoformat(timespec="seconds"), "saved_by": by,
                                                        "tenants": [{"tenant_id": t["tenant_id"], "name": t["name"]} for t in tenants]}, indent=1), encoding="utf-8")
        shutil.rmtree(base, ignore_errors=True)
        tmp.rename(base)
    return settings(root)


def baseline_tenants(root: Path) -> list[str]:
    return [t["tenant_id"] for t in _manifest(root).get("tenants", [])]


def restore(root: Path, tenant_id: str, tenant_dir, category_ids: list[str]) -> dict:
    """Put the chosen categories of one company back to the baseline (call while its VENTHORA is stopped).
    Rows are replaced table by table (or kind by kind); a whole-file target is copied. Returns rows/files per category."""
    base_dir, live_dir = _paths(root)[1] / tenant_id, tenant_dir(tenant_id)
    out: dict[str, int] = {}
    if not base_dir.is_dir():
        return out
    with _LOCK:
        for cid in category_ids:
            n = 0
            for file, table, kcol, kinds in CAT[cid]["targets"]:
                src, dst = base_dir / file, live_dir / file
                if table is None:  # a whole file
                    if src.exists():
                        shutil.copy2(src, dst)
                        n += 1
                    continue
                if not dst.exists():
                    continue
                with closing(sqlite3.connect(dst, timeout=30)) as c:
                    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    where, args = (f" WHERE {kcol} IN ({','.join('?' * len(kinds))})", list(kinds)) if kcol else ("", [])
                    attached = False
                    if src.exists():  # ATTACH must come before the DELETE opens a transaction
                        c.execute("ATTACH DATABASE ? AS base", (str(src),))
                        attached = True
                    try:
                        c.execute(f"DELETE FROM main.{table}{where}", args)
                        if attached and c.execute("SELECT 1 FROM base.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                            cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({table})")]
                            have = {r[1] for r in c.execute(f"PRAGMA base.table_info({table})")}
                            use = [x for x in cols if x in have]
                            cur = c.execute(f"INSERT INTO main.{table} ({','.join(use)}) SELECT {','.join(use)} FROM base.{table}{where}", args)
                            n += max(cur.rowcount, 0)
                        c.commit()
                    finally:
                        if attached:
                            c.execute("DETACH DATABASE base")
            out[cid] = n
    return out


def remove_extra(root: Path, tenant_id: str, tenant_dir, registry_db: Path) -> None:
    """A company registered after the baseline: out of the registry, its folder moved aside (kept, not deleted)."""
    removed = _paths(root)[2]
    removed.mkdir(parents=True, exist_ok=True)
    d = tenant_dir(tenant_id)
    if d.exists():
        shutil.move(str(d), str(removed / f"{tenant_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"))
    with closing(sqlite3.connect(registry_db, timeout=30)) as c:
        c.execute("DELETE FROM tenant WHERE tenant_id = ?", (tenant_id,))
        c.commit()


def due_categories(root: Path, now: datetime | None = None) -> list[str]:
    """The categories whose daily UTC time has come today and that have not run today (master switch on, baseline saved)."""
    s = settings(root)
    if not s["enabled"] or s["baseline_at"] is None:
        return []
    now = now or datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    out = []
    for c in s["categories"]:
        hh, mm = (int(x) for x in c["time_utc"].split(":"))
        if c["reset"] and c.get("last_run_date") != today and (now.hour, now.minute) >= (hh, mm):
            out.append(c["id"])
    return out


def selected(root: Path) -> list[str]:
    return [c["id"] for c in settings(root)["categories"] if c["reset"]]


def record_run(root: Path, category_ids: list[str], result: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    with _LOCK:
        s = _load(root)
        per = s.get("categories") or {}
        for cid in category_ids:
            per[cid] = (per.get(cid) or {}) | {"last_run_at": now.isoformat(timespec="seconds"), "last_run_date": now.strftime("%Y-%m-%d")}
        s.update(categories=per, last_result=result | {"at": now.isoformat(timespec="seconds"), "categories": category_ids})
        _write(root, s)
    return settings(root)
