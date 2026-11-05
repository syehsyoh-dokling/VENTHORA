# Draft v1: full test report (5 Nov 2026)

Run on a **copy** of the demo data (a separate server on port 8011, schedulers off), so the demo was never changed.
Airwallex calls went to the real sandbox.

## What was tested, and the result

| Test | Result |
|---|---|
| Every API endpoint | 183 routes (87 GET, 91 POST, 21 PUT, 8 DELETE). All 74 GETs without parameters answer: 73 × 200, 1 × 422 that is correct (`/v1/org/account-code` needs its `code`). Slowest: `/v1/metal` 2.5 s (it reads the testnet), `/v1/agent-plan` 2.0 s |
| 136-scenario matrix | **136 / 136** variants satisfy every invariant (normal, cash, obligations, FX, receivables, policy, inbox, rail, 37 Kit 03 incidents, load, roles, recalculation) |
| Automated tests | full suite **918 passed** before the fixes below (the run after the fixes is listed at the end) |
| Data in: uploaded people file | 2 people created (after fix 2) |
| Data in: uploaded vendor list with bank | 1 vendor created, bank completed (after fix 3) |
| Data in: employee and consultant by API | both registered NEW, waiting for the bank call-back on the registered number |
| Data in: pulled from another system's API | ERP connector: discover finds every entity, sync runs for all of them |
| Data in: invoice by email | read, sender checked against the registered mailbox, amount and invoice quoted; status decided by the rules (see question B1) |
| Airwallex account | connected; 5 global accounts |
| Webhooks | subscribed, 36 confirmed events, the last at 02:52 UTC today |
| AI model | Claude Opus 5.5 answers in 1.6 s |
| Airwallex Cards | cardholder READY; 0 cards (blocked, A1) |
| Visa TAP | key published; public file reachable through the tunnel again (fix 1) |
| Airwallex MCP docs server | **connected**, 2 tools; a real question answered (GB LOCAL beneficiary fields) |
| Airwallex MCP developer server | 401: needs an OAuth sign-in (VENTHORA uses REST, as the guide allows) |

## C. Fixed directly (and re-tested)

| # | Found | Fixed |
|---|---|---|
| 1 | The webhook gateway had run since 4 Nov on old code: the Visa TAP public key (`/.well-known/venthora-agent.json`) answered 404 on the public address | Restarted the gateway only (the tunnel and the Airwallex subscription unchanged): now 200; unsigned webhooks still refused |
| 2 | An uploaded people file with job titles ("staff", "Finance Manager") was rejected row by row | A title becomes the nearest VENTHORA role, else requester (the least: may only ask); **a file can never grant an admin role**; the title is kept as the job title |
| 3 | An uploaded vendor list without "account name" or "account currency" was rejected | A company's account takes its own name, currency and country when the file leaves them out |
| 4 | Freezing a card froze it only in VENTHORA (the guide asks for `POST /issuing/cards/{id}/update`) | Freeze now sets the card INACTIVE on Airwallex too (unfreeze: ACTIVE); if Airwallex cannot be reached it stays frozen in VENTHORA and says so |
| 5 | The Airwallex MCP servers were not in Platform Setup | Platform Setup → **Airwallex MCP**: both servers' status, the docs tools, and "Ask the Airwallex docs" (only the question is sent) |

## A. Questions for the organisers and sponsors

| # | To | Question |
|---|---|---|
| A1 | Airwallex | Card issuance is refused for every `program.type` ("Your account is not allowed…"), and remote authorization is not enabled (`remote_auth_settings` empty). Can both be enabled for our sandbox? (already in AIRWALLEX_FOLLOWUP.md) |
| A2 | Airwallex | Connected accounts and platform payments for Kit 05/06; Airi CLI for Kit 09 (drafts in ACCESS_REQUESTS.md) |
| A3 | Airwallex | The developer MCP server needs an interactive OAuth sign-in. Is there a server-to-server login, or is REST the expected path for an app? |
| A4 | Visa | VIC access (onboarding, about 10 business days) and the award criteria: do TAP + Airwallex virtual cards count? (VISA_QUESTIONS.md) |
| A5 | Metal | **The faucet's test METAL never arrived**: VENTHORA's address `0xd64E00faEBe7B1cC519F0A41e05de29B3A0E5Fa6` shows 0 on the RPC and on the explorer, and the Tahoe C-Chain's last block is from **29 Oct** (block 2,014, nothing since). Is Tahoe the right network, is the faucet working, and could they fund the address? Also: does anchoring count for the Metal Award, and where are Kit 11's `@metalntwx/metal-pay` docs? | **Resolved 6 Nov:** wrong network (Tahoe is Metal Blockchain, a different project); moved to Metal Testnet, chain 63825; x402 RPC purchase and the first anchor succeeded.
| A6 | AWS | Does Bedrock as a model provider count, or do they expect AgentCore? |
| A7 | AI accounting partner | Which import format and chart of accounts? (the journal export is ready) |

## B. Needs your direction

| # | Decision | Why |
|---|---|---|
| B1 | ~~Demo email 01 (Lumen LD-78) lands in Needs review~~ **Done (your choice: add a PO):** the email now quotes PO-2026-0101; it is VERIFIED, matched as term T1 "Phase 1 on approval", USD 500.00 left on the PO | — |
| B2 | Email Sync has **no inbox** set up | Invoices come in only through the inbound-email webhook. Connect a real mailbox (IMAP / Gmail) or keep it as it is |
| B3 | Outgoing email (SMTP) is **not set up** | Vendor notices stay queued with a mailto link. Give an SMTP app password, or keep it so |
| B4 | The webhook address is a Cloudflare **quick tunnel** | Its address changes when cloudflared restarts, and the Airwallex subscription would then point nowhere. A named tunnel or a fixed domain keeps it stable for Demo Day |
| B5 | Visa VIC keys, the Bedrock API key | Only you can create them (Visa Developer, AWS console) |
| B6 | Kit 09 places **real orders** with a real card | Decide before asking for Airi CLI |
| B7 | Cash placement counts approved bills and open commitments separately, so a bill may be counted twice | It errs on the safe side (more cash kept). Keep, or de-duplicate |
| B8 | The repository is **private**; the video is not recorded | Needed for the submission by 13 Nov |

## After the fixes

- Re-run of the input test on a fresh copy: people 19 → 21, vendors 6 → 7, employee and consultant registered; every route answers.
- New tests: `test_import_gaps.py`, `test_card_freeze.py` (plus today's `test_placement_metal_bedrock.py`).
- Full suite after the fixes: **922 passed**, 0 failed (6 min).
