# Quality and control map

VENTHORA is a hackathon build on the Airwallex sandbox. It holds **no certification**, and this page does not claim
one. It maps the controls that exist in the code to the international standards a bank's reviewer would ask about,
names where each one lives, and lists what is still missing. Every row can be checked in the repository.

## ISO/IEC 27001:2022 (Annex A): information security controls

| Control | In VENTHORA | Where |
|---|---|---|
| A.5.3 Segregation of duties | The person who asks for a payment never approves it; a change is never approved by who made it; a call-back is recorded by a finance role, not the requester | `proofcore/approvals.py`, `api.py` (DoA run, transfers), `payees.py` |
| A.5.15 Access control | Roles decide every write; menus and endpoints are refused by role on the server, not only hidden in the page | `access.py`, `api.py` (`menu_access`, `_writer`) |
| A.5.17 Authentication information | Passwords hashed with scrypt; five wrong passwords in five minutes lock the email; a person sets their own password through a one-time link | `auth.py` |
| A.5.18 Access rights | A signed-in person's role and name replace whatever the request claims; API keys carry scopes | `api.py` (`sessions`, `api_keys`), `apiaccess.py` |
| A.8.5 Secure authentication | Session cookie is HttpOnly, SameSite=Lax and Secure over HTTPS; sign-in routes are rate limited per address; an optional second factor (a TOTP code, RFC 6238) that a person turns on for themselves: a code is never accepted twice, wrong codes lock the email, and it is not yet mandatory for approver roles | `hardening.py`, `totp.py`, `auth.py`, `tests/test_mfa.py`, `tests/test_gateway_login.py` |
| A.8.15 Logging | Every decision, approval, execution and refusal is appended to a hash-chained audit log; a changed entry breaks the chain | `audit.py`, `GET /api/audit` |
| A.8.16 Monitoring | Webhooks are verified by signature, then the object is re-read from Airwallex before anything is believed | `webhooks.py` |
| A.8.20 Network security | Security headers on every answer (CSP, HSTS over HTTPS, frame, sniffing, referrer, permissions) | `hardening.py` |
| A.8.24 Use of cryptography | Secrets are kept write-only (never returned by the API); the audit head and contract chains are anchored on Alta Testnet | `secret_values.py`, `metal.py` |
| A.8.25 Secure development | Lint and the test suite run on every push | `.github/workflows/ci.yml` |
| A.8.28 Secure coding | Typed models validate every request body; money is integer minor units, never floats | `domain/`, `masterdata.py` |

## Payment-specific controls (what a payments auditor checks)

| Risk | Control | Where |
|---|---|---|
| Payment to a fraudulent account | New or changed bank details are checked at Airwallex before they are kept; nothing is paid until a call-back on the **registered** number; a bank change starts a cooling period | `bankcheck.py`, `payees.py` |
| Paying twice | One payout claim per invoice across executions; a persisted `request_id` per operation; an `Idempotency-Key` on writes | `execution/store.py`, `execution/executor.py`, `api.py` (`idempotency`) |
| Spending below the reserve | The plan is proven against the reserve floor before execution and checked again on quoted rates | `proofcore/`, `execution/executor.py` |
| Spending past the budget | Each payment with a charge code must fit its budget allocation, checked before planning and again when money moves | `plan_gates.py`, `budget_allocation.py`, `execution/executor.py` |
| Unapproved first payment | The first payment to a newly verified payee always waits for a person | `autonomy.py`, `payees.py` |
| Amount above what the bank accepts | A refused amount is remembered per currency and method; the next plan asks for a split | `plan_gates.py` |

## ISO/IEC 25010: product quality

| Characteristic | Evidence | Gap |
|---|---|---|
| Functional suitability | Kit 01 end to end on the Airwallex sandbox; hand-derived ground-truth cases (`docs/GROUND_TRUTH.md`) | — |
| Reliability | Crash and resume with the same `request_id`; lost-response recovery; fail-closed gates | One process, SQLite stores |
| Security | The tables above | No external penetration test |
| Maintainability | Lint in CI; typed models; one module per control | `api.py` and the dashboard page are single large files |
| Performance efficiency | Planning in well under a second in process | The live plan endpoint takes seconds because it reads the bank and re-plans on each call |
| Portability | Dockerfile (non-root, healthcheck), `docker-compose.yml` | — |

## ISO 20022: payment messaging

VENTHORA pays through the Airwallex REST API, and Airwallex speaks to the banking rails. For a bank or an auditor that
works in ISO 20022, two messages are built (`iso20022.py`, `tests/test_iso20022.py`):

| Message | Endpoint | What it does |
|---|---|---|
| `pain.001.001.09` Customer Credit Transfer Initiation | `GET /v1/iso20022/pain.001?date=YYYY-MM-DD` | The payments made on one day: a block per currency and date, one credit transfer per payment, the obligation id as the `EndToEndId`, creditor account as IBAN or account number with BIC or clearing code |
| `camt.053` Bank-to-Customer Statement (any version) | `POST /v1/iso20022/camt.053` | Reads the statement and reconciles its debits with VENTHORA's payments by `EndToEndId`, amount and currency: matched, a different amount, on the statement only, paid but not on the statement |

Limits, stated plainly: the files follow the published message structure and are checked by VENTHORA's own tests; they
have **not** been validated against the official XSD files or accepted by a bank. `pain.001` is exported as a record of
what was paid through Airwallex, not sent to a bank as an instruction. There is no page for it in the dashboard yet;
it is an API.

## Evidence: the test behind each control

| Control | Test file |
|---|---|
| Segregation of duties, approval matrix | `tests/test_approvals.py` |
| Payee lifecycle, call-back on the registered number, cooling | `tests/test_payees.py` |
| Bank check before details are kept or paid | `tests/test_beneficiaries.py`, `tests/test_review_validation.py` |
| One payout per invoice, resume and lost-response recovery | `tests/test_execution.py` |
| Reserve floor proven before execution | `tests/test_kit01_flow.py`, `tests/test_autonomy.py`, `tests/test_simulation.py` |
| Budget allocation at planning and at execution | `tests/test_plan_gates.py`, `tests/test_budget_allocation.py` |
| Transfer limit learned and respected | `tests/test_plan_gates.py` |
| A customer's deposit tops up the book once | `tests/test_book_funding.py` |
| Limits and velocity | `tests/test_limits.py` |
| Book ledger, a book never spends another's money | `tests/test_ledger.py` |
| Tenant and book segregation | `tests/test_tenancy.py` |
| Security headers, Secure cookie, rate limit | `tests/test_hardening.py` |
| Role-based access to menus and endpoints | `tests/test_access.py` |
| ISO 20022 export and reconciliation | `tests/test_iso20022.py` |

A test that no longer matches the product would be listed with its reason in `tests/known_gaps.txt`; the list is empty.

## What is missing for a production bank deployment

- An external penetration test and a formal threat model.
- A managed database with replication instead of SQLite files on one host.
- Structured application logs shipped off the host, with alerting.
- The backup schedule installed and a restore rehearsed on the live server (the procedure and a tested script exist: [OPERATIONS.md](OPERATIONS.md)).
- ISO 20022 files validated against the official XSD and accepted by a bank (the export and the statement import exist; see above).
- Independent review of the approval matrix and limits by a finance control owner.
