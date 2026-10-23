# ruff: noqa: E501
"""The whole set on the REAL Airwallex sandbox, in one command, with fresh books, stopping at the first failure.

  python scripts/real_suite.py --approve "Dana" [--port 8010] [--opa PATH]

Steps (each one's own script; this runner starts the service it needs, captures every step's output, and writes
docs/REAL_SUITE.md and docs/real_suite_log.json):
  1  Kit 01 cycle on a fresh book: plan, approve, convert, pay, deposit lands, re-plan, reconcile        venthora-sandbox --receipt --fresh
  2  Kit 03 incident on a fresh book: left in flight, failed with a type, replaced under the lock          venthora-sandbox --incident --fresh
  3  Intake of the five external emails into a fresh book, remittance lands, executed                    venthora-intake --fresh
  4  Ground truth suites 1 + 2 through the live service (live balances) + reproducible execution anomalies  groundtruth_real.py
  5  Budget suite through the live service, then the mandate cycle on the rail (stop, request, grant)     groundtruth_bench.py --suite budget; budget_real.py
  6  Proofs that are not sandbox-bound but belong to the set: Rego mirror on a real OPA, oracle audit        check_policy_mirror.py; oracle_audit.py
What the real rail cannot reproduce (lost responses, 429 storms, a reused request_id, non-settling conversions and
deposits, exact wallet sizes) stays on the fake sandbox: scripts/scenario_matrix.py, listed at the end for completeness.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
RUNS_RE = re.compile(r"run report: (\S+)")


def run_step(name: str, cmd: list[str], env: dict, timeout: int = 1800) -> dict:
    print(f"\n=== {name}\n    {' '.join(cmd)}")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        code, out = proc.returncode, (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else "")
    except subprocess.TimeoutExpired as exc:
        code, out = 124, (exc.stdout or "") + "\n[timeout]"
    secs = time.perf_counter() - t0
    tail = "\n".join(out.strip().splitlines()[-25:])
    print(tail)
    print(f"    -> exit {code} in {secs:.0f} s")
    return {"name": name, "command": " ".join(cmd), "exit": code, "seconds": round(secs, 1), "ok": code == 0,
            "reports": RUNS_RE.findall(out), "tail": tail, "output": out}


def wait_for(url: str, seconds: int = 60) -> bool:
    for _ in range(seconds):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", required=True, help="the finance manager approving REQUIRES_APPROVAL plans")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--opa", default=os.environ.get("OPA_BIN"), help="official OPA binary (else the Docker image is used)")
    ap.add_argument("--skip-opa", action="store_true")
    ap.add_argument("--from", dest="start", type=int, default=1, help="resume from step N, keeping earlier steps from the last log")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    base = f"http://127.0.0.1:{args.port}"
    started = dt.datetime.now(dt.UTC)
    log: dict = {"started_at": started.isoformat(), "approver": args.approve, "base": base, "steps": []}
    server = subprocess.Popen([PY, "-m", "uvicorn", "venthora.api:app", "--port", str(args.port), "--log-level", "warning"],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for(f"{base}/health"):
            print("the service did not come up")
            return 2
        status = json.loads(urllib.request.urlopen(f"{base}/api/status", timeout=5).read())
        if not status["authorities"]["airwallex"]:
            print("the service is not connected to the Airwallex sandbox (.env)")
            return 2
        log["service"] = status
        steps = [
            ("1 Kit 01 cycle, fresh book", [PY, "-m", "venthora.cli", "--receipt", "--yes", "--approve", args.approve, "--no-challenge", "--fresh"]),
            ("2 Kit 03 incident, fresh book", [PY, "-m", "venthora.cli", "--scenario", "kit03_incident", "--inbox", "", "--yes", "--approve", args.approve,
                                              "--no-challenge", "--fresh", "--incident", "SUPPLIER-EU-NOV:BENEFICIARY_BANK_RETURNED"]),
            ("3 Intake of external emails, fresh book", [PY, "-m", "venthora.intake.cli", "--as-of", "2026-10-24", "--execute", "--receipt",
                                                        "--approve", args.approve, "--fresh"]),
            ("4 Ground truth suites 1+2 on the real sandbox", [PY, "scripts/groundtruth_real.py", "--base", base, "--approve", args.approve]),
            ("5a Budget suite through the live service", [PY, "scripts/groundtruth_bench.py", "--suite", "budget", "--base", base]),
            ("5b Budget mandate cycle on the rail", [PY, "scripts/budget_real.py", "--approve", args.approve]),
        ]
        if not args.skip_opa:
            opa_env = env | ({"OPA_BIN": args.opa} if args.opa else {})
            steps.append(("6a Rego mirror on a real OPA", [PY, "scripts/check_policy_mirror.py"], opa_env))
        steps.append(("6b Oracle audit", [PY, "scripts/oracle_audit.py"]))
        if args.start > 1:
            previous = json.loads((ROOT / "docs" / "real_suite_log.json").read_text(encoding="utf-8"))["steps"]
            log["steps"] = [s for s in previous[: args.start - 1] if s["ok"]]
            log["resumed_from"] = args.start
        for n, step in enumerate(steps, 1):
            if n < args.start:
                continue
            name, cmd, step_env = (step + (env,))[:3]
            rec = run_step(name, cmd, step_env)
            log["steps"].append(rec)
            if not rec["ok"]:
                print(f"\nSTOPPED at '{name}' (exit {rec['exit']})")
                break
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    log["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
    log["ok"] = all(s["ok"] for s in log["steps"]) and len(log["steps"]) == len(steps)
    (ROOT / "docs" / "real_suite_log.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
    write_report(log, len(steps))
    print(f"\n{'ALL STEPS PASSED' if log['ok'] else 'FAILED'}: {sum(s['ok'] for s in log['steps'])} / {len(steps)} steps; docs/REAL_SUITE.md")
    return 0 if log["ok"] else 1


def write_report(log: dict, planned: int) -> None:
    lines = ["# The whole set on the real Airwallex sandbox", "",
             f"`python scripts/real_suite.py --approve \"{log['approver']}\"` — started {log['started_at'][:16].replace('T', ' ')} UTC, "
             f"finished {log['finished_at'][:16].replace('T', ' ')} UTC, approved by **{log['approver']}** as finance manager. "
             f"**{sum(s['ok'] for s in log['steps'])} / {planned} steps passed.** Every step runs on a fresh book (new obligation ids and "
             "invoice fingerprints, same amounts, evidence and policy) against the real sandbox; the runner stops at the first failure. "
             "Full output per step in `docs/real_suite_log.json`.", "",
             "| Step | Command | Result | s | Reports |", "|---|---|---|---|---|"]
    for s in log["steps"]:
        command = s["command"].split("python.exe ")[-1].split("python ")[-1]
        result = "✅ passed" if s["ok"] else "❌ exit " + str(s["exit"])
        reports = ", ".join("`" + Path(r).name + "`" for r in s["reports"]) or "-"
        lines.append(f"| {s['name']} | `{command}` | {result} | {s['seconds']:.0f} | {reports} |")
    lines += ["", "## What each step proved", ""]
    for s in log["steps"]:
        lines += [f"### {s['name']}", "", "```text", s["tail"], "```", ""]
    lines += ["## Deliberately not on the real rail", "",
              "The scenario matrix (`python scripts/scenario_matrix.py`, 136 variants) and nine execution anomalies stay on the stateful fake sandbox, "
              "because a real rail cannot be asked for a lost HTTP response, a 429 storm, a reused `request_id`, a wallet that drops between the balance "
              "read and the transfer, a conversion or deposit that never settles, or a wallet of exactly EUR 1,512.85. Their mechanisms — recovery by "
              "`request_id`, the duplicate lock, reconciliation to the cent, the Kit 03 decision table — are the ones steps 2 and 4 exercised on the real rail.", ""]
    (ROOT / "docs" / "REAL_SUITE.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
