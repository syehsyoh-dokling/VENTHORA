# ruff: noqa: E501
"""The demo walkthrough: every control, in order, on the real Airwallex sandbox, driven through the dashboard's own endpoints.

  python scripts/demo_walkthrough.py [--base http://127.0.0.1:8000] [--approve NAME] [--cfo NAME]

Run it beside the open dashboard (http://localhost:8000): each scene prints its narration cue, calls the same endpoints the
dashboard's buttons call, and prints what the viewer should now see on screen. Nothing here needs a model; every outcome
comes from rules, and every amount that moves is on the sandbox. Writes docs/demo_run_log.json; the storyboard with the
narration outline is docs/DEMO_STORYBOARD.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from venthora.testing.groundtruth import load_cases  # noqa: E402

SCENES: list[dict] = []


def scene(n: int, title: str, say: str, show: str, result: dict) -> None:
    SCENES.append({"scene": n, "title": title, "say": say, "show": show, "result": result})
    print(f"\n=== Scene {n} · {title}\n  say:  {say}\n  show: {show}\n  got:  {json.dumps(result, default=str)[:400]}")


def requires(plan: dict) -> list[str]:
    pol = next((s for s in plan["stages"] if s["engine"] == "Policy"), None)
    return ((pol or {}).get("details") or {}).get("local", {}).get("requires_approval", []) if pol else []


def main(base: str, approver: str, cfo: str) -> int:
    c = httpx.Client(base_url=base, timeout=120)
    tag = dt.datetime.now().strftime("%H%M%S")
    t0 = time.perf_counter()

    # 1 · the plan
    plan = c.post("/api/scenarios/kit01_initial/plan").raise_for_status().json()
    scene(1, "The plan, proven", "Kit 01's book: five obligations, two receivables, a reserve floor. The agent pays what it can prove safe, defers the cheapest bill, escalates the one with a policy exception. Every stage is a check, not a narrative.",
          "Plan section: press Plan on kit01_initial; read the Decision tile and the stage list; open the counterfactuals.",
          {"decision": plan["decision"], "actions": {d["obligation_id"]: d["action"] for d in plan["obligations"]}, "stages": [s["engine"] for s in plan["stages"]], "ms": plan["duration_ms"]})

    # 2 · intake: the fraud email, quarantined
    fraud = next(x for x in load_cases("anomalies") if x["kind"] == "intake" and x["expect"].get("status") == "QUARANTINED")
    req = c.post("/v1/intake/emails", params={"dry_run": "true"}, content=fraud["input"].encode(), headers={"content-type": "message/rfc822"}).raise_for_status().json()
    scene(2, "An email that wants money", f"A payee 'moved banks', urgent, please pay today. The sender is not the registered mailbox. The pipeline quarantines it with the verbatim quotes; no model opinion is involved. Case {fraud['id']}: {fraud['title']}.",
          "Submit a case: suite anomalies, pick the quarantined email, Submit; the verdict and the quotes appear; ground truth PASS.",
          {"case": fraud["id"], "status": req["status"], "trust": req["trust"], "flags": req["flags"], "quotes": [q["quote"] for q in req["quotes"]][:3]})

    # 3 · payee lifecycle: a bank change on Lumen, the wrong call-back, the right one
    ch = c.post("/v1/payees/ben-lumen-design/bank-change", json={"bank_fingerprint": f"iban:demo-{tag}", "source": "demo: supplier email", "note": "walkthrough"}).json()
    wrong = c.post("/v1/payees/ben-lumen-design/verify", json={"by": approver, "role": "finance_manager", "called_number": "+1 555 0100", "note": "number from the email"})
    right = c.post("/v1/payees/ben-lumen-design/verify", json={"by": approver, "role": "finance_manager", "called_number": c.get("/v1/payees/ben-lumen-design").json()["registered_phone"], "note": "registered number"}).json()
    scene(3, "The call-back nobody can skip", "New bank details move the payee to CHANGED: no plan and no approval can pay it. The call-back to the number printed in the email is refused by code; the call-back to the registered number verifies, and a cooling period follows.",
          "Payees · lifecycle: Lumen Design turns CHANGED, press Call-back with a wrong number (refused), then the registered number (VERIFIED, cooling until).",
          {"after_change": ch["status"], "wrong_callback": {"http": wrong.status_code, "detail": wrong.json().get("detail")}, "after_callback": right["status"], "cooling_until": right["cooling_until"]})

    # 4 · budget mandate
    b = c.get("/api/budget").raise_for_status().json()
    scene(4, "The plan is the mandate", "The organisation's November plan is the agent's mandate per line: plan, realized, paying now, committed, available, minus. Past the auto share a person decides; past the mandate the agent raises a budget request with the exact shortfall.",
          "Budget · mandate: the Kit 01 book against the period plan; read the line table, the alerts and the budget requests.",
          {"period": b.get("period"), "decision": b.get("decision"), "lines": [(r["line"], r["status"], r["utilisation_bp"]) for r in b.get("lines", [])], "requests": len(b.get("requests", []))})

    # 5 · limits and four-eyes, through the form
    v = c.post("/api/groundtruth/V03/check").raise_for_status().json()
    q = c.post("/api/groundtruth/Q03/check").raise_for_status().json()
    scene(5, "How much, and how many signatures", "One cent above the single-payment limit stops the plan for a person. At the two-person threshold the plan itself says: two approvals from different people, never the requester.",
          "Submit a case: suite limits V03 (one cent above), then suite approvals Q03; the Decision tile shows the tier and the number of people.",
          {"V03": {"match": v["match"], "requires": v["view"].get("requires")}, "Q03": {"match": q["match"], "tier": q["view"].get("approval_tier"), "people": q["view"].get("approvals_required")}})

    # 6 · a real execution, reconciled to the cent, on its own ledger
    state = {"case_id": f"demo-{tag}", "book_id": f"demo-{tag}", "base_currency": "USD", "balances": {"USD": 2_000_000, "EUR": 0, "GBP": 0},
             "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
             "obligations": [{"obligation_id": f"DEMO-{tag}", "amount_minor": 100_000, "currency": "USD", "priority": 8, "due_in_hours": 24,
                              "beneficiary_id": "ben-saas-vendor", "payee_name": "Cloudloom Software LLC", "payout_method": "LOCAL",
                              "evidence_ref": f"ev-{tag}", "invoice_fingerprint": f"cloudloom:DEMO-{tag}"}],
             "receivables": [], "evidence": [{"ref": f"ev-{tag}", "subject_id": f"DEMO-{tag}", "source": "ap:invoice", "status": "verified", "summary": "invoice"}],
             "scenarios": [], "policy": {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 1_000_000, "swift_fee_currency": "EUR", "swift_fee_minor": 1285,
                                         "planning_horizon_hours": 72, "payout_fees_minor": {"LOCAL:USD": 300}}, "actor_role": "treasury_agent"}
    live = c.post("/v1/plans", json=state).raise_for_status().json()
    ex = c.post("/v1/executions", json={"state": state, "plan_hash": live["plan_hash"]}).raise_for_status().json()
    ledger = c.get(f"/v1/ledger/demo-{tag}").json()
    recon = ex.get("reconciliation") or {}
    scene(6, "Money moves, and the books agree to the cent", "A fresh book, USD 1,000 to a verified payee, inside every control: the agent executes alone on the Airwallex sandbox. Claims before money moves, a transfer polled to PAID, then the wallet's movement against every book's cash lines: zero unexplained.",
          "Executions: the new run appears COMPLETED; open it to see the steps and the reconciliation; Books · ledger: the new book with cash, expense, fees.",
          {"plan": live["decision"], "execution": ex["status"], "message": ex["message"][:160], "discrepancies": recon.get("discrepancies_minor"), "ledger_entries": len(ledger.get("journal") or []),
           "book_balances": {k: {a: v for a, v in vals.items() if v} for k, vals in (ledger.get("balances") or {}).items()}})

    # 7 · incident: the decision table, no money
    inc = c.post("/v1/incidents/decide", json={"status": "CANCELLED", "failure_type": "BENEFICIARY_BANK_RETURNED", "hours_in_flight": 6}).raise_for_status().json()
    esc = c.post("/v1/incidents/decide", json={"status": "CANCELLED", "failure_type": "INVALID_BENEFICIARY_DETAILS", "hours_in_flight": 6}).raise_for_status().json()
    scene(7, "A supplier says the money never arrived", "Kit 03: the rail's typed failure decides. Returned by the beneficiary bank: replace once, with a new request id and a second fee budgeted. Wrong account details: escalate, because paying again pays the wrong account again.",
          "Submit a case: kind incident; paste the two facts in turn; read the decision and the reason.",
          {"returned": inc, "wrong_details": esc})

    # 8 · aging and the final notice
    g02 = c.post("/api/groundtruth/G02/check").raise_for_status().json()
    d03 = c.post("/api/groundtruth/D03/check").raise_for_status().json()
    scene(8, "What lateness costs", "An invoice 45 days overdue that the book cannot fund: bucketed, interest accrued at 8 % a year, and the plan stops for a person instead of deferring quietly. A supplier's final notice, read from the inbox with its quote, raises the book's dunning level.",
          "Submit a case: suite aging, pick G02 (overdue and unfundable) then D03 (a final notice); the reasons and the quote appear; ground truth PASS.",
          {"G02": {"match": g02["match"], "requires": g02["view"].get("requires"), "days": g02["view"].get("aging_days"), "accrued": g02["view"].get("aging_accrued")},
           "D03": {"match": d03["match"], "status": d03["view"].get("status"), "dunning_level": d03["view"].get("dunning_level")}})

    # 9 · tenants
    t = c.get("/v1/tenants").raise_for_status().json()
    scene(9, "Whose book, whose suppliers, whose signature", "One wallet, several tenants. Each book may pay only its payees, is signed only by its people, runs under its own policy, and keeps its own ledger. An unregistered book cannot spend at all.",
          "Tenants · books: the registry with the books seen, their executions and cash; the demo book just created sits under the sandbox lab.",
          {"strict": t["strict"], "tenants": sorted({r["tenant"] for r in t["rows"]}), "books_seen": sum(len(r["books_seen"]) for r in t["rows"])})

    log = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "base": base, "seconds": round(time.perf_counter() - t0, 1), "scenes": SCENES}
    (ROOT / "docs" / "demo_run_log.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
    print(f"\n{len(SCENES)} scenes in {log['seconds']} s -> docs/demo_run_log.json")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--approve", default="finance manager (owner)")
    ap.add_argument("--cfo", default="CFO (owner, second signature)")
    sys.stdout.reconfigure(encoding="utf-8")
    a = ap.parse_args()
    sys.exit(main(a.base, a.approve, a.cfo))
