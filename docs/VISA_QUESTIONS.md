**Slack post for the Visa team (Visa Award)**

Hi Visa team :wave: We're building **VENTHORA**, an autonomous treasury agent on Airwallex (Kit 01). It pays a company's bills under a mandate set by the finance officer. For the Visa Award we want the agent to pay card-billed vendors (software subscriptions) with an **Airwallex-issued Visa virtual card**, through **Visa Intelligent Commerce**: a purchase instruction with mandates, and a TAP-signed agent.

**What we checked before asking**
- **TAP:** the sample repo (`visa/trusted-agent-protocol`) runs locally with its own agent registry and needs no Visa credentials. ✅
- **VIC starter kit:** the docs index (`sandbox.mcp.visa.com/mcp/doc/llms.txt`) is readable, and the flow (enroll-card → initiate-purchase-instruction → retrieve-payment-credentials → confirm-transaction-events) is clear. ✅
- **VIC sandbox, direct API:** `sandbox.api.visa.com` rejects calls without X-Pay or two-way SSL (400). That is expected without a project.
- **VIC MCP server:** `sandbox.mcp.visa.com/mcp` returns **401** without a JWE token.
- **The real blocker:** the VIC page says sandbox connectivity takes **about 10 business days**. It also needs a TRID and externalAppId from VTS, a BID from the regional team, an onboarded JWE key and mutual TLS. Submissions close on **13 Nov**.

**Our questions**
1. **Sandbox access in time:** is there a hackathon fast track or pre-provisioned VIC sandbox credentials? This would be MCP access with a test consumer, an enrolled test card and the TRID, BID and key onboarding done, so we can call `initiate-purchase-instruction` before 13 Nov.
2. **What counts for the Visa Award:** if full VIC access can't be ready in time, which Visa integration counts? For example: TAP signatures from our agent to a merchant, VIC calls on the sandbox MCP, or Visa's Scenario Runner?
3. **Card eligibility:** the docs limit VIC to tokenisable, CTF-enrolled cards. In the sandbox, can we enrol an **Airwallex-issued Visa virtual card**, or should we use Visa's test cards?
4. **TAP agent identity:** for the hackathon, may our agent sign with its own key and registry, as in the sample? Or does it have to be registered with Visa as an approved agent?
5. **Mandates:** in `initiate-purchase-instruction`, do mandate limits (amount cap, merchant, expiry) make **Visa decline** a charge outside them, or are they informational? A link to the field reference would help.

Thanks a lot! :pray:
