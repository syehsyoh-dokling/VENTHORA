"""Subscribe VENTHORA to Airwallex webhooks at a public URL, and store the endpoint's secret in .env.

  python scripts/register_webhook.py https://<public-host>          # the tunnel or host in front of the gateway
  python scripts/register_webhook.py --show                          # list this account's API-created webhooks

The subscription is VENTHORA's own: an existing webhook whose URL ends in /webhooks/airwallex is moved to the new
host (only its URL can change), otherwise one is created for the deposit, transfer and conversion events. The
secret Airwallex returns is written to AWX_WEBHOOK_SECRET in .env and never printed. Sandbox only.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venthora.config import load_env  # noqa: E402

load_env()
from venthora.gateways.airwallex import Airwallex  # noqa: E402
from venthora.webhook_gateway import PATH  # noqa: E402

EVENTS = ["deposit.pending", "deposit.settled", "deposit.rejected", "deposit.reversed",
          "payout.transfer.processing", "payout.transfer.sent", "payout.transfer.paid", "payout.transfer.failed",
          "conversion.settled", "conversion.cancelled"]
VERSION = "2025-02-14"


def write_secret(secret: str, env_file: Path = ROOT / ".env") -> None:
    text = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
    line = f"AWX_WEBHOOK_SECRET={secret}"
    if re.search(r"^AWX_WEBHOOK_SECRET=.*$", text, flags=re.M):
        text = re.sub(r"^AWX_WEBHOOK_SECRET=.*$", lambda _: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    env_file.write_text(text, encoding="utf-8")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="?", help="public https origin, e.g. https://example.ngrok-free.app")
    ap.add_argument("--show", action="store_true", help="list the webhooks and stop")
    a = ap.parse_args()
    bank = Airwallex()
    try:
        if not bank.configured or not bank.sandbox:
            print("Airwallex sandbox credentials are required (production is refused)")
            return 2
        hooks = await bank.list_webhooks()
        if a.show or not a.host:
            for h in hooks:
                print(h.get("id"), h.get("url"), len(h.get("events", [])), "events", h.get("version"))
            print(f"{len(hooks)} webhook(s)")
            return 0
        if not a.host.startswith("https://"):
            print("the public URL must be https")
            return 2
        url = a.host.rstrip("/") + PATH
        ours = [h for h in hooks if str(h.get("url", "")).endswith(PATH)]
        if ours and ours[0].get("url") == url:
            hook, action = ours[0], "already subscribed"
        elif ours:
            hook, action = await bank.update_webhook(ours[0]["id"], url), "moved to the new URL"
        else:
            hook = await bank.create_webhook(request_id=str(uuid.uuid4()), url=url, events=EVENTS, version=VERSION)
            action = "created"
        if not hook.get("secret"):
            print("Airwallex returned no secret; nothing written")
            return 1
        write_secret(hook["secret"])
        print(f"webhook {hook.get('id')} {action}: {hook.get('url')}")
        print(f"events: {', '.join(hook.get('events', []))} (version {hook.get('version')})")
        print("secret written to .env as AWX_WEBHOOK_SECRET (not shown); restart the API to load it")
        return 0
    finally:
        await bank.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
