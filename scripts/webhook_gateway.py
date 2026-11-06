"""Run the webhook-only gateway that a tunnel may expose (see src/venthora/webhook_gateway.py).

  python scripts/webhook_gateway.py [--port 8002] [--upstream http://127.0.0.1:8001] [--api]

--api also forwards the API to other applications, only for requests carrying an API key.
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
    ap.add_argument("--api", action="store_true", help="also forward the API for requests carrying a key")
    ap.add_argument("--email-upstream", default=None, help="send inbound email (/webhooks/email) to this VENTHORA instead")
    ap.add_argument("--paysim-upstream", default=None, help="send the phone pay simulator (/pay-sim/...) elsewhere")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    import uvicorn

    from venthora.webhook_gateway import create_app

    app = create_app(a.upstream, api=a.api, email_upstream=a.email_upstream, paysim_upstream=a.paysim_upstream)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info")


if __name__ == "__main__":
    main()
