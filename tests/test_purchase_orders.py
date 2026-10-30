# ruff: noqa: E501
"""Purchase orders end to end: an invoice quotes its PO, code matches supplier, currency, room and term, the payment is
booked against the PO and its account code (office . project . account), spending rolls up per office, branch, project
and account, and a payment above its unit's limit needs a person from that unit chain."""
import asyncio
import datetime as dt
from email.message import EmailMessage

import pytest

from conftest import scenario
from venthora import procurement
from venthora.domain.models import TreasuryState
from venthora.execution.store import ExecutionStore
from venthora.intake.pipeline import Intake, IntakeStore
from venthora.organisation import OrgError, OrgStore, PurchaseOrder, Standards
from venthora.proofcore.limits import limit_reasons

LUMEN, RHEIN, THAMES = "lumen.design.studio.billing@gmail.com", "debitoren@rheinwerk.example", "thames.freight.accounts@gmail.com"


def mail(sender, subject, body, mid):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = sender, "ap@acme.example", subject, "Fri, 24 Oct 2026 09:00:00 +0000", f"<{mid}@t>"
    m.set_content(body)
    return m.as_bytes()


@pytest.fixture
def env(tmp_path):
    org = OrgStore(str(tmp_path / "org.db"))
    pay = ExecutionStore(str(tmp_path / "e.db"))
    return org, pay, Intake(IntakeStore(str(tmp_path / "i.db")), org=org, payments=pay)


def book(pay, po, term, amount, ccy, code, n, base=None):
    pay.book_payment({"transfer_id": f"tr-{n}", "book_id": "b", "obligation_id": f"o-{n}", "invoice_fingerprint": f"fp-{n}",
                      "installment": term, "contract_total_minor": None, "line": None, "currency": ccy, "amount_minor": amount,
                      "base_cost_minor": base or amount, "paid_at": "2026-10-20T10:00:00+00:00", "reporting_period": "2026-10",
                      "beneficiary_id": "x", "po_number": po, "account_code": code})


def test_account_codes_name_office_project_account(env):
    org, _, _ = env
    assert org.account_code("de-400.prj900.6500")["unit"]["unit_id"] == "co-de"
    assert org.account_code("PRJ-900.PRJ900.6500")["unit"]["unit_id"] == "pf-eu-rollout"
    for bad, why in [("US-200.PRJ900.6500", "belongs to"), ("XX-1.OPS.6100", "cost centre"), ("DE-400.OPS.9999", "chart of accounts"),
                     ("DE-400-OPS-6500", "OFFICE.PROJECT.ACCOUNT")]:
        with pytest.raises(OrgError, match=why):
            org.account_code(bad)
    with pytest.raises(OrgError, match="paid in GBP"):
        org.save("po", PurchaseOrder(po_id="PO-X-1", supplier_id="ben-uk-logistics", currency="USD", total_minor=100,
                                     account_code="UK-300.OPS.6100"), "t")
    with pytest.raises(OrgError, match="more than the PO total"):
        org.save("po", PurchaseOrder(po_id="PO-X-2", supplier_id="ben-uk-logistics", currency="GBP", total_minor=100,
                                     account_code="UK-300.OPS.6100", terms=[{"term_id": "T1", "label": "all", "amount_minor": 101}]), "t")


def test_scheduled_terms_then_the_po_is_full(env):
    _, _, intake = env
    t1 = intake.ingest(mail(LUMEN, "Invoice LD-201", "Invoice LD-201 for PO-2026-0101, phase 1. Amount due: USD 1,800.00, due 7 Nov 2026.", "a"))
    assert (t1.status, t1.po_number, t1.installment, t1.account_code) == ("VERIFIED", "PO-2026-0101", "T1", "US-200.BRAND26.6600")
    assert any(q.field == "po_number" and q.value == "PO-2026-0101" for q in t1.quotes)
    t2 = intake.ingest(mail(LUMEN, "Invoice LD-202", "Invoice LD-202, P.O. # PO-2026-0101, phase 2. Amount due: USD 3,200.00, due 21 Nov 2026.", "b"))
    assert (t2.status, t2.installment) == ("VERIFIED", "T2")
    over = intake.ingest(mail(LUMEN, "Invoice LD-203", "Invoice LD-203 against PO-2026-0101. Amount due: USD 100.00, due 28 Nov 2026.", "c"))
    assert over.status == "NEEDS_REVIEW" and "po_exceeded" in over.flags  # 1,800 + 3,200 already hold the whole USD 5,000


def test_partial_invoices_and_paid_money(env):
    org, pay, intake = env
    book(pay, "PO-2026-0103", "partial", 600_000, "GBP", "UK-300.OPS.6100", 1)  # GBP 6,000 already paid on the call-off PO
    ok = intake.ingest(mail(THAMES, "Invoice TF-401", "Invoice TF-401, PO-2026-0103: November freight. Amount due: GBP 3,500.00, due 30 Nov 2026.", "d"))
    assert (ok.status, ok.installment) == ("VERIFIED", "partial")
    more = intake.ingest(mail(THAMES, "Invoice TF-402", "Invoice TF-402, PO-2026-0103. Amount due: GBP 600.00, due 30 Nov 2026.", "e"))
    assert "po_exceeded" in more.flags  # 6,000 paid + 3,500 pending + 600 > 10,000
    st = procurement.status(org, pay, "PO-2026-0103")
    assert (st["paid_minor"], st["remaining_minor"]) == (600_000, 400_000)


def test_an_over_po_invoice_holds_nothing(env):
    # seen in the demo cases: a GBP 12,000 invoice against a PO with GBP 10,000 left blocked an honest GBP 450 one
    _, _, intake = env
    big = intake.ingest(mail(THAMES, "Invoice TF-501", "Invoice TF-501, PO-2026-0103. Amount due: GBP 12,000.00, due 30 Nov 2026.", "o1"))
    assert "po_exceeded" in big.flags
    small = intake.ingest(mail(THAMES, "Invoice TF-502", "Invoice TF-502, PO-2026-0103. Amount due: GBP 450.00, due 30 Nov 2026.", "o2"))
    assert small.status == "VERIFIED" and small.installment == "partial"


def test_terms_only_po_and_every_mismatch(env):
    org, _, intake = env
    odd = intake.ingest(mail(RHEIN, "Rechnung RW-77", "Invoice RW-77, PO-2026-0102. Amount due: EUR 2,000.00, due 30 Nov 2026.", "f"))
    # EUR 2,000 is neither scheduled term (EUR 3,600 / 8,400): it is simply the PO's next termin while the PO has room
    assert odd.trust == "registered" and odd.status == "VERIFIED" and odd.installment == "partial" and "termin-1" in " ".join(odd.reasons)
    t1 = intake.ingest(mail(RHEIN, "Rechnung RW-78", "Invoice RW-78, PO-2026-0102, deposit. Amount due: EUR 3,600.00, due 30 Nov 2026.", "f2"))
    assert (t1.status, t1.installment, t1.account_code) == ("VERIFIED", "T1", "DE-400.PRJ900.6500")
    wrong = intake.ingest(mail(THAMES, "Invoice TF-403", "Invoice TF-403, PO-2026-0101. Amount due: GBP 500.00, due 30 Nov 2026.", "g"))
    assert {"po_supplier_mismatch", "po_currency_mismatch"} <= set(wrong.flags) and wrong.status == "NEEDS_REVIEW"
    unknown = intake.ingest(mail(LUMEN, "Invoice LD-204", "Invoice LD-204, PO-2099-0001. Amount due: USD 50.00, due 30 Nov 2026.", "h"))
    assert "po_unknown" in unknown.flags
    none = intake.ingest(mail(LUMEN, "Invoice LD-205", "Invoice LD-205. Amount due: USD 50.00, due 30 Nov 2026.", "i"))
    assert none.status == "NEEDS_REVIEW" and "po_missing" in none.flags and none.invoice_no == "LD-205"
    org.save_standards(Standards(require_po=False), "t")
    free = intake.ingest(mail(LUMEN, "Invoice LD-206", "Invoice LD-206. Amount due: USD 50.00, due 30 Nov 2026.", "j"))
    assert free.status == "VERIFIED" and free.po_number is None


def test_into_the_plan_with_unit_limit_and_approvers(env):
    org, _, intake = env
    intake.ingest(mail(LUMEN, "Invoice LD-207", "Invoice LD-207, PO-2026-0101, phase 2. Amount due: USD 3,200.00, due 21 Nov 2026.", "k"))
    st = intake.to_state(TreasuryState.model_validate(scenario("kit01_initial")), as_of=dt.date(2026, 10, 24))
    o = next(x for x in st.obligations if x.po_number == "PO-2026-0101")
    assert (o.account_code, o.installment, o.contract_total_minor, o.paid_so_far_minor) == ("US-200.BRAND26.6600", "T2", 500_000, 0)
    # US-200's limit is USD 10,000; Dana (US finance manager, limit 50,000) and Omar (CFO) may approve above it
    assert o.unit_payment_limit_minor == 1_000_000 and o.unit_approvers == ["Dana Finch", "Omar Reyes"]
    f = {"payouts": [{"obligation_id": "o", "beneficiary_id": "b", "cost_minor": 1_200_000, "new_payee": False,
                      "unit_limit_minor": 1_000_000, "account_code": "US-200.OPS.6200"}], "payees": []}
    assert "unit_payment_limit_exceeded" in limit_reasons(f)
    f["payouts"][0]["cost_minor"] = 900_000
    assert "unit_payment_limit_exceeded" not in limit_reasons(f)


def test_spending_rolls_up_office_branch_project_account(env):
    org, pay, _ = env
    book(pay, "PO-2026-0102", "T1", 360_000, "EUR", "PRJ-900.PRJ900.6500", 1, base=390_000)
    book(pay, "PO-2026-0103", "partial", 100_000, "GBP", "UK-310.OPS.6100", 2, base=127_000)
    book(pay, None, None, 50_000, "USD", "US-210.OPS.6400", 3)
    s = procurement.spending(org, pay)
    u = {r["unit_id"]: r for r in s["by_unit"]}
    assert u["pf-eu-rollout"]["own_minor"] == 390_000 and u["co-de"]["with_units_below_minor"] == 390_000
    assert u["br-uk-manchester"]["own_minor"] == 127_000 and u["co-uk"]["own_minor"] == 0 and u["co-uk"]["with_units_below_minor"] == 127_000
    assert u["hq"]["with_units_below_minor"] == 390_000 + 127_000 + 50_000
    assert {p["project"]: p["base_cost_minor"] for p in s["by_project"]} == {"OPS": 177_000, "PRJ900": 390_000}
    assert {a["account"]: a["base_cost_minor"] for a in s["by_account"]} == {"6100": 127_000, "6400": 50_000, "6500": 390_000}
    open_ = {c["po_id"]: c["open_minor"] for c in s["open_commitments"]}
    assert open_["PO-2026-0102"] == 840_000 and open_["PO-2026-0101"] == 500_000


def test_executor_needs_an_approver_from_the_unit_chain(tmp_path, monkeypatch):
    import httpx

    from venthora.audit import AuditStore
    from venthora.execution.executor import Executor, terms_digest
    from venthora.execution.models import Approval, ExecutionRequest
    from venthora.gateways.airwallex import Airwallex
    from venthora.orchestrator import Orchestrator
    from venthora.proofcore.gate import ProofCore
    from venthora.testing.fake_airwallex import FakeAirwallex
    from venthora.testing.groundtruth import load_cases

    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "sandbox-key")
    base = TreasuryState.model_validate(next(c for c in load_cases("approvals") if c["id"] == "Q03")["input"]).model_dump()
    first = base["obligations"][0]
    first |= {"account_code": "UK-300.OPS.6100", "unit_payment_limit_minor": 1, "unit_approvers": ["Amira Shah"]}

    async def run(state, approvals):
        fake = FakeAirwallex(balances={"USD": "1000000", "EUR": "0", "GBP": "0"})
        client = httpx.AsyncClient(base_url="https://api-demo.airwallex.com", transport=httpx.MockTransport(fake.handler))
        agent = Orchestrator(ProofCore(airwallex=Airwallex(base_url="https://api-demo.airwallex.com", client=client, backoff_s=0)))
        ex = Executor(agent, ExecutionStore(str(tmp_path / f"e{len(approvals)}{state.obligations[0].unit_approvers[0][:2]}.db")),
                      AuditStore(str(tmp_path / "a.db")), poll_interval_s=0)
        try:
            plan = await agent.decide(state)
            d = terms_digest(plan)
            res = await ex.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash,
                                                    approvals=[Approval(approver=n, role=r, terms_digest=d) for n, r in approvals]))
            return plan, res
        finally:
            await agent.close()

    state = TreasuryState.model_validate(base)
    plan, refused = asyncio.run(run(state, [("Dana Finch", "finance_manager"), ("Omar Reyes", "cfo")]))
    assert "unit_payment_limit_exceeded" in next(s for s in plan.stages if s.engine == "Policy · limits").details["reasons"]
    assert refused.status == "REFUSED" and "must be approved by Amira Shah" in refused.message
    first["unit_approvers"] = ["Omar Reyes"]
    _, ok = asyncio.run(run(TreasuryState.model_validate(base), [("Dana Finch", "finance_manager"), ("Omar Reyes", "cfo")]))
    assert ok.status == "COMPLETED"
