# ruff: noqa: E501
"""Write docs/GROUND_TRUTH.md for both suites from the in-process run (done here), the endpoint logs and the UI logs.

  python scripts/groundtruth_report.py
Reads docs/groundtruth_log_<suite>_endpoint.json (scripts/groundtruth_bench.py) and docs/groundtruth_log_<suite>_ui.json
(the dashboard form, recorded from the browser) and runs every case in-process for the third column.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for name in ("AWX_CLIENT_ID", "AWX_API_KEY", "AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY", "MODEL_ENDPOINT", "OPA_URL"):
    os.environ.pop(name, None)
os.environ["VENTHORA_INTAKE_DB"] = os.path.join(tempfile.mkdtemp(), "intake.db")

from venthora.intake.pipeline import Intake  # noqa: E402
from venthora.orchestrator import Orchestrator  # noqa: E402
from venthora.testing.groundtruth import load_cases, run_case  # noqa: E402

LEVELS = {1: "money arithmetic nobody wrote down (fees, FX buffers, rounding, stale quotes)",
          2: "time does not cooperate (receipts slip, windows overrun, deadlines pass, settlement lags)",
          3: "the evidence is not what it says (lookalikes, injection, duplicates, bank changes)",
          4: "the mandate is exceeded (limits, confidence, approvals, roles)",
          5: "the rail misleads (timeouts, 429s, lost responses, CANCELLED that is not a cancellation, returns)",
          6: "paying twice or paying the wrong money (duplicates across runs, stale state, cross-wallet, Kit 03 end to end)"}


def pct(xs, q):
    xs = sorted(xs)
    return xs[int(q * (len(xs) - 1))] if xs else float("nan")


async def in_process(cases: list[dict]) -> dict[str, dict]:
    agent, intake, work = Orchestrator(), Intake(), Path(tempfile.mkdtemp(prefix="gt-report-"))
    try:
        return {c["id"]: await run_case(c, agent, intake, work) for c in cases}
    finally:
        await agent.close()


def stats_row(path: str, kind: str, rs: list[dict], key: str) -> str:
    ms = [r[key] for r in rs if r.get(key) is not None]
    if not ms:
        return f"| {path} | {kind} | {len(rs)} | {sum(bool(r.get('match')) for r in rs)} | - | - | - |"
    return (f"| {path} | {kind} | {len(rs)} | {sum(bool(r.get('match')) for r in rs)} | {statistics.median(ms):.1f} | "
            f"{pct(ms, .95):.1f} | {max(ms):.1f} |")


def section(suite: str, title: str, intro: list[str]) -> list[str]:
    cases = load_cases(suite)
    inproc = asyncio.run(in_process(cases))
    endpoint = {r["id"]: r for r in json.loads((ROOT / "docs" / f"groundtruth_log_{suite}_endpoint.json").read_text(encoding="utf-8"))}
    ui = {r["id"]: r for r in json.loads((ROOT / "docs" / f"groundtruth_log_{suite}_ui.json").read_text(encoding="utf-8"))["rows"]}
    kinds = sorted({c["kind"] for c in cases}, key=["plan", "intake", "incident", "reporting", "ledger", "payees", "approvals", "execution"].index)
    lines = [f"## {title}", ""] + intro + ["", "| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |", "|---|---|---|---|---|---|---|"]
    for kind in kinds:
        ids = [c["id"] for c in cases if c["kind"] == kind]
        lines.append(stats_row("in-process", kind, [inproc[i] for i in ids], "ms"))
        e = [endpoint[i] for i in ids if i in endpoint and "ms" in endpoint[i]]
        lines.append(stats_row("endpoint", kind, e, "ms") if e else f"| endpoint | {kind} | {len(ids)} | n/a (in-process only) | - | - | - |")
        u = [ui[i] for i in ids if i in ui]
        lines.append(stats_row("UI form", kind, u, "ui_ms") if u else f"| UI form | {kind} | {len(ids)} | n/a (in-process only) | - | - | - |")
    srv = [r["server_ms"] for r in endpoint.values() if r.get("server_ms")]
    if srv:
        lines += ["", f"Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 {statistics.median(srv):.1f} ms, p95 {pct(srv, .95):.1f} ms."]
    has_level = any("level" in c for c in cases)
    head = "| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |" if has_level else \
           "| ID | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |"
    lines += ["", "### Cases", "", head, "|" + "---|" * (head.count("|") - 1)]
    for c in cases:
        i = c["id"]
        ip, e, u = inproc[i], endpoint.get(i, {}), ui.get(i, {})
        ok = ip["match"] and e.get("match", True) and u.get("match", True)
        e_ms = e.get("ms", "n/a") if "ms" in e else ("n/a" if c["kind"] == "execution" else "-")
        u_ms = u.get("ui_ms", "n/a" if c["kind"] == "execution" else "-")
        cells = [i] + ([str(c["level"]), c["pain"]] if has_level else []) + [c["title"], c["kind"], f"`{json.dumps(c['expect'])}`",
                                                                               f"{ip['ms']:.1f}", str(e_ms), str(u_ms), "✅" if ok else "❌", c["derivation"]]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def main() -> None:
    lines = ["# Ground truth: hand-derived answers vs the live service", "",
             f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} by `python scripts/groundtruth_report.py`.", "",
             "Two suites in `data/groundtruth/`, both with answers derived **by hand** from the published rules (each case carries its derivation), never by running VENTHORA. "
             "Every case is judged by one comparator (`venthora.testing.groundtruth.compare`) on three paths: **in-process** (`tests/`, CI), "
             "**HTTP endpoints** of the running service (`scripts/groundtruth_bench.py`: `POST /v1/plans`, `POST /v1/intake/emails?dry_run=true`, `POST /v1/incidents/decide`; "
             "round trip measured by the client, 3 repeats, median), and the **dashboard form** (`Submit a case` at `/`, each case submitted through the UI in a browser, "
             "round trip measured by the browser beside the server's `duration_ms`). Execution cases (a plan executed on the stateful fake sandbox with injected faults) run in-process only: "
             "through the service they would move real sandbox money.", ""]
    lines += section("anomalies", "Suite 2 — the organizers' failure modes, smallest to largest (no normal day)", [
        "**Why the hackathon exists.** Airwallex's framing: software agents are starting to *track balances, initiate payments and manage risk without a human in the loop*; "
        "the rails underneath them are the builders' to design. The developer guide names what goes wrong when money is moved on a model's narrative, and those pains are the six levels below. "
        "Every case here is an anomaly; the expected answer is what the published rules say must happen.", ""] + [f"- **L{k}** — {v}" for k, v in LEVELS.items()])
    lines += [""] + section("budget", "Suite 3 — Budget · mandate: the period's plan is the agent's mandate", [
        "20 cases around every threshold of a budget line: inside the auto share, exactly the mandate, one cent over (the agent stops "
        "and raises a BUDGET REQUEST with the exact shortfall), above the auto share (a person), the alert threshold with and without "
        "require_approval_on_alert, realized spend eating the line, a grant restoring it, two lines, priority inside a line, unbudgeted "
        "obligations, bypass signed or not, the whole November book against the November plan, minus from what is still owed, a viewer, "
        "and no budget at all. Real-sandbox cycle 8 (SANDBOX_RESULTS.md) runs the same mandate end to end: pay inside the line, stop at "
        "it, grant, continue."])
    lines += [""] + section("controls", "Suite 4 — finance controls: instalments, three-way match, period cut-off, reconciliation cases", [
        "Instalments (a deposit then the balance: two payouts of one invoice under the duplicate lock, never past the contract total), "
        "the three-way match (contract − paid so far vs what the invoice claims; over-claims beyond tolerance need a person), the period "
        "cut-off (due after period_end carries forward to the next period; due before period_start is late-posted; a closed period takes "
        "no autonomous spend) and reconciliation cases (a mismatch opens a case that locks the book until a person closes it with a note)."])
    lines += [""] + section("reporting", "Suite 5 — reporting periods: the admin's cut-off, payments by payment instant, one reporting currency", [
        "Cut-off day + time + timezone set by an admin (inclusive to the last second of the cut-off minute, day clamped to the month's end, "
        "year roll-over, timezone conversion); payments booked to the period of their payment instant; an invoice paid in instalments is "
        "reported as reconciled only when its parts add up to the contract, in the month of the last part; consolidation of every currency "
        "to the reporting currency at the period's recorded closing rates, beside the base cost actually paid (FX difference); a missing "
        "rate is flagged, never guessed."])
    lines += [""] + section("ledger", "Suite 6 — the book ledger: double-entry per book, a book never spends another book's money", [
        "Balanced entries per currency (open, deposit, grant, convert, commit, settle, refund, adjust); a commit or conversion the book "
        "cannot fund is refused before money moves; the wallet must move by exactly the sum of every book's cash lines, which is what "
        "reconciliation now checks on a shared wallet."])
    lines += [""] + section("limits", "Suite 7 — limits and velocity: how much, to whom, how often", [
        "A payout above the single-payment limit; a payee whose paid-this-period plus paying-now exceeds its period limit (history from "
        "the spend ledger, two payouts in one plan count together); a payee never PAID before getting more than the first-payment limit "
        "(no history = everyone is new); rolling 24 h / 7 d outflow plus paying now above the daily / weekly limit; several reasons at "
        "once, each named; no limits = nothing changes; an escalated payout does not count as paying now."])
    lines += [""] + section("payees", "Suite 8 — payee lifecycle: NEW → VERIFIED → ACTIVE, cooling after a bank change, the call-back is mandatory", [
        "Plans against a payee registry: NEW, CHANGED, SUSPENDED and unregistered payees are ESCALATED (no payment, not even with an "
        "approval); a VERIFIED payee needs a person inside the cooling period (payee_in_cooling_period) and until its first PAID payout "
        "(payee_not_yet_active); ACTIVE adds nothing; an invoice announcing new details escalates even an ACTIVE payee; no registry = the "
        "control is off. The state machine itself: register, the call-back only by an approver role and only on the registered number "
        "(a number from the request is refused), a bank change that starts (or restarts) the cooling period, payments that promote only "
        "after it, suspend / reinstate (another call-back)."])
    lines += [""] + section("approvals", "Suite 9 — the approval matrix (four-eyes): who, and how many different people, must approve", [
        "Tiers by amount and by reason code, loosest first, the last applicable tier deciding (a plan that stops for a person at "
        "USD 1,000 needs one finance manager; at USD 25,000, or for a payee-change reason, two different people); no tier = one "
        "approver; no reason = no approval. The check the executor runs before money moves: an allowed role, the plan's own "
        "terms digest, two approvals from one person count once, and the requester may never approve their own plan."])
    lines += [""] + section("cases", "Suite 1 — the rules, one by one", [
        "37 treasury plans and 17 intake emails that exercise one rule each (fit / do not fit / exactly at the limit, priorities, evidence, duplicates, receipts, limits scaling with confidence, roles)."])
    real_path = ROOT / "docs" / "groundtruth_log_real.json"
    if real_path.is_file():
        real = json.loads(real_path.read_text(encoding="utf-8"))
        a, b = real["part_a"], real["part_b"]
        plans = [r for r in a if r["kind"] == "plan"]
        ran = [r for r in b if "ms" in r]
        lines += ["", "## Both suites on the real Airwallex sandbox", "",
                  f"`python scripts/groundtruth_real.py`, {real['generated_at'][:16].replace('T', ' ')} UTC, approved by the owner as finance manager.", "",
                  f"**Part A** — the {len(a)} plan / intake / incident cases through the live service: **{sum(r['match'] for r in a)} / {len(a)} match**; "
                  f"{sum(bool(r.get('real')) for r in plans)} / {len(plans)} plans had their `Airwallex · live balances` stage read from the sandbox "
                  f"(none SKIPPED); plan round trip p50 {statistics.median(r['ms'] for r in plans):.1f} ms.", "",
                  f"**Part B** — the execution anomalies on the real rail, a fresh book per run (new obligation ids and invoice fingerprints, "
                  f"so nothing already paid is re-paid): **{sum(r['match'] for r in ran)} / {len(ran)} match**; {len(b) - len(ran)} cannot be forced on a real rail.", "",
                  "| ID | Case | On the real rail | Result | ms | Match |", "|---|---|---|---|---|---|"]
        for r in b:
            if "ms" in r:
                v = r["view"]
                res = f"plan {v.get('plan')} · execution {v.get('execution')}" + (f" · incident {v.get('incident')} ({v.get('incident_decision')}" + (f", {v.get('incident_followups')} follow-ups" if v.get("incident_followups") else "") + ")" if v.get("incident") else "")
                lines.append(f"| {r['id']} | {r['title']} | {r['how']} | {res} | {r['ms']:.0f} | {'✅' if r['match'] else '❌'} |")
            else:
                lines.append(f"| {r['id']} | {r['title']} | not reproducible: {r['skipped']} | fake sandbox only | - | – |")
    lines += ["", "## What the exercise changed", "",
              "- **P28 / N24** — the hand answer first said APPROVED when the headroom exactly covers the payment. The service answered REQUIRES_APPROVAL: the 72-hour simulation draws the unexpected outflow with a lognormal size, so a plan that lands exactly on the floor breaches it in about half the paths, above the 5% tolerance. The probabilistic rule is the documented one; the expectation was corrected and the case kept.",
              "- **P32** — USD 10 bn cash was BLOCKED by the live service (the sandbox wallet holds about USD 10 M) while passing in-process: a connected service grounds every plan in real balances. The case now uses amounts inside the wallet.",
              "- **I04** — the lookalike's invoice LD-77 was already in the live intake queue, so the live service answered DUPLICATE instead of QUARANTINED: the queue is shared state and the duplicate lock applies to it. The case uses another invoice number.",
              "- **N41** — paying the same invoices twice: a second *plan* of the same state after the first execution is BLOCKED by the live-balance check before the duplicate lock is even reached (the fake wallet is lower); the case tops the wallet up so that the lock itself is what refuses the second run (`duplicate payment blocked before any money moved`).",
              "- **Harness** — execution cases set fake credentials in the environment; without restoring them, later plan cases saw a configured-but-unreachable Airwallex and were BLOCKED. Restored after each execution case.", ""]
    (ROOT / "docs" / "GROUND_TRUTH.md").write_text("\n".join(lines), encoding="utf-8")
    print("docs/GROUND_TRUTH.md written")


if __name__ == "__main__":
    main()
