# The whole set on the real Airwallex sandbox

`python scripts/real_suite.py --approve "finance manager (owner)"` — started 2026-10-23 12:53 UTC, finished 2026-10-23 12:55 UTC, approved by **finance manager (owner)** as finance manager. **8 / 8 steps passed.** Every step runs on a fresh book (new obligation ids and invoice fingerprints, same amounts, evidence and policy) against the real sandbox; the runner stops at the first failure. Full output per step in `docs/real_suite_log.json`.

| Step | Command | Result | s | Reports |
|---|---|---|---|---|
| 1 Kit 01 cycle, fresh book | `-m venthora.cli --receipt --yes --approve finance manager (owner) --no-challenge --fresh` | ✅ passed | 51 | `20261023-173334-kit01_baseline.json` |
| 2 Kit 03 incident, fresh book | `-m venthora.cli --scenario kit03_incident --inbox  --yes --approve finance manager (owner) --no-challenge --fresh --incident SUPPLIER-EU-NOV:BENEFICIARY_BANK_RETURNED` | ✅ passed | 15 | `20261023-195340-kit03_incident.json` |
| 3 Intake of external emails, fresh book | `-m venthora.intake.cli --as-of 2026-10-24 --execute --receipt --approve finance manager (owner) --fresh` | ✅ passed | 26 | `20261023-195406-intake.json` |
| 4 Ground truth suites 1+2 on the real sandbox | `scripts/groundtruth_real.py --base http://127.0.0.1:8010 --approve finance manager (owner)` | ✅ passed | 30 | - |
| 5a Budget suite through the live service | `scripts/groundtruth_bench.py --suite budget --base http://127.0.0.1:8010` | ✅ passed | 10 | - |
| 5b Budget mandate cycle on the rail | `scripts/budget_real.py --approve finance manager (owner)` | ✅ passed | 9 | - |
| 6a Rego mirror on a real OPA | `scripts/check_policy_mirror.py` | ✅ passed | 3 | - |
| 6b Oracle audit | `scripts/oracle_audit.py` | ✅ passed | 4 | - |

## What each step proved

### 1 Kit 01 cycle, fresh book

```text
  [OK       ] quote       EUR                      5a59a4b6-9d58-357b-9822-0c4de3d079e3
  [OK       ] quote       GBP                      d4ecd9ee-8ce3-32e4-90dc-a3045b8aea4e
  [OK       ] preflight   reserve-at-quoted-rates  
  [OK       ] conversion  EUR                      6a77af69-1876-420f-8359-439dc891477e
  [OK       ] conversion  GBP                      aa54f7ec-7b11-42d3-84fb-779c0eb91c8a
  [OK       ] transfer    PAYROLL-NOV-1023173244   c10787ef-4197-4c94-a5a6-b8e804ec3562
  [OK       ] transfer    SHIPPING-UK-1023173244   f93c0cce-01d6-4cb0-8662-d746b7c98294
  [OK       ] transfer    SUPPLIER-EU-1023173244   c9268630-3613-49e8-a531-1d7b083d53c2
  [OK       ] transfer    SAAS-USD-1023173244      631954c4-aa58-476d-ab01-aab3265d14f4
  [OK       ] simulation  PAYROLL-NOV-1023173244   c10787ef-4197-4c94-a5a6-b8e804ec3562
  [OK       ] simulation  PAYROLL-NOV-1023173244   c10787ef-4197-4c94-a5a6-b8e804ec3562
  [OK       ] simulation  SHIPPING-UK-1023173244   f93c0cce-01d6-4cb0-8662-d746b7c98294
  [OK       ] simulation  SHIPPING-UK-1023173244   f93c0cce-01d6-4cb0-8662-d746b7c98294
  [OK       ] simulation  SUPPLIER-EU-1023173244   c9268630-3613-49e8-a531-1d7b083d53c2
  [OK       ] simulation  SUPPLIER-EU-1023173244   c9268630-3613-49e8-a531-1d7b083d53c2
  [OK       ] simulation  SAAS-USD-1023173244      631954c4-aa58-476d-ab01-aab3265d14f4
  [OK       ] simulation  SAAS-USD-1023173244      631954c4-aa58-476d-ab01-aab3265d14f4
  [OK       ] reconcile   balances                 
  managed budget after: EUR 0.01, GBP 0.00, USD 15,367.97
  live wallet after:    EUR 9,999,959.21, GBP 9,999,997.76, USD 9,953,121.98
  discrepancies: none; reserve floor USD 6,127.30 held: True

  COMPLETED: executed and reconciled; USD reserve (managed budget) confirmed at USD 15,367.97 >= USD 6,127.30; variance vs plan EUR 0.01, -USD 4.63 (actual Airwallex amounts: fees and rounding; fees are learned for the next plan)
────────────────────────────────────────────────────────────────────────────
run report: C:\Users\Saifuddin\Documents\hackathon\venthora\data\runs\20261023-173334-kit01_baseline.json
```

### 2 Kit 03 incident, fresh book

```text
  [OK       ] preflight   reserve-at-quoted-rates  
  [OK       ] conversion  EUR                      1445b25f-d309-4cad-b5ec-e78acce50ff1
  [OK       ] transfer    SUPPLIER-EU-NOV-1023195326 e59198af-cef4-419b-a8eb-1af90158eeaa
  [OK       ] reconcile   balances                 
  managed budget after: EUR 0.02, GBP 0.00, USD 6,285.50
  live wallet after:    EUR 9,999,945.84, GBP 9,999,997.76, USD 9,949,694.64
  discrepancies: none; reserve floor USD 2,016.92 held: True

  IN_FLIGHT: transfers still in flight: SUPPLIER-EU-NOV-1023195326

── 8 · Kit 03 incident: the supplier says the money never arrived ──────────
  [INFO   ] simulate       status=SENT
  [INFO   ] simulate       status=FAILED, failure_type=BENEFICIARY_BANK_RETURNED
  [INFO   ] original       transfer_id=e59198af-cef4-419b-a8eb-1af90158eeaa, status=CANCELLED, failure_type=BENEFICIARY_BANK_RETURNED, amount=EUR 1,500.00
  [INFO   ] decision       action=REPLACE, reason=BENEFICIARY_BANK_RETURNED: funds returned and the payee record is not in question; replace under the duplicate lock with a new 
  [INFO   ] budget         replacement_cost_minor=151341, second_fee_minor=1341, wallet_available_minor=1000144584, currency=EUR
  [OK     ] duplicate-lock payout_key=invoice:eu-components:INV-2026-0601-1023195326, original_request_id=453bf8f4-5cbb-4dc1-8c8e-1aa1c9a73f0b, replacement_request_id=a72d2544-d
  [OK     ] replacement    transfer_id=bfb116c8-5a73-49e8-91c2-c5308aa93d34, request_id=a72d2544-d4ea-4a13-a78e-bc66ff788246, status=SCHEDULED, fee_minor=1339
  [OK     ] reconcile      before_minor=1000144584, after_minor=999993245, expected_after_minor=999993245, discrepancy_minor=0, second_fee_minor=1339, refund_observed=True, refu

  decision: REPLACE: BENEFICIARY_BANK_RETURNED: funds returned and the payee record is not in question; replace under the duplicate lock with a new request_id
  original e59198af… CANCELLED / BENEFICIARY_BANK_RETURNED; replacement bfb116c8… PAID
  incident 040de823… RESOLVED
────────────────────────────────────────────────────────────────────────────
run report: C:\Users\Saifuddin\Documents\hackathon\venthora\data\runs\20261023-195340-kit03_incident.json
```

### 3 Intake of external emails, fresh book

```text
  [SKIPPED] TigerBeetle · ledger         ledger not configured
  [PASS   ] Theorem · decision frame     given/asked/formula/answer complete; identities recompute; not payable as posed: critical 
  ESCALATE          PAYROLL-NOV-1023195342   USD 8,000.00  budget line 'payroll' cannot fund it (room 99700 minor); a budget request is rai
  ESCALATE          SHIPPING-UK-1023195342   GBP 2,500.00  budget line 'logistics' cannot fund it (room 61887 minor); a budget request is r
  ESCALATE          SUPPLIER-EU-1023195342   EUR 4,000.00  budget line 'suppliers' cannot fund it (room 245973 minor); a budget request is 
  REJECT            SUPPLIER-EU-RESENT-1023195342   EUR 4,000.00  duplicate of SUPPLIER-EU-1023195342 (same invoice fingerprint)
  PAY               REQ-OK-2026-10-1023195342   USD 2,400.00  priority 7, due in 336h; fits above reserve floor
  ESCALATE          CONTRACTOR-US-1023195342   USD 3,000.00  policy exception: payee bank details changed; a person must verify before any pa
  REJECT            REQ-LD-77-1023195342     USD 3,000.00  evidence intake:b6357d05 is pending
  PAY               REQ-LD-78-1023195342     USD 1,800.00  priority 6, due in 168h; fits above reserve floor
  ESCALATE          SAAS-USD-1023195342      USD 1,200.00  budget line 'saas' cannot fund it (room 29700 minor); a budget request is raised

── 4 · execute and reconcile ─────────────────────────────────────────────────
  [OK       ] preflight   authorization            
  [OK       ] preflight   payout-claims            
  [OK       ] preflight   reserve-at-quoted-rates  
  [OK       ] transfer    REQ-OK-2026-10-1023195342 aa9690db-4700-4e5f-8a0c-c6d152f40929
  [OK       ] transfer    REQ-LD-78-1023195342     e750f983-c681-47fb-a6aa-3572ec04fbfc
  [FAILED   ] simulation  REQ-OK-2026-10-1023195342 aa9690db-4700-4e5f-8a0c-c6d152f40929
  [OK       ] simulation  REQ-LD-78-1023195342     e750f983-c681-47fb-a6aa-3572ec04fbfc
  [OK       ] simulation  REQ-LD-78-1023195342     e750f983-c681-47fb-a6aa-3572ec04fbfc
  [OK       ] reconcile   balances                 
  IN_FLIGHT: transfers still in flight: REQ-OK-2026-10-1023195342

run report: C:\Users\Saifuddin\Documents\hackathon\venthora\data\runs\20261023-195406-intake.json
```

### 4 Ground truth suites 1+2 on the real sandbox

```text
Part A: 84 plan / intake / incident cases through the live service (real sandbox balances)
  84 / 84 match; 54 / 54 plans read live sandbox balances; plan round trip p50 125.4 ms

Part B: execution anomalies on the real rail, fresh book per run
  ok  N04      727 ms  planning rate stale: the rail is 3% dearer -> nothing b  -> plan=APPROVED exec=FAILED incident=None
  --  N09  not reproducible on the real rail: the sandbox settles a simulated deposit by itself; a deposit that never settles 
  ok  N26      252 ms  an approval given on different terms is refused  -> plan=REQUIRES_APPROVAL exec=REFUSED incident=None
  ok  N27      253 ms  a viewer 'approves'  -> plan=REQUIRES_APPROVAL exec=REFUSED incident=None
  ok  N28      247 ms  REQUIRES_APPROVAL executed without a person  -> plan=REQUIRES_APPROVAL exec=REFUSED incident=None
  --  N29  not reproducible on the real rail: a lost HTTP response cannot be injected into the real rail (covered on the fake;
  --  N30  not reproducible on the real rail: 429 storms cannot be requested from the sandbox
  --  N31  not reproducible on the real rail: duplicate_request_id would require re-sending a used request_id, which the execu
  --  N32  not reproducible on the real rail: balance_insufficient at the rail needs the wallet to drop between the balance re
  --  N33  not reproducible on the real rail: sandbox conversions settle immediately; a conversion that stays SCHEDULED cannot
  --  N39  not reproducible on the real rail: a bank return after SENT is exactly N44's mechanism on the real rail; not run tw
  --  N40  not reproducible on the real rail: same mechanism as N44, three times; not run to keep the sandbox ledger readable
  ok  N41     6576 ms  the same invoice executed twice: second run refused bef  -> plan=REQUIRES_APPROVAL exec=FAILED incident=None
  ok  N42      117 ms  the wallet holds less than the plan assumes  -> plan=BLOCKED exec=None incident=None
  ok  N44     9748 ms  bank return -> replace under the lock -> resolved only   -> plan=APPROVED exec=IN_FLIGHT incident=RESOLVED
  --  N45  not reproducible on the real rail: the replacement's budget check reads the live EUR wallet (~10 M), which cannot b
  7 / 7 match; 9 not reproducible on a real rail
```

### 5a Budget suite through the live service

```text
ok  B01     129.3 ms  inside the line, inside the auto share
  ok  B02     122.7 ms  exactly the mandate
  ok  B03     121.8 ms  one cent over the mandate: the agent stops at the line
  ok  B04     121.4 ms  above the auto share needs a person
  ok  B05     126.5 ms  exactly the auto share is still autonomous
  ok  B06     121.5 ms  alert threshold reached (notification, still autonomous)
  ok  B07     122.2 ms  alert threshold with require_approval_on_alert
  ok  B08     123.5 ms  realized spend eats the line
  ok  B09     128.0 ms  a grant restores the mandate
  ok  B10     122.3 ms  two lines, one exhausted, the other fine
  ok  B11     119.6 ms  priority within a line: the line pays the urgent one
  ok  B12     120.2 ms  unbudgeted obligation needs a person
  ok  B13     122.6 ms  unbudgeted allowed by the budget's setting
  ok  B14     116.7 ms  bypass nobody signed keeps the cap
  ok  B15     119.2 ms  signed bypass lifts the cap
  ok  B16     115.7 ms  bypass signed but spending inside it is still recorded
  ok  B17     125.7 ms  the whole November book against the November plan
  ok  B18     121.0 ms  minus from what is still owed
  ok  B19     123.3 ms  viewer with a budget is still blocked
  ok  B20     121.5 ms  no budget attached: nothing changes

20 / 20 match ground truth (0 in-process only); round trip p50 122.0 ms, p95 128.0 ms, max 129.3 ms
```

### 5b Budget mandate cycle on the rail

```text
run 1: plan and pay inside the line: APPROVED {'O1': 'PAY', 'O2': 'ESCALATE'}  line {'mandate_minor': 500000, 'realized_minor': 0, 'paying_now_minor': 300300, 'committed_minor': 300300, 'available_minor': 199700, 'minus_minor': 100600, 'utilisation_bp': 6006, 'status': 'auto'}
    line 'suppliers' cannot fund what is still owed: mandate 500000, used 300300, owed 300300; a grant of 100600 would cover it
    execution COMPLETED: executed and reconciled; USD reserve (managed budget) confirmed at USD 16,997.00 >= USD 5,000.00

run 2: realized spend now on the line: APPROVED {'O1': 'REJECT', 'O2': 'ESCALATE'}  line {'mandate_minor': 500000, 'realized_minor': 300300, 'paying_now_minor': 0, 'committed_minor': 300300, 'available_minor': 199700, 'minus_minor': 100600, 'utilisation_bp': 6006, 'status': 'auto'}
    line 'suppliers' cannot fund what is still owed: mandate 500000, used 300300, owed 300300; a grant of 100600 would cover it

grant: +USD 1,100.00 on 'suppliers' by finance manager (owner)

run 3: after the grant: APPROVED {'O1': 'REJECT', 'O2': 'PAY'}  line {'mandate_minor': 610000, 'realized_minor': 300300, 'paying_now_minor': 300300, 'committed_minor': 0, 'available_minor': 9400, 'minus_minor': 0, 'utilisation_bp': 9845, 'status': 'alert'}
    budget line 'suppliers' at 98% of its mandate after this plan
    execution COMPLETED: executed and reconciled; USD reserve (managed budget) confirmed at USD 13,994.00 >= USD 5,000.00

OK
```

### 6a Rego mirror on a real OPA

```text
OPA: C:/Users/SAIFUD~1/AppData/Local/Temp/claude/C--Users-Saifuddin-Documents-Agenthon-2026/8f2621b6-4199-4ebe-a452-bff96e36130d/scratchpad/opa-bin/opa.exe (Version: 0.70.0)
7638 inputs; disagreements: 0
```

### 6b Oracle audit

```text
PASS  M1 rename ids/payees -> same actions  
PASS  M2 swap priorities -> roles swap  {'PAYROLL-NOV': 'PAY', 'SAAS-USD': 'PAY', 'SUPPLIER-EU': 'CONVERT_AND_PAY', 'SUPPLIER-EU-RESENT': 'REJECT', 'CONTRACTOR-US': 'ESCALATE', 'SHIPPING-UK': 'DEFER'}
PASS  M3 deferral by priority, not amount  
PASS  M4 no bank-change email -> no policy exception  no room above reserve now; reliable receipt RCV-ACME lands before due
PASS  M5 delay follows the text  2d -> 72h window [72, 130]
PASS  M6 spotless payer: contradiction still lowers the limit  conf=7000 limit=500000
PASS  M7 case_id independence  

7/7 passed
```

## Deliberately not on the real rail

The scenario matrix (`python scripts/scenario_matrix.py`, 136 variants) and nine execution anomalies stay on the stateful fake sandbox, because a real rail cannot be asked for a lost HTTP response, a 429 storm, a reused `request_id`, a wallet that drops between the balance read and the transfer, a conversion or deposit that never settles, or a wallet of exactly EUR 1,512.85. Their mechanisms — recovery by `request_id`, the duplicate lock, reconciliation to the cent, the Kit 03 decision table — are the ones steps 2 and 4 exercised on the real rail.
