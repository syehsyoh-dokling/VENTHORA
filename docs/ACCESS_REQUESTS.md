# Access requests (drafts, not sent)

Prepared 5 Nov 2026 for the items VENTHORA cannot finish alone. Each one names who must act, what to ask, and what
VENTHORA already has ready. Nothing here has been sent. Fill the `<…>` placeholders yourself; never paste a key.

## 0. Airwallex, all in one Slack reply (post in the thread of AIRWALLEX_FOLLOWUP.md)

> Hi again :wave: Three more things from VENTHORA, on top of the card program and remote authorization above:
>
> 1. **Connected accounts + platform payments** on our sandbox, so we can add Kit 06 (payroll for several employers,
>    each employer's funds kept apart; VENTHORA already keeps every book's money separate). Sandbox login email:
>    `<your sandbox email>`, Client ID: `<your client id>`.
> 2. **Airi CLI access (Kit 09)** for the Airi account `<your Airi account email>` (created at app.airi.com).
>    And a check: does Kit 09 place real orders on the Airwallex-issued card, and what budget does that card carry?
> 3. **Developer MCP server:** it asks for an interactive OAuth sign-in. Is there a server-to-server login for an app,
>    or is REST (one function per endpoint, as in the guide) the expected path? We use REST today.
>
> Happy to send the same by email to devhelp@airwallex.com if that's the formal route. Thank you!

## 1. Airwallex: connected accounts + platform payments (Kit 05, Kit 06)

**To:** devhelp@airwallex.com · **Why:** Kit 06 (multi-employer payroll) and Kit 05 (platform spend) need connected
accounts and platform payments, which only Airwallex can switch on (guide: "Ask for both").

> Subject: Sandbox: enable connected accounts and platform payments
>
> Hi Airwallex team, we are building VENTHORA for the Agentic Banking Hackathon (Kit 01 and Kit 03, live on the
> sandbox). We would like to extend it to Kit 06, payroll for several employers with each employer's funds kept
> apart. Could you enable **connected accounts** and **platform payments** on our sandbox?
>
> Sandbox login email: `<your sandbox email>` · Client ID: `<your client id>`
>
> VENTHORA already keeps every book's money apart (a book never spends another book's money) and runs payroll from
> its own register, so connected accounts would let each employer be a real separate wallet. Thank you!

**Ready in VENTHORA:** Reporting → 1 · Payroll; Books & ledger → Segregation (one book per employer); the executor's
per-book cash check. **To build once enabled:** `x-on-behalf-of` on balances, FX, beneficiaries and transfers;
`charges/create` for the platform fee.

## 2. Airwallex: Airi CLI allowlist (Kit 09)

**To:** devhelp@airwallex.com · **Why:** Kit 09 needs Airi CLI, which is allowlisted.

> Subject: Airi CLI access for the hackathon
>
> Hi, we'd like to try Kit 09 (approval-bound shopping) next to our treasury agent. Our Airi account email is
> `<your Airi account email>` (created at app.airi.com). Could you allowlist it for Airi CLI?

**Note:** Kit 09 places **real orders** at real merchants with an Airwallex-issued card. Decide first whether you
want that; VENTHORA's virtual card and Visa pages would host it (10 · Virtual card (Visa), Visa Intelligent Commerce).

## 3. Metal (Metal Award)

**Where:** the Metal Slack channel, as a reply to the message in `SPONSOR_FOLLOWUPS.md` §1.

> Update from VENTHORA: anchoring is built. Platform Setup → Metal creates our testnet address, and **Anchor now**
> writes the SHA-256 digest of our audit-log head and every contract chain head on Tahoe (chain id 381932) in one
> transaction. Anyone can recompute it from our records. Two questions: (1) does this count for the Metal Award, or
> do you expect value to move on Metal, e.g. Kit 11's mandates? (2) Kit 11's agent guides are behind the Metal
> console. Is `@metalntwx/metal-pay` available to us, and where are its docs?

**You must do:** claim test METAL from the faucet (captcha) for the address shown in Platform Setup → Metal.

## 4. AWS (credits, Bedrock)

**You must do:** in the AWS console (region of your choice, e.g. us-east-1): Bedrock → Model access → enable the
models you want, then Bedrock → API keys → create a key. Paste it in Platform Setup → Agent (AI & Model) → Amazon
Bedrock (it is write-only), and change the region in the base URL if it is not us-east-1.

**Question for the AWS Slack channel:**
> We use Bedrock as one of VENTHORA's model providers (Converse with a Bedrock API key). Does that count toward the
> AWS credits, or do you expect Bedrock AgentCore? If AgentCore, which part fits best: Runtime to host the agent,
> or Gateway to expose VENTHORA's typed tools?

## 5. Coinbase CDP (stablecoin infrastructure)

No request is needed to start: CDP keys are self-serve at portal.cdp.coinbase.com. **Open question before building:**
VENTHORA pays in fiat on Airwallex; a stablecoin leg would only make sense for a payee that wants USDC. Ask in the CDP
Slack whether a hackathon project that only *receives* or *reports* stablecoin balances is in scope, before any work.

## 6. AI accounting partner (MCP server, sandbox)

**Ready in VENTHORA:** Books & ledger → **Export journal (CSV)**: every line as debit or credit with an account code
(1010 cash at bank, 1050 payments in transit, 6000 payments, 6950 bank fees, 7900 FX, 3000 funding).
**Question for their Slack:** which import format and chart of accounts their sandbox expects, so the account codes
can be mapped instead of fixed.
