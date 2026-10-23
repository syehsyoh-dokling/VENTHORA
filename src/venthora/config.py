"""Settings from environment, with a small `.env` loader (real env vars win)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | None = None) -> None:
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.replace("_", "").isalnum():
            os.environ.setdefault(key, value.strip().strip("'\""))


# The organizers' guide (airwallexdev.com/guide) names the sandbox credentials AWX_CLIENT_ID and
# AWX_API_KEY. Those are the primary names; the longer AIRWALLEX_* names are accepted as aliases.
ALIASES = {
    "AIRWALLEX_CLIENT_ID": "AWX_CLIENT_ID",
    "AIRWALLEX_API_KEY": "AWX_API_KEY",
    "AIRWALLEX_BASE_URL": "AWX_BASE_URL",
    "AIRWALLEX_GLOBAL_ACCOUNT_ID": "AWX_GLOBAL_ACCOUNT_ID",
}


def env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    if not value and name in ALIASES:
        value = os.getenv(ALIASES[name], "").strip()
    return value or default
