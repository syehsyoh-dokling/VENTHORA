# ruff: noqa: E501
"""What VENTHORA reads from a company's own finance or accounting application, and what it sends back.

A company that already runs an accounting, ERP or reporting tool with an API connects it with one line (the base
URL and its key), ticks what VENTHORA may read and what it may write back, and nothing is typed in twice. Every item
below is ticked by default; a person unticks what the other application does not have or should not receive.

PULL   master and open-item data VENTHORA needs before it can pay anyone: offices, people and limits, projects,
       chart of accounts, vendors with bank details, customers, purchase orders, open bills, receivables, budgets.
PUSH   what VENTHORA did, so the other books stay true without manual entry: payments made, journal entries,
       purchase-order balances, payee verification, payment-request decisions and budget usage.

``parse_connection`` reads the one line: ``<base URL> [credential]`` where the credential is ``Bearer <token>``,
``<Header-Name>: <value>``, a bare token (sent as Bearer), or nothing; ``https://user:password@host/...`` is basic.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

# entity -> (label, why VENTHORA needs it, default path on the other application, VENTHORA's fields)
PULL: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "unit": ("Offices, branches and cost centres", "who may pay from which office, in which currency and up to which limit", "units",
             ("unit_id", "name", "kind", "parent_id", "country", "currency", "timezone", "cost_centre", "payment_limit_minor")),
    "user": ("People and approval limits", "who may approve what, by office and amount", "users",
             ("name", "email", "role", "unit_id", "approval_limit_minor")),
    "project": ("Projects", "the project part of every charge code", "projects", ("code", "name", "unit_id")),
    "account": ("Chart of accounts", "the GL account every payment is booked to", "accounts", ("code", "name", "category")),
    "price": ("Unit prices", "checking invoice lines against agreed prices", "prices",
              ("item", "category", "unit", "price_minor", "currency", "unit_id")),
    "vendor": ("Vendors and their bank details", "who can be paid, to which account; a change of bank details is held for a call-back", "vendors",
               ("vendor_id", "name", "currency", "payout_method", "contacts", "registered_phone", "budget_line", "default_priority",
                "bank.account_name", "bank.account_number", "bank.iban", "bank.swift_code", "bank.routing_type", "bank.routing_value",
                "bank.bank_name", "bank.bank_country_code", "bank.account_currency", "address.street_address", "address.city",
                "address.state", "address.postcode", "address.country_code")),
    "customer": ("Customers", "matching money that arrives to who sent it", "customers",
                 ("customer_id", "name", "currency", "contacts", "promises_kept", "promises_missed")),
    "po": ("Purchase orders", "a supplier invoice is paid only against an open PO, never beyond its total", "purchase-orders",
           ("po_id", "supplier_id", "currency", "total_minor", "account_code", "description", "allow_partial", "terms",
            "issued_date", "start_date", "expiry_date", "loe", "loe_unit", "rate_minor", "recurring")),
    "pr": ("Purchase requests", "what people ask to buy before there is a PO; an approved one is raised as a PO", "purchase-requests",
           ("pr_number", "title", "requested_by", "supplier_id", "currency", "estimated_minor", "charge_code", "needed_by", "requested_date",
            "justification", "status", "decided_by")),
    "engagement": ("One-off engagements", "consultants for a single task, legal specialists, resource persons, keynote speakers", "engagements",
                   ("engagement_id", "name", "role", "organisation", "email", "event", "event_date", "fee_minor", "currency", "charge_code", "payee_id")),
    "ta": ("Travel authorisations", "approved trips, checked again when the travel claim arrives", "travel-authorisations",
           ("ta_number", "traveller", "origin", "destination", "start", "end", "purpose", "estimated_minor", "currency", "charge_code", "status",
            "approved_by")),
    "fund_request": ("Cash advances, petty cash and ODC", "advances to staff, petty cash floats and other direct costs to approve and pay", "fund-requests",
                     ("request_number", "kind", "requested_by", "amount_minor", "currency", "charge_code", "purpose", "activity", "request_date",
                      "needed_by", "settle_by", "status", "decided_by")),
    "bill": ("Open bills (accounts payable)", "what is due, to whom and when", "bills",
             ("bill_id", "vendor_id", "invoice_no", "amount_minor", "currency", "due_date", "po_number", "description")),
    "receivable": ("Receivables (accounts receivable)", "what customers owe, for the cash forecast and aging", "receivables",
                   ("receivable_id", "customer_id", "invoice_no", "amount_minor", "currency", "due_date")),
    "budget_line": ("Budget lines", "what each line may still spend this period", "budget",
                    ("period", "line", "amount_minor", "auto_share_bp", "alert_share_bp")),
    "tenant_book": ("Entities and books", "which entity a payment belongs to, its allowed payees and approvers", "books",
                    ("tenant", "tenant_name", "book", "allowed_payees", "approvers")),
}

# item -> (label, what the other application gets, default path on the other application, the fields sent)
PUSH: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "payment": ("Payments made", "every transfer the bank reported PAID, to mark the bill paid", "payments",
                ("transfer_id", "paid_at", "payee_id", "payee", "obligation_id", "po_number", "termin", "account_code", "currency",
                 "amount_minor", "base_cost_minor", "reporting_period")),
    "journal": ("Journal entries", "a balanced entry per payment: debit the charge code, credit the bank account", "journal-entries",
                ("entry_id", "date", "memo", "currency", "lines")),
    "po_balance": ("Purchase-order balances", "paid, in flight and left per PO, term by term", "purchase-orders/balances",
                   ("po_id", "supplier_id", "currency", "total_minor", "paid_minor", "in_flight_minor", "left_minor", "state", "next_termin")),
    "payee_status": ("Payee verification", "whether a payee's bank details are verified, changed or suspended", "vendors/status",
                     ("payee_id", "status", "verified_at", "verified_by", "changed_at", "cooling_until")),
    "request_status": ("Payment request decisions", "verified, held for review, quarantined or rejected, with the reasons", "bills/status",
                       ("request_id", "invoice_no", "counterparty_id", "amount_minor", "currency", "due_date", "status", "reasons")),
    "budget_usage": ("Budget usage", "spent and remaining per allocation", "budget/usage",
                     ("allocation_id", "period", "unit_id", "line", "currency", "amount_minor", "spent_minor", "remaining_minor")),
}


def default_mapping(entity: str) -> dict[str, str]:
    """The other application uses VENTHORA's field names until a person maps its own."""
    return {f: f for f in PULL[entity][3]}


def parse_connection(line: str) -> tuple[str, dict]:
    """(base URL, auth) from one line; raises ValueError with what is wrong."""
    line = (line or "").strip()
    if not line:
        raise ValueError("paste the base URL of the API, then its key")
    url, _, cred = line.partition(" ")
    cred = cred.strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("the line must start with the API's base URL (http:// or https://)")
    auth: dict = {"type": "none"}
    if u.username or u.password:  # https://user:password@host/... is basic authentication
        auth = {"type": "basic", "username": u.username or "", "secret": u.password or ""}
        netloc = u.hostname + (f":{u.port}" if u.port else "")
        url = urlunparse(u._replace(netloc=netloc))
    if cred:
        if m := re.fullmatch(r"(?i)bearer\s+(\S+)", cred):
            auth = {"type": "bearer", "secret": m.group(1)}
        elif m := re.fullmatch(r"([A-Za-z][A-Za-z0-9-]{0,59})\s*:\s*(\S+)", cred):
            auth = {"type": "header", "header_name": m.group(1), "secret": m.group(2)}
        elif re.fullmatch(r"\S+", cred):
            auth = {"type": "bearer", "secret": cred}
        else:
            raise ValueError("after the URL put one credential: Bearer <token>, <Header-Name>: <value>, or the token itself")
    return url.rstrip("/"), auth
