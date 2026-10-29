"""Run the webhook-only gateway that a tunnel may expose (see src/venthora/webhook_gateway.py).

  python scripts/webhook_gateway.py [--port 8002] [--upstream http://127.0.0.1:8001]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--upstream", default="http://127.0.0.1:8001", help="the VENTHORA API, never exposed itself")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    import uvicorn

    from venthora.webhook_gateway import create_app

    uvicorn.run(create_app(a.upstream), host="127.0.0.1", port=a.port, log_level="info")


if __name__ == "__main__":
    main()
