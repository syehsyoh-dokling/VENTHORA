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
    monkeypatch.setenv("VENTHORA_REPORTING_CONFIG", str(tmp_path / "reporting.json"))  # starts from the shipped copy
    monkeypatch.setenv("VENTHORA_ECB_CSV", str(tmp_path / "missing-ecb.csv"))


def scenario(name: str) -> dict:
    return copy.deepcopy(json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))["state"])


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
