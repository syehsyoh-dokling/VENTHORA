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


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()
