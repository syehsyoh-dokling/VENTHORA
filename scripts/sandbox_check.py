"""Read-only Airwallex sandbox check: login, balances, Global Accounts, one indicative rate.

Moves no money and prints no credential. Run: python scripts/sandbox_check.py
"""
import asyncio
import json
import sys

from venthora.config import load_env
from venthora.domain.money import fmt
from venthora.gateways.airwallex import Airwallex, AirwallexError


async def main() -> int:
    load_env()
    bank = Airwallex()
    print(f"base URL: {bank.base_url} (sandbox={bank.sandbox}); credentials configured: {bank.configured}")
    if not bank.configured or not bank.sandbox:
        return 2
    try:
        await bank._auth_headers()
        print("login: OK (bearer token issued)")
        balances = await bank.balances_minor()
        print("cash balances:", ", ".join(fmt(v, c) for c, v in sorted(balances.items())) or "(none)")
        accounts = await bank.list_global_accounts()
        print(f"global accounts: {len(accounts)}")
        for a in accounts[:10]:
            keys = {k: a.get(k) for k in ("id", "status", "country_code", "currency", "account_name") if k in a}
            features = a.get("required_features") or a.get("supported_features")
            print("  ", json.dumps(keys), "features:", json.dumps(features)[:160] if features else "-")
        for ccy in ("EUR", "GBP"):
            try:
                print(f"rate {ccy}->USD:", await bank.rate_to_base(ccy, "USD"))
            except AirwallexError as exc:
                print(f"rate {ccy}->USD failed: {exc} (status={exc.status}, code={exc.code})")
    except AirwallexError as exc:
        print(f"FAILED: {exc} (status={exc.status}, code={exc.code})")
        return 1
    finally:
        await bank.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
