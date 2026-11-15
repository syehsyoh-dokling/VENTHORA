# ruff: noqa: E501
import asyncio
import copy
import json
import os
from pathlib import Path

import pytest

# Set before any test imports venthora.api, whose import reads .env: the suite must never see the developer's
# credentials or webhook secret, whichever test happens to import the API first.
os.environ["VENTHORA_NO_DOTENV"] = "1"
os.environ["VENTHORA_BUDGET_SCHEDULER"] = "0"  # the daily / monthly budget sync never runs on its own in a test
os.environ["VENTHORA_SYNC_SCHEDULER"] = "0"  # nor does the connector sync
os.environ["VENTHORA_OUTBOX_WORKER"] = "0"  # nor does the outbox worker (tests call outbox.flush directly)
os.environ["VENTHORA_T0_AUTOPOST"] = "0"  # nor does the T:0 books worker
os.environ["VENTHORA_CARD_WINDOWS"] = "0"  # nor does the staff-card window worker
os.environ["VENTHORA_RATE_LIMIT"] = "0"  # the rate limit has its own test; the suite itself is never throttled
os.environ["VENTHORA_PLAN_TTL"] = "0"  # tests change the stores directly, so the plan is always made again (its cache has its own test)
# VENTHORA ships blank (one head office, one Super admin). The cases need a company to work on: the sample one.
os.environ["VENTHORA_SAMPLE_DATA"] = str(Path(__file__).resolve().parent / "sample_data")

SCENARIOS = Path(__file__).resolve().parents[1] / "data" / "scenarios"
ISOLATED_ENV = ["AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY", "AIRWALLEX_BASE_URL", "AIRWALLEX_GLOBAL_ACCOUNT_ID",
                "AWX_CLIENT_ID", "AWX_API_KEY", "AWX_BASE_URL", "AWX_GLOBAL_ACCOUNT_ID",
                "MODEL_ENDPOINT", "MODEL_NAME", "MODEL_TOKEN", "MODEL_API_KEY",
                "OPA_URL", "TB_ADDRESS", "TB_ACCOUNT_ID",
                "AWX_WEBHOOK_SECRET",
                "VENTHORA_VERITY_URL", "VENTHORA_HORIZON_URL", "VENTHORA_THEOREM_URL", "VENTHORA_NATIVE_LIB"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ISOLATED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VENTHORA_AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("VENTHORA_EXECUTION_DB", str(tmp_path / "execution.db"))
    monkeypatch.setenv("VENTHORA_INTAKE_DB", str(tmp_path / "intake.db"))  # the API tests never touch the developer's queue
    monkeypatch.setenv("VENTHORA_WEBHOOK_DB", str(tmp_path / "webhooks.db"))
    monkeypatch.setenv("VENTHORA_ORG_DB", str(tmp_path / "org.db"))
    monkeypatch.setenv("VENTHORA_AI_DB", str(tmp_path / "ai.db"))
    monkeypatch.setenv("VENTHORA_MASTER_DB", str(tmp_path / "master.db"))
    monkeypatch.setenv("VENTHORA_SECRETS_DB", str(tmp_path / "secrets.db"))  # no connected Airwallex account in tests
    monkeypatch.setenv("VENTHORA_REPORTING_CONFIG", str(tmp_path / "reporting.json"))  # starts from the shipped copy
    monkeypatch.setenv("VENTHORA_ECB_CSV", str(tmp_path / "missing-ecb.csv"))


def scenario(name: str) -> dict:
    from venthora.config import seed_file

    return copy.deepcopy(json.loads(seed_file(f"scenarios/{name}.json").read_text(encoding="utf-8"))["state"])


def decide(state_dict: dict):
    from venthora.domain.models import TreasuryState
    from venthora.orchestrator import Orchestrator

    async def go():
        agent = Orchestrator()
        try:
            return await agent.decide(TreasuryState.model_validate(state_dict))
        finally:
            await agent.close()

    return asyncio.run(go())


def actions(result) -> dict[str, str]:
    return {d.obligation_id: d.action for d in result.obligations}


# -- tests that cannot or should not count as failures, each for a stated reason ---------------------------------------

# packages a machine may not have: a test that needs one is skipped with the package's name, not failed.
OPTIONAL_PACKAGES = {"anthropic", "eth_account", "tigerbeetle", "ortools"}
KNOWN_GAPS = Path(__file__).resolve().parent / "known_gaps.txt"


def _known_gaps() -> dict[str, str]:
    out = {}
    for line in KNOWN_GAPS.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            test_id, _, why = line.partition("|")
            out[test_id.strip()] = why.strip()
    return out


def pytest_collection_modifyitems(config, items):
    """tests/known_gaps.txt: tests that describe behaviour the product no longer has. They still run; they are reported
    as expected failures with the reason, and as xpassed the day they pass again."""
    gaps = _known_gaps()
    for item in items:
        why = gaps.get(item.nodeid.replace("\\", "/"))
        if why:
            item.add_marker(pytest.mark.xfail(reason=why, strict=False))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    exc = call.excinfo.value if call.excinfo is not None else None
    if isinstance(exc, ModuleNotFoundError) and exc.name in OPTIONAL_PACKAGES:
        report.outcome = "skipped"
        report.longrepr = (str(item.fspath), item.location[1] or 0, f"Skipped: needs the package {exc.name}, which is not installed here")
