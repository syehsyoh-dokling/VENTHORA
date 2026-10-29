# ruff: noqa: E501
"""Send the review cases through the inbound mail webhook and show what stopped for a person.

  python scripts/demo_review_cases.py                                   # to the local API (127.0.0.1:8001)
  python scripts/demo_review_cases.py --to https://<tunnel-host>         # the real path: tunnel -> gateway -> API
  python scripts/demo_review_cases.py --processing --approve            # then pay what is ready, left in flight

Eleven emails (src/venthora/testing/review_cases.py): two clean invoices become bills ready to pay, one customer
announces a payment, one forged email is quarantined, and seven stop for a person, each for a reason quoted from the
email or its PDF. Every outcome is judged against what the tests expect; the run fails if any differs.

--processing plans the intake-only book (data/scenarios/intake_only.json: nothing payable but what arrived by email) and
executes it on the Airwallex sandbox without simulating settlement, so the transfers stay in flight and the overview
shows them as processing. --approve signs as Dana Finch (finance manager) and Omar Reyes (CFO) when the plan needs it.
The inbound secret comes from VENTHORA_INBOUND_EMAIL_SECRET in .env and is never printed.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venthora.config import env, load_env  # noqa: E402

load_env()
from venthora.testing.review_cases import cases, judge, postmark  # noqa: E402


def call(method: str, url: str, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json", "user-agent": "venthora-demo/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, raw[:200].decode(errors="replace")


def money(minor, ccy):
    return f"{ccy} {minor / 100:,.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", default="http://127.0.0.1:8001", help="where the mail service would post (local API or tunnel host)")
    ap.add_argument("--api", default="http://127.0.0.1:8001", help="the VENTHORA API (private)")
    ap.add_argument("--processing", action="store_true", help="pay what is ready, without simulating settlement")
    ap.add_argument("--approve", action="store_true", help="approve as Dana Finch and Omar Reyes when the plan needs it")
    ap.add_argument("--skip-send", action="store_true", help="send nothing; only pay what is ready and show the position")
    a = ap.parse_args()
    secret = env("VENTHORA_INBOUND_EMAIL_SECRET")
    if not secret and not a.skip_send:
        print("set VENTHORA_INBOUND_EMAIL_SECRET in .env first")
        return 2
    failures = 0
    if not a.skip_send:
        auth = {"authorization": "Basic " + base64.b64encode(f"postmark:{secret}".encode()).decode()}
        stamp = time.strftime("%H%M%S")
        print(f"1 · {len(cases(stamp))} emails to {a.to}/webhooks/email\n")
        sent = {}
        for c in cases(stamp):
            code, out = call("POST", a.to.rstrip("/") + "/webhooks/email", postmark(c, stamp), auth)
            if code != 200:
                print(f"  {c['id']:<14} HTTP {code}: {out}")
                return 1
            sent[c["id"]] = out["intake_id"]
        _, queue = call("GET", a.api + "/v1/intake")
        by_id = {r["intake_id"]: r for r in queue}
        for c in cases(stamp):
            r = by_id[sent[c["id"]]]
            bad = judge(c, r)
            failures += bool(bad)
            # the reason that decided it, not the checks it passed on the way
            why = next((x for x in r["reasons"] if not ("matched as" in x or x.startswith("registered mailbox"))), "")
            print(f"  {'✓' if not bad else '✗'} {c['id']:<14} {r['status']:<12} {money(r['amount_minor'] or 0, r['currency'] or ''):>15}  "
                  f"{c['title']}" + (f"\n      → {why[:150]}" if why and c["tile"] != "ready" else "") + (f"\n      ✗ {'; '.join(bad)}" if bad else ""))
        print(f"\n  {len(sent) - failures} of {len(sent)} as expected")

    if a.processing:
        print("\n2 · pay what is ready, left in flight (no simulated settlement)")
        code, state = call("GET", a.api + "/v1/intake/state/intake_only?live=true")  # live balances and today's rates
        if code != 200:
            print(f"  could not build the book: HTTP {code} {state}")
            return 1
        code, plan = call("POST", a.api + "/v1/plans", state)
        print(f"  plan {plan['decision']}: " + ", ".join(f"{d['obligation_id']} {d['action']}" for d in plan["obligations"]))
        approvals = []
        if plan["decision"] == "REQUIRES_APPROVAL":
            if not a.approve:
                print("  the plan needs a person's approval; run again with --approve")
                return 0
            from venthora.domain.models import PlanResult
            from venthora.execution.executor import terms_digest

            digest = terms_digest(PlanResult.model_validate(plan))
            approvals = [{"approver": "Dana Finch", "role": "finance_manager", "terms_digest": digest},
                         {"approver": "Omar Reyes", "role": "cfo", "terms_digest": digest}]
        if plan["decision"] != "BLOCKED":
            code, res = call("POST", a.api + "/v1/executions", {"state": state, "plan_hash": plan["plan_hash"], "approvals": approvals,
                                                                  "requested_by": "VENTHORA", "simulate_settlement": False}, timeout=300)
            print(f"  execution {res.get('status')}: {str(res.get('message'))[:160]}")
    code, pos = call("GET", a.api + "/v1/treasury/position?fresh=true")
    if code == 200:
        b = pos["base_currency"]
        print("\n3 · the overview's top row now")
        print(f"  available       {money(pos['available']['base_minor'], b):>22}   (−{money(pos['processing_out']['base_minor'], b)} processing, "
              f"{pos['processing_out']['count']} transfer(s))")
        print(f"  bills to pay    {money(pos['bills_unpaid']['base_minor'], b):>22}   {pos['bills_unpaid']['approved_count']} ready, "
              f"{pos['bills_unpaid']['waiting_count']} waiting for a person")
        print(f"  pending deposits{money(pos['deposits_pending']['base_minor'], b):>22}   {pos['deposits_pending']['count']}")
        print(f"  incoming        {money(pos['incoming_uncleared']['base_minor'], b):>22}   {pos['incoming_uncleared']['arrived_count']} arrived, "
              f"{pos['incoming_uncleared']['announced_count']} announced")
        print(f"  outlook         {money(pos['after_all_base_minor'], b):>22}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
