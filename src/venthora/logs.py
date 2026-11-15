"""Application logs: one logger, `venthora`, written to stderr (systemd and Docker collect it from there).

The audit chain records what VENTHORA decided and did; it is the financial record. These logs are for the operator:
a background job that failed, a request that was throttled, a call to the bank that broke. VENTHORA_LOG_LEVEL sets the
level (INFO by default). Nothing secret is ever written here: no key, token, password or request body.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("venthora")


def setup() -> logging.Logger:
    """Attach the handler once; calling it again changes nothing."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        log.addHandler(handler)
        log.propagate = False
    log.setLevel(getattr(logging, os.environ.get("VENTHORA_LOG_LEVEL", "INFO").upper(), logging.INFO))
    return log
