# ruff: noqa: E501
"""An uploaded file is read the way a person would: a job title becomes the nearest VENTHORA role (never an admin
role from a file), and a company's bank account takes the company's name, currency and country when the file leaves
them out."""
from venthora import imports


def test_job_titles_become_roles_and_no_file_grants_admin():
    rows = [{"Name": "A", "Role": "staff"}, {"Name": "B", "Role": "Finance Manager"}, {"Name": "C", "Role": "Senior Accountant"},
            {"Name": "D", "Role": "super_admin"}, {"Name": "E", "Role": "auditor"}, {"Name": "F", "Role": "CFO"}]
    out = imports.clean("user", rows, {"name": "Name", "role": "Role"})
    assert [r["role"] for r in out] == ["requester", "finance_manager", "project_accountant", "requester", "auditor", "cfo"]
    assert out[0]["job_title"] == "staff" and out[3]["job_title"] == "super_admin" and "job_title" not in out[4]


def test_a_vendor_bank_account_is_completed_from_the_vendor():
    rows = [{"Vendor": "Lumina Print LLC", "Ccy": "USD", "Country": "US", "Acct": "000123456780", "ABA": "026009593"}]
    out = imports.clean("vendor", rows, {"name": "Vendor", "currency": "Ccy", "address.country_code": "Country", "bank.account_number": "Acct", "bank.routing_value": "ABA"})
    b = out[0]["bank"]
    assert b["account_name"] == "Lumina Print LLC" and b["account_currency"] == "USD" and b["bank_country_code"] == "US"
