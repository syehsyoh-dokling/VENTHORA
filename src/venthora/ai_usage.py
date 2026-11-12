# ruff: noqa: E501
"""What every model call cost: one row per call (when, which feature asked, which model answered, tokens in and out,
whether it worked), kept in the company's own AI database (VENTHORA_AI_DB). Nothing of the prompt or the answer is kept.

The cost is an estimate from prices the company enters itself (USD per million tokens, per model, from its provider's
price list); VENTHORA ships no prices, so without one only the tokens are shown.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import data_path

SCHEMA = """CREATE TABLE IF NOT EXISTS ai_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, feature TEXT NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL, ms INTEGER NOT NULL,
  error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ai_usage_at ON ai_usage (at);
CREATE TABLE IF NOT EXISTS ai_price (model TEXT PRIMARY KEY, input_per_m REAL NOT NULL, output_per_m REAL NOT NULL, updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_budget (id INTEGER PRIMARY KEY CHECK (id = 1), cap_usd REAL NOT NULL, since TEXT NOT NULL, until TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);"""
PAID_PROVIDERS = ("anthropic", "bedrock")  # a self-hosted model costs nothing per call and never counts against the budget

# the module that asked -> the feature a person recognises
FEATURES = {"venthora.intake.reader": "Invoice reader (email and PDF)", "venthora.intake.validation": "Document check (AI review)",
            "venthora.imports": "Upload dataset (column mapping and reading)", "venthora.fund_email": "Cash advance by email",
            "venthora.pr_email": "Purchase request by email", "venthora.sourcing": "Vendor search (procurement)",
            "venthora.sync_discover": "App Integration (field discovery)", "venthora.evidence.reader": "Evidence reader",
            "venthora.evidence.explain": "Explanations", "venthora.engines.llm_planner": "Payment planner", "venthora.api": "Platform Setup (tests)"}
_LOCK = threading.RLock()


def _db() -> Path:
    p = data_path("VENTHORA_AI_DB", "data/venthora_ai.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(_db(), timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def caller() -> str:
    """The VENTHORA module that made the model call (the first frame outside the model clients)."""
    f = sys._getframe(1)
    while f is not None:
        name = f.f_globals.get("__name__", "")
        if name.startswith("venthora.") and name not in ("venthora.evidence.llm", "venthora.ai_usage"):
            return name
        f = f.f_back
    return "other"


def record(*, module: str, provider: str, model: str, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0,
           cache_write: int = 0, ok: bool = True, ms: int = 0, error: str = "") -> None:
    """One call; never raises (a usage log must not break the call it describes)."""
    try:
        with _LOCK, _connect() as c:
            c.execute("INSERT INTO ai_usage (at, feature, provider, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, ok, ms, error)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (datetime.now(UTC).isoformat(timespec="seconds"), module, provider, model or "?", int(input_tokens or 0), int(output_tokens or 0),
                       int(cache_read or 0), int(cache_write or 0), 1 if ok else 0, int(ms), error[:300]))
    except Exception:  # noqa: BLE001
        pass


def latest() -> dict | None:
    """The most recent call (the AI connection test shows its tokens)."""
    with _connect() as c:
        r = c.execute("SELECT * FROM ai_usage ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def prices() -> dict[str, dict]:
    with _connect() as c:
        return {r["model"]: {"input_per_m": r["input_per_m"], "output_per_m": r["output_per_m"], "updated_at": r["updated_at"], "updated_by": r["updated_by"]}
                for r in c.execute("SELECT * FROM ai_price ORDER BY model")}


def set_price(model: str, input_per_m: float | None, output_per_m: float | None, by: str) -> dict:
    """The company's own price for a model (USD per million tokens); both empty removes it."""
    model = model.strip()
    if not model:
        raise ValueError("name the model")
    with _LOCK, _connect() as c:
        if input_per_m is None and output_per_m is None:
            c.execute("DELETE FROM ai_price WHERE model = ?", (model,))
        else:
            if (input_per_m or 0) < 0 or (output_per_m or 0) < 0:
                raise ValueError("a price is zero or more")
            c.execute("INSERT INTO ai_price VALUES (?,?,?,?,?) ON CONFLICT(model) DO UPDATE SET input_per_m = excluded.input_per_m,"
                      " output_per_m = excluded.output_per_m, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                      (model, float(input_per_m or 0), float(output_per_m or 0), datetime.now(UTC).isoformat(timespec="seconds"), by))
    return prices()


def _cost(p: dict | None, tin: int, tout: int) -> float | None:
    return None if p is None else round((tin * p["input_per_m"] + tout * p["output_per_m"]) / 1_000_000, 6)


def _price_for(pr: dict[str, dict], model: str) -> dict | None:
    """The entered price for a model; a Bedrock id (us.anthropic.claude-...-v1:0) also matches the plain model name."""
    if model in pr:
        return pr[model]
    base = model.split(".", 2)[-1] if model.count(".") >= 2 else model
    return next((p for m, p in pr.items() if base.startswith(m) or m.startswith(base)), None)


# ---- the AI budget: a dollar cap on paid model calls over a period (e.g. USD 100 until Demo Day) ---------------------
def budget() -> dict:
    """The cap, what paid calls cost since it started, and whether calls are now stopped. Spend counts cache reads at
    a tenth and cache writes at 1.25x the input price (the providers' usual rates); a paid model with tokens but no
    entered price is reported in `unpriced`, so the figure is never silently low."""
    pr = prices()
    with _connect() as c:
        row = c.execute("SELECT * FROM ai_budget WHERE id = 1").fetchone()
        if row is None:
            return {"set": False}
        rows = c.execute("SELECT model, SUM(input_tokens) tin, SUM(output_tokens) tout, SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw"
                         " FROM ai_usage WHERE at >= ? AND provider IN (?, ?) GROUP BY model", (row["since"], *PAID_PROVIDERS)).fetchall()
    spent, unpriced = 0.0, []
    for r in rows:
        p = _price_for(pr, r["model"])
        if p is None:
            if (r["tin"] or 0) + (r["tout"] or 0):
                unpriced.append(r["model"])
            continue
        spent += ((r["tin"] or 0) * p["input_per_m"] + (r["tout"] or 0) * p["output_per_m"]
                  + (r["cr"] or 0) * p["input_per_m"] * 0.1 + (r["cw"] or 0) * p["input_per_m"] * 1.25) / 1_000_000
    now = datetime.now(UTC).isoformat(timespec="seconds")
    active = row["since"] <= now <= row["until"]
    return {"set": True, "cap_usd": row["cap_usd"], "since": row["since"], "until": row["until"], "spent_usd": round(spent, 4),
            "remaining_usd": round(max(0.0, row["cap_usd"] - spent), 4), "active": active, "stopped": active and spent >= row["cap_usd"],
            "unpriced": unpriced, "updated_at": row["updated_at"], "updated_by": row["updated_by"]}


def set_budget(cap_usd: float | None, since: str, until: str, by: str) -> dict:
    """Set or replace the cap (cap None removes it). since/until are ISO times in UTC."""
    with _LOCK, _connect() as c:
        if cap_usd is None:
            c.execute("DELETE FROM ai_budget WHERE id = 1")
            return budget()
        if cap_usd <= 0:
            raise ValueError("the cap is more than zero")
        s, u = datetime.fromisoformat(since).astimezone(UTC), datetime.fromisoformat(until).astimezone(UTC)
        if u <= s:
            raise ValueError("the budget must end after it starts")
        c.execute("INSERT INTO ai_budget VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET cap_usd = excluded.cap_usd, since = excluded.since,"
                  " until = excluded.until, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                  (float(cap_usd), s.isoformat(timespec="seconds"), u.isoformat(timespec="seconds"),
                   datetime.now(UTC).isoformat(timespec="seconds"), by))
    return budget()


def over_budget() -> str | None:
    """Called before every paid model call: a reason to refuse it when the cap is spent, else None. Never raises."""
    try:
        b = budget()
    except Exception:  # noqa: BLE001 - a broken usage log must not stop the AI; the provider's own limit still applies
        return None
    if b.get("stopped"):
        return (f"the AI budget is spent: USD {b['spent_usd']:.2f} of USD {b['cap_usd']:.2f} until {b['until'][:10]} "
                "(Platform Setup → Agent (AI & Model)); VENTHORA continues with its rules")
    return None


def summary(days: int = 30, recent: int = 30) -> dict:
    """Totals for the last `days` days, by day, by feature and by model, and the latest calls."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    pr = prices()
    with _connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ai_usage WHERE at >= ? ORDER BY at", (since,))]

    def group(key) -> list[dict]:
        out: dict[str, dict] = {}
        for r in rows:
            g = out.setdefault(key(r), {"key": key(r), "calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "priced": True})
            g["calls"] += 1
            g["failed"] += 0 if r["ok"] else 1
            g["input_tokens"] += r["input_tokens"]
            g["output_tokens"] += r["output_tokens"]
            cost = _cost(pr.get(r["model"]), r["input_tokens"], r["output_tokens"])
            if cost is None:
                g["priced"] = g["priced"] and r["input_tokens"] + r["output_tokens"] == 0
            else:
                g["cost_usd"] = round(g["cost_usd"] + cost, 6)
        return list(out.values())

    total = group(lambda r: "all")
    feature = lambda r: FEATURES.get(r["feature"], r["feature"].removeprefix("venthora."))  # noqa: E731
    return {"days": days, "since": since,
            "total": total[0] if total else {"key": "all", "calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "priced": True},
            "by_day": sorted(group(lambda r: r["at"][:10]), key=lambda g: g["key"]),
            "by_feature": sorted(group(feature), key=lambda g: -(g["input_tokens"] + g["output_tokens"])),
            "by_model": sorted(group(lambda r: f"{r['provider']} · {r['model']}"), key=lambda g: -(g["input_tokens"] + g["output_tokens"])),
            "recent": [{"at": r["at"], "feature": feature(r), "provider": r["provider"], "model": r["model"], "input_tokens": r["input_tokens"],
                        "output_tokens": r["output_tokens"], "ok": bool(r["ok"]), "ms": r["ms"], "error": r["error"],
                        "cost_usd": _cost(pr.get(r["model"]), r["input_tokens"], r["output_tokens"])} for r in rows[::-1][:recent]],
            "models_seen": sorted({r["model"] for r in rows}), "prices": pr, "budget": budget()}
