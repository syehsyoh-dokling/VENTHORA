# VENTHORA: submission pack (Build Phase)

Everything the HackerEarth Build Phase form and the organisers ask for, ready to paste.
Requirements (airwallex.hackerearth.com FAQ): a working demo, a video walkthrough under five minutes, a repository
link with setup instructions.

## Title

VENTHORA: Mandate-Driven Autonomous Treasury & Payment Operations on Airwallex

## Track and kit

Treasury & Ops, Starter Kit 01 Adaptive Treasury Controller (Kit 03 Payment Ops Incident Commander on the same rails).
Awards in scope: Founder's Choice, Judges' Choice, Visa Award (Visa + Airwallex), Alta Award (Alta + Airwallex).

## Links

| What | Link |
|---|---|
| Live demo | https://app-venthora.muwahid.org (Airwallex sandbox; no real money moves) |
| Video walkthrough (short) | https://youtu.be/x8PyK0DiK-g |
| Full product tour (14 min) | https://youtu.be/lNEL_iDV1y8 |
| Repository | https://github.com/syehsyoh-dokling/VENTHORA (setup: README, Quick start) |
| Menu-by-menu guide | docs/WALKTHROUGH.md |

## One-line pitch

Every financial decision, proven before execution: an agent that decides what to pay, convert, defer or escalate when
cash is short, and moves money on Airwallex only after an independent gate proves the reserve floor, the policy and the
live bank state hold.

## Description (for the form)

**Problem.** A cross-border business rarely has cash for everything due this week. Today a treasury lead ranks bills by
hand across currencies, chases approvals by email, and finds out after the fact that a supplier changed bank details or
that a conversion breached the reserve.

**What VENTHORA does.**
- Reads invoices, bills, cash requests and purchase requests as they really arrive (email, PDF, Word, Excel, CSV, JSON,
  HTML), checks each against the vendor register, the purchase order and the budget, and quarantines what fails.
- Plans the next 72 hours against the cash above the reserve floor: what to fund now, the minimum FX conversion, what to
  defer and what to escalate to a person, ranked by priority. A deposit re-opens only the decisions it changes.
- Proves the plan before money moves (reserve floor, mandate, limits, FX exposure, four-eyes approvals, a 72-hour
  simulation of breach probability), then executes on Airwallex: FX quote, conversion, beneficiary check, transfer,
  each with a persisted `request_id`, so a lost response or a crash never pays twice.
- Reconciles afterwards: the book ledger, the bank's status, T:0 journal entries, an ISO 20022 `pain.001` validated
  against the official schema, a `camt.053` statement matched by EndToEndId.

**Airwallex APIs used (sandbox, live).** Balances, global accounts, simulated deposits, FX rates / quotes /
conversions, beneficiaries (schema, validation, name check), transfers and their simulated transitions, webhooks
(signed), Issuing (cardholders, virtual cards with authorization controls, remote authorization), payment intents,
connected accounts for each company on the platform.

**Sponsors.**
- Visa: Supplier Matching decides card vs bank transfer per supplier; Foreign Exchange Rates compares Visa's rate
  with the Airwallex quote; Trusted Agent Protocol keys published at `/.well-known/venthora-agent.json`.
- Alta: the audit-chain head and every contract chain head anchored on Alta Testnet; the agent paid for its own RPC
  endpoint over x402.
- Airi: staff cash advances funded to a card a person adds to Airi (CLI installed on the server).
- Claude: reads unstructured documents and drafts; every amount and limit stays in code, and a claim the email does
  not support is discarded.
- AWS: the live demo runs on EC2, with a PostgreSQL copy of the stores after every payment; Bedrock is a
  selectable model provider.

**Human control.** The FCO sets a mandate (reserve floor, limits, who approves what). The agent acts alone only inside
it; a new payee, a bank change (call-back on the registered number first), an over-claim or a breach goes to a
person. Every decision is on a hash-chained, HMAC-sealed audit log.

**Quality.** 1,158 automated tests (0 failing), 323 hand-derived ground-truth cases, a 136-scenario matrix from the
extreme to the normal, and two independent code reviews with their fixes and open items listed
(docs/QA_REPORT_2026-11-15.md).

## How a judge can try it

1. Open the live demo and sign in with the judge account given in the form's notes.
2. Agent plan → Today's plan: the 72-hour line-up against the reserve floor.
3. Autopilot Console: the Kit 01 checklist and the five-step run.
4. Pending payment → Review: the evidence behind one held invoice.
5. Platform Setup → Blockchain Record: the Alta anchor and its explorer link.

## Built with

Python 3.13, FastAPI, OR-Tools CP-SAT, Open Policy Agent (Rego mirror of the policy), SQLite with a PostgreSQL copy,
lxml (ISO 20022 XSD), cryptography (Fernet, Ed25519), eth-account (Alta), Claude, AWS EC2 and Cloudflare.
