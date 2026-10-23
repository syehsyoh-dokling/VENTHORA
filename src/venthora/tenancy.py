# ruff: noqa: E501
"""Book and tenant segregation: a book belongs to a tenant, may pay only its payees, is approved only by its people, and runs
under its own policy values on top of the shared policy.

Resolution (data/tenants.json): an exact book id beats a glob pattern (fnmatch); the longest matching pattern wins among
patterns; an unknown book goes to `default_tenant` unless the registry is `strict`, in which case the plan is BLOCKED.
What the plan does with it:
  unknown book (strict)            Policy stage FAILS -> BLOCKED ("book unknown to the tenant registry")
  payee not allowed for the book   Verity escalates the obligation (never paid, with or without an approval)
  policy overrides                 merged into state.policy before planning (facts.py / attach)
  approvers                        the executor accepts only the book's named people (name and role), on top of the matrix
Isolation that already holds per book: the ledger (a book never spends another book's money), reconciliation cases, the
spend ledger and budget realisation, the report per book.
"""
from __future__ import annotations

import json
from fnmatch import fnmatchcase
from pathlib import Path

from .config import ROOT
from .domain.models import BookConfig, ObligationDecision, Tenancy, TreasuryPolicy, TreasuryState

TENANTS_FILE = ROOT / "data" / "tenants.json"
PAYING = ("PAY", "CONVERT_AND_PAY")


class TenancyError(ValueError):
    pass


def load_registry(path: Path | None = None) -> dict:
    return json.loads((path or TENANTS_FILE).read_text(encoding="utf-8"))


def _config(tenant: str, book_id: str, raw: dict) -> BookConfig:
    cfg = BookConfig(tenant=tenant, book_id=book_id, allowed_payees=list(raw.get("allowed_payees") or ["*"]),
                     approvers=list(raw.get("approvers") or []), policy_overrides=dict(raw.get("policy_overrides") or {}))
    unknown = set(cfg.policy_overrides) - set(TreasuryPolicy.model_fields)
    if unknown:
        raise TenancyError(f"book {book_id!r} of {tenant!r} overrides unknown policy fields: {sorted(unknown)}")
    return cfg


def resolve(registry: dict, book_id: str) -> Tenancy:
    """Which tenant and config a book id resolves to."""
    strict = bool(registry.get("strict", False))
    tenants = registry.get("tenants") or {}
    for tenant, t in tenants.items():
        if book_id in (t.get("books") or {}):
            return Tenancy(known=True, strict=strict, config=_config(tenant, book_id, t["books"][book_id]), matched_by="exact")
    best = None
    for tenant, t in tenants.items():
        for pattern, raw in (t.get("books") or {}).items():
            if any(ch in pattern for ch in "*?[") and fnmatchcase(book_id, pattern) and (best is None or len(pattern) > len(best[1])):
                best = (tenant, pattern, raw)
    if best and not (best[1] == "*" and best[0] == registry.get("default_tenant")):
        tenant, pattern, raw = best
        return Tenancy(known=True, strict=strict, config=_config(tenant, pattern, raw), matched_by=f"pattern:{pattern}")
    default = registry.get("default_tenant")
    if default and not strict and default in tenants:
        books = tenants[default].get("books") or {}
        raw = books.get("*") or books.get(book_id) or {}
        return Tenancy(known=True, strict=strict, config=_config(default, "*", raw), matched_by="default")
    return Tenancy(known=False, strict=strict, config=None, matched_by=None)


def attach(state: TreasuryState, registry: dict | None = None) -> TreasuryState:
    """Resolve the book and apply its policy overrides; the state carries the tenancy for the gate and the executor."""
    ten = resolve(registry if registry is not None else load_registry(), state.book_id)
    update: dict = {"tenancy": ten}
    if ten.config and ten.config.policy_overrides:
        update["policy"] = state.policy.model_copy(update=dict(ten.config.policy_overrides))
    return state.model_copy(update=update)


def blocking_reason(state: TreasuryState, beneficiary_id: str) -> str | None:
    """Why Verity must escalate a payout to this payee under this book (None = allowed)."""
    ten = state.tenancy
    if ten is None or not ten.known or ten.config is None:
        return None
    return None if ten.config.allows(beneficiary_id) else "payee_not_allowed_for_book"


def tenancy_facts(state: TreasuryState, decisions: list[ObligationDecision]) -> dict:
    ten = state.tenancy
    if ten is None:
        return {"attached": False, "book_known": True}
    by_id = {o.obligation_id: o for o in state.obligations}
    cfg = ten.config
    disallowed = sorted(o.obligation_id for o in state.obligations if cfg and not cfg.allows(o.beneficiary_id))
    return {"attached": True, "book_id": state.book_id, "book_known": ten.known, "strict": ten.strict, "matched_by": ten.matched_by,
            "tenant": cfg.tenant if cfg else None, "allowed_payees": list(cfg.allowed_payees) if cfg else [],
            "approvers": [a.model_dump() for a in cfg.approvers] if cfg else [], "policy_overrides": dict(cfg.policy_overrides) if cfg else {},
            "disallowed_obligations": disallowed,
            "paying_disallowed": sorted(d.obligation_id for d in decisions if d.action in PAYING and d.obligation_id in by_id
                                        and cfg and not cfg.allows(by_id[d.obligation_id].beneficiary_id))}


def describe(f: dict) -> str:
    if not f.get("attached"):
        return "tenant segregation not enforced"
    if not f["book_known"]:
        return f"book {f['book_id']} is unknown to the tenant registry (strict): no plan may spend from it"
    bits = [f"tenant {f['tenant']} ({f['matched_by']})", "payees " + ("any" if "*" in f["allowed_payees"] else ", ".join(f["allowed_payees"]))]
    if f["approvers"]:
        bits.append("approvers " + ", ".join(f"{a['name']} ({a['role']})" for a in f["approvers"]))
    if f["policy_overrides"]:
        bits.append("overrides " + ", ".join(f"{k}={v}" for k, v in sorted(f["policy_overrides"].items())))
    if f["disallowed_obligations"]:
        bits.append("not this book's payees: " + ", ".join(f["disallowed_obligations"]))
    return "; ".join(bits)


def evaluate(inp: dict) -> dict:
    """Pure: {'registry': {...}, 'book_id': str} -> how the book resolves. Used by ground truth and the form."""
    try:
        ten = resolve(inp["registry"], inp["book_id"])
    except TenancyError as exc:
        return {"known": False, "error": str(exc), "tenant": None, "matched_by": None, "allowed_payees": [], "approvers": [], "policy_overrides": {}}
    cfg = ten.config
    return {"known": ten.known, "strict": ten.strict, "tenant": cfg.tenant if cfg else None, "matched_by": ten.matched_by,
            "allowed_payees": list(cfg.allowed_payees) if cfg else [], "approvers": [a.model_dump() for a in cfg.approvers] if cfg else [],
            "policy_overrides": dict(cfg.policy_overrides) if cfg else {}, "error": None,
            "allows": {p: cfg.allows(p) for p in inp.get("check_payees", [])} if cfg else {}}
