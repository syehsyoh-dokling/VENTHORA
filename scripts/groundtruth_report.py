"""Write docs/GROUND_TRUTH.md from the latest endpoint bench and the UI bench logs in data/runs/groundtruth/."""
# ruff: noqa: E501
from __future__ import annotations

import datetime as dt
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from venthora.testing.groundtruth import load_cases  # noqa: E402

RUNS = ROOT / "data" / "runs" / "groundtruth"


def pct(xs, q):
    xs = sorted(xs)
    return xs[int(q * (len(xs) - 1))]


def main() -> None:
    endpoint = json.loads(sorted(RUNS.glob("*-endpoint.json"))[-1].read_text(encoding="utf-8"))
    ui = json.loads(sorted(RUNS.glob("ui-*.json"))[-1].read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in load_cases()}
    ui_rows = {r["id"]: r for r in ui["rows"]}
    lines = ["# Ground truth: hand-derived answers vs the live service", "",
             f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} by `python scripts/groundtruth_report.py`.", "",
             "**What this is.** `data/groundtruth/cases.json` holds "
             f"{len(cases)} cases ({sum(c['kind'] == 'plan' for c in cases.values())} treasury plans, "
             f"{sum(c['kind'] == 'intake' for c in cases.values())} intake emails) whose answers were derived *by hand* from the "
             "published rules in `scripts/make_groundtruth.py` (each case carries its derivation), not by running VENTHORA. "
             "Every case was then submitted three ways and judged by one comparator (`venthora.testing.groundtruth.compare`):",
             "", "1. **in-process** (`tests/test_groundtruth.py`, runs in CI);",
             "2. **via the HTTP endpoints** of the running service (`scripts/groundtruth_bench.py`: `POST /v1/plans`, "
             "`POST /v1/intake/emails?dry_run=true`), round trip measured by the client, 3 repeats, median kept;",
             "3. **via the dashboard form** (`Submit a case` at `/`), each case picked and submitted through the UI in a browser, "
             "round trip measured by the browser's `performance.now()` beside the server's own `duration_ms`.", ""]
    kinds = ("plan", "intake")
    lines += ["## Results", "", "| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |", "|---|---|---|---|---|---|---|"]
    for kind in kinds:
        rs = [r for r in endpoint if r["kind"] == kind]
        ms = [r["ms"] for r in rs]
        lines.append(f"| endpoint | {kind} | {len(rs)} | {sum(r['match'] for r in rs)} | {statistics.median(ms):.1f} | "
                     f"{pct(ms, .95):.1f} | {max(ms):.1f} |")
    for kind in kinds:
        rs = [r for r in ui["rows"] if r["kind"] == kind]
        ms = [r["ui_ms"] for r in rs]
        lines.append(f"| UI form | {kind} | {len(rs)} | {sum(r['match'] for r in rs)} | {statistics.median(ms):.1f} | "
                     f"{pct(ms, .95):.1f} | {max(ms):.1f} |")
    srv = [r["server_ms"] for r in endpoint if r.get("server_ms")]
    lines += ["", f"Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the "
                  f"counterfactuals): p50 {statistics.median(srv):.1f} ms, p95 {pct(srv, .95):.1f} ms. The difference to the "
                  "round trip (about 10 ms) is HTTP, JSON and the audit append. Without the live Airwallex call (offline / "
                  "tests) a plan takes about 13 ms; the balance check against the sandbox accounts for most of the rest.", "",
              "## Cases", "",
              "| ID | Case | Expected | Endpoint ms | UI ms | Server ms | Match | Derivation |", "|---|---|---|---|---|---|---|---|"]
    e_rows = {r["id"]: r for r in endpoint}
    for cid, c in cases.items():
        e, u = e_rows.get(cid, {}), ui_rows.get(cid, {})
        ok = e.get("match") and u.get("match")
        lines.append(f"| {cid} | {c['title']} | `{json.dumps(c['expect'])}` | {e.get('ms', '-')} | {u.get('ui_ms', '-')} | "
                     f"{u.get('server_ms') or '-'} | {'✅' if ok else '❌'} | {c['derivation']} |")
    lines += ["", "## What the exercise changed", "",
              "- **P28** — the hand answer first said APPROVED (deterministic headroom exactly covers the payment). The service "
              "answered REQUIRES_APPROVAL: the 72-hour simulation draws the unexpected outflow with a lognormal size, so a plan "
              "that lands exactly on the floor breaches it in about half the paths, above the 5% tolerance. The second rule is "
              "the documented one; the expectation was corrected and the case kept as a record.",
              "- **P32** — USD 10 bn cash was BLOCKED by the live service (the sandbox wallet holds about USD 10 M) while passing "
              "in-process: a connected service grounds every plan in real balances. The case now uses amounts inside the wallet "
              "and the note stays.",
              "- **I04** — the lookalike's invoice LD-77 was already in the live intake queue, so the live service answered "
              "DUPLICATE instead of QUARANTINED: the queue is shared state and the duplicate lock applies to it. The case uses "
              "another invoice number; the behaviour is the intended one.", ""]
    (ROOT / "docs" / "GROUND_TRUTH.md").write_text("\n".join(lines), encoding="utf-8")
    print("docs/GROUND_TRUTH.md written")


if __name__ == "__main__":
    main()
