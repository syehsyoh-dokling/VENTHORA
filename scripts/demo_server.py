# ruff: noqa: E501
"""Serve the dashboard on a clean set of stores for the demo recording.

  python scripts/demo_server.py [--port 8001] [--reset]

The shared stores under data/ carry every test cycle's history (realized budget spend, payee changes, dozens of books), which
is exactly what a demo should not start from. This process points the execution, intake and audit stores at data/demo/ (a
fresh directory, created on first start; --reset wipes it) and runs the same API as `python -m uvicorn venthora.api:app`.
The sandbox credentials still come from .env; nothing else differs.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--reset", action="store_true", help="wipe data/demo before starting")
    a = ap.parse_args()
    demo = ROOT / "data" / "demo"
    if a.reset and demo.exists():
        shutil.rmtree(demo)
    demo.mkdir(parents=True, exist_ok=True)
    os.environ["VENTHORA_EXECUTION_DB"] = str(demo / "execution.db")
    os.environ["VENTHORA_INTAKE_DB"] = str(demo / "intake.db")
    os.environ["VENTHORA_AUDIT_DB"] = str(demo / "audit.db")
    os.environ["VENTHORA_REPORTING_CONFIG"] = str(demo / "reporting.json")  # the mandate form may change the cut-off
    sys.path.insert(0, str(ROOT / "src"))
    import uvicorn

    uvicorn.run("venthora.api:app", host="127.0.0.1", port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
