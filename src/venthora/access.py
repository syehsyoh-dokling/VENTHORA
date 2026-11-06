"""Who may reach which menu, and how VENTHORA is deployed.

Deployment (VENTHORA_DEPLOYMENT, read at start):
  self_hosted  (default) a company runs VENTHORA itself: its IT has the Developer Console for the one-time technical setup.
  saas         VENTHORA runs it for many companies: the technical setup is the operator's (server config), so the Developer
               Console and its settings endpoints do not exist for the companies (404).

Menus by role:
  Platform Setup  everything a company sets up to start: the FCO (Super admin) changes it; Developer (IT) helps with the
                  technical parts and only views the Airwallex account and the cards, which hold the company's money.
  FCO Authorize   who may approve what, payment priority, finance standards, the admin team: the FCO only. A Developer
                  never reads it.
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


# FCO Authorize: changed by the FCO only, never read by a Developer
FCO_AUTHORIZE = _rx(r"/v1/doa(/.*)?", r"/v1/mandate", r"/v1/priority-sop", r"/v1/org/standards")
# Platform Setup, the money part: the FCO changes it, a Developer may only look
PLATFORM_MONEY = _rx(r"/v1/airwallex/.*", r"/v1/cards(/.*)?", r"/v1/card-authorization(/.*)?", r"/v1/visa/vic(/.*)?")
# Platform Setup, the technical part: the FCO or a Developer changes it
PLATFORM_TECH = _rx(r"/v1/webhooks/register", r"/v1/settings/ai(/.*)?", r"/v1/settings/email(/.*)?", r"/v1/outbox/smtp(/.*)?",
                    r"/v1/developer/keys(/.*)?",
                    r"/v1/sources/connectors", r"/v1/sources/connectors/[^/]+", r"/v1/sources/connectors/[^/]+/discover")
# Developer Console: a Developer changes it, the FCO views it; absent on SaaS
DEV_CONSOLE = _rx(r"/v1/visa/tap/keys", r"/v1/visa/tap/sign-test", r"/v1/metal/(key|endpoint|fund|settings)")
# the GETs of FCO Authorize a Developer may not make (standards stay readable: every screen shows the base currency)
FCO_READS = _rx(r"/v1/doa(/.*)?", r"/v1/mandate", r"/v1/priority-sop")


def check(role: str | None, method: str, path: str) -> tuple[int, str] | None:
    """(status, reason) when the request is refused, None when it may go on."""
    write = method in WRITES
    if DEV_CONSOLE.match(path):
        if deployment() == "saas":
            return 404, "not available: on SaaS the technical setup is done by the VENTHORA operator"
        if write and role is not None and role != DEV:
            return 403, "Developer Console: only a Developer (IT) changes this; the FCO can view it"
        return None
    if role is None:
        return None
    if role == DEV and not write and FCO_READS.match(path):
        return 403, "FCO Authorize is visible to the FCO (Super admin) only"
    if not write:
        return None
    if FCO_AUTHORIZE.match(path) and role != FCO:
        return 403, "FCO Authorize: only the FCO (Super admin) changes this"
    if PLATFORM_MONEY.match(path) and role != FCO:
        return 403, "Platform Setup: only the FCO (Super admin) changes the Airwallex account and cards; a Developer views them"
    if PLATFORM_TECH.match(path) and role not in (FCO, DEV):
        return 403, "Platform Setup: only the FCO (Super admin) or a Developer (IT) changes this"
    return None
