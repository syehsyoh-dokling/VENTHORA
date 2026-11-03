# ruff: noqa: E501
"""Finance standards: every accounting, control, tax and audit choice an organisation makes, for any kind of
organisation (a large or listed company, an SME, a state-owned enterprise, a government agency, an NGO, a foundation,
a cooperative, a Sharia-compliant entity).

  generic   applies to every organisation; it has a working default
  sector    applies to some organisations only (NGOs, government, listed companies, cooperatives, Sharia); its
            first option is "None" and that is its default, so an organisation it does not concern leaves it alone
  enforced  VENTHORA applies the value when it checks a request (payment terms, PO rule, claim deadline, categories,
            base currency, fiscal year); every other value is recorded with the standards, shown to approvers and
            auditors, and read by the AI agent, but not yet checked automatically

Fields marked top live directly on organisation.Standards (they predate the catalog); every other field is kept in
Standards.policies under its key.
"""
from __future__ import annotations

from typing import Any

# sector -> label; "all" means every organisation
SECTORS = {
    "all": "Every organisation",
    "company": "Companies",
    "listed": "Listed companies",
    "sme": "Small and micro businesses",
    "public": "Government and public sector",
    "ngo": "NGOs, foundations and charities",
    "coop": "Cooperatives",
    "sharia": "Sharia-compliant organisations",
}

# entity type -> (label, the sectors whose fields concern it)
ENTITY_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "large_company": ("Large private company", ("company",)),
    "listed_company": ("Listed company", ("company", "listed")),
    "sme": ("Small or medium enterprise", ("company", "sme")),
    "micro": ("Micro enterprise", ("company", "sme")),
    "state_owned": ("State-owned enterprise", ("company", "public")),
    "central_government": ("Government ministry or agency", ("public",)),
    "local_government": ("Local or regional government", ("public",)),
    "ngo": ("Non-governmental / not-for-profit organisation", ("ngo",)),
    "foundation": ("Foundation", ("ngo",)),
    "charity": ("Registered charity", ("ngo",)),
    "religious": ("Religious organisation", ("ngo",)),
    "multilateral": ("International or multilateral organisation", ("ngo", "public")),
    "cooperative": ("Cooperative", ("coop", "sme")),
    "other": ("Other", ()),
}

NONE = ("none", "None")

SECTIONS = [
    ("framework", "Organisation and reporting framework"),
    ("payables", "Payables and payment controls"),
    ("cash", "Cash, expenses and foreign exchange"),
    ("tax", "Tax"),
    ("close", "Records, close and audit"),
    ("listed", "Listed companies"),
    ("public", "Government and public sector"),
    ("ngo", "NGOs, foundations and charities"),
    ("coop", "Cooperatives"),
    ("sharia", "Sharia accounting"),
]

# each field: key, section, label, kind, default, sectors, enforced, store, help, options / min / max
FIELDS: list[dict[str, Any]] = [
    # -- framework -------------------------------------------------------------------------------------------------
    {"key": "company_name", "section": "framework", "label": "Legal entity name", "kind": "text", "store": "top", "enforced": True,
     "help": "checked against the addressee on invoices and documents"},
    {"key": "entity_type", "section": "framework", "label": "Type of organisation", "kind": "select", "default": "large_company",
     "options": [(k, v[0]) for k, v in ENTITY_TYPES.items()], "help": "decides which sector fields below concern you"},
    {"key": "reporting_framework", "section": "framework", "label": "Financial reporting framework", "kind": "select", "default": "ifrs",
     "options": [("ifrs", "IFRS (full, IASB)"), ("ifrs_sme", "IFRS for SMEs"), ("us_gaap", "US GAAP (FASB ASC)"),
                 ("frs102", "UK FRS 102"), ("frs105", "UK FRS 105 (micro-entities)"),
                 ("sak_id", "Indonesian Financial Accounting Standards (IFRS-converged)"), ("sak_ep", "Indonesian Financial Accounting Standards for Private Entities"),
                 ("sak_emkm", "Indonesian Financial Accounting Standards for Micro, Small and Medium Entities"), ("sak_syariah", "Indonesian Sharia Financial Accounting Standards"),
                 ("sfrs_i", "Singapore Financial Reporting Standards (International)"), ("aasb", "Australian Accounting Standards (AASB)"), ("ind_as", "Indian Accounting Standards (Ind AS)"), ("jgaap", "Japanese GAAP"),
                 ("ipsas_accrual", "IPSAS, accrual basis (public sector)"), ("ipsas_cash", "IPSAS, cash basis (public sector)"),
                 ("sap_id", "Indonesian Government Accounting Standards (Government Regulation 71/2010)"), ("gasb", "US GASB standards (state and local government)"),
                 ("fasab", "US FASAB standards (federal government)"), ("inpag", "International Non-Profit Accounting Guidance (INPAG)"),
                 ("other", "Other national GAAP")]},
    {"key": "accounting_basis", "section": "framework", "label": "Accounting basis", "kind": "select", "default": "accrual",
     "options": [("accrual", "Accrual"), ("modified_accrual", "Modified accrual"), ("modified_cash", "Modified cash"), ("cash", "Cash")]},
    {"key": "consolidation", "section": "framework", "label": "Reporting entity", "kind": "select", "default": "consolidated",
     "options": [("single", "Single entity"), ("consolidated", "Consolidated group (parent and subsidiaries)"), ("combined", "Combined (entities under common control)")]},
    {"key": "base_currency", "section": "framework", "label": "Base currency", "kind": "currency", "store": "top", "enforced": True,
     "help": "approval limits and budgets are set in it"},
    {"key": "fiscal_year_start_month", "section": "framework", "label": "Fiscal year starts", "kind": "month", "store": "top", "enforced": True},

    # -- payables --------------------------------------------------------------------------------------------------
    {"key": "default_payment_terms_days", "section": "payables", "label": "Default supplier payment terms (days)", "kind": "int", "min": 0, "max": 365,
     "store": "top", "enforced": True, "help": "due date when an invoice states none"},
    {"key": "require_po", "section": "payables", "label": "Purchase order required for supplier invoices", "kind": "bool", "store": "top", "enforced": True},
    {"key": "invoice_matching", "section": "payables", "label": "Invoice matching", "kind": "select", "store": "top",
     "options": [("three_way", "Three-way (order, receipt, invoice)"), ("two_way", "Two-way (order, invoice)")]},
    {"key": "segregation_of_duties", "section": "payables", "label": "Segregation of duties", "kind": "select", "default": "standard",
     "options": [("standard", "Requester and approver differ"), ("strict", "Requester, approver and releaser all differ")],
     "help": "VENTHORA always keeps requester and approver apart"},
    {"key": "dual_approval_above_minor", "section": "payables", "label": "Dual approval threshold (base currency, 0 = none)", "kind": "money", "default": 0},
    {"key": "payment_run", "section": "payables", "label": "Payment run frequency", "kind": "select", "default": "on_demand",
     "options": [("on_demand", "On demand"), ("weekly", "Weekly"), ("twice_monthly", "Semi-monthly"), ("monthly", "Monthly")]},
    {"key": "early_payment_discount", "section": "payables", "label": "Early-payment (settlement) discounts", "kind": "select", "default": "when_cash_allows",
     "options": [("when_cash_allows", "Take when cash allows"), ("always", "Always take"), ("never", "Never take")]},
    {"key": "sanctions_screening", "section": "payables", "label": "Sanctions screening of payees", "kind": "select", "default": "none",
     "options": [NONE, ("provider", "By the payment provider (Airwallex)"), ("ofac_un_eu", "OFAC, UN and EU sanctions lists"), ("local", "National sanctions list"), ("other", "Other")]},
    {"key": "vendor_tax_id_required", "section": "payables", "label": "Supplier tax identification number required", "kind": "bool", "default": False},

    # -- cash and FX -----------------------------------------------------------------------------------------------
    {"key": "petty_cash_limit_minor", "section": "cash", "label": "Petty cash float limit (base currency)", "kind": "money", "store": "top"},
    {"key": "expense_claim_deadline_days", "section": "cash", "label": "Expense claim submission deadline (days)", "kind": "int", "min": 1, "max": 365,
     "store": "top", "enforced": True},
    {"key": "per_diem_rates", "section": "cash", "label": "Travel and subsistence (per diem) rates", "kind": "select", "default": "none", "sectors": ("public", "ngo"),
     "options": [NONE, ("government", "Government rates (e.g. Indonesian Ministry of Finance standard costs, US General Services Administration)"), ("donor", "Donor rates"), ("own", "Own rate table")]},
    {"key": "fx_rate_source", "section": "cash", "label": "Exchange rate source", "kind": "select", "store": "top",
     "options": [("airwallex", "Airwallex live rates"), ("ecb", "European Central Bank reference rates"), ("central_bank", "National central bank reference rate (e.g. Jakarta Interbank Spot Dollar Rate)"), ("other", "Other")]},
    {"key": "fx_rate_type", "section": "cash", "label": "Exchange rate for initial recognition", "kind": "select", "default": "spot",
     "options": [("spot", "Spot rate on the transaction date (IAS 21)"), ("monthly_average", "Monthly average"), ("month_end", "Month-end closing rate")]},
    {"key": "expense_categories", "section": "cash", "label": "Expense categories (comma separated)", "kind": "list", "store": "top", "enforced": True, "wide": True},

    # -- tax -------------------------------------------------------------------------------------------------------
    {"key": "vat_regime", "section": "tax", "label": "VAT / GST", "kind": "select", "default": "none",
     "options": [NONE, ("vat", "VAT"), ("gst", "GST"), ("ppn_id", "Indonesian VAT"), ("us_sales_tax", "US sales and use tax")]},
    {"key": "withholding_tax", "section": "tax", "label": "Withholding tax on supplier payments", "kind": "select", "default": "none",
     "options": [NONE, ("pph_id", "Indonesian income tax withholding (Articles 21, 23, 26 and 4(2))"), ("us_1099", "US 1099 reporting and backup withholding"),
                 ("treaty", "Cross-border services at treaty rates"), ("other", "Other")]},
    {"key": "e_invoicing", "section": "tax", "label": "E-invoicing", "kind": "select", "default": "none",
     "options": [NONE, ("coretax_id", "Indonesian electronic tax invoices (Coretax system)"), ("peppol", "PEPPOL e-invoicing network"), ("other", "Other")]},

    # -- close and audit -------------------------------------------------------------------------------------------
    {"key": "document_retention_years", "section": "close", "label": "Document retention period (years)", "kind": "int", "min": 1, "max": 30, "store": "top"},
    {"key": "month_end_close_day", "section": "close", "label": "Month-end close (working day)", "kind": "int", "min": 1, "max": 20, "default": 5},
    {"key": "internal_control_framework", "section": "close", "label": "Internal control framework", "kind": "select", "default": "none",
     "options": [NONE, ("coso_2013", "COSO Internal Control - Integrated Framework (2013)"), ("intosai_gov_9100", "INTOSAI GOV 9100 internal control guidelines (public sector)"), ("other", "Other")]},
    {"key": "audit_standard", "section": "close", "label": "External audit standard", "kind": "select", "default": "none",
     "options": [NONE, ("isa", "International Standards on Auditing (IAASB)"), ("pcaob", "PCAOB auditing standards (US-listed companies)"), ("sa_id", "Indonesian Standards on Auditing (Institute of Certified Public Accountants)"),
                 ("issai", "International Standards of Supreme Audit Institutions (ISSAI)"), ("spkn_id", "Indonesian State Financial Audit Standards (Audit Board)"), ("gagas", "US Generally Accepted Government Auditing Standards (Yellow Book)"),
                 ("single_audit", "Single Audit (2 CFR 200 Subpart F)")]},

    # -- sector: listed --------------------------------------------------------------------------------------------
    {"key": "icfr_regime", "section": "listed", "label": "Internal control over financial reporting", "kind": "select", "default": "none", "sectors": ("listed",),
     "options": [NONE, ("sox_404", "Sarbanes-Oxley Act section 404 (US)"), ("jsox", "Japanese internal control reporting (J-SOX)"), ("uk_code", "UK Corporate Governance Code"),
                 ("ojk_id", "Indonesian Financial Services Authority and Stock Exchange rules"), ("other", "Other")]},

    # -- sector: public --------------------------------------------------------------------------------------------
    {"key": "budget_execution", "section": "public", "label": "Budget execution", "kind": "select", "default": "none", "sectors": ("public",),
     "options": [NONE, ("commitment_control", "Commitment control (appropriation, commitment, payment)"),
                 ("dipa_id", "Indonesian budget execution (budget allotment, payment request, payment order, disbursement order)"), ("tsa", "Treasury single account")]},
    {"key": "public_procurement", "section": "public", "label": "Public procurement rules", "kind": "select", "default": "none", "sectors": ("public", "ngo"),
     "options": [NONE, ("perpres_id", "Indonesian Presidential Regulation 16/2018 on public procurement, as amended"), ("eu_2014_24", "EU Directive 2014/24/EU"),
                 ("us_far", "US Federal Acquisition Regulation"), ("uncitral", "UNCITRAL Model Law"), ("world_bank", "World Bank Procurement Regulations")]},
    {"key": "public_chart_of_accounts", "section": "public", "label": "Government chart of accounts", "kind": "select", "default": "none", "sectors": ("public",),
     "options": [NONE, ("bas_id", "Indonesian government standard chart of accounts"), ("gfsm_2014", "IMF Government Finance Statistics Manual 2014"), ("other", "Other")]},

    # -- sector: NGO -----------------------------------------------------------------------------------------------
    {"key": "npo_standard", "section": "ngo", "label": "Non-profit reporting standard", "kind": "select", "default": "none", "sectors": ("ngo",),
     "options": [NONE, ("isak_335", "Indonesian non-profit financial statement presentation (ISAK 335, formerly ISAK 35)"), ("asc_958", "US GAAP ASC 958 (not-for-profit entities)"),
                 ("charities_sorp", "UK Charities Statement of Recommended Practice (SORP, FRS 102)"), ("inpag", "International Non-Profit Accounting Guidance (INPAG)"), ("aasb_1058", "AASB 1058 Income of Not-for-Profit Entities (Australia)"), ("other", "Other")]},
    {"key": "net_asset_classes", "section": "ngo", "label": "Net asset and fund classification", "kind": "select", "default": "none", "sectors": ("ngo",),
     "options": [NONE, ("two_classes", "With and without donor restrictions"), ("fund_per_grant", "A fund per grant or donor"),
                 ("both", "Both: two classes and a fund per grant")]},
    {"key": "donor_rules", "section": "ngo", "label": "Donor compliance requirements", "kind": "multi", "default": [], "sectors": ("ngo", "public"),
     "options": [("us_2cfr200", "US Uniform Guidance (2 CFR 200)"), ("eu_fr", "EU Financial Regulation and EU humanitarian aid (ECHO)"), ("fcdo", "UK Foreign, Commonwealth and Development Office"),
                 ("world_bank", "World Bank disbursement guidelines"), ("un_hact", "UN Harmonised Approach to Cash Transfers (HACT)"), ("global_fund", "Global Fund"), ("other", "Other")],
     "help": "tick every donor whose rules your grants carry; none ticked = none"},
    {"key": "indirect_cost_recovery", "section": "ngo", "label": "Indirect cost recovery", "kind": "select", "default": "none", "sectors": ("ngo",),
     "options": [NONE, ("nicra", "Negotiated indirect cost rate agreement (NICRA)"), ("de_minimis", "De minimis rate: up to 15% of modified total direct costs (2 CFR 200.414(f), since Oct 2024)"),
                 ("fixed_rate", "Fixed rate per donor agreement"), ("full_cost", "Full cost recovery")]},
    {"key": "indirect_cost_rate_bp", "section": "ngo", "label": "Indirect cost rate (%)", "kind": "percent", "default": 0, "max": 10000, "sectors": ("ngo",)},

    # -- sector: coop ----------------------------------------------------------------------------------------------
    {"key": "member_surplus", "section": "coop", "label": "Member surplus distribution", "kind": "select", "default": "none", "sectors": ("coop",),
     "options": [NONE, ("shu_id", "Net operating surplus distributed to members (Indonesian cooperative law)"), ("patronage", "Patronage dividend"), ("other", "Other")]},

    # -- sector: sharia --------------------------------------------------------------------------------------------
    {"key": "sharia_standard", "section": "sharia", "label": "Sharia accounting standard", "kind": "select", "default": "none", "sectors": ("sharia",),
     "options": [NONE, ("psak_syariah", "Indonesian Sharia accounting standards"), ("aaoifi", "AAOIFI"), ("other", "Other")]},
    {"key": "zakat", "section": "sharia", "label": "Zakat", "kind": "select", "default": "none", "sectors": ("sharia",),
     "options": [NONE, ("collected", "Collected and distributed as zakat administrator"), ("paid", "Paid by the organisation as zakat payer")]},
]

BY_KEY = {f["key"]: f for f in FIELDS}
POLICY_KEYS = [f["key"] for f in FIELDS if f.get("store") != "top"]


def defaults() -> dict[str, Any]:
    return {k: BY_KEY[k].get("default") for k in POLICY_KEYS}


def check(policies: dict[str, Any]) -> dict[str, Any]:
    """Every policy known and valid; a missing one takes its default."""
    out = defaults()
    for k, v in policies.items():
        f = BY_KEY.get(k)
        if f is None or f.get("store") == "top":
            raise ValueError(f"unknown finance standard {k!r}")
        kind, opts = f["kind"], [o[0] for o in f.get("options", [])]
        if kind == "select" and v not in opts:
            raise ValueError(f"{f['label']}: {v!r} is not one of the options")
        if kind == "multi" and (not isinstance(v, list) or any(x not in opts for x in v)):
            raise ValueError(f"{f['label']}: choose from the options")
        if kind in ("int", "money", "percent") and (not isinstance(v, int) or isinstance(v, bool) or v < f.get("min", 0) or v > f.get("max", 10**12)):
            raise ValueError(f"{f['label']}: a whole number from {f.get('min', 0)} is needed")
        if kind == "bool" and not isinstance(v, bool):
            raise ValueError(f"{f['label']}: yes or no")
        out[k] = sorted(set(v), key=opts.index) if kind == "multi" else v
    return out


def catalog() -> dict:
    return {"sectors": SECTORS, "entity_types": [{"key": k, "label": lab, "sectors": list(sec)} for k, (lab, sec) in ENTITY_TYPES.items()],
            "sections": [{"key": k, "label": lab} for k, lab in SECTIONS],
            "fields": [f | {"sectors": list(f.get("sectors", ("all",))), "options": [list(o) for o in f.get("options", [])],
                            "store": f.get("store", "policies"), "enforced": bool(f.get("enforced"))} for f in FIELDS]}
