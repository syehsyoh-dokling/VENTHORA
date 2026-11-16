# How to Use VENTHORA

This guide follows the application's own menu. Every heading below is a menu or a submenu exactly as it appears in the
side menu, in the same order inside each menu, and every tab named is a tab on that page. The steps are in the order a
company sets VENTHORA up and then uses it.

| Step | Menu in the side bar | Who sees it |
|---|---|---|
| 1 | Platform Setup | Finance Director (Super admin) and Developer (IT) |
| 2 | FCO Authorize | Finance Director only |
| 3 | System Setting | Everyone except the Developer |
| 4 | Users & Beneficiaries | Everyone except the Developer |
| 5 | Payment | Everyone except the Developer |
| 6 | Transfer | Everyone except the Developer |
| 7 | Procurement Process | Everyone except the Developer |
| 8 | Reporting | Everyone except the Developer |
| 9 | Dashboard | Everyone except the Developer |
| A | Developer Console | Developer (IT); the Finance Director can view it |
| B | Demo-Only | Shown in demo mode only |

In the side bar itself the order is: Dashboard, Payment, Transfer, Procurement Process, Reporting, Users & Beneficiaries,
Platform Setup, FCO Authorize, System Setting, Developer Console, Demo-Only. A menu opens its submenus when you click it.

This build runs on sandboxes only: no real money moves.

---

## Step 1: Platform Setup

The menu has two parts: **Required** (1.1 to 1.4) and **Optional** (1.5 to 1.10).

### 1.1 Airwallex Global Account (required)

The company's own Airwallex account: open it, its account numbers, where deposits arrive. The page offers two buttons:
**Connect to Existing Account** and **Open New Account**.

**If the company already has an Airwallex account**

Before you start, outside VENTHORA: anyone who is Owner or Admin of the company's Airwallex account can do this, no
developer is needed.

1. Sign in to the Airwallex web app and open **Developer → API keys**.
2. Copy the **Client ID** shown there, then create an **API key** and copy it. Airwallex shows it once.

In VENTHORA:

1. Press **Connect to Existing Account**. No new account is opened.
2. Paste the Client ID and the API key and press **Check the account**: you see the account, balances and account numbers before anything is connected.
3. Press **Connect this account**. VENTHORA then subscribes to the account's notifications by itself, keeps the key write-only and never shows it again. Sandbox keys only in this build.

**If the company has no Airwallex account**

1. Press **Open New Account**. Everything VENTHORA already knows is filled in and marked with where it comes from; only the empty fields are asked.
2. The fields are: registered company name, country of registration, business structure, registration number (UEN, NIB, SSM, CRN …), industry, what the business sells, registered address (street, city, state, postcode), estimated monthly revenue and its currency, the primary contact email (the FCO) and mobile, and the director's first and last name, date of birth, nationality and ID type.
3. Tick the two Airwallex agreements (they are never prefilled), press **Check the details**, then **Create Airwallex account**. The account is opened, its company details added and handed to Airwallex's verification in one step.

Opening an account here needs VENTHORA's own platform connection, which is the Developer's one-time setup (A.1).

**When the account is ready** the page shows "Deposit into this account · account numbers". Each Global Account is a bank
account in the company's name at Airwallex, one per currency. To top up the balance, the FCO sends a bank transfer from
the company's bank to these details. The balance counts the money only once Airwallex reports the deposit SETTLED.
**+ New Global Account** adds one.

### 1.2 Agent (AI & Model) (required)

The model that reads invoices, plans and explains; the rules work without it. The page offers three choices:

| Choice on the page | What it is | What you enter |
|---|---|---|
| **Amazon Bedrock** | The models in your AWS account, listed live from Bedrock: Claude, Llama, Nova, Mistral, Qwen and more, with a Bedrock API key. | The Bedrock API key (AWS console → Amazon Bedrock → API keys), then pick a model in the Model list. |
| **Self-hosted model** | Qwen or any OpenAI-compatible server: this laptop, a GPU machine, vLLM, Ollama, LM Studio. | Server address, model name, access token (optional). |
| **API key** | Anthropic, OpenAI, Gemini, Mistral, DeepSeek, NVIDIA NIM, Qwen and more, with the provider's own API key. | The provider, then its API key. |

How to get a key (outside VENTHORA): an AI provider key cannot be created from VENTHORA. Each provider issues its own key
in its console (Amazon Bedrock, Anthropic, NVIDIA NIM, OpenAI, Google Gemini, Groq, OpenRouter). Sign in with the
company's account and create a key; some ask for billing first.

In VENTHORA: paste the key for that provider, press **Test connection**, then **Save**. The key is kept write-only: VENTHORA
shows only its last 4 characters. VENTHORA's rules work without a model; the model only reads documents and explains.

### 1.3 Email Sync (required)

The mailboxes invoices, claims and payment orders arrive in, and the outgoing mail. The page is "Email: four steps;
VENTHORA fills in the servers". You create the sign-in yourself, in your own account at the provider: VENTHORA never
makes or holds a master key for your mailbox.

**The addresses involved**

| Address | Set where | Notes |
|---|---|---|
| Where invoices arrive | Email Sync, step 2 | The address vendors already send invoices to. VENTHORA reads new mail every minute. |
| Sender of confirmations | Email Sync, step 3 | VENTHORA's POs, RFQs and payment notices to vendors and staff go out from this address. It can be the same mailbox as the invoice address or a different one. |
| The FCO's own email | The FCO's record in People & roles, and the notice settings under FCO Authorize | Where the FCO receives notices about requests that need a person. |

**Step 1 · Mail provider.** Where the company's mailbox is. The list on the page, and what each needs:

| Provider in the list | Outside VENTHORA | In VENTHORA |
|---|---|---|
| Microsoft 365 (work or school mailbox) | Register your own Microsoft app (App registrations): the page names the Redirect URI to add and asks for the Application (client) ID; supported accounts "Any Entra ID Tenant + Personal Microsoft accounts". | Enter the client ID and sign in with Microsoft. |
| Outlook.com / Hotmail (personal mailbox) | The same Microsoft app registration. | The same. |
| Cloudflare Email Routing (own domain, e.g. invoices@yourcompany.com) | Route the address to VENTHORA in Cloudflare; no mailbox password. | Enter the address. |
| Gmail / Google Workspace | Turn on 2-Step Verification, then Google Account → Security → App passwords; IMAP must be on in Gmail settings. | Enter the address and the app password. |
| Yahoo Mail | Yahoo Account Security → Generate app password. | Address and app password. |
| iCloud Mail | appleid.apple.com → Sign-In and Security → App-Specific Passwords. | Address and app password. |
| Zoho Mail | Zoho Mail → Settings → Mail Accounts → IMAP access on; an application-specific password if 2FA is on. | Address and password. |
| Fastmail | Settings → Privacy & Security → Integrations → New app password (IMAP). | Address and app password. |
| Yandex Mail | Yandex ID → Security → App passwords → Mail. | Address and app password. |
| AOL Mail | AOL Account Security → Generate app password. | Address and app password. |
| Other IMAP server (company mail host, cPanel …) | The host, port and security your mail provider documents for IMAP. | Enter them with the address and password. |

**Step 2 · Where invoices arrive.** Connect the address vendors already send invoices to. Choose "Read unread mail from"
(last 1 day, 3 days, 7 days, 15 days, 1 month, 3 months, 6 months, 1 year, or all mail), then **Check connection** and
**Read now**. Attachments are read by the AI model.

**Step 3 · Sender of confirmations.** Set the address VENTHORA's own emails go out from, then **Check connection**.

**Step 4 · Who may send instructions.** A payment order by email is acted on only from these senders, and only after the
receiving server authenticated them. Tick **Accept payment orders by email**, then either everyone at a company domain
or people one by one. Someone else still approves it; email never pays by itself.

### 1.4 Setup Checklist (required)

Platform setup: what must be configured before VENTHORA can run. It checks that everything above is set and what is
still a placeholder. The **Edition · Demo mode** switch is on this page too: ON shows the developer view (Developer
Console, Demo-Only, demo data), OFF the end-user view; only a Super admin switches it.

### 1.5 App Integration (optional)

Sync people, vendors, POs and bills from your ERP; API keys for other apps. Create one key per application that calls
VENTHORA. "Share with developers" gives a public, read-only page for the IT developers of other applications: every
endpoint a key may call, its scope, parameters and a curl example, with the OpenAPI file and a Postman collection.

### 1.6 Airwallex Cards (optional)

Staff cards under the same mandate as transfers. Each person can have one Visa virtual card from Airwallex Issuing,
closed until an approved advance opens it: only for the purposes the Finance Director ticks, only inside its window and
amount, and only at the merchants allowed. Every charge is asked first (remote authorization) and decided from the
mandate and the card program alone. VENTHORA keeps the card id and the masked number, never the full number.

### 1.7 Visa controls (optional)

What VENTHORA may use from Visa. The Developer connects Visa once (A.4); the FCO decides here which Visa features are
on. A feature switched off is refused by the server, whoever calls it.

### 1.8 Airi usage (optional)

Whether staff may check out with Airi, and within what. These limits apply to every Airi checkout VENTHORA approves;
above the approval threshold the FCO approves it first.

### 1.9 Blockchain Record (optional)

A tamper-proof record of payments, approvals and documents, anchored on Alta. The page explains why keep it, what it
costs and Alta's products; the Finance Director says yes or not now and chooses which documents go on the record.

### 1.10 Local Rails (optional)

Everything the other menus cannot do in Indonesia, and nothing they already do. Airwallex pays Indonesian bank accounts,
cards spend at Visa merchants and Alta keeps the record, but none of them pays an e-wallet, says whose account it is, or
collects rupiah by QRIS, virtual account or cash. Two tabs: **Xendit** for money going out and the holder's name,
**Midtrans** for money coming in. The Finance Director chooses the products; the keys are entered by the Developer
(A.8). Keys are write-only and sandbox only.

---

## Step 2: FCO Authorize

Only the Finance Director (Super admin) sees this menu.

### 2.1 Delegation of Authority

Who may approve what, up to which amount, and what the agent may pay alone. Three tabs:

- **Internal payments**: the organisation's own recurring obligations. The FCO sets one standing payment instruction per cost category, and on the pay date the agent pays on its own what the instruction allows.
- **External payments**: invoices from vendors and short-term specialists.
- **Treasury limits and controls**, with three parts: **1 · Mandate by category**, **2 · Limits and thresholds**, **3 · Approval policy**. Inside the mandate the agent checks, plans and pays on its own; outside it, it stops and asks a person.

Anything outside the delegation is prepared for a person; some things are never executed. Every payment still passes the
treasury limits and controls.

### 2.2 Standard Payment Priority Policy

What is paid first when cash cannot cover everything. Set the priority (High priority first, or first in first out), the
plan's horizon (24, 48, 72, 96 or 120 hours), the times approved payments run, on which days and in which time zone
(company time or UTC), and the level (High, Medium, Low) of each spending category. Press **Save policy**.

Scheduled payments (payroll and benefits from People & roles) are paid on their pay date at 00:30 UTC.

### 2.3 Finance standards

Base currency, fiscal year, company name and the expense categories every report uses.

### 2.4 Admin team

The administrators and what each role may do: Super admin (FCO), Developer (IT), HQ admin, Country office admin, Branch
admin, Project finance admin, CFO, Finance manager, Country finance lead, Project accountant, Treasury analyst,
Requester, Auditor. **Add admin** adds one.

### 2.5 Placement providers

Where idle cash may go, and how it comes back: term deposits, money market funds and Airwallex Yield. VENTHORA proposes
a placement with the best provider on this list; when the FCO approves, the money goes by Airwallex transfer to the
provider's checked account.

### 2.6 Period, cut-off & sync

This page sets the organisation's financial calendar. Choose the month the fiscal year starts and the budget period,
check the quarters, set the cut-off, save. Set it once at the start of the fiscal year.

### 2.7 Document validation

Which documents an invoice or claim needs before it may be paid: validate every invoice, claim and bank letter.

---

## Step 3: System Setting

### 3.1 Dataset upload

Load people, vendors, POs or bills from a CSV, Excel or PDF when there is no ERP connection. On the page:

1. **What you do.** Choose one or more files: Excel, CSV, JSON, Word, PDF, HTML, an e-mail or plain text. Leave Auto-detect on, or pick the kind of data. Press **Read the file(s)**, check the column matches, press **Preview**. Press **Commit** when the preview looks right.
2. **What VENTHORA does.** Reads the file and decides what it holds; matches each column to a VENTHORA field and cleans the values; the preview shows what would be created, updated or rejected, and why. Nothing is written until Commit.
3. **The result.** Each record lands on its own page. A row that fails a check is rejected with the reason; the rest go in. A vendor's new or changed bank details still wait for a call-back before any payment. Every commit is in the audit log.

### 3.2 Unit prices

Standard prices per item and unit that expense claims are checked against. **Add price** adds one.

---

## Step 4: Users & Beneficiaries

### 4.1 Branches, units & country offices

The organisation as VENTHORA sees it; every payment is placed in it before it is paid. It decides charge codes, budgets,
who approves, and how much the agent pays alone per unit. **Add office** adds one, with its local currency, time zone
and cost centre.

### 4.2 People & roles

Everyone VENTHORA pays, routes to, or takes an approval from: salary, their own bank account, benefits, approval role
and limit, the email their requests are accepted from, and employment details. **Add person** adds one.

### 4.3 Consultants

Experts and consultants, regular or short-term, with their contracts. A consultant is paid like a vendor, under its own
delegation, inside the contract value. **Add consultant** adds one.

### 4.4 Vendors

Who supplies goods and services, and the account they are paid into. VENTHORA pays a vendor only from this record: an
invoice is trusted only from a registered mailbox; the money goes only to the bank account here; a new vendor, or a
changed account, is not paid until a person calls back on the registered phone number. **Add vendor** adds one.

### 4.5 Customers

Who pays the organisation, to match the money that arrives. **Add customer** adds one.

### 4.6 Payee lifecycle

NEW → VERIFIED → ACTIVE; a bank change starts a cooling period; the call-back goes to the registered number only.

### 4.7 Projects & accounts

The projects and the chart of accounts used in charge codes. **Add project** adds one.

### 4.8 Charge codes

A charge code is OFFICE.PROJECT.ACCOUNT. When the company has no standard of its own, **Fill VENTHORA defaults** gives
one. Only blanks are filled: a code the company set is never changed.

---

## Step 5: Payment

### 5.1 Agent plan

Three tabs:

- **1 · Today's plan**: what VENTHORA would do now with the organisation's real payments, and why. Every request waiting in Pending payment and every salary ready for the pay day, on the live Airwallex balance and rates, ranked by the Standard Payment Priority Policy, inside the planning horizon. The payment line-up shows green (the cash covers it), yellow (the last the cash covers), the blue reserve-floor line, and red (waits). Below it: Ready to pay, Waiting for an approval, Approve ahead, Stopped by a check.
- **2 · Transaction log**: every payment's story, step by step, with the date and time of each step.
- **3 · Autopilot Console**: the agent's whole cycle on the live wallet: the goal and the mandate; its initial plan; new information (an email, quoted word for word); the revised decision; the payments out on Airwallex and the book reconciled to the cent.

### 5.2 Intake

Seven tabs:

- **1 · Purchase request**: what someone asks to buy, before there is a PO. A person other than the requester approves or rejects it; an approved request is raised as a purchase order with Create PO.
- **2 · Purchase order**: every purchase order received: registered by hand, raised from an approved request, uploaded, or synced. Once saved, a PO is locked on its number and its value.
- **3 · Consultant & resource persons**: consultant contracts and one-off engagements.
- **4 · Travel authorisation**: a trip requested here goes through the same checks as an emailed one.
- **5 · Cash advance**: to a person for an approved activity, settled with receipts by the settle-by date.
- **6 · Petty cash / slush fund**: a float held by a custodian for small office expenses, and each top-up of it.
- **7 · Other direct cost (ODC)**: costs of a programme activity that belong to no other category.

**Requests by email.** An email that asks for cash or for a purchase is not a bill: it is **FILED** in Intake under
the number of the request it became, and continues in its own process.

- *Only the organisation's own email.* A request for cash (advance, travel advance, petty cash, slush fund, ODC) from
  an address that is not a registered person is rejected as it stands.
- *Two ways to approve.* The FCO's own email naming the person approves it. So does a registered person's email that
  forwards the FCO's approval: only the FCO's own words count, and a sentence that takes the approval back ("not yet
  approved", a question, a line the FCO only quoted) approves nothing. Without either, the request waits for a decision.
- *Numbers.* No purchase request or purchase order is needed. The request number the email states is kept
  ("Advance request no: CA-2026-0144"); without one VENTHORA numbers it. A travel advance carries its Travel
  Authorisation (Travel Clearance) number the same way.
- *Allocated, then paid at 23:59.* An approved advance is set aside when it is green: the money above the reserve
  floor, after what falls due inside the planning horizon and what was already promised to other advances, covers it.
  The person and the FCO get one email: allocated, paid at 23:59 company time, in so many hours. The FCO can cancel it
  until then (Cash advance → Cancel). When it is not green, both are told why and nothing is promised.
- *The card.* At 23:59 the card is created (or the person's staff card opened) and funded, once however often the run
  retries, and the person gets a second email with the link to the card. A travel advance goes on the person's own
  staff card, the card the person adds to Airi, when the card program has staff cards on.
- *The card link.* It shows the card (masked number, what it may spend, until when). A person who turned on the second
  factor is asked for the code from their authenticator app first; VENTHORA sends no SMS or WhatsApp code. The code a
  card network asks for when shopping online is the card issuer's, not VENTHORA's.

### 5.3 Commitment

Read only. Five tabs: **1 · Vendors**, **2 · Consultants**, **3 · One-off engagements**, **4 · Customers & receivables**,
**5 · Staff & payroll**. Each shows what is owed and what is left; an invoice is paid only inside an open PO that has not
expired.

### 5.4 Pending payment

Every payment waiting to go out: invoices and claims that arrived by email, payment orders raised here or from a
commitment, and requests synced or uploaded. **New payment order** raises one. **Review** opens a request: the checks
and their evidence, the details to correct, the emails to the vendor and to the FCO, what came back from the vendor, and
**Manual call-back** (the call sheet). Then **Verify**, **Reject**, **Save** or **Approve & pay**.

### 5.5 Balance received

Money other parties paid into the organisation's Airwallex account. Two tabs: **1 · Payments received**,
**2 · Receiving accounts**.

### 5.6 Budget allocation

Which budget a payment draws on. Four tabs: **1 · Regular budget allocation**, **2 · Project based allocation**,
**3 · Budget lines & thresholds**, **4 · Cash placement**.

### 5.7 Executions & incidents

Everything that waits for a person, and the run reports. Seven tabs: **1 · Payroll changes**, **2 · Blocked due
payments**, **3 · Payee call-backs**, **4 · Requests to review**, **5 · Reconciliation & executions**, **6 · Payment
incidents**, **7 · Run reports**.

### 5.8 E-wallet payouts

Pay an Indonesian e-wallet (OVO, DANA, GoPay, ShopeePay, LinkAja) through Xendit. Checked, approved by a second person,
sent once, proven. Bank accounts are paid through Airwallex, not here.

---

## Step 6: Transfer

Manual · from the Airwallex account. Money moved by a person, never by the agent alone: fill the form, check it, ask;
the FCO approves (never the person who asked) and it is sent at once from the company's Airwallex account. The
destinations, in menu order:

1. **To another Airwallex account**
2. **To MyCard (card account)**
3. **To a Visa virtual card**
4. **To the Airi card**
5. **To a local bank (Indonesia)**
6. **To Xendit**
7. **To Midtrans**
8. **To an international bank**

---

## Step 7: Procurement Process

One number runs through all of it: the purchase request (PR). Everything below hangs on that number, and the purchase
order number it ends in is the one the supplier's invoice quotes, so the invoice arrives in Payment → Intake against
the same PR and PO.

A purchase request can also start from an email (see *Requests by email* under 5.2): from a registered person it is
submitted; from the FCO, or with the FCO's approval forwarded in it, it is approved. It keeps the PR number the email
states, or gets VENTHORA's own.

### 7.1 Vendor Sourcing

One row per purchase request, with every vendor asked and everything that happened. The request's own lines (what, how
many, brand, specifications, needed by) become the request for quotation. The agent looks for at least **five**
relevant vendors, in this order: the **preferred vendors** that fit (7.2), the registered vendors, and only for what
is missing to five, the web (the demo directory in demo mode). Each invited vendor gets its own form link. Open a
request to see *Everything on this PR*: each RFQ sent, each quotation received, the award, the winner's announcement
and the regret letters, with their times.

### 7.2 Preferred Vendor

Every supplier that has sent a quotation, each with its own number (`PV-0001` ...). A vendor enters the list with its
first quotation, whether it wins or not; later quotations bring its record up to date: what it quoted for, what it
won, and how complete its administration was. These vendors are asked first on the next request that fits.

### 7.3 Quotation

Every quotation, grouped by purchase request, with a **ranking** and a **conclusion** on top:

- the ranking orders the quotations on three things: the specifications met (a quotation that misses one never beats
  one that meets them all, whatever its price), the price among those, and the administration (legal name,
  registration, tax id, contact, address, bank account, quotation document, payment terms: 11 points, with what is
  missing named);
- the conclusion says where the request stands: **Winner decided**, **Ready for the award**, **Needs a person** (with
  the reason: nobody meets every specification, fewer than three quotations, or the winner is above the approved
  estimate), or **Waiting for quotations**.

When every invited vendor has answered, or the deadline has passed, the agent applies the rule by itself: the cheapest
quotation that meets every specification wins. It never awards on fewer than three quotations, above the estimate, or
when nobody meets the specifications: those go to a person, by email, with the closest quotation and why.

### 7.4 Shortlist

At most three vendors per purchase request, best first, each with its justification. **Select** is a person's award:
the recommended quotation, or another with a written reason. Never by the person who asked.

### 7.5 Vendor List

The winners, and where each purchase stands. The **PO threshold** on this page decides the paper: a purchase at or
above it gets a **draft purchase order** (open it from the row: buyer, supplier, the line, the terms, who authorised
it; print it or save it as a PDF); one below it is a **direct purchase**, announced to the winner with the same
reference. Each row shows the steps: winner announced, order, supplier details and bank account, invoice received,
paid, with a link to the requests in Intake on that PO number.

The winner completes its details on its own form (the link is in the announcement and works for 30 days): company,
bank account (checked with the bank at once), the delivery date, and the invoice. An invoice that matches the order
goes through the usual intake checks and is payable under the mandate; one that differs, in amount or delivery date,
is held for a person before anything can pay it.

## Step 8: Reporting

In menu order:

1. **1 · Payroll**
2. **2 · Fringe benefit**
3. **3 · Goods and services**
4. **4 · Operation**
5. **5 · Cash advance**
6. **6 · Travel & transport**
7. **7 · Other direct cost**
8. **8 · FX & transfer costs**: what it cost to change currency and send the money.
9. **9 · Contract blockchain**: every contract is its own chain.
10. **10 · Virtual card (Visa)**: every charge on a VENTHORA virtual card.
11. **11 · Spending per staff**: per employee, advances received and virtual card spend, by project, branch and country.
12. **Total Spending**: tabs **Spending & commitments** and **Against budget**.
13. **Books & ledger**
14. **Period close**

Each report has **Download CSV**.

---

## Step 9: Dashboard

The first item in the side bar: the overview of balances, what is due and what needs a person.

---

## A. Developer Console

For the Developer (IT). One-time technical setup, in menu order:

1. **Airwallex platform**: VENTHORA's platform keys, one Client ID and API key for every company.
2. **Airwallex webhooks**: tabs **Setup** and **Received events**.
3. **Airwallex MCP**: Airwallex documentation and developer tools for the agent.
4. **Visa (VIC credentials)**: Visa Developer project credentials and Intelligent Commerce onboarding.
5. **Visa Agent Identity (TAP)**: the agent signs its requests so merchants know it is VENTHORA.
6. **Airi (CLI)**: Airi CLI installed and signed in to the company's Airi account.
7. **Alta (testnet anchoring)**: anchor the audit chain on a public testnet.
8. **Local Rails (keys)**: Xendit and Midtrans keys, mode and connection test.
9. **T:0 (books)**: T:0 books API key and its MCP server.
10. **Placement connections**: how VENTHORA reaches each deposit or fund provider.

Tools: **API Catalog**, **Local model (vLLM)**, **Server health**, **Data reset**.

## B. Demo-Only

Shown in demo mode only ("Until Demo Day"), in menu order: **Workflow**, **Video tutorial**, **Autopilot Console**, **Checks & resilience**,
**Hackathon evidence**, **Sandbox simulator**, **Test log**, **Acme Finance ERP ↗**.

---

## C. Continuity, security and standards

- **Continuity** (link beside *Second factor* in the sidebar, `/continuity`): whether this server may pay and why,
  **safe mode** (the FCO stops every automatic payment with a reason: the scheduled run, payment on arrival, the
  autopilot, the 23:59 advance run), the writer lease, and the standby. On a one-server deployment the standby actions
  answer plainly that no standby is connected. Design and limits: `docs/CONTINUITY.md`.
- **Second factor** (`/security`): a code from an authenticator app after the password.
- **Who acts.** With sign-in on, the signed-in person's own name and role are used for every approval, whatever a
  request claims; only a finance role runs a plan on the bank; nobody decides their own request.
- **Standards.** ISO 4217 decides the decimals of an amount (no decimals for JPY, two for most, three kept to two for
  BHD and KWD); a day's payments as ISO 20022 `pain.001`, a `camt.053` statement reconciled (Reporting).
- **What was checked and what is still open**: `docs/QA_REPORT_2026-11-15.md`.
