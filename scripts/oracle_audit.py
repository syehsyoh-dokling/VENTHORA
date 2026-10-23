"""Adversarial oracle audit: does the Kit 01 behaviour come from the rules, or from the demo data?

Run: python scripts/oracle_audit.py            (exit code 1 on any failure)

M1  rename every id / payee            -> identical actions (no id- or name-based logic)
M2  swap the priorities of shipping and SaaS -> their roles swap
M3  make SaaS the largest amount, lowest priority -> still the one deferred (priority, not amount)
M4  remove the bank-change email        -> no policy exception; the contractor is decided on cash
M5  edit the delay to 'two days'        -> the calibrated forecast follows the text (72 h)
M6  spotless payer history              -> a contradicting email still caps confidence below the full band
M7  change case_id                      -> identical plan
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import sys
from pathlib import Path

from venthora.domain.models import TreasuryState
from venthora.evidence.llm import ChatModel
from venthora.evidence.reader import InboxMessage, read_inbox
from venthora.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]
INBOX = json.loads((ROOT / "data" / "inbox" / "kit01_inbox.json").read_text(encoding="utf-8"))
NO_MODEL = ChatModel(endpoint="", model="")


def scenario(name: str) -> dict:
    return json.loads((ROOT / "data" / "scenarios" / f"{name}.json").read_text(encoding="utf-8"))["state"]


def plan(state: dict):
    async def go():
        agent = Orchestrator()
        try:
            return await agent.decide(TreasuryState.model_validate(state))
        finally:
            await agent.close()
    return asyncio.run(go())


def actions(p) -> dict[str, str]:
    return {d.obligation_id: d.action for d in p.obligations}


def read(state: dict, messages: list[dict], day: str):
    return asyncio.run(read_inbox(TreasuryState.model_validate(state), [InboxMessage(**m) for m in messages],
                                  as_of=dt.date.fromisoformat(day), model=NO_MODEL))


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")

    base = scenario("kit01_initial")
    ref = actions(plan(base))

    m1 = copy.deepcopy(base)
    mapping = {o["obligation_id"]: f"X{i}" for i, o in enumerate(m1["obligations"])}
    for o in m1["obligations"]:
        o["obligation_id"] = mapping[o["obligation_id"]]
        o["beneficiary_id"] = "b-" + o["beneficiary_id"]
        o["payee_name"] = (o.get("payee_name") or "") + " Zz"
    for e in m1["evidence"]:
        e["subject_id"] = mapping.get(e["subject_id"], e["subject_id"])
    check("M1 rename ids/payees -> same actions", {mapping[k]: v for k, v in ref.items()} == actions(plan(m1)))

    m2 = copy.deepcopy(base)
    by = {o["obligation_id"]: o for o in m2["obligations"]}
    by["SHIPPING-UK"]["priority"], by["SAAS-USD"]["priority"] = 2, 10
    a2 = actions(plan(m2))
    check("M2 swap priorities -> roles swap", a2["SAAS-USD"] == "PAY" and a2["SHIPPING-UK"] in ("DEFER", "ESCALATE"), str(a2))

    m3 = copy.deepcopy(base)
    {o["obligation_id"]: o for o in m3["obligations"]}["SAAS-USD"]["amount_minor"] = 900_000
    a3 = actions(plan(m3))
    check("M3 deferral by priority, not amount",
          a3["SAAS-USD"] in ("DEFER", "ESCALATE") and a3["SHIPPING-UK"] == "CONVERT_AND_PAY")

    msgs = [m for m in INBOX["messages"] if m["doc_id"] != "msg-lumen-bank-1022"]
    r4 = read(scenario("kit01_baseline"), msgs, INBOX["timeline"][0])
    flagged = [o.obligation_id for o in r4.state.obligations if o.policy_flags]
    reason = next(d.reason for d in plan(r4.state.model_dump()).obligations if d.obligation_id == "CONTRACTOR-US")
    check("M4 no bank-change email -> no policy exception", flagged == [] and "policy exception" not in reason, reason)

    msgs = copy.deepcopy(INBOX["messages"])
    for m in msgs:
        if m["doc_id"] == "msg-acme-delay-1023":
            m["text"] = m["text"].replace("five days", "two days")
    u5 = read(scenario("kit01_baseline"), msgs, INBOX["timeline"][1]).updates[0]
    check("M5 delay follows the text", u5.verified and u5.delay_days == 2 and u5.new_expected_in_hours == 72,
          f"{u5.delay_days}d -> {u5.new_expected_in_hours}h window {u5.arrival_window_hours}")

    s6 = scenario("kit01_baseline")
    s6["receivables"][0]["payer_history"] = {"kept": 60, "missed": 0}
    r6 = read(s6, INBOX["messages"], INBOX["timeline"][1])
    p6 = plan(r6.state.model_dump())
    check("M6 spotless payer: contradiction still lowers the limit", r6.updates[0].new_confidence_bp == 7000
          and p6.decision == "REQUIRES_APPROVAL", f"conf={r6.updates[0].new_confidence_bp} limit={p6.conversion_limit_minor}")

    m7 = copy.deepcopy(base)
    m7["case_id"] = "totally-different"
    check("M7 case_id independence", actions(plan(m7)) == ref)

    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
