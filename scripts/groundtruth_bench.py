"""Bench the ground-truth cases through the HTTP endpoints of a running server and write the log.

  python scripts/groundtruth_bench.py [--suite cases|anomalies] [--base http://127.0.0.1:8000] [--repeat 3]

Every case is sent the way a client would: plans to POST /v1/plans, emails to POST
/v1/intake/emails?dry_run=true, incident facts to POST /v1/incidents/decide. Execution cases (fake
sandbox with injected faults) are in-process only and are skipped here with a note. Latency is the
wall-clock round trip measured by this client; the server's own duration_ms is recorded beside it for
plans. Writes data/runs/groundtruth/<stamp>-<suite>-endpoint.json and docs/groundtruth_log_<suite>_endpoint.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from venthora.testing.groundtruth import compare, load_cases, view_of  # noqa: E402


def send(client: httpx.Client, case: dict) -> httpx.Response:
    if case["kind"] == "plan":
        return client.post("/v1/plans", json=case["input"])
    if case["kind"] == "incident":
        return client.post("/v1/incidents/decide", json=case["input"])
    if case["kind"] == "reporting":
        return client.post("/v1/reporting/eval", json=case["input"])
    if case["kind"] == "ledger":
        return client.post("/v1/ledger/eval", json=case["input"])
    if case["kind"] == "payees":
        return client.post("/v1/payees/eval", json=case["input"])
    if case["kind"] == "approvals":
        return client.post("/v1/approvals/eval", json=case["input"])
    if case["kind"] == "aging":
        return client.post("/v1/aging/eval", json=case["input"])
    if case["kind"] == "tenancy":
        return client.post("/v1/tenants/eval", json=case["input"])
    params = {"dry_run": "true"} | ({"book": case["book"]} if case.get("book") else {})
    return client.post("/v1/intake/emails", content=case["input"].encode("utf-8"),
                       headers={"content-type": "message/rfc822"}, params=params)


def run(base: str, suite: str, repeat: int) -> list[dict]:
    out = []
    cases = load_cases(suite)
    with httpx.Client(base_url=base, timeout=60) as client:
        warm = next((c for c in cases if c["kind"] == "plan"), None)
        if warm:
            client.post("/v1/plans", json=warm["input"])  # warm the process once, like a real deployment
        for case in cases:
            if case["kind"] == "execution":
                out.append({"id": case["id"], "kind": case["kind"], "title": case["title"], "skipped": "in-process only"})
                print(f"  --  {case['id']:<4}          -    {case['title'][:60]} (in-process only)")
                continue
            times, server_ms, view = [], None, None
            for _ in range(repeat):
                t0 = time.perf_counter()
                res = send(client, case)
                times.append((time.perf_counter() - t0) * 1000)
                res.raise_for_status()
                body = res.json()
                view = view_of(case["kind"], body)
                server_ms = body.get("duration_ms") if case["kind"] == "plan" else None
            verdict = compare(case["expect"], view)
            out.append({"id": case["id"], "kind": case["kind"], "title": case["title"], "ms": round(statistics.median(times), 2),
                        "ms_all": [round(t, 2) for t in times], "server_ms": round(server_ms, 2) if server_ms else None,
                        **verdict, "view": view})
            mark = "ok " if verdict["match"] else "BAD"
            print(f"  {mark} {case['id']:<4} {statistics.median(times):8.1f} ms  {case['title'][:60]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="cases")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    results = run(args.base, args.suite, args.repeat)
    runs = ROOT / "data" / "runs" / "groundtruth"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = json.dumps(results, indent=1)
    (runs / f"{stamp}-{args.suite}-endpoint.json").write_text(payload, encoding="utf-8")
    (ROOT / "docs" / f"groundtruth_log_{args.suite}_endpoint.json").write_text(payload, encoding="utf-8")
    ran = [r for r in results if "ms" in r]
    bad = [r for r in ran if not r["match"]]
    ms = sorted(r["ms"] for r in ran)
    print(f"\n{len(ran) - len(bad)} / {len(ran)} match ground truth ({len(results) - len(ran)} in-process only); "
          f"round trip p50 {statistics.median(ms):.1f} ms, p95 {ms[int(0.95 * (len(ms) - 1))]:.1f} ms, max {ms[-1]:.1f} ms")
    for r in bad:
        print("  ", r["id"], {k: v for k, v in r["fields"].items() if not v["ok"]})
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
