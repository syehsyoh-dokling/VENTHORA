"""VENTHORA's default charge codes, for a company that has no standard of its own, and the spending of each staff
member traced through them.

A charge code stays OFFICE.PROJECT.ACCOUNT (e.g. ID-210.P001.005001). Where the FCO left a part blank, VENTHORA fills it:

  office   HQ-100 · a country office <ISO>-<n00> (ID-200, MY-300) · a branch its country office + 10 (ID-210, ID-220)
           · a project-finance unit PRJ-9nn (PRJ-901)
  project  OPS (general operations, company-wide); new projects P001, P002 ...
  account  six digits: the report category (001 payroll ... 007 other direct cost) then a running number (001001 Salaries)
  staff    <office cost centre>-<four-digit number> (ID-210-0007)

Only blanks are filled: a code the company set itself is never changed. Everything VENTHORA writes is saved by
"VENTHORA default", so the store says which codes are its own."""

from __future__ import annotations

import re
from collections.abc import Callable

BY = "VENTHORA default"

# the report categories (Reporting 1-7), their three-digit prefix and the accounts VENTHORA opens under each
CATEGORIES: list[dict] = [
    {
        "key": "payroll",
        "prefix": "001",
        "title": "Payroll",
        "accounts": [("001001", "Salaries and wages"), ("001002", "Overtime"), ("001003", "Allowances")],
    },
    {
        "key": "fringe",
        "prefix": "002",
        "title": "Fringe benefits",
        "accounts": [
            ("002001", "Social security, employer share"),
            ("002002", "Health insurance"),
            ("002003", "Pension"),
            ("002004", "Paid leave"),
        ],
    },
    {
        "key": "goods_services",
        "prefix": "003",
        "title": "Goods and services",
        "accounts": [
            ("003001", "Supplies"),
            ("003002", "Equipment"),
            ("003003", "Contracted services"),
            ("003004", "Consultants"),
        ],
    },
    {
        "key": "operation",
        "prefix": "004",
        "title": "Operation",
        "accounts": [
            ("004001", "Rent"),
            ("004002", "Utilities"),
            ("004003", "Communications"),
            ("004004", "Software and subscriptions"),
        ],
    },
    {
        "key": "cash_advance",
        "prefix": "005",
        "title": "Cash advance, petty cash and slush fund",
        "accounts": [
            ("005001", "Cash advance to staff"),
            ("005002", "Petty cash float"),
            ("005003", "Advance on a virtual card"),
        ],
    },
    {
        "key": "travel",
        "prefix": "006",
        "title": "Travel and transport",
        "accounts": [("006001", "Flights"), ("006002", "Lodging"), ("006003", "Per diem"), ("006004", "Local transport")],
    },
    {
        "key": "odc",
        "prefix": "007",
        "title": "Other direct cost",
        "accounts": [("007001", "Programme activities"), ("007002", "Workshops and events")],
    },
]
SALARY_ACCOUNT = "001001"
OPS = ("OPS", "General operations")

def scheme() -> dict:
    """The rules, as the Charge codes page shows them."""
    return {
        "format": "OFFICE.PROJECT.ACCOUNT",
        "example": "ID-210.P001.005001",
        "office": [
            {"kind": "HQ", "rule": "HQ-100", "example": "HQ-100"},
            {"kind": "COUNTRY_OFFICE", "rule": "<country>-<n x 100>, from 200", "example": "ID-200"},
            {"kind": "BRANCH_OFFICE", "rule": "its country office + 10 per branch", "example": "ID-210"},
            {"kind": "PROJECT_FINANCE", "rule": "PRJ-9nn, from 901", "example": "PRJ-901"},
        ],
        "project": {"default": OPS[0], "next": "P001, P002 ..."},
        "account": {
            "digits": 6,
            "rule": "3-digit category + 3-digit running number",
            "categories": [
                {
                    "prefix": c["prefix"],
                    "key": c["key"],
                    "title": c["title"],
                    "accounts": [{"code": a, "name": n} for a, n in c["accounts"]],
                }
                for c in CATEGORIES
            ],
        },
        "employee": {"rule": "<office cost centre>-<4-digit number>", "example": "ID-210-0007"},
    }


# -- generators -------------------------------------------------------------------------------------------------------
def _num(cc: str) -> int | None:
    m = re.match(r"^[A-Z]{2,3}-(\d{3})$", cc or "")
    return int(m.group(1)) if m else None


def _depth(units: dict, u: dict) -> int:
    d, cur, seen = 0, u, set()
    while cur and cur.get("parent_id") and cur["unit_id"] not in seen:
        seen.add(cur["unit_id"])
        cur, d = units.get(cur["parent_id"]), d + 1
    return d


def _plan_units(units: list[dict]) -> dict[str, str]:
    """unit_id -> the cost centre VENTHORA gives it, for every unit with none."""
    by_id = {u["unit_id"]: u for u in units}
    cc = {u["unit_id"]: (u.get("cost_centre") or "").upper() for u in units}
    taken = {c for c in cc.values() if c}
    out: dict[str, str] = {}
    for u in sorted(units, key=lambda x: (_depth(by_id, x), x["unit_id"])):
        if cc[u["unit_id"]]:
            continue
        kind, country = u["kind"], (u.get("country") or "XX").upper()
        new = None
        if kind == "HQ":
            new = next(c for c in ("HQ-100", *(f"HQ-{n}" for n in range(101, 1000))) if c not in taken)
        elif kind == "COUNTRY_OFFICE":
            used = {_num(c) for c in taken if _num(c) is not None}
            n = next((n for n in range(200, 1000, 100) if n not in used), None) or next(
                n for n in range(200, 1000) if n not in used
            )
            new = f"{country}-{n}"
        elif kind == "BRANCH_OFFICE":
            parent = cc.get(u.get("parent_id") or "") or ""
            base = _num(parent)
            prefix = parent.split("-")[0] if base is not None else country
            base = base if base is not None else 200
            new = next(
                (f"{prefix}-{base + 10 * k}" for k in range(1, 10) if f"{prefix}-{base + 10 * k}" not in taken),
                next(f"{prefix}-{n}" for n in range(base + 1, 1000) if f"{prefix}-{n}" not in taken),
            )
        else:  # PROJECT_FINANCE
            new = next(f"PRJ-{n}" for n in range(901, 1000) if f"PRJ-{n}" not in taken)
        cc[u["unit_id"]] = new
        taken.add(new)
        out[u["unit_id"]] = new
    return out


def next_project_code(org) -> str:
    have = {p["code"] for p in org.list("project")}
    return next(f"P{n:03d}" for n in range(1, 1000) if f"P{n:03d}" not in have)


def _plan_employees(users: list[dict], unit_cc: dict[str, str]) -> dict[str, str]:
    taken = {(u.get("employee_no") or "").upper() for u in users if u.get("employee_no")}
    out: dict[str, str] = {}
    for u in sorted(users, key=lambda x: x["user_id"]):
        if u.get("employee_no") or not u.get("active", True):
            continue
        cc = unit_cc.get(u["unit_id"]) or "HQ-100"
        new = next(f"{cc}-{n:04d}" for n in range(1, 10_000) if f"{cc}-{n:04d}" not in taken)
        taken.add(new)
        out[u["user_id"]] = new
    return out


def plan(org) -> dict:
    """What 'Fill VENTHORA defaults' would add, without changing anything."""
    units = org.list("unit")
    unit_cc_new = _plan_units(units)
    unit_cc = {u["unit_id"]: (u.get("cost_centre") or "").upper() or unit_cc_new.get(u["unit_id"], "") for u in units}
    projects = {p["code"] for p in org.list("project")}
    accounts = {a["code"] for a in org.list("account")}
    users = org.list("user")
    emp = _plan_employees(users, unit_cc)
    names = {u["unit_id"]: u["name"] for u in units}
    salary = [
        {
            "user_id": u["user_id"],
            "name": u["name"],
            "charge_code": f"{unit_cc.get(u['unit_id']) or 'HQ-100'}.{OPS[0]}.{SALARY_ACCOUNT}",
        }
        for u in users
        if u.get("active", True) and not u.get("charge_code") and unit_cc.get(u["unit_id"])
    ]
    return {
        "units": [{"unit_id": k, "name": names.get(k, k), "cost_centre": v} for k, v in unit_cc_new.items()],
        "projects": [] if OPS[0] in projects else [{"code": OPS[0], "name": OPS[1]}],
        "accounts": [
            {"code": a, "name": n, "category": c["title"]} for c in CATEGORIES for a, n in c["accounts"] if a not in accounts
        ],
        "employees": [
            {"user_id": k, "name": next(u["name"] for u in users if u["user_id"] == k), "employee_no": v} for k, v in emp.items()
        ],
        "charge_codes": salary,
    }


def apply(org, *, models: dict) -> dict:
    """Fill the blanks the plan names; a part the company set is left as it is."""
    p = plan(org)
    for row in p["units"]:
        u = org.get("unit", row["unit_id"])
        if u and not u.get("cost_centre"):
            org._put("unit", models["unit"].model_validate(u | {"cost_centre": row["cost_centre"]}).model_dump(), BY)
    for row in p["projects"]:
        org._put(
            "project",
            models["project"]
            .model_validate(
                {"project_id": f"pj-{row['code'].lower()}", "code": row["code"], "name": row["name"], "unit_id": None}
            )
            .model_dump(),
            BY,
        )
    for row in p["accounts"]:
        org._put("account", models["account"].model_validate({"account_id": f"ac-{row['code']}", **row}).model_dump(), BY)
    for row in p["employees"]:
        u = org.get("user", row["user_id"])
        if u and not u.get("employee_no"):
            org._put("user", u | {"employee_no": row["employee_no"]}, BY)
    for row in p["charge_codes"]:
        u = org.get("user", row["user_id"])
        if u and not u.get("charge_code"):
            org._put("user", u | {"charge_code": row["charge_code"]}, BY)
    return p | {"filled": sum(len(p[k]) for k in ("units", "projects", "accounts", "employees", "charge_codes"))}


def current(org) -> dict:
    """The codes in use, each with who set it: the company or VENTHORA's default."""

    def src(kind: str, rid: str) -> str:
        return "VENTHORA default" if org.source(kind, rid) == BY else "Company"

    units = org.list("unit")
    return {
        "units": [
            {
                "unit_id": u["unit_id"],
                "name": u["name"],
                "kind": u["kind"],
                "country": u.get("country"),
                "parent_id": u.get("parent_id"),
                "cost_centre": u.get("cost_centre") or "",
                "source": src("unit", u["unit_id"]),
            }
            for u in units
        ],
        "projects": [
            {
                "code": p["code"],
                "name": p["name"],
                "unit_id": p.get("unit_id"),
                "active": p.get("active", True),
                "source": src("project", p["project_id"]),
            }
            for p in org.list("project")
        ],
        "accounts": sorted(
            (
                {
                    "code": a["code"],
                    "name": a["name"],
                    "category": a["category"],
                    "active": a.get("active", True),
                    "source": src("account", a["account_id"]),
                }
                for a in org.list("account")
            ),
            key=lambda a: a["code"],
        ),
        "employees": [
            {
                "user_id": u["user_id"],
                "name": u["name"],
                "unit_id": u["unit_id"],
                "employee_no": u.get("employee_no") or "",
                "charge_code": u.get("charge_code") or "",
                "source": src("user", u["user_id"]),
            }
            for u in org.list("user")
        ],
        "next_project": next_project_code(org),
    }


# -- spending per staff member ----------------------------------------------------------------------------------------
def _place(units: dict, unit_id: str | None) -> dict:
    u = units.get(unit_id or "")
    branch = office = None
    cur, seen = u, set()
    while cur and cur["unit_id"] not in seen:
        seen.add(cur["unit_id"])
        if cur["kind"] == "BRANCH_OFFICE" and branch is None:
            branch = cur
        if cur["kind"] in ("COUNTRY_OFFICE", "HQ") and office is None:
            office = cur
        cur = units.get(cur.get("parent_id") or "")
    return {
        "unit": u["name"] if u else None,
        "unit_cc": (u or {}).get("cost_centre") or "",
        "branch": branch["name"] if branch else None,
        "branch_cc": (branch or {}).get("cost_centre") or "",
        "office": office["name"] if office else None,
        "office_cc": (office or {}).get("cost_centre") or "",
        "country": (u or {}).get("country") or (office or {}).get("country") or "",
    }


def _project(code: str | None) -> str:
    parts = str(code or "").upper().split(".")
    return parts[1] if len(parts) == 3 else ""


RECEIVED = ("IN_PAYMENT", "SETTLED")


def staff_spend(
    org,
    *,
    to_base: Callable[[int, str], int],
    base: str,
    country: str = "",
    unit_id: str = "",
    project: str = "",
    user_id: str = "",
) -> dict:
    """Per staff member: who they are (employee number, office, branch, country), the cash advances and petty cash
    they received, what their virtual cards spent, by project; amounts in each currency and in the base currency."""
    units = {u["unit_id"]: u for u in org.list("unit")}
    cards = {c["_id"]: c for c in org.list("card")}
    auth: dict[str, int] = {}
    for d in org.list("card_auth"):
        if d.get("decision") == "AUTHORIZED" and d.get("type", "AUTHORIZATION") == "AUTHORIZATION" and d.get("card_id"):
            # Airwallex signs spend negative: what was spent is its size
            auth[d["card_id"]] = auth.get(d["card_id"], 0) + abs(int(d.get("amount_minor") or 0))
    reach = org.subtree(unit_id) if unit_id and unit_id in units else None
    project = project.strip().upper()
    rows = []
    for u in org.list("user"):
        if user_id and u["user_id"] != user_id:
            continue
        place = _place(units, u["unit_id"])
        if country and place["country"].upper() != country.upper():
            continue
        if reach is not None and u["unit_id"] not in reach:
            continue
        lines = []
        for f in org.list("fund_request"):
            if f.get("requested_by") != u["user_id"] or f["kind"] not in ("cash_advance", "petty_cash"):
                continue
            pj = _project(f.get("charge_code"))
            if project and pj != project:
                continue
            received = f["amount_minor"] if f["status"] in RECEIVED else 0
            card = cards.get(f.get("card_id") or "")
            spent = None
            if card:
                spent = (  # an older settlement may have kept Airwallex's negative sign
                    abs(f["settled_minor"])
                    if f["status"] == "SETTLED" and f.get("settled_minor") is not None
                    else auth.get(card["_id"], 0)
                )
            lines.append(
                {
                    "request": f["request_number"],
                    "kind": f["kind"],
                    "status": f["status"],
                    "charge_code": f.get("charge_code"),
                    "project": pj,
                    "currency": f["currency"],
                    "amount_minor": f["amount_minor"],
                    "received_minor": received,
                    "by_card": bool(card),
                    "card_masked": (card or {}).get("masked"),
                    "card_spent_minor": spent,
                    "settled_minor": f.get("settled_minor"),
                    "date": f.get("request_date"),
                }
            )
        if project and not lines:
            continue
        tot = {
            "advances": len([x for x in lines if x["received_minor"]]),
            "received_base_minor": sum(to_base(x["received_minor"], x["currency"]) for x in lines),
            "card_spent_base_minor": sum(to_base(x["card_spent_minor"] or 0, x["currency"]) for x in lines),
            "cards": len([x for x in lines if x["by_card"]]),
        }
        by_project: dict[str, dict] = {}
        for x in lines:
            p = by_project.setdefault(
                x["project"] or "-", {"project": x["project"] or "-", "received_base_minor": 0, "card_spent_base_minor": 0}
            )
            p["received_base_minor"] += to_base(x["received_minor"], x["currency"])
            p["card_spent_base_minor"] += to_base(x["card_spent_minor"] or 0, x["currency"])
        rows.append(
            {
                "user_id": u["user_id"],
                "name": u["name"],
                "employee_no": u.get("employee_no") or "",
                "role": u["role"],
                "salary_code": u.get("charge_code") or "",
                **place,
                "totals": tot,
                "by_project": list(by_project.values()),
                "lines": lines,
            }
        )
    rows.sort(key=lambda r: (-r["totals"]["received_base_minor"], r["name"]))
    return {
        "base": base,
        "rows": rows,
        "total": {
            "staff": len(rows),
            "received_base_minor": sum(r["totals"]["received_base_minor"] for r in rows),
            "card_spent_base_minor": sum(r["totals"]["card_spent_base_minor"] for r in rows),
        },
        "filters": {
            "countries": sorted({(u.get("country") or "").upper() for u in units.values() if u.get("country")}),
            "units": [
                {"unit_id": u["unit_id"], "name": u["name"], "cost_centre": u.get("cost_centre") or ""} for u in units.values()
            ],
            "projects": sorted(p["code"] for p in org.list("project")),
        },
    }
