import asyncio
import copy
import json
from pathlib import Path

import pytest

SCENARIOS = Path(__file__).resolve().parents[1] / "data" / "scenarios"
ISOLATED_ENV = ["AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY", "OPA_URL", "TB_ADDRESS", "TB_ACCOUNT_ID",
                "VENTHORA_VERITY_URL", "VENTHORA_HORIZON_URL", "VENTHORA_THEOREM_URL", "VENTHORA_NATIVE_LIB"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ISOLATED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VENTHORA_AUDIT_DB", str(tmp_path / "audit.db"))
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
