# ruff: noqa: E501
"""Reporting periods: an admin's cut-off, three layers of report, one reporting currency.

Cut-off (admin, data/reporting.json): day of month + time + timezone. A document posted at or before the cut-off instant
of month M belongs to M's report; posted after it belongs to M+1's. The due date still decides WHEN something is paid;
the posting instant decides WHICH report it appears in. Payments are booked to the period of their PAYMENT instant.

Layers:
  1. monthly spend      every payment whose payment instant fell in the period, per currency and per budget line
  2. invoice statement  an invoice paid in instalments is reported as reconciled only when its parts add up to the
                        contract total; each part stays in the month it was paid
  3. consolidation      every foreign-currency payment converted to the reporting currency at the period's recorded
                        closing rate (snapshot from Airwallex or set by the admin), beside the base-currency cost the
                        agent actually paid, so the FX difference is visible
All arithmetic in integer minor units; rates as decimal strings, scaled by 10^6 for integer products.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from .config import ROOT

CONFIG_FILE = ROOT / "data" / "reporting.json"
MINOR = {"IDR": 1, "JPY": 1, "KRW": 1}  # currencies without cents; default 100


class ReportingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = Field(default="Asia/Jakarta", max_length=60)
    cutoff_day: int = Field(default=25, ge=1, le=31, description="day of month; clamped to the month's last day")
    cutoff_time: str = Field(default="23:59", pattern=r"^\d{2}:\d{2}$")
    reporting_currency: str = Field(default="IDR", min_length=3, max_length=3)
    base_currency: str = Field(default="USD", min_length=3, max_length=3)
    closing_rates: dict[str, dict[str, str]] = Field(default_factory=dict,
                                                     description="period -> {ccy: reporting units per 1 ccy}; admin fallback")
    set_by: str | None = None


def config_path() -> Path:
    """The admin's reporting configuration; a separate deployment (VENTHORA_REPORTING_CONFIG) keeps its own copy."""
    import os

    override = os.environ.get("VENTHORA_REPORTING_CONFIG")
    return Path(override) if override else CONFIG_FILE


def load_config(path: Path | None = None) -> ReportingConfig:
    path = path or config_path()
    if path.is_file():
        return ReportingConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if path != CONFIG_FILE and CONFIG_FILE.is_file():  # a fresh deployment starts from the shipped configuration
        return ReportingConfig.model_validate(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return ReportingConfig()


def save_config(cfg: ReportingConfig, path: Path | None = None) -> None:
    (path or config_path()).write_text(json.dumps(cfg.model_dump(), indent=2), encoding="utf-8")


# -- period assignment --------------------------------------------------------------------------

def cutoff_instant(year: int, month: int, cfg: ReportingConfig) -> dt.datetime:
    day = min(cfg.cutoff_day, calendar.monthrange(year, month)[1])
    hh, mm = (int(x) for x in cfg.cutoff_time.split(":"))
    return dt.datetime(year, month, day, hh, mm, 59, 999_999, tzinfo=ZoneInfo(cfg.timezone))


def _local(instant: str | dt.datetime, cfg: ReportingConfig) -> dt.datetime:
    t = dt.datetime.fromisoformat(instant) if isinstance(instant, str) else instant
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo(cfg.timezone))  # a naive time is read as local time
    return t.astimezone(ZoneInfo(cfg.timezone))


def reporting_period(instant: str | dt.datetime, cfg: ReportingConfig) -> str:
    """YYYY-MM of the report an instant belongs to: at or before the month's cut-off -> that month, after -> next."""
    t = _local(instant, cfg)
    if t <= cutoff_instant(t.year, t.month, cfg):
        return f"{t.year:04d}-{t.month:02d}"
    y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    return f"{y:04d}-{m:02d}"


def period_window(period: str, cfg: ReportingConfig) -> tuple[dt.datetime, dt.datetime]:
    """(exclusive start, inclusive end) instants of a report period."""
    y, m = (int(x) for x in period.split("-"))
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    return cutoff_instant(py, pm, cfg), cutoff_instant(y, m, cfg)


# -- money -----------------------------------------------------------------------------------------

def to_reporting(amount_minor: int, currency: str, rate: str, reporting: str) -> int:
    """amount (minor of `currency`) x rate (reporting units per 1 unit of currency) -> minor of `reporting`."""
    major = Decimal(amount_minor) / Decimal(100 if MINOR.get(currency, 100) == 100 else 1)
    value = major * Decimal(rate) * Decimal(MINOR.get(reporting, 100))
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt_minor(amount_minor: int, currency: str) -> str:
    unit = MINOR.get(currency, 100)
    if unit == 1:
        return f"{currency} {amount_minor:,}"
    return f"{currency} {amount_minor // 100:,}.{amount_minor % 100:02d}"


# -- reports -----------------------------------------------------------------------------------------

def monthly_spend(period: str, ledger: list[dict], cfg: ReportingConfig, rates: dict[str, str]) -> dict:
    """Layer 1 + 3: every payment booked to the period, per currency and line, consolidated at closing rates."""
    rows = [r for r in ledger if r["reporting_period"] == period]
    by_ccy: dict[str, int] = {}
    by_line: dict[str, int] = {}
    base_total = 0
    for r in rows:
        by_ccy[r["currency"]] = by_ccy.get(r["currency"], 0) + r["amount_minor"]
        by_line[r.get("line") or "(unbudgeted)"] = by_line.get(r.get("line") or "(unbudgeted)", 0) + r["base_cost_minor"]
        base_total += r["base_cost_minor"]
    consolidated, missing = [], []
    rep_total = 0
    for ccy, amt in sorted(by_ccy.items()):
        rate = rates.get(ccy)
        if rate is None:
            missing.append(ccy)
            continue
        value = to_reporting(amt, ccy, rate, cfg.reporting_currency)
        rep_total += value
        consolidated.append({"currency": ccy, "amount_minor": amt, "closing_rate": rate, "reporting_minor": value})
    base_at_closing = to_reporting(base_total, cfg.base_currency, rates[cfg.base_currency], cfg.reporting_currency) if cfg.base_currency in rates else None
    return {"period": period, "payments": len(rows), "by_currency": by_ccy, "by_line_base_minor": by_line,
            "base_cost_minor": base_total, "reporting_currency": cfg.reporting_currency, "closing_rates": rates,
            "consolidated": consolidated, "reporting_total_minor": rep_total, "missing_rates": missing,
            "base_cost_at_closing_minor": base_at_closing,
            "fx_difference_minor": (rep_total - base_at_closing) if base_at_closing is not None and not missing else None,
            "rows": rows}


def invoice_statement(fingerprint: str, ledger: list[dict]) -> dict:
    """Layer 2: the instalments paid on one invoice, across months; reconciled only when they add up to the contract."""
    parts = sorted((r for r in ledger if r.get("invoice_fingerprint") == fingerprint), key=lambda r: r["paid_at"])
    paid = sum(r["amount_minor"] for r in parts)
    contract = next((r["contract_total_minor"] for r in parts if r.get("contract_total_minor") is not None), None)
    complete = contract is not None and paid == contract
    return {"invoice_fingerprint": fingerprint, "contract_total_minor": contract, "paid_minor": paid,
            "outstanding_minor": (contract - paid) if contract is not None else None,
            "status": "RECONCILED" if complete else ("OPEN" if parts else "NONE"),
            "completed_in_period": parts[-1]["reporting_period"] if complete and parts else None,
            "instalments": [{k: r[k] for k in ("installment", "amount_minor", "currency", "paid_at", "reporting_period", "transfer_id")}
                            for r in parts]}


def period_report(period: str, ledger: list[dict], cfg: ReportingConfig, rates: dict[str, str]) -> dict:
    spend = monthly_spend(period, ledger, cfg, rates)
    # The invoice layer as of the END of the period: later instalments are not yet known to that month's report.
    as_of_period = [r for r in ledger if r["reporting_period"] <= period]
    invoices = sorted({r["invoice_fingerprint"] for r in as_of_period if r.get("invoice_fingerprint")})
    statements = [invoice_statement(fp, as_of_period) for fp in invoices]
    start, end = period_window(period, cfg)
    return {"period": period, "window": {"after": start.isoformat(), "until": end.isoformat(), "timezone": cfg.timezone,
                                         "cutoff": f"day {cfg.cutoff_day} {cfg.cutoff_time}"},
            "spend": spend,
            "invoices_completed": [s for s in statements if s["completed_in_period"] == period],
            "invoices_open": [s for s in statements if s["status"] == "OPEN"]}


def evaluate(inp: dict) -> dict:
    """Pure evaluation for ground truth and the form: {'config', 'posted'?, 'ledger'?, 'period'?, 'rates'?, 'invoice'?}."""
    cfg = ReportingConfig.model_validate(inp.get("config") or {})
    out: dict = {}
    if "posted" in inp:
        out["reporting_period"] = reporting_period(inp["posted"], cfg)
    if "ledger" in inp:
        ledger = [r | {"reporting_period": r.get("reporting_period") or reporting_period(r["paid_at"], cfg)} for r in inp["ledger"]]
        if "period" in inp:
            rates = inp.get("rates") or cfg.closing_rates.get(inp["period"], {})
            rep = period_report(inp["period"], ledger, cfg, rates)
            out |= {"payments": rep["spend"]["payments"], "by_currency": rep["spend"]["by_currency"],
                    "reporting_total_minor": rep["spend"]["reporting_total_minor"], "missing_rates": rep["spend"]["missing_rates"],
                    "fx_difference_minor": rep["spend"]["fx_difference_minor"],
                    "invoices_completed": [s["invoice_fingerprint"] for s in rep["invoices_completed"]],
                    "invoices_open": [s["invoice_fingerprint"] for s in rep["invoices_open"]]}
        if "invoice" in inp:
            s = invoice_statement(inp["invoice"], ledger)
            out |= {"invoice_status": s["status"], "invoice_paid_minor": s["paid_minor"], "invoice_outstanding_minor": s["outstanding_minor"],
                    "invoice_completed_in": s["completed_in_period"], "instalment_periods": [i["reporting_period"] for i in s["instalments"]]}
    return out
