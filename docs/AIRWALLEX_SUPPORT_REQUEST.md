**To:** Airwallex hackathon / developer support team
**Subject:** Sandbox: every transfer now fails with `fee_unknown` (plus 3 questions) — VENTHORA, Agentic Banking Hackathon

Hello Airwallex team,

We are building VENTHORA, an autonomous treasury agent, for the Airwallex Agentic Banking Hackathon (submission due 13 Nov 2026). Our sandbox account is **"Sandbox Business"**. Since the evening of 4 Nov 2026 (UTC), every transfer we create fails with `fee_unknown`. We need your help to unblock it. Below: what already works, what is blocked, and the IDs to trace it.

## 1. Blocked: every transfer fails with `fee_unknown`

`POST /api/v1/transfers/create` returns HTTP 400:

```
{"code": "fee_unknown", "message": "There is a problem with your transfer fee configuration. Please contact your Account Manager."}
```

- **Since:** about 22:30 UTC on 4 Nov 2026. It is still happening on 5 Nov 2026 (UTC).
- **It fails for every corridor we tried**, including ones that worked before:

| What | Source → transfer currency | Beneficiary ID | Result |
|---|---|---|---|
| US vendor, LOCAL | USD → USD (1.00) | `429fd3d5-3e03-4e6e-ad16-ff6c3a0ac356` | `fee_unknown` (worked on 23 and 29 Oct) |
| UK vendor, LOCAL | USD → GBP (1.00) | `900137b2-6b09-4269-9fd1-6f19c19fcf71` | `fee_unknown` |
| Indonesian personal account, LOCAL | USD → IDR (50,000 and 75,000) | `9f88052f-c770-4a65-a860-2b4c3f447e1a` | `fee_unknown` |

- **What we already checked:** the same request body that succeeded at 22:14 UTC (the IDR transfer in section 2) fails now when re-sent with a new `request_id`. Beneficiary validation (`/beneficiaries/validate`) and quotes (`/fx/quotes/create`) still succeed. No money moved on any failed attempt.

**Could you check the transfer fee configuration of our sandbox account and restore it?**

## 2. A transfer that went out just before the error, still PROCESSING

| Transfer ID | Short reference | Details | Status |
|---|---|---|---|
| `96c776f4-dc41-4874-8b7b-8d845e71d198` | P261104-E5KIJJY | request_id `idr-probe-usd-4`; USD → IDR 50,000 LOCAL; source USD 2.78 + fee USD 5.00; created 2026-11-04T22:14:07Z | PROCESSING |

**Could you confirm whether it will settle?** If PROCESSING is final for IDR in the sandbox, please tell us whether we can move it to PAID with `/simulation/transfers/{id}/transition`, as we do for USD and GBP.

## 3. What works in our sandbox (for context)

- **Authentication:** `/authentication/login` with token refresh.
- **Balances and rates:** `/balances/current` and `/fx/rates/current`.
- **Quotes:** `/fx/quotes/create`, including `mid_rate` and `awx_rate`. We use the mid rate to report the real cost of every exchange.
- **Conversions:** `/fx/conversions/create`, e.g. USD → GBP, conversion `f968e8a8-7bc1-4da0-9643-d223f4d8e247` (SETTLED, 29 Oct).
- **Beneficiaries:** schema, validate and create, for US (ABA), UK (sort code), DE (IBAN/SWIFT) and ID (local bank).
- **Transfers before 4 Nov:**
  - USD LOCAL: `3ed1cad8-49b9-4322-b373-9231e537c6d8` (P261023-RLWS7Y0) and `23800157-1608-4608-a32b-bf03204b4eaa` (P261029-5EEXATK).
  - GBP LOCAL: `a1be301a-72f2-4242-95a6-72df875f0470` (P261029-340D1X7).
  - EUR SWIFT: `5e333106-7d52-4197-b679-dcfede23721a` (P261023-RKY519C), fee EUR 13.37.
- **Sandbox simulation:** SENT → PAID, plus failure transitions (e.g. BENEFICIARY_BANK_RETURNED).
- **Global accounts, deposits and webhooks:** `/global_accounts`, `/deposits`, `/webhooks/create`; events are received live.

## 4. Three short questions

1. **Source currencies.** A transfer funded in IDR returns error `016` and lists 27 allowed `source_currency` values (AED, AUD, BRL, CAD, CHF, CNY, CZK, DKK, EUR, GBP, HKD, HUF, ILS, JPY, KRW, MXN, NOK, NZD, PHP, PLN, RON, SEK, SGD, THB, USD, VND, ZAR). Is this list fixed for all accounts, or specific to our account or corridor? Is there an endpoint that returns it?
2. **Funding a payout from another currency.** For a payout in a currency that is not a source currency (IDR, INR, ...), is funding the transfer in USD and letting Airwallex convert the recommended approach? Or should we hold a balance in that currency through a conversion first?
3. **Fees and amounts.** Is there an endpoint to quote the transfer fee before creating a transfer? Our IDR LOCAL transfer was charged USD 5.00. Also, is the minimum transfer amount (USD 1 equivalent, error `038`) the same in production?

Thank you very much. Happy to share full request and response logs (without credentials) if useful.

Best regards,
VENTHORA team
