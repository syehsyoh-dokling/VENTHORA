"""Edition: whether the demo parts of VENTHORA are on (alpha-sandbox) or off (the real version).

One switch, in Platform Setup → Setup Checklist, changed by a Super admin only and recorded in the audit chain.
Demo mode ON shows the Tools & evidence menu (Autopilot Console, Checks & resilience, Hackathon evidence), the demo
buttons (demo purchase requests, demo vendor replies, sandbox simulations) and the demo vendor directory. OFF hides
them and their endpoints refuse to answer (404), so they are gone, not only out of sight.

A server started with VENTHORA_EDITION=production is locked: demo mode is OFF and the switch cannot turn it on, so
nobody can bring the demo back on a production server from the dashboard.
"""
from __future__ import annotations

from datetime import UTC, datetime

from .config import env

SWITCH_ROLES = ("super_admin",)
# the endpoints that exist only for the demo; they answer 404 when demo mode is off
DEMO_PATHS = ("/v1/evidence", "/v1/procurement/demo-requests", "/api/sandbox/deposits", "/live",
              "/demo/finance-app", "/demo/finance-app/data")  # the stand-in finance application's own screen
DEMO_SUFFIXES = ("/demo-replies", "/simulate")
DEMO_PREFIXES = ("/v1/procurement/",)  # .../form-link/... is the demo vendor's form


def locked() -> bool:
    return env("VENTHORA_EDITION").lower() == "production"


def state(org) -> dict:
    if locked():
        return {"demo": False, "locked": True, "source": "server: VENTHORA_EDITION=production",
                "changed_by": None, "changed_at": None}
    rec = org.get("edition", "edition") or {}
    return {"demo": rec.get("demo", True), "locked": False, "source": "Setup Checklist" if rec else "default (alpha-sandbox)",
            "changed_by": rec.get("changed_by"), "changed_at": rec.get("changed_at")}


def demo_on(org) -> bool:
    return state(org)["demo"]


def set_demo(org, on: bool, *, role: str, by: str) -> dict:
    if locked():
        raise PermissionError("this server runs the production edition (VENTHORA_EDITION=production): demo mode stays off")
    if role not in SWITCH_ROLES:
        raise PermissionError("only a Super admin may switch demo mode")
    org._put("edition", {"_id": "edition", "demo": bool(on), "changed_by": f"{by} ({role})",
                         "changed_at": datetime.now(UTC).isoformat(timespec="seconds")}, f"{by} ({role})")
    return state(org)


def is_demo_path(path: str) -> bool:
    if path in DEMO_PATHS or path.endswith(DEMO_SUFFIXES):
        return True
    return path.startswith(DEMO_PREFIXES) and "/form-link/" in path
