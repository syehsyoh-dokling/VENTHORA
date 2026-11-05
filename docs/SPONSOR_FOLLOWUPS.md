# Sponsor follow-ups (updated 6 Nov 2026, after the Metal Testnet test)

Tested on Metal Testnet before writing (6 Nov 2026; test tokens only):
- The earlier integration used **Metal Blockchain's Tahoe** (metalblockchain.org, chain 381932), a different project.
  Metal said "wrong chain"; VENTHORA now runs on **Metal Testnet** (docs.metalntwx.com, chain 63825).
- Faucet API `POST faucet.metalntwx.com/api/fund`: 100 USDZ to `0xd64E00faEBe7B1cC519F0A41e05de29B3A0E5Fa6` (no captcha).
- **x402**: the bare RPC answers 402; VENTHORA signed an EIP-3009 transfer of 1 USDZ with its own key and received
  its own RPC endpoint (`rpc-aas-endpoint` header) in 1.4 s, with no console sign-up. The endpoint URL is kept as a secret.
- Chain: about 15 blocks a second; gas price 1 base unit; an anchor uses 23,000 gas (a negligible fraction of a USDZ).
- **Anchor sent from the app**: one EIP-1559 transaction, to self, value 0, data = `VENTHORA-ANCHOR-1:` + SHA-256 of
  the audit-log head (212 events) and 10 contract heads. Status 1, final in about a second, visible on
  testnet.metalntwx.com: `0xb57d7e0d7bc8641e4238877793e4a532c97571f6764cf3893682e1028e03a4f6`.

## 1. Metal team (Metal Award, USD 15,000): paste into the Metal Slack channel

Thanks for the pointer! :raised_hands: We've moved VENTHORA to **Metal Testnet (chain 63825)**, and it works end to end:
- VENTHORA funds its address from the faucet API.
- It **buys its own RPC endpoint over x402**: 1 USDZ, EIP-3009, signed by the agent.
- It **anchors its audit trail**: one EIP-1559 transaction, value 0, carrying the SHA-256 digest of our audit-log head and every contract's hash-chain head.

The first one is on the explorer: `0xb57d7e0d7bc8641e4238877793e4a532c97571f6764cf3893682e1028e03a4f6`.

A few questions so we build the right thing for the Award:

1. **Anchoring pattern:** is a zero-value self-transaction with the digest in calldata the way you'd like it done? Or should we use `MetalAssertions` (`0x…018c`, a Merkle-root claim) or an event from a small contract?
2. **What counts for the Metal Award:** is anchoring plus the agent paying for its own RPC over x402 enough? Or do you want value moving on Metal? We could add a **USDZ / EURZ / GBPZ payout rail** beside Airwallex: VENTHORA would pay a vendor in a Metal stablecoin under the same approvals.
3. **Mandates (Kit 11):** VENTHORA already enforces a Delegation of Authority: who approves, per-payment and monthly limits, allowed payees. Should we mirror it as a **Metal mandate**, so the limits are enforced on chain when the agent pays? Is there a testnet example of `@metalntwx/metal-pay` mandates from Python, or only TypeScript?
4. **The x402-bought endpoint:** does it expire, and does its credit last until Demo Day (19 Nov)? Can its owner address see or manage it in the console, or is the console needed at all?
5. **Testnet stability:** will the testnet, faucet and explorer stay up, with no reset, until judging ends? Is there an explorer API (or a verify page) we can link judges to, so they can check the digest in a transaction's input themselves?

Thanks! :pray:

## 2. AWS team (credits USD 50,000): paste into the AWS Slack channel

Hi AWS team :wave: VENTHORA here, an autonomous treasury agent on Airwallex (Kits 01 and 03). Development is done locally and runs end to end on the Airwallex sandbox. Next we'd like to deploy on AWS, so the judges get a stable live URL for Demo Day (19 Nov). A few questions:

1. **Account and credits:** how do we get the hackathon AWS credits, or an AWS account to deploy on? What do you need from us (email, team name)?
2. **Recommended setup:** the app is one Python (FastAPI) service with SQLite files, plus a small webhook gateway. Would you suggest App Runner, ECS Fargate, or a single EC2 or Lightsail instance? We need a **fixed HTTPS URL**, because Airwallex sends webhooks and card-authorization calls to it.
3. **Bedrock or AgentCore:** VENTHORA already supports Amazon Bedrock as a model provider (the Converse API with a Bedrock API key). Does that count on the AWS side, or do you expect Bedrock AgentCore (Runtime to host the agent, or Gateway to expose its typed tools)? Which models are enabled for hackathon accounts (Claude on Bedrock?), and in which region?
4. **Email:** can we use Amazon SES to receive vendor invoices and send vendor notices? Inbound needs a verified domain: is one available to teams, or should we bring our own?
5. **Secrets:** are Secrets Manager or SSM Parameter Store included, for the Airwallex sandbox keys?
6. **After Demo Day:** anything to know about credit limits, or teardown after 19 Nov?

Thanks a lot! :pray:

## 3. Claude (credits USD 10,000)

Hi :wave: How do we claim the hackathon **Claude API credits**? Our agent uses Claude to read invoices and emails and to draft plans. Thanks!

## Done since the first drafts
- Metal: moved to Metal Testnet (chain 63825); x402 RPC purchase and the first anchor done on 6 Nov.
- Bedrock is a model provider (Platform Setup → Agent (AI & Model)); only the API key is missing.
