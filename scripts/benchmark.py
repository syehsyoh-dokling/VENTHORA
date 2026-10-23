"""Measure the local decision loop (no network): plan + prove latency, p50/p95/p99, per stage.

Run: python scripts/benchmark.py [--n 300]
Airwallex/OPA/TigerBeetle/LLM are not configured here, so this is the in-process cost of
Verity + Horizon + Theorem (CP-SAT) + ProofCore + counterfactuals. Network calls are measured
separately from real sandbox run reports.
"""
import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

for name in ("AWX_CLIENT_ID", "AWX_API_KEY", "AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY", "OPA_URL", "TB_ADDRESS",
             "MODEL_ENDPOINT"):
    os.environ.pop(name, None)

from venthora.domain.models import TreasuryState  # noqa: E402
from venthora.orchestrator import Orchestrator  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


async def main(n: int, scenario: str) -> None:
    state = TreasuryState.model_validate(
        json.loads((ROOT / "data" / "scenarios" / f"{scenario}.json").read_text(encoding="utf-8"))["state"])
    agent = Orchestrator()
    try:
        t0 = time.perf_counter()
        await agent.decide(state)  # cold: imports, OR-Tools load
        cold = (time.perf_counter() - t0) * 1000
        totals, stages = [], {}
        for _ in range(n):
            t = time.perf_counter()
            plan = await agent.decide(state)
            totals.append((time.perf_counter() - t) * 1000)
            for s in plan.stages:
                stages.setdefault(s.engine, []).append(s.duration_ms)
        print(f"scenario {scenario}: cold start {cold:.1f} ms; warm n={n}")
        print(f"  plan+prove total   p50 {pct(totals, .5):7.2f} ms   p95 {pct(totals, .95):7.2f} ms   "
              f"p99 {pct(totals, .99):7.2f} ms   mean {statistics.mean(totals):.2f} ms")
        for engine, values in stages.items():
            print(f"  {engine:<28} p50 {pct(values, .5):7.2f} ms   p95 {pct(values, .95):7.2f} ms")
    finally:
        await agent.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--scenario", default="kit01_baseline")
    args = ap.parse_args()
    asyncio.run(main(args.n, args.scenario))
