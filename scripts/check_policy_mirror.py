# ruff: noqa: E501
"""Prove policy/treasury.rego and proofcore/policy.py agree, using a real OPA (Docker).

Run: python scripts/check_policy_mirror.py
Evaluates both on a grid of inputs (confidence None/0..10000, limits, roles, escalations) and
exits non-zero on the first disagreement.
"""
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from venthora.proofcore import policy

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "openpolicyagent/opa:0.70.0"


def opa_eval(inputs: list[dict]) -> list[dict]:
    (ROOT / "data" / "runs").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "data" / "runs") as tmp:  # a plain path Docker can mount
        tmp_path = Path(tmp)
        (tmp_path / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")
        query = "[d | i := input[_]; d := data.venthora.treasury.decision with input as i]"
        cmd = ["docker", "run", "--rm", "-v", f"{ROOT / 'policy'}:/policy:ro", "-v", f"{tmp_path}:/in:ro", IMAGE,
               "eval", "-d", "/policy/treasury.rego", "-i", "/in/inputs.json", "--format", "json", query]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return json.loads(out)["result"][0]["expressions"][0]["value"]


def main() -> int:
    grid = []
    for conf, limit, role, esc, breach in itertools.product(
            [None, 0, 2500, 5000, 5001, 6000, 7000, 8999, 9000, 9500, 10000], [1_000_000, 500_000, 0],
            ["treasury_agent", "finance_manager", "viewer"], [[], ["X"]], [None, 0, 500, 501, 10000]):
        for conversion in (0, 499_999, 500_000, 500_001, 1_000_001):
            grid.append({"actor_role": role, "approver_roles": ["finance_manager"], "conversion_base_minor": conversion,
                         "forecast_confidence_bp": conf, "autonomous_conversion_limit_minor": limit,
                         "full_confidence_bp": 9000, "zero_limit_below_bp": 5000, "escalations": esc,
                         "breach_probability_bp": breach, "max_breach_probability_bp": 500})
    # Budget · mandate: lines around every threshold (auto share, alert, mandate), bypass signed or not, unbudgeted.
    base = grid[0] | {"actor_role": "treasury_agent", "conversion_base_minor": 0, "escalations": [], "breach_probability_bp": None}
    for mandate, used, auto, alert, on_alert, bypass, signed, unb, unb_req in itertools.product(
            [0, 100_000], [0, 69_999, 70_000, 79_999, 80_000, 100_000, 100_001], [7000, 8000, 10000], [7000, 8000],
            [False, True], [False, True], [False, True], [0, 1], [False, True]):
        grid.append(base | {"budget": {"attached": True, "unbudgeted_paying": unb, "unbudgeted_requires_approval": unb_req,
                                       "lines": [{"line": "l", "mandate_minor": mandate, "used_minor": used, "paying_now_minor": used,
                                                  "auto_share_bp": auto, "alert_share_bp": alert, "require_approval_on_alert": on_alert,
                                                  "bypass": bypass, "bypass_signed": bypass and signed}]}})
    local = [policy.evaluate(i) for i in grid]
    remote = opa_eval(grid)
    bad = [(i, a, b) for i, a, b in zip(grid, local, remote, strict=True) if a != b]
    print(f"{len(grid)} inputs; disagreements: {len(bad)}")
    for i, a, b in bad[:5]:
        print(" input", json.dumps(i), "\n  python", a, "\n  opa   ", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
