# ruff: noqa: E501
"""Server, once: switch remote authorization on for the PLATFORM's issuing config (it governs the cards of every
connected account), ask Airwallex for a new shared secret, and keep it in .env as AWX_REMOTE_AUTH_SECRET so the
platform gateway can check Airwallex's signature (saas_gateway.remote_auth). Prints no secret.

    sudo /opt/venthora/.venv/bin/python scripts/set_platform_remote_auth.py [https://host/webhooks/airwallex/remote-auth] [DECLINED|AUTHORIZED]
"""
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venthora/src")
from venthora.config import env, load_env  # noqa: E402

load_env()
from venthora.gateways.airwallex import Airwallex  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "https://venthora.muwahid.org/webhooks/airwallex/remote-auth"
DEFAULT = sys.argv[2] if len(sys.argv) > 2 else "DECLINED"
ENV = Path("/opt/venthora/.env")


async def main():
    bank = Airwallex(credentials=(env("AIRWALLEX_CLIENT_ID"), env("AIRWALLEX_API_KEY")))
    resp = await bank.update_issuing_config({"remote_auth": {"url": URL, "enabled": True, "default_action": DEFAULT,
                                                             "version": 2, "new_shared_secret": True}})
    await bank.close()
    st = resp.get("remote_auth_settings") or resp.get("remote_auth") or {}
    secret = st.get("shared_secret") or ""
    print("platform remote auth:", {k: st.get(k) for k in ("enabled", "url", "default_action", "version")},
          "secret:", f"...{secret[-4:]}" if secret else "NOT RETURNED")
    if not secret:
        raise SystemExit("Airwallex returned no shared secret; nothing written")
    shutil.copy2(ENV, ENV.with_name(".env.pre-remote-auth"))
    kept = [ln for ln in ENV.read_text().splitlines() if not ln.startswith("AWX_REMOTE_AUTH_SECRET=")]
    ENV.write_text("\n".join(kept + [f"AWX_REMOTE_AUTH_SECRET={secret}"]) + "\n")
    shutil.chown(ENV, "venthora", "venthora")
    print(".env updated: AWX_REMOTE_AUTH_SECRET")


asyncio.run(main())
