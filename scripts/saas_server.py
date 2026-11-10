"""Run VENTHORA as SaaS: the platform front door and one VENTHORA process per company (src/venthora/saas.py).

  python scripts/saas_server.py [--port 8030] [--dir data/saas]

Open http://127.0.0.1:<port>/platform to sign in to a company, /platform/console for the Developer's console.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8030)
    ap.add_argument("--dir", default=None, help="the platform's directory (registry and one folder per company)")
    a = ap.parse_args()
    if a.dir:
        os.environ["VENTHORA_SAAS_DIR"] = str(Path(a.dir).resolve())
    sys.path.insert(0, str(ROOT / "src"))
    import uvicorn

    from venthora.saas_gateway import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
