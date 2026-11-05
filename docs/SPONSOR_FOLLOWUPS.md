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

Answered by our own tests and the docs (6 Nov), so not asked:
- **Eligibility:** the hackathon page says the Metal Award "Requires use of Metal and Airwallex tooling". VENTHORA
  uses Metal Testnet (RPC, faucet, x402, anchoring) and Airwallex.
- **Anchoring pattern:** a zero-value self-transaction with the digest in calldata is accepted (status 1). The Atlas
  explorer shows it as *finalized*, with the raw input `VENTHORA-ANCHOR-1:` + the digest, readable by any judge.
  `MetalAssertions` needs a bonded domain whose names are not documented, and it is not needed for this.
- **RPC endpoint expiry or credit:** VENTHORA tops its endpoint up over x402 by itself when it answers 402 (a GET on
  the endpoint shows the 1 USDZ offer), within 5 USDZ a day. One credit pack covers about 20,000 calls; an anchor
  uses about 6.
- **Mandates (Kit 11):** tested with `metal-agent` 0.3.3 on testnet (6 Nov). Account created; a mandate with cap 2 USDZ,
  budget 5, one payee and a 1-day expiry was bound on chain. A 1 USDZ pull was **paid**; 3 USDZ was **refused**
  (`mandate_cap_exceeded`); an unlisted payee was **refused** (`mandate_payee_not_listed`). `mandate renew` revoked it
  and bound a fresh one with nothing spent, so a monthly limit is a renew each month (the budget never resets alone).

Message:

Thanks for the pointer! :raised_hands: VENTHORA now runs on **Metal Testnet (chain 63825)** end to end:
- It funds its address from the faucet API.
- It **buys and tops up its own RPC endpoint over x402** (1 USDZ, EIP-3009, signed by the agent).
- It **anchors its audit trail**: one transaction carries the SHA-256 digest of our audit log and every contract's hash chain.
- We tried **mandates** with `metal-agent`: a pull inside the cap was paid, one above the cap and one to an unlisted payee were refused, and `renew` started a fresh budget. That's how we'll mirror our Delegation of Authority's monthly limits.

The first anchor: https://testnet.metalntwx.com/tx/0xb57d7e0d7bc8641e4238877793e4a532c97571f6764cf3893682e1028e03a4f6

One quick check: **will Metal Testnet, the faucet and the Atlas explorer stay up, with no reset, until judging ends** (after Demo Day, 19 Nov)? Our proof links for the judges point at testnet.metalntwx.com.

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
