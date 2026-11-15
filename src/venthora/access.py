# ruff: noqa: E501
"""Who may reach which menu, and how VENTHORA is deployed.

Deployment (VENTHORA_DEPLOYMENT, read at start):
  self_hosted  (default) a company runs VENTHORA itself: its IT has the Developer Console for the one-time technical setup.
  saas         VENTHORA runs it for many companies: the technical setup is the operator's (server config), so the Developer
               Console and its settings endpoints do not exist for the companies (404).

Menus by role:
  Platform Setup  everything a company sets up to start: the FCO (Super admin) changes it; Developer (IT) helps with the
                  technical parts and only views the Airwallex account and the cards, which hold the company's money.
  FCO Authorize   who may approve what, the mandate, payment priority: whoever holds the mandate authority (the FCO,
                  until the FCO hands it on). Finance standards and running the due lines: the FCO only. A Developer
                  never reads it. The daily work the delegation governs (the payroll register, travel requests) is
                  not FCO Authorize: each of those endpoints checks the role its own step needs.
  Cards           the FCO issues and changes them; a finance manager or the CFO may freeze one, only the FCO unfreezes.
  Developer       one-time technical setup (agent signing keys, anchoring): Developer changes it, the FCO views it.
  Demo-Only       demo mode only (edition.py).

The role comes from the request: the ``role`` query parameter every dashboard write carries, or the X-Venthora-Role
header the dashboard sends with every call. A request with neither (an API key's call has its role set by the key)
is not narrowed here; the endpoint's own checks still apply.
"""
from __future__ import annotations

import re

from .config import env

FCO = "super_admin"
DEV = "developer"
WRITES = ("POST", "PUT", "PATCH", "DELETE")


def deployment() -> str:
    return "saas" if env("VENTHORA_DEPLOYMENT").lower() == "saas" else "self_hosted"


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("^(?:" + "|".join(patterns) + ")$")


# FCO Authorize, the settings of the delegation: who may approve what, the mandate, payment priority. Changed by
# whoever holds the mandate authority (the FCO, until the FCO hands it on), never read by a Developer
MANDATE = _rx(r"/v1/mandate", r"/v1/priority-sop", r"/v1/doa/policies/[^/]+", r"/v1/doa/custom(/.*)?", r"/v1/doa/settings")
# FCO Authorize, the rest: changed by the FCO only
FCO_AUTHORIZE = _rx(r"/v1/org/standards", r"/v1/placement-providers(/.*)?", r"/v1/doa/run")
# The daily work under /v1/doa is NOT FCO Authorize: the payroll register (a finance manager prepares it, HR approves
# its step, the FCO approves the last one) and travel requests. Each of those endpoints checks its own roles.
# Freezing a card is a safety action a finance manager may take; the endpoint lets only the FCO unfreeze one.
FREEZE = _rx(r"/v1/cards/[^/]+/freeze")
FREEZERS = {FCO, "finance_manager", "cfo"}
HOLDERS = {"super_admin": "FCO (Super admin)", "cfo": "CFO", "finance_manager": "Finance manager"}
# Platform Setup, the money part: the FCO changes it, a Developer may only look
PLATFORM_MONEY = _rx(r"/v1/airwallex/.*", r"/v1/company-account(/.*)?", r"/v1/providers/.*", r"/v1/controls/.*",
                     r"/v1/cards(/.*)?", r"/v1/card-authorization(/.*)?", r"/v1/visa/vic(/.*)?", r"/v1/local-rails/.*")
# Platform Setup, the technical part: the FCO or a Developer changes it
PLATFORM_TECH = _rx(r"/v1/webhooks/register", r"/v1/settings/ai(/.*)?", r"/v1/settings/email(/.*)?", r"/v1/outbox/smtp(/.*)?",
                    r"/v1/developer/keys(/.*)?",
                    r"/v1/sources/connectors", r"/v1/sources/connectors/[^/]+", r"/v1/sources/connectors/[^/]+/discover")
# Developer Console: a Developer changes it, the FCO views it; absent on SaaS
DEV_CONSOLE = _rx(r"/v1/visa/tap/keys", r"/v1/visa/tap/sign-test", r"/v1/metal/(key|endpoint|fund|settings)")
# Developer Console, the one-time service setup whose state the FCO's pages also read: only its WRITES are the
# Developer's (the GETs stay open to the FCO's screens); on SaaS the writes are the operator's
DEV_SETUP = _rx(r"/v1/airwallex/connection", r"/v1/providers/(xendit|midtrans)", r"/v1/visa/vic/(credentials|xpay-public-key|xpay-secret)",
                r"/v1/placement-connections/[^/]+")
# the GETs of FCO Authorize a Developer may not make (standards stay readable: every screen shows the base currency)
FCO_READS = _rx(r"/v1/doa(/.*)?", r"/v1/mandate", r"/v1/priority-sop", r"/v1/placement-providers(/.*)?")


def check(role: str | None, method: str, path: str, holder: str | None = None) -> tuple[int, str] | None:
    """(status, reason) when the request is refused, None when it may go on. `holder`: the role that holds the
    mandate authority now (the FCO when not given)."""
    write = method in WRITES
    if DEV_CONSOLE.match(path):
        if deployment() == "saas":
            return 404, "not available: on SaaS the technical setup is done by the VENTHORA operator"
        if write and role is not None and role != DEV:
            return 403, "Developer Console: only a Developer (IT) changes this; the FCO can view it"
        return None
    if write and DEV_SETUP.match(path):
        if deployment() == "saas":
            return 404, "not available: on SaaS the technical setup is done by the VENTHORA operator"
        if role is not None and role != DEV:
            return 403, "Developer Console: only a Developer (IT) changes this; the FCO can view it"
        return None
    if role is None:
        return None
    if role == DEV and not write and FCO_READS.match(path):
        return 403, "FCO Authorize is visible to the FCO (Super admin) only"
    if not write:
        return None
    if MANDATE.match(path) and role != (holder or FCO):
        return 403, f"FCO Authorize: only the {HOLDERS.get(holder or FCO, holder)} changes this"
    if FCO_AUTHORIZE.match(path) and role != FCO:
        return 403, "FCO Authorize: only the FCO (Super admin) changes this"
    if FREEZE.match(path):
        return None if role in FREEZERS else (403, "Cards: a finance manager, the CFO or the FCO (Super admin) freezes a card")
    if PLATFORM_MONEY.match(path) and role != FCO:
        return 403, "Platform Setup: only the FCO (Super admin) changes the Airwallex account and cards; a Developer views them"
    if PLATFORM_TECH.match(path) and role not in (FCO, DEV):
        return 403, "Platform Setup: only the FCO (Super admin) or a Developer (IT) changes this"
    return None
