**To:** Airwallex hackathon / developer support team
**Subject:** Thank you, transfers work again; three requests and five questions until Demo Day (VENTHORA)

Hello Airwallex team,

Thank you for fixing the transfer fee configuration on our sandbox account ("Sandbox Business"). We re-tested on 5 Nov 2026, and everything we use works:

- USD, GBP and IDR transfers (the IDR one funded in USD) are created and reach PAID by simulation. Transfers `84dd0057…`, `d1a6a757…` and `aafbe806…`; the IDR transfer stuck yesterday (`96c776f4…`) is now PAID too.
- A simulated deposit settles within about 30 seconds (deposit `d58b7ff4…`).
- Our agent runs end to end on the sandbox: plan, quote, payout, settlement, reconciliation to the cent.

So that we don't have to write again, here is everything we still need from you until Demo Day (19 Nov).

## Four requests

0. **Card issuance on our sandbox (blocking).** We can create a cardholder: DELEGATE `09d0f098-84d2-495d-80c5-b7a72e36349f` is READY. But every `POST /api/v1/issuing/cards/create` for a virtual commercial card is refused:
   - without `program.type`: `invalid_request` "Invalid issuance details";
   - with `program.type` PREPAID, DEBIT or CREDIT: "Your account is not allowed to set "…" card type, please reach out to our support team about issuing setting for your account."

   Could you set up the issuing program for our sandbox account (virtual **Visa** commercial cards), and tell us which `program.type` to use?

1. **Keep our sandbox as it is until 19 Nov 2026.** That means the account, the credentials and the transfer fee configuration: no reset, no expiry. *Why: the final demo runs live on this sandbox.*
2. **A contact for Demo Day** (a Slack handle or a channel) in case the sandbox stops answering during the live demo.
3. **Remote authorization for Issuing on our sandbox, if it is not on yet.** This is the webhook where we approve or decline each card charge before it completes. *Why: we are entering the Visa Award. Software subscriptions will be paid with an Airwallex virtual card, and our agent must check every charge against its mandate, as it does for transfers. Issuing already answers on our sandbox (cardholders, cards, authorizations).*

## Five questions (only you can answer them)

4. **Card network:** are the virtual cards our sandbox issues **Visa** cards? *Why: the Visa Award needs Visa tooling together with Airwallex.*
5. **Visa Intelligent Commerce:** can an Airwallex-issued Visa card be tokenised for Visa Intelligent Commerce, so the agent never holds the full card number? *Why: that is how we would join the card flow to Visa's agent tooling.*
6. **Rate limits:** our agent reads the balance every 30 seconds and re-plans every 5 minutes. Is that within the sandbox limits for a live demo?
7. **Fees before a transfer:** is there a way to know a transfer's fee before creating it? *Why: an IDR payout was charged USD 5.00, which our plan only learned afterwards.*
8. **Source currencies:** a transfer funded in IDR returns error `016` with 27 allowed `source_currency` values. Is that list the same for every account and corridor, and is there an endpoint that returns it? *Why: our agent decides from it which payouts it pays from USD.*

Thank you very much.

Best regards,
VENTHORA team
