"""Run the scenario matrix (~100 real-world variants) and write the report.

Run: python scripts/scenario_matrix.py [--only FAMILY] [--out docs/SCENARIO_MATRIX.md]
Writes docs/SCENARIO_MATRIX.md (table) and data/runs/matrix/<timestamp>.json (full log).
Exit code 1 if any variant violates an invariant.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import platform
import sys
import tempfile
from pathlib import Path

for name in ("AWX_CLIENT_ID", "AWX_API_KEY", "AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY", "OPA_URL", "TB_ADDRESS",
             "MODEL_ENDPOINT", "MODEL_NAME"):
    os.environ.pop(name, None)
os.environ["AIRWALLEX_CLIENT_ID"] = "matrix"
os.environ["AIRWALLEX_API_KEY"] = "matrix"

from venthora.testing.matrix import build_variants, run_matrix, write_report  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="family prefix to run (e.g. incident, rail, load)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "SCENARIO_MATRIX.md"))
    args = ap.parse_args()
    variants = build_variants()
    if args.only:
        variants = [v for v in variants if v.family.startswith(args.only)]
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"{len(variants)} variants")

    def progress(rec):
        mark = "ok " if rec["ok"] else "BAD"
        plan = (rec.get("plan") or {}).get("decision", "-")
        ex = (rec.get("execution") or {}).get("status", "-")
        inc = (rec.get("incident") or {}).get("status", "-")
        print(f"  {mark} {rec['id']:<4} {rec['family']:<12} {rec['severity']:<8} plan={plan:<17} exec={ex:<24} "
              f"incident={inc:<28} {rec['total_ms']:>7.0f} ms  {rec['title'][:50]}")

    with tempfile.TemporaryDirectory(prefix="venthora-matrix-", ignore_cleanup_errors=True) as tmp:
        records = asyncio.run(run_matrix(variants, Path(tmp), progress))
    runs = ROOT / "data" / "runs" / "matrix"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    extra = {"machine": f"{platform.machine()} / {platform.python_implementation()} {platform.python_version()}",
             "reader": "deterministic lexicon reader (no model), plus stub models for the adversarial inbox variants",
             "sandbox": "stateful fake Airwallex sandbox with real-sandbox fees (SWIFT EUR 13.37, LOCAL USD 3.00, GBP 2.24)"}
    findings = [
        "**B10 (only foreign cash) found a real inconsistency, now fixed.** With USD 0, EUR 50k and GBP 10k the plan pays "
        "the EUR and GBP invoices from their own wallets at zero base cost; ProofCore passes it (the floor binds base "
        "*spend*) and the policy escalates the 100% breach risk to a person. The executor, however, refused at the "
        "pre-flight and again at reconciliation because it compared the absolute USD level with the floor. Both checks now "
        "apply the gate's rule: a run that does not touch the base wallet cannot breach the floor; a base balance already "
        "below the floor is reported as such, not blamed on the run (`tests/test_execution.py::"
        "test_plan_funded_from_foreign_wallets_runs_when_base_is_already_below_floor`).",
        "**A00 (cold start) is expected to fail on the rail and does so cleanly.** Before the first cycle the plan only knows "
        "the guide's fees (SWIFT EUR 12.85, LOCAL free); the real sandbox charges LOCAL GBP 2.24, so a wallet holding "
        "exactly the budget is GBP 2.24 short and Airwallex answers `balance_insufficient`. The executor closes the run "
        "(FAILED, reconciled, no money lost), the fee is learned, and every later variant plans with the real fees. This is "
        "the cycle-1 behaviour recorded in docs/SANDBOX_RESULTS.md.",
        "**Stale planning rates (H00, H01) stop before anything is booked**: the quoted sell amount above the 1% slippage "
        "limit halts the run with nothing converted; a rail that is cheaper than planned (H01b) completes with a favourable "
        "variance.",
        "**Every Airwallex failure_type (I01–I32) lands on the decision table**: 6 types with returned funds are replaced "
        "under the duplicate lock and resolved; 26 types that mean wrong payee details or intent/control are escalated; a "
        "transfer in flight waits up to the 48 h window (I33–I35) and is escalated after it (I36, I37); a replacement the "
        "wallet cannot fund (I99) is escalated instead of attempted.",
        "**Adversarial inbox (G06–G11)**: prompt injection, garbage and hallucinated model output never change a decision: "
        "claims that cannot be located verbatim in the cited document are rejected and the lexicon reader re-reads.",
        "**Load**: 100 sequential plans p50 ≈ 13–14 ms, p99 ≈ 20 ms, no drift (last fifth / first fifth ≈ 1.0); 20 concurrent "
        "plans give one identical hash; 10 executions on 10 books complete with at most one PAID per invoice; a chain of 10 "
        "re-plans stays incremental.",
    ]
    write_report(records, Path(args.out), runs / f"{stamp}.json", extra, findings)
    # data/runs is git-ignored; keep the latest full log beside the report so the submission carries it.
    (ROOT / "docs" / "scenario_matrix_log.json").write_bytes((runs / f"{stamp}.json").read_bytes())
    bad = [r for r in records if not r["ok"]]
    print(f"\n{len(records) - len(bad)} / {len(records)} variants satisfy every invariant; report: {args.out}")
    for r in bad:
        print(f"  {r['id']} {r['title']}: {[k for k, ok in r['invariants'].items() if not ok]} {r['errors'][:1]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
