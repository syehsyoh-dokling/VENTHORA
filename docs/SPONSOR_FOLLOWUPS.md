# Sponsor follow-ups (updated 5 Nov 2026, after the draft v1 test)

Checked before writing:
- Metal Tahoe RPC `https://tahoe.metalblockchain.org/ext/bc/C/rpc` answers: chain id `0x5d3ec` = 381932, but the latest
  block is **2,014 from 29 Oct 2026**; nothing new since.
- VENTHORA's testnet address `0xd64E00faEBe7B1cC519F0A41e05de29B3A0E5Fa6`: balance 0 on the RPC and on
  `tahoe.metalscan.io`, no transactions, after the faucet claim on 5 Nov.
- The builder guide says Kits 11 and 12 pay in *USDZ "which also pays gas", funded from the faucet in Metal's agent
  guides or the **Metal console's** Faucet page: probably a different network from Tahoe (where METAL pays gas).

## 1. Metal team (Metal Award, USD 15,000): paste into the Metal Slack channel

Hi Metal team :wave: VENTHORA here, an autonomous treasury agent on Airwallex (Kits 01 and 03). We've built a Metal integration and hit a wall on the testnet. Four quick questions:

1. **Faucet: nothing arrived.** We claimed test METAL at faucet.metalblockchain.org for `0xd64E00faEBe7B1cC519F0A41e05de29B3A0E5Fa6` today. Both the RPC and tahoe.metalscan.io show a balance of 0 and no transactions. Could you check, or fund that address?
2. **Which network?** The Tahoe C-Chain RPC answers (chain id 381932), but its latest block is 2,014 from 29 Oct, with no new blocks since. The builder guide says Kits 11/12 pay in *USDZ, "which also pays gas", from the Metal console's faucet. Is that a different testnet? Could you share the RPC URL and chain id we should use, and console access for our registered email?
3. **Award eligibility:** after each payment run, VENTHORA anchors its proof on chain. One transaction carries the SHA-256 digest of our audit-log head and of every contract's hash-chain head, and anyone can recompute it on the explorer. The money itself moves on Airwallex. Does that count for the Metal Award, or do you need value moving on Metal (e.g. Kit 11 mandates paying in *USDZ)?
4. **Kit 11 tooling:** the guide refers to `metal-agent` and `@metalntwx/metal-pay`, but we can't find them on npm. Where can we get them and the agent guides?

We can switch networks the same day once you point us to the right one. Thanks! :pray:

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
- Anchoring is built (Platform Setup → Metal); only the testnet gas is missing.
- Bedrock is a model provider (Platform Setup → Agent (AI & Model)); only the API key is missing.
