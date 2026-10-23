"""The original engines are present, unmodified, and actually wired into the decision."""
import asyncio
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest

from conftest import decide, scenario
from venthora.domain.models import Conversion, TreasuryState
from venthora.engines.horizon_m0 import fx_stress_ppm
from venthora.engines.theorem_fx import check_conversion_legs
from venthora.evidence.llm import ChatModel
from venthora.evidence.reader import UNVERIFIED_SIGNAL_BP, InboxMessage, read_inbox

ROOT = Path(__file__).resolve().parents[1]
ORIGINALS = ROOT / "src" / "venthora" / "originals"
INBOX = json.loads((ROOT / "data" / "inbox" / "kit01_inbox.json").read_text(encoding="utf-8"))
AS_OF = dt.date(2026, 10, 23)


def test_vendored_originals_match_provenance_hashes():
    table = (ORIGINALS / "PROVENANCE.md").read_text(encoding="utf-8")
    pairs = re.findall(r"`([a-z_]+/[a-z_0-9]+\.py)` \| .*? \| `([0-9a-f]{64})`", table)
    assert len(pairs) == 5
    for rel, digest in pairs:
        assert hashlib.sha256((ORIGINALS / rel).read_bytes()).hexdigest() == digest, rel


def test_theorem_legs_check_passes_plan_and_catches_tampering():
    state = TreasuryState.model_validate(scenario("kit01_baseline"))
    good = Conversion(sell_currency="USD", buy_currency="EUR", buy_amount_minor=401_285, estimated_sell_minor=435_395)
    assert check_conversion_legs(state, [good])[0]
    bad = good.model_copy(update={"estimated_sell_minor": 400_000})
    ok, detail = check_conversion_legs(state, [bad])
    assert not ok and "USD leg" in detail["problems"][0]


def test_theorem_stage_runs_inside_proofcore():
    r = decide(scenario("kit01_baseline"))
    stage = next(s for s in r.stages if s.engine == "Theorem · FX legs")
    assert stage.status == "PASS"
    assert stage.details["theorem_net_exposure"]["EUR"] == pytest.approx(4012.85)


def ecb_fixture(tmp_path) -> Path:
    path = tmp_path / "ecb.csv"
    rows = ["Date,USD,GBP,"]
    day = dt.date(2025, 1, 1)
    for i in range(200):  # test fixture only; real runs use scripts/import_ecb.py
        if day.weekday() < 5:
            rows.append(f"{day},{1.08 + 0.002 * ((i * 7) % 11 - 5):.4f},{0.85 + 0.001 * ((i * 3) % 7 - 3):.4f},")
        day += dt.timedelta(days=1)
    path.write_text("\n".join(rows), encoding="utf-8")
    return path


def test_horizon_m0_engine_produces_seeded_fx_stress(tmp_path):
    path = ecb_fixture(tmp_path)
    a, b = fx_stress_ppm("EUR", "USD", path=path), fx_stress_ppm("EUR", "USD", path=path)
    assert a["available"] and a["engine"] == "horizon-m0" and a["stress_ppm"] > 0
    assert a["stress_ppm"] == b["stress_ppm"]  # seeded per series: reproducible
    assert fx_stress_ppm("GBP", "USD", path=path)["available"]


def test_horizon_m0_stress_flows_into_the_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("VENTHORA_ECB_CSV", str(ecb_fixture(tmp_path)))
    s = scenario("kit01_baseline")
    s["use_ecb_stress"] = True
    r = decide(s)
    horizon = next(st for st in r.stages if st.engine == "Horizon · risk")
    assert horizon.details["ecb"]["EUR"]["engine"] == "horizon-m0"
    assert r.required_base_floor_minor > 600_000  # floor now includes the FX stress buffer


class StubModel(ChatModel):
    def __init__(self, reply: str):
        super().__init__(endpoint="http://stub", model="stub")
        self.reply = reply

    async def complete(self, messages, max_tokens=300):
        return self.reply


def read(model, messages=None):
    msgs = [InboxMessage(**m) for m in (messages or INBOX["messages"])]
    return asyncio.run(read_inbox(TreasuryState.model_validate(scenario("kit01_baseline")), msgs, as_of=AS_OF,
                                  model=model))


QUOTE = "the Acme Retail Ltd payment of USD 10,000 for invoice AR-5531 will be delayed by five days."


def test_model_claim_verified_by_verity_updates_forecast():
    reply = json.dumps({"delayed": True, "delay_days": 5, "doc_id": "[msg-acme-delay-1023]", "quote": QUOTE,
                        "claim": "a delay of 5 days"})
    r = read(StubModel(reply))
    [u] = r.updates
    assert u.verified and u.reader == "llm" and u.delay_days == 5
    rcv = r.state.receivables[0]
    assert (rcv.expected_in_hours, rcv.confidence_bp) == (144, 4000)
    assert rcv.evidence_ref == "inbox:msg-acme-delay-1023"


def test_model_inventing_a_figure_is_not_believed_but_reduces_reliance():
    reply = json.dumps({"delayed": True, "delay_days": 2, "doc_id": "msg-acme-delay-1023", "quote": QUOTE})
    r = read(StubModel(reply))
    [u] = r.updates
    assert not u.verified and "does not state a 2-day delay" in u.problems[0]
    rcv = r.state.receivables[0]
    assert rcv.expected_in_hours == 24 and rcv.confidence_bp == UNVERIFIED_SIGNAL_BP  # never earlier, never surer


def test_model_quoting_text_not_in_the_email_is_rejected():
    reply = json.dumps({"delayed": True, "delay_days": 5, "doc_id": "msg-acme-delay-1023",
                        "quote": "Acme will pay in five days and also double the amount."})
    [u] = read(StubModel(reply)).updates
    assert not u.verified and "does not occur" in u.problems[0]


def test_embargoed_future_email_is_never_read():
    reply = json.dumps({"delayed": True, "delay_days": 1, "doc_id": "msg-acme-released-1030",
                        "quote": "the transfer should arrive within one business day."})
    r = read(StubModel(reply))
    assert "msg-acme-released-1030" in r.stage.details["embargoed"]
    assert not r.updates[0].verified


def test_unreachable_model_falls_back_to_lexicon_reader():
    r = read(ChatModel(endpoint="http://127.0.0.1:9", model="none", timeout_s=0.5))
    [u] = r.updates
    assert u.reader == "lexicon" and u.verified and u.delay_days == 5
    assert "lexicon fallback" in r.stage.details["notes"][0]


def test_delayed_receipt_read_from_inbox_drives_escalation():
    r = read(ChatModel(endpoint="", model=""))  # no model configured: lexicon reader
    plan = decide(r.state.model_dump())
    assert plan.decision == "REQUIRES_APPROVAL"
    assert {d.obligation_id: d.action for d in plan.obligations}["VENDOR-UK"] == "ESCALATE"


def test_cli_fake_run_completes(tmp_path, monkeypatch, capsys):
    from venthora import cli

    monkeypatch.setenv("VENTHORA_EXECUTION_DB", str(tmp_path / "exec.db"))
    monkeypatch.setattr(cli, "ROOT", ROOT)
    monkeypatch.setattr(cli, "load_env", lambda: None)  # never read the developer's real .env in tests
    args = type("A", (), dict(scenario="kit01_baseline", inbox=str(ROOT / "data" / "inbox" / "kit01_inbox.json"),
                              fake=True, receipt=True, full_wallet=False, approve="Dana", yes=False,
                              plan_only=False, no_fx_stress=True, no_challenge=False))
    monkeypatch.setattr(httpx.AsyncClient, "get", _no_network)
    code = asyncio.run(cli.run(args))
    out = capsys.readouterr().out
    assert code == 0 and "COMPLETED" in out and "VERIFIED (lexicon)" in out


async def _no_network(self, url, **kw):
    raise httpx.ConnectError("offline test")
