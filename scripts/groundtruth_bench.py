"""Bench the ground-truth cases through the HTTP endpoints of a running server and write the report.

  python scripts/groundtruth_bench.py [--base http://127.0.0.1:8000] [--repeat 3]

Every case is sent the way a client would: plans to POST /v1/plans, emails to POST
/v1/intake/emails?dry_run=true. Latency is wall-clock round trip measured by this client; the
server's own duration_ms is recorded beside it for plans. Writes docs/GROUND_TRUTH.md (endpoint
section) and data/runs/groundtruth/<stamp>.json.
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
from venthora.testing.groundtruth import compare, intake_view, load_cases, plan_view  # noqa: E402


def run(base: str, repeat: int) -> list[dict]:
    out = []
    with httpx.Client(base_url=base, timeout=60) as client:
        client.post("/v1/plans", json=load_cases()[0]["input"])  # warm the process once, like a real deployment
        for case in load_cases():
            times, server_ms, view = [], None, None
            for _ in range(repeat):
                t0 = time.perf_counter()
                if case["kind"] == "plan":
                    res = client.post("/v1/plans", json=case["input"])
                else:
                    params = {"dry_run": "true"} | ({"book": case["book"]} if case.get("book") else {})
                    res = client.post("/v1/intake/emails", content=case["input"].encode("utf-8"),
                                      headers={"content-type": "message/rfc822"}, params=params)
                times.append((time.perf_counter() - t0) * 1000)
                res.raise_for_status()
                body = res.json()
                view = plan_view(body) if case["kind"] == "plan" else intake_view(body)
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
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    results = run(args.base, args.repeat)
    runs = ROOT / "data" / "runs" / "groundtruth"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (runs / f"{stamp}-endpoint.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    bad = [r for r in results if not r["match"]]
    ms = sorted(r["ms"] for r in results)
    print(f"\n{len(results) - len(bad)} / {len(results)} match ground truth; round trip p50 {statistics.median(ms):.1f} ms, "
          f"p95 {ms[int(0.95 * (len(ms) - 1))]:.1f} ms, max {ms[-1]:.1f} ms")
    for r in bad:
        print("  ", r["id"], {k: v for k, v in r["fields"].items() if not v["ok"]})
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
