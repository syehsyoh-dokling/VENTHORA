# ruff: noqa: E501
"""The public face of the API: one version prefix, VENTHORA's own resource names (accounts, balances, collection
accounts) rather than the bank provider's, a written title and description for every public endpoint, and the
internal endpoints (demo scenarios, evidence suites, the operations console) kept out of it.

VENTHORA is the system other applications talk to; it keeps itself in sync with the bank provider (Airwallex
today). So the public paths name no provider, and an application does not change when the provider does.

``publish(app)`` runs once, after every route is declared:

* an endpoint still under ``/api/...`` gets its ``/v1/...`` path (ALIASES); the old path keeps working for the
  dashboard but leaves the schema, so other applications see one prefix;
* an INTERNAL endpoint leaves the schema and the catalogue, and API keys cannot call it (apiaccess.scope_for);
* every public endpoint takes its title and description from DOCS; tests/test_api_access.py fails when a new
  public endpoint has none.
"""
from __future__ import annotations

ALIASES = {
    "/api/airwallex/balances": "/v1/balances",
    "/v1/airwallex/accounts": "/v1/accounts",
    "/v1/airwallex/accounts/{account_id}/submit": "/v1/accounts/{account_id}/submit",
    "/v1/airwallex/accounts/{account_id}/use": "/v1/accounts/{account_id}/use",
    "/v1/airwallex/accounts/{account_id}/details": "/v1/accounts/{account_id}/details",
    "/v1/airwallex/industries": "/v1/reference/industries",
    "/v1/airwallex/acting": "/v1/acting-as",
    "/v1/airwallex/global-accounts": "/v1/collection-accounts",
    "/api/audit": "/v1/audit",
    "/api/budget": "/v1/budgets/pipeline",
    "/api/status": "/v1/status",
}

# the dashboard's own machinery and the evidence suites: not a contract for other applications
INTERNAL = ("/api/live", "/api/runs", "/api/sandbox", "/api/scenarios", "/api/groundtruth", "/api/matrix",
            "/api/airwallex/global-accounts", "/v1/intake/plan", "/v1/intake/state")


def is_internal(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in INTERNAL) or path.endswith("/sandbox-approve")


# (method, path) -> (title, description)
DOCS: dict[tuple[str, str], tuple[str, str]] = {
    # treasury
    ("GET", "/v1/balances"): ("Balances", "The available balance per currency in minor units (cents), kept in sync with the bank provider."),
    ("GET", "/v1/accounts"): ("List accounts", "The business and personal accounts opened through VENTHORA, with their verification status."),
    ("POST", "/v1/accounts"): ("Open an account", "Open an account for a business or person at the bank provider; the same call works on the sandbox and in production."),
    ("POST", "/v1/accounts/{account_id}/details"): ("Complete company details", "Add the KYB details the provider needs before verification: structure, registration number, industry, address, expected revenue and the director."),
    ("GET", "/v1/reference/industries"): ("Industry categories", "The provider's industry categories and their codes, for the company details."),
    ("POST", "/v1/accounts/{account_id}/submit"): ("Submit an account for verification", "Send an opened account to the provider's KYC/KYB review."),
    ("POST", "/v1/accounts/{account_id}/use"): ("Work for an account", "VENTHORA acts for this verified account from now on: balances, payments and reports are its own."),
    ("DELETE", "/v1/acting-as"): ("Stop working for an account", "VENTHORA returns to the platform's own account."),
    ("GET", "/v1/collection-accounts"): ("List collection accounts", "The local bank details per currency that customers pay into, with their status."),
    ("POST", "/v1/collection-accounts"): ("Open a collection account", "Open local bank details in a currency so customers can pay by local transfer."),
    ("GET", "/v1/audit"): ("Audit log", "The append-only audit trail (SHA-256 hash chain): who did what and when, newest first, with an integrity check of the chain."),
    ("GET", "/v1/executions"): ("List executions", "Every plan execution with its status and the transfers it made."),
    ("POST", "/v1/executions"): ("Execute an approved plan", "Run quote, conversion, transfers and settlement checks for an approved plan; each step passes the budget and approval gates first."),
    ("POST", "/v1/executions/autonomous"): ("Execute what is inside the mandate", "Pay now, without an approval, the largest part of the plan the policy approves by itself, most urgent first; return the held obligations with their reasons and the remaining book for a person to approve."),
    ("GET", "/v1/executions/{execution_id}"): ("Get an execution", "One execution step by step: quotes, conversions, transfers and their Airwallex status."),
    ("POST", "/v1/executions/{execution_id}/resume"): ("Resume an execution", "Continue an execution that waited for a person or for Airwallex, from the step where it stopped."),
    ("GET", "/v1/incidents"): ("List incidents", "Transfers that did not arrive, with the decision taken on each."),
    ("POST", "/v1/incidents"): ("Report an incident", "Report a sent transfer that did not arrive; VENTHORA decides to wait, replace or escalate and records why."),
    ("POST", "/v1/incidents/decide"): ("Decide an incident (dry run)", "What VENTHORA would decide for a failure type and its finality, without recording anything."),
    ("GET", "/v1/incidents/{incident_id}"): ("Get an incident", "One incident with its timeline and decision."),
    ("GET", "/v1/ledger"): ("List ledger books", "Every book with its cash, committed, expense and FX balances per currency."),
    ("POST", "/v1/ledger/eval"): ("Evaluate postings (dry run)", "The balances and refusals a set of postings would produce, without writing them."),
    ("GET", "/v1/ledger/{book_id}"): ("Get a ledger book", "One book's balances and postings."),
    ("POST", "/v1/ledger/{book_id}/adjust"): ("Adjust a ledger book", "Record a person's correction to a book, such as an unreported bank charge; it is audited."),
    ("POST", "/v1/plans"): ("Create a payment plan", "Turn obligations and balances into a plan of conversions and transfers, verified by the independent engines."),
    ("POST", "/v1/plans/challenge"): ("Challenge a recommendation", "Check an outside recommendation against the same engines and say where it breaks a rule."),
    ("POST", "/v1/plans/replan"): ("Replan after new information", "Recalculate only the decisions that new information affects; the rest of the plan stays."),
    ("POST", "/v1/plans/split"): ("Split a plan: autonomous now, held for a person", "What the agent would execute on its own now and what waits for an approver, with the policy's reasons per held obligation; nothing is executed."),
    ("GET", "/v1/recon-cases"): ("List reconciliation cases", "Reconciliations that did not add up; an open case blocks payments from its book."),
    ("POST", "/v1/recon-cases/{case_id}/close"): ("Close a reconciliation case", "Close a case once a person has explained the difference; the book is unblocked."),
    ("GET", "/v1/treasury/overview"): ("Treasury overview", "Available balance with its value in the major currencies, pending payments, cash in and out, and pending actions."),
    ("GET", "/v1/treasury/position"): ("Cash position", "The wallet now and what is about to change it: committed payments, expected receipts and the forecast."),
    # payables
    ("POST", "/v1/approvals/eval"): ("Approval chain (dry run)", "Who must approve a payment of a given amount, office and type under the approval matrix."),
    ("GET", "/v1/intake"): ("List payment requests", "Invoices, claims and payment orders in the queue, each with its checks and the transfers that settled it."),
    ("GET", "/v1/intake/commitments"): ("List the commitments register", "Every PO, consultant contract and one-off engagement received (entered, uploaded or synced) with its source and state; only open, unexpired ones are commitments."),
    ("GET", "/v1/budget-lines"): ("Read the budget lines of a month", "Amount per line, the share the agent pays alone, the alert share, approver at alert, and a signed bypass."),
    ("PUT", "/v1/budget-lines/{period}"): ("Save the budget lines of a month", "Replaces the month's lines; a bypass needs its signatory; synced lines are read only."),
    ("GET", "/v1/intake/requests/{intake_id}"): ("Read a payment request", "One request in full: what was read, the checks, documents and history."),
    ("PUT", "/v1/intake/requests/{intake_id}"): ("Correct a payment request", "Save the corrections and run the payee, PO and duplicate checks again."),
    ("POST", "/v1/intake/requests/{intake_id}/pay"): ("Correct and pay", "Save, check again, approve as this person and pay through the treasury proof."),
    ("GET", "/v1/fund-requests"): ("List cash advances, petty cash and ODC", "Requests with the person, payee, decision, payment order and settlement."),
    ("POST", "/v1/fund-requests/{number}/decision"): ("Decide a cash advance, petty cash or ODC request", "Never by the person asking; within the approver's limit."),
    ("POST", "/v1/fund-requests/{number}/pay"): ("Send a request for payment", "A payment order to its payee in Pending payment, through the usual checks."),
    ("POST", "/v1/fund-requests/{number}/settle"): ("Settle an advance or a float", "Against its receipts: what is left is returned, what is over is reimbursed."),
    ("GET", "/v1/intake/travel-authorisations"): ("List travel authorisations", "Approved trips: traveller, route, dates, estimate, approver and source."),
    ("POST", "/v1/intake/import"): ("Import into Intake", "Pull purchase requests, POs, consultant contracts, engagements or travel authorisations from the connection or an uploaded file: between two dates, or one number; dry run first."),
    ("POST", "/v1/sources/test-connection"): ("Test a connection", "Reach the application (and the item's own address) before anything is imported; nothing is written."),
    ("GET", "/v1/purchase-requests"): ("List purchase requests", "Every purchase request with its decision and the PO raised from it."),
    ("POST", "/v1/purchase-requests/{pr_number}/decision"): ("Decide a purchase request", "Approve or reject; never by the requester, and within the approver's limit."),
    ("GET", "/v1/reports/fx-costs"): ("FX and transfer costs per reporting period", "Every conversion and every payout Airwallex converted inside the transfer, at its spread against the mid-market rate, and every payout fee, in the base currency; set against what the period converted and paid."),
    ("GET", "/v1/reports/spend"): ("Spending report per category", "Payroll, fringe, goods and services, operation, cash advance, travel, ODC: amount, paid, left, status, office, branch, unit and person."),
    ("GET", "/v1/exceptions"): ("What waits for a person", "Payroll changes, blocked due payments, payee call-backs, requests to review, reconciliation cases and incidents; empty means VENTHORA pays as normal."),
    ("POST", "/v1/exceptions/payroll/approve"): ("Approve a changed payroll line", "Once, by someone other than who changed it; VENTHORA pays it on the pay day."),
    ("POST", "/v1/exceptions/payroll/to-review"): ("Send a payroll line to Pending payment", "A line outside the delegation becomes a payment order a person decides."),
    ("POST", "/v1/agent-plan/hold"): ("Hold one payment of the plan", "A person's intervention: the Standard Payment Priority Policy does not pay it (no deemed approval, no autonomous run) until it is released."),
    ("DELETE", "/v1/agent-plan/hold/{obligation_id}"): ("Release a held payment", "The policy may pay it again."),
    ("POST", "/v1/agent-plan/policy-run"): ("Run the payment policy now", "High priority first: plan the horizon and pay what the policy lets the agent pay now (the scheduler runs it every five minutes)."),
    ("GET", "/v1/notifications"): ("Notices for the FCO", "What the Standard Payment Priority Policy reports to the FCO: a balance that does not cover every payment, a planned payment not made, a deemed approval it could not give."),
    ("POST", "/v1/notifications/{key}/close"): ("Close a notice", "Marks a notice for the FCO as handled."),
    ("GET", "/v1/prompt-system"): ("Read the Generic Prompting System", "The FCO's generic prompt and notes, the rules compiled from what is in force, the full system prompt every model call carries, and when it is sent."),
    ("PUT", "/v1/prompt-system"): ("Save the generic prompt and notes", "The system prompt of every model call from now on; the engines still enforce every rule as it is set."),
    ("GET", "/v1/priority-sop"): ("Read the Standard Payment Priority Policy", "Each spending category with its level (High, Medium, Low), the priority it gives in the plan, and the waiting payments ranked by it."),
    ("PUT", "/v1/priority-sop"): ("Save the Standard Payment Priority Policy", "Only the mandate authority; every plan of the organisation's payments ranks them by it."),
    ("GET", "/v1/agent-plan"): ("Today's plan", "What VENTHORA would do now with the real payments: each decision and why, the part it pays alone and the part for a person."),
    ("POST", "/v1/agent-plan/execute"): ("Pay the agent's part now", "Execute the part of today's plan inside the mandate; the rest waits for a person."),
    ("GET", "/v1/receipts"): ("Balance received", "Deposits other parties paid into the Airwallex account, and receipts recorded here, matched to each other and to customer invoices."),
    ("POST", "/v1/intake/emails"): ("Submit an emailed invoice", "Submit one raw email (RFC 822); the sender, attachments and documents are checked before it enters the queue."),
    ("POST", "/v1/intake/payment-orders"): ("Create a payment order", "Add an internal payment order to the same queue and checks as emailed invoices."),
    ("POST", "/v1/intake/{intake_id}/review"): ("Review a payment request", "A person verifies or rejects a request held by a check, with a note; audited."),
    ("GET", "/v1/payees"): ("List payees", "Every payee with status, bank-details fingerprint, cooling period and verification."),
    ("POST", "/v1/payees/eval"): ("Payee lifecycle (dry run)", "The payee record a sequence of lifecycle events would produce, without saving it."),
    ("GET", "/v1/payees/{beneficiary_id}"): ("Get a payee", "One payee with its history of bank-detail changes and verifications."),
    ("POST", "/v1/payees/{beneficiary_id}/bank-change"): ("Record a bank-detail change", "The payee becomes CHANGED and cannot be paid until a new call-back verifies the details."),
    ("POST", "/v1/payees/{beneficiary_id}/reinstate"): ("Reinstate a payee", "Back to NEW: a call-back is required before the next payment."),
    ("POST", "/v1/payees/{beneficiary_id}/suspend"): ("Suspend a payee", "No payment goes to this payee until it is reinstated."),
    ("POST", "/v1/payees/{beneficiary_id}/verify"): ("Verify a payee", "Record the call-back in which a finance manager confirmed the bank details with the payee."),
    ("GET", "/v1/procurement/purchase-orders"): ("List purchase orders", "Every PO with what is paid (term by term), in flight and still open; a PO is never paid beyond its total."),
    ("GET", "/v1/commitments"): ("List commitments", "Per vendor, consultant, one-off engagement, customer and employee: PO or contract, active and expiry dates, amount, paid, remaining, LoE left, and the status New / Partial / Multi Payment / Done."),
    ("GET", "/v1/procurement/spending"): ("Spending by office and project", "Paid spend by office, project, account code and payee for a period."),
    # receivables
    ("GET", "/v1/aging"): ("Aging report", "Every obligation of a book as of a date, in aging buckets."),
    ("POST", "/v1/aging/eval"): ("Aging (dry run)", "The aging buckets a set of invoices would fall into on a date."),
    # budget and reporting
    ("GET", "/v1/budgets/pipeline"): ("Budget pipeline", "For a book: planned, granted, realized, paying now and committed, per budget line."),
    ("POST", "/v1/budget-allocation/check"): ("Check a payment against the budget", "What the execution gate would say about one payment now; nothing is booked."),
    ("GET", "/v1/budget-allocation/policy"): ("Get the budget policy", "Enforcement, active period type, fiscal-year start, cut-off and sync schedule."),
    ("PUT", "/v1/budget-allocation/policy"): ("Set the budget policy", "Change enforcement, period type, cut-off or sync schedule; audited."),
    ("GET", "/v1/budget-allocation/status"): ("Budget status", "Every allocation of a period with what was spent and what is left."),
    ("POST", "/v1/budget-allocation/sync"): ("Sync budget spend now", "Run the daily or monthly budget sync immediately instead of waiting for its schedule."),
    ("POST", "/v1/budgets/{period}/close"): ("Close a budget period", "Issue the period's statement at cut-off: plan, realized and variance per line."),
    ("GET", "/v1/budgets/{period}/cutoff"): ("Get a closed period", "The statement issued when the period was closed; 404 while it is still open."),
    ("POST", "/v1/budgets/{period}/{line}/grant"): ("Grant budget", "A person raises a budget line's mandate in answer to a request; the role decides how much."),
    ("GET", "/v1/reporting/config"): ("Get reporting settings", "Cut-off day and time, timezone, reporting currency and rate fallback."),
    ("PUT", "/v1/reporting/config"): ("Set reporting settings", "Change the cut-off, reporting currency or rate fallback; audited."),
    ("POST", "/v1/reporting/eval"): ("Reporting period (dry run)", "Which period and report a posting would belong to under the current cut-off."),
    ("GET", "/v1/reporting/periods/{period}"): ("Period report", "Monthly spend by payment date and the invoice statement for a period, in the reporting currency."),
    ("POST", "/v1/reporting/periods/{period}/rates"): ("Record closing rates", "Store the period's closing FX rates from Airwallex's rates, for the period report."),
    ("GET", "/v1/reports/payments"): ("Payment report", "Every paid payment, running (today, week, month, quarter) and accumulated (month to year)."),
    ("GET", "/v1/reports/purchase-orders"): ("PO reconciliation report", "Every PO against what was paid, in flight and invoiced, with any over- or under-payment."),
    ("GET", "/v1/tenants"): ("List tenants", "Every tenant with its books, allowed payees and limits."),
    ("POST", "/v1/tenants/eval"): ("Resolve a book (dry run)", "Which tenant a book id belongs to under a registry."),
    # organisation
    ("GET", "/v1/org/account-code"): ("Check an account code", "Validate an account code (office cost centre . project . account) and name each part."),
    ("GET", "/v1/org/allocation-tree"): ("Allocation tree", "Offices, projects and accounts as a tree with their budget allocations for a period (default 2026)."),
    ("GET", "/v1/org/standards"): ("Get finance standards", "Framework, payment controls, cash and FX, tax, records and audit, and the sector rules (listed, public, NGO, cooperative, Sharia) the organisation follows."),
    ("GET", "/v1/mandate"): ("Get the standing mandate", "What the agent may do on its own, what it checks first (each check on hold, warn or off) and when it calls a person, by category, with every limit, and which role holds the mandate authority."),
    ("PUT", "/v1/mandate"): ("Set the standing mandate", "Only the role holding the mandate authority may call it. Change limits, check modes, switches, or hand the authority on. A change that weakens a critical control answers 409 with the warnings; send it again with their keys in acknowledged. Audited; every Autopilot Console starts from it."),
    ("GET", "/v1/doa"): ("Get the delegation of authority", "Internal payment categories with their policy (schedule, pay day, payout currencies) and this period's source document judged against it; external categories with their limits; the autopilot switch."),
    ("PUT", "/v1/doa/policies/{key}"): ("Set a category's policy", "Schedule, pay day and payout currencies of one internal category; it names no person or amount. Only the holder of the delegation may call it."),
    ("PUT", "/v1/doa/settings"): ("Switch the DoA autopilot", "On: the agent pays every due line the DoA lets it pay on its own, every minute, without a click. Only the holder of the delegation may call it."),
    ("POST", "/v1/doa/registers"): ("Prepare a payroll register", "One line per employee: the employee's registered bank account, currency, net pay and charge code; it needs HR's and then the FCO's approval."),
    ("GET", "/v1/doa/registers"): ("List payroll registers", "Every payroll register, newest first, with the organisation's people and the registered accounts a line may pay into."),
    ("GET", "/v1/doa/registers/{period}"): ("Get a payroll register", "The period's register with its lines, totals per currency and approvals."),
    ("POST", "/v1/doa/registers/{period}/approve"): ("Approve a payroll register", "step=hr, then step=fco, by two different people, neither of them the preparer."),
    ("POST", "/v1/doa/run"): ("Run the due lines", "Pay now what the DoA lets the agent pay on its own, plus the prepared lines a person approves; the treasury proof and the split still apply."),
    ("POST", "/v1/doa/custom"): ("Add a delegation category", "A category of the FCO's own: item, charge code, who may ask for it by email or signed in on the dashboard, payout currencies and what the request must carry; paid in real time when complete."),
    ("DELETE", "/v1/doa/custom/{key}"): ("Remove a delegation category", "Removes a category the FCO added; requests for it then wait for a person."),
    ("POST", "/v1/doa/travel-requests"): ("Make a travel request", "A travel claim from the dashboard: traveller, origin and destination from the travel rates, dates, amount and the checklist documents (base64). It goes through the same intake and checks as an emailed claim."),
    ("GET", "/v1/org/standards/catalog"): ("Finance standards catalog", "Every finance standard with its options, the organisations it concerns and whether VENTHORA enforces it."),
    ("PUT", "/v1/org/standards"): ("Set finance standards", "Change the finance SOP; audited."),
    ("GET", "/v1/org/{collection}"): ("List organisation records", "Units, people, projects, accounts, mailboxes and other master records of one collection."),
    ("POST", "/v1/org/{collection}"): ("Save an organisation record", "Create or update one record of a collection; the role decides what may change."),
    ("DELETE", "/v1/org/{collection}/{record_id}"): ("Delete an organisation record", "Remove one record of a collection; audited."),
    # data sources
    ("GET", "/v1/sources/catalog"): ("Sync catalogue", "What VENTHORA reads from a finance application and what it sends back, each with why and its fields."),
    ("POST", "/v1/sources/connectors"): ("Connect a finance application", "One line (base URL and key) and the items to read and send back; every item is ticked unless named otherwise."),
    ("POST", "/v1/sources/connectors/{connector_id}/discover"): ("Discover and propose a mapping", "Find where the application keeps each item (its OpenAPI document, else the usual paths, GET only) and propose its field mapping by name and AI; nothing is saved."),
    ("POST", "/v1/sources/connectors/{connector_id}/push"): ("Send back now", "Write every ticked item that is new or changed since the last send; dry_run=true only counts."),
    ("GET", "/v1/sources/connectors"): ("List connectors","Connections to the old system or ERP, with what each one syncs and when it last ran."),
    ("DELETE", "/v1/sources/connectors/{connector_id}"): ("Delete a connector", "Remove a connection; synced data stays."),
    ("PUT", "/v1/sources/connectors/{connector_id}"): ("Save a connector", "Create or change a connection to the old system or ERP."),
    ("POST", "/v1/sources/connectors/{connector_id}/sync"): ("Run a sync", "Read every enabled entity from the source and write it; dry_run=true only reports what would change."),
    ("GET", "/v1/sources/fields"): ("Import fields", "The fields each entity takes and how a cell is read, for building import files."),
    ("GET", "/v1/sources/entities"): ("Dataset kinds", "Every kind of record a dataset can hold, what VENTHORA uses it for and the page it shows on."),
    ("GET", "/v1/beneficiaries"): ("List vendors, consultants or customers", "kind=vendor|consultant|customer: every field a payment or a receipt reads, with bank details masked, the lifecycle status and the purchase orders held against each."),
    ("POST", "/v1/beneficiaries"): ("Add or change a vendor, consultant or customer", "Through the same pipeline as a sync or an upload: new or changed bank details make the payee NEW or CHANGED until a call-back on the registered number."),
    ("GET", "/v1/sources/master/{entity}"): ("List master data", "The synced or imported records of one entity."),
    ("GET", "/v1/sources/readiness"): ("Readiness", "Platform setup: every connection, organisation record, master data set, intake channel and policy VENTHORA needs, whether it is done, required or optional, and where to set it."),
    ("GET", "/v1/sources/runs"): ("List sync runs", "Every sync and import with its counts of created, updated and rejected records."),
    ("POST", "/v1/sources/uploads"): ("Upload a file", "Upload any data file (CSV, Excel, JSON, Word, HTML, e-mail, PDF or text) as the raw body; with entity=auto VENTHORA decides what it holds."),
    ("POST", "/v1/sources/uploads/{upload_id}/commit"): ("Import an upload", "Write the previewed records of an upload."),
    ("POST", "/v1/sources/uploads/{upload_id}/preview"): ("Preview an upload", "What the import would create, update or reject, without writing it."),
    # settings
    ("GET", "/v1/settings/documents"): ("Get document rules", "The document-validation rules and the catalogue of document types and checks."),
    ("PUT", "/v1/settings/documents"): ("Set document rules", "Change required documents, checks and limits; audited."),
    ("GET", "/v1/webhooks/events"): ("Received webhook events", "The latest events Airwallex and the mail service pushed, with how each was handled."),
    ("GET", "/v1/webhooks/status"): ("Webhook status", "Whether each inbound channel is configured and when it last received an event."),
    # system
    ("GET", "/health"): ("Health", "Liveness: status, version and server time."),
    ("GET", "/v1/status"): ("Service status", "Which engines, authorities and connections are active."),
    # developer
    ("GET", "/v1/developer/catalog"): ("API catalogue", "Every public endpoint with its area and the scope a key needs."),
    ("GET", "/v1/developer/keys"): ("List API keys", "Every key with its scopes, role, status and use; never the key itself."),
    ("POST", "/v1/developer/keys"): ("Create an API key", "The key is in this response only; VENTHORA keeps its hash."),
    ("POST", "/v1/developer/keys/{key_id}/revoke"): ("Revoke an API key", "The application using it is refused at once."),
    ("GET", "/v1/developer/keys/{key_id}/requests"): ("Key request log", "The latest calls made with one key: time, method, path, status and duration."),
    ("GET", "/v1/airwallex/connection"): ("Airwallex connection", "Which Airwallex account VENTHORA works on; secrets are never returned."),
    ("PUT", "/v1/airwallex/connection"): ("Connect Airwallex", "Save and test Airwallex credentials."),
    ("DELETE", "/v1/airwallex/connection"): ("Disconnect Airwallex", "Forget the saved account; VENTHORA falls back to the server's configured credentials."),
    ("GET", "/v1/settings/email"): ("Get email settings", "Who may send what, the inboxes VENTHORA reads (secrets never returned), the mail providers to pick from, and the push address."),
    ("PUT", "/v1/settings/email/policy"): ("Set the email policy", "Payment orders by email on or off, company domains, sender authentication and the subject tag."),
    ("PUT", "/v1/settings/email/inboxes/{inbox_id}"): ("Connect an inbox", "An IMAP mailbox VENTHORA reads on a schedule: provider preset or any server, app password or OAuth 2.0 token, folder and what to do after reading."),
    ("DELETE", "/v1/settings/email/inboxes/{inbox_id}"): ("Remove an inbox", "VENTHORA stops reading it; what was read stays in the queue."),
    ("POST", "/v1/settings/email/inboxes/{inbox_id}/test"): ("Test an inbox", "Sign in and open the folder: messages and unread; nothing is read or changed."),
    ("POST", "/v1/settings/email/inboxes/{inbox_id}/fetch"): ("Read an inbox now", "Read the new mail now instead of waiting for the schedule."),
    ("GET", "/v1/settings/ai"): ("Get AI model settings", "The model in use and which provider keys are set; secrets are never returned."),
    ("PUT", "/v1/settings/ai"): ("Set the AI model", "Choose rules only, a self-hosted model or a provider."),
    ("PUT", "/v1/settings/ai/keys/{provider}"): ("Save a provider key", "Keep a provider's API key and preferred model in the vault."),
    ("DELETE", "/v1/settings/ai/keys/{provider}"): ("Delete a provider key", "Remove a provider's key from the vault."),
    ("POST", "/v1/settings/ai/models"): ("List provider models", "The model ids the chosen server or provider offers."),
    ("POST", "/v1/settings/ai/test"): ("Test the AI model", "One short request to the chosen model, to prove the address, key and model."),
    # inbound
    ("POST", "/webhooks/airwallex"): ("Airwallex webhook", "Airwallex pushes deposit, transfer and conversion status changes here, signed with the webhook secret."),
    ("POST", "/webhooks/email"): ("Inbound email webhook", "A mail service pushes each email sent to the company's VENTHORA address: Postmark JSON, the raw message, or a multipart form (SendGrid, Mailgun); basic auth or X-Venthora-Secret."),
    ("POST", "/webhooks/email/mime"): ("Inbound email webhook (Mailgun)", "The same, at the URL Mailgun needs to post the full message (body-mime)."),
}


def publish(app) -> None:
    from fastapi.routing import APIRoute

    for route in list(app.routes):
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        if is_internal(route.path):
            route.include_in_schema = False
            continue
        new = ALIASES.get(route.path)
        if new:
            route.include_in_schema = False  # the old path keeps working for the dashboard
            app.add_api_route(new, route.endpoint, methods=sorted(route.methods), name=f"{route.name}_v1",
                              response_model=route.response_model)
    for route in app.routes:
        if isinstance(route, APIRoute) and route.include_in_schema:
            for method in route.methods:
                doc = DOCS.get((method, route.path))
                if doc:
                    route.summary, route.description = doc
    app.openapi_schema = None
