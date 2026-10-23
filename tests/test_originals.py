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
from venthora.evidence.reader import BANK_CHANGE_FLAG, InboxMessage, calibrate_delay, read_inbox

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
    # Acme kept 6 of 8 promises counting this delay: Laplace posterior 7/10; window widens with thin history.
    assert (rcv.expected_in_hours, rcv.confidence_bp, rcv.arrival_window_hours) == (144, 7000, [144, 290])
    assert rcv.evidence_ref == "inbox:msg-acme-delay-1023"


def test_model_inventing_a_figure_is_rejected_and_the_email_is_re_read():
    reply = json.dumps({"delayed": True, "delay_days": 2, "doc_id": "msg-acme-delay-1023", "quote": QUOTE})
    r = read(StubModel(reply))
    [u] = r.updates
    assert u.verified and u.reader == "lexicon" and u.delay_days == 5  # what the email actually says
    [claim] = [c for c in r.stage.details["rejected_claims"] if c["subject"] == "RCV-ACME"]
    assert claim["reader"] == "llm" and "does not state a 2-day delay" in claim["problems"][0]


def test_model_quoting_text_not_in_the_email_has_no_effect():
    reply = json.dumps({"delayed": True, "delay_days": 5, "doc_id": "msg-acme-delay-1023",
                        "quote": "Acme will pay in five days and also double the amount."})
    r = read(StubModel(reply))
    [u] = r.updates
    assert u.reader == "lexicon" and u.quote.startswith("Hi team, because of an internal approval hold")
    assert any("does not occur" in c["problems"][0] for c in r.stage.details["rejected_claims"])


def test_hallucinated_delay_before_any_delay_email_changes_nothing():
    reply = json.dumps({"delayed": True, "delay_days": 3, "doc_id": "", "quote": ""})
    day1 = [m for m in INBOX["messages"] if m["date"] <= "2026-10-22"]
    r = read(StubModel(reply), day1)
    assert r.updates == [] and r.state.receivables[0].confidence_bp == 9000


def test_hallucinated_bank_change_for_an_unaffected_payee_is_not_flagged():
    reply = json.dumps({"changed": True, "doc_id": "msg-rheinwerk-notice-1022",
                        "quote": "The email mentions that the payment is due on October 25th."})
    r = read(StubModel(reply))
    flagged = {o.obligation_id for o in r.state.obligations if BANK_CHANGE_FLAG in o.policy_flags}
    assert flagged == {"CONTRACTOR-US"}  # found by re-reading Lumen's real email, not the model's claim


def test_embargoed_future_email_is_never_read():
    reply = json.dumps({"delayed": True, "delay_days": 1, "doc_id": "msg-acme-released-1030",
                        "quote": "the transfer should arrive within one business day."})
    r = read(StubModel(reply))
    assert "msg-acme-released-1030" in r.stage.details["embargoed"]
    [u] = r.updates
    assert u.doc_id == "msg-acme-delay-1023" and u.delay_days == 5  # never the future message


def test_unreachable_model_falls_back_to_lexicon_reader():
    r = read(ChatModel(endpoint="http://127.0.0.1:9", model="none", timeout_s=0.5))
    [u] = r.updates
    assert u.reader == "lexicon" and u.verified and u.delay_days == 5
    assert "lexicon fallback" in r.stage.details["notes"][0]


def test_delayed_receipt_read_from_inbox_drives_escalation():
    r = read(ChatModel(endpoint="", model=""))  # no model configured: lexicon reader
    plan = decide(r.state.model_dump())
    assert plan.decision == "REQUIRES_APPROVAL"
    acts = {d.obligation_id: d.action for d in plan.obligations}
    assert acts["SAAS-USD"] == "ESCALATE" and acts["CONTRACTOR-US"] == "ESCALATE"


def test_bank_detail_change_flags_only_the_named_payee():
    r = read(ChatModel(endpoint="", model=""))
    assert [(f.obligation_id, f.verified) for f in r.flags] == [("CONTRACTOR-US", True)]
    flagged = {o.obligation_id for o in r.state.obligations if BANK_CHANGE_FLAG in o.policy_flags}
    assert flagged == {"CONTRACTOR-US"}


def test_ungrounded_bank_claim_is_replaced_by_what_the_email_says():
    reply = json.dumps({"changed": True, "doc_id": "msg-lumen-bank-1022", "quote": "Lumen moved to Bank X yesterday."})
    r = read(StubModel(reply))
    flag = next(f for f in r.flags if f.obligation_id == "CONTRACTOR-US")
    assert flag.verified and flag.reader == "lexicon" and "changed banks" in flag.quote


def test_calibration_widens_with_thin_history_and_never_trusts_unverified_news():
    rich = calibrate_delay(24, 9000, None, 5, True)
    from venthora.domain.models import PayerHistory
    deep = calibrate_delay(24, 9000, PayerHistory(kept=40, missed=2), 5, True)
    assert rich[2][1] - rich[2][0] > deep[2][1] - deep[2][0]  # no history -> wider 90% window
    assert deep[1] > rich[1]                                   # a reliable payer keeps more confidence
    hours, conf, _, _ = calibrate_delay(24, 9000, PayerHistory(kept=40, missed=2), None, False)
    assert hours == 24 and conf < 9000


def test_cli_fake_run_completes(tmp_path, monkeypatch, capsys):
    from venthora import cli

    monkeypatch.setenv("VENTHORA_EXECUTION_DB", str(tmp_path / "exec.db"))
    monkeypatch.setattr(cli, "ROOT", ROOT)
    monkeypatch.setattr(cli, "load_env", lambda: None)  # never read the developer's real .env in tests
    args = type("A", (), dict(scenario="kit01_baseline", inbox=str(ROOT / "data" / "inbox" / "kit01_inbox.json"),
                              fake=True, receipt=True, full_wallet=False, approve="Dana", yes=False,
                              plan_only=False, no_fx_stress=True, no_challenge=False, incident=None))
    monkeypatch.setattr(httpx.AsyncClient, "get", _no_network)
    code = asyncio.run(cli.run(args))
    out = capsys.readouterr().out
    assert code == 0 and "COMPLETED" in out and "VERIFIED (lexicon)" in out
    assert "POLICY EXCEPTION" in out and "re-read balances" in out and "incremental" in out


async def _no_network(self, url, **kw):
    raise httpx.ConnectError("offline test")
