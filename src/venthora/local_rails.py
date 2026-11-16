# ruff: noqa: E501
"""Platform Setup → Local Rails: the Indonesian payment rails that Airwallex, Visa cards and Alta do not cover.

The page closes every gap the other menus leave, and nothing more: Indonesian bank transfers stay on Airwallex
(Payment → Transfer), cards on the Card program, currency conversion on Airwallex at payment time, proof on the
Blockchain Record. Two local providers, one tab each:

  Xendit    money going OUT to Indonesians who have no bank account VENTHORA can pay (e-wallets), and the holder's
            name of an Indonesian account before anything is paid
  Midtrans  money coming IN in rupiah through the local methods payers actually use (QRIS, bank virtual accounts,
            cash at convenience stores)

Each provider has the usual connection (its keys, write-only, sandbox only) and a list of products the Finance
Director turns on or off. A product marked "planned" is recorded as wanted; VENTHORA says so instead of pretending.
"""
from __future__ import annotations

import hashlib
import secrets

import httpx

from . import secret_values

KIND, ID = "local_rails", "settings"

# what the other menus cannot do in Indonesia, why, and the local rail that does it
GAPS = [
    {"need": "Pay a person or vendor into an e-wallet (OVO, DANA, GoPay, ShopeePay, LinkAja)",
     "why": "Airwallex's wallet payouts are Airwallex and PayPal only; a Visa card cannot top up an Indonesian e-wallet",
     "provider": "xendit", "product": "ewallet_payout"},
    {"need": "Confirm whose Indonesian bank account or e-wallet it is, before paying",
     "why": "Airwallex checks the format of an Indonesian account, not the holder's name",
     "provider": "xendit", "product": "name_check"},
    {"need": "Pay many staff wallets in one approval (field allowances, per diem, honoraria)",
     "why": "Airwallex batch transfers go to bank accounts only; most field staff are paid on e-wallets",
     "provider": "xendit", "product": "batch_ewallet"},
    {"need": "Be paid by QRIS, the one QR code every Indonesian bank app and e-wallet scans",
     "why": "Visa and Airwallex card acceptance is not QRIS; most Indonesian customers and donors pay by QRIS, not by card",
     "provider": "midtrans", "product": "qris"},
    {"need": "Give each customer invoice its own Indonesian bank virtual account, matched automatically",
     "why": "VENTHORA's Airwallex account pays out; it gives no BCA / BNI / BRI / Mandiri virtual account number per invoice",
     "provider": "midtrans", "product": "virtual_account"},
    {"need": "Be paid in cash by payers with no bank account or card",
     "why": "no card or bank rail reaches cash; Indomaret and Alfamart counters do",
     "provider": "midtrans", "product": "retail_cash"},
]

# already covered elsewhere: never offered again on this page
NOT_HERE = [
    ("Transfers to Indonesian bank accounts (BI-FAST / SKN)", "Airwallex · Payment → Transfer"),
    ("Company and staff cards, merchant limits", "Airwallex cards · Platform Setup → Card program"),
    ("Currency conversion (USD, SGD → IDR)", "Airwallex, just in time at payment"),
    ("Agent checkout at online merchants", "Airi · Platform Setup → Airi usage"),
    ("A record no one can rewrite", "Alta · Platform Setup → Blockchain Record"),
]

PROVIDERS = {
    "xendit": {
        "label": "Xendit", "site": "https://dashboard.xendit.co", "direction": "Money out · names",
        "summary": "Pays Indonesians where Airwallex cannot (e-wallets) and checks the holder's name of an Indonesian account first.",
        "products": [
            {"key": "ewallet_payout", "label": "Pay to e-wallet", "status": "active", "default": True,
             "channels": ["OVO", "DANA", "GoPay", "ShopeePay", "LinkAja"],
             "what": "Send rupiah straight to a person's e-wallet by their registered mobile number: field staff, drivers, freelancers, small vendors without a bank account. Checked, approved by a second person, sent once.",
             "where": "Payment → E-wallet payouts"},
            {"key": "name_check", "label": "Account-holder name check", "status": "active", "default": True,
             "channels": ["BCA", "BNI", "BRI", "Mandiri", "Permata", "CIMB Niaga", "BSI", "e-wallets"],
             "what": "Before a new Indonesian payee is paid, the account's registered holder name is read and compared with the payee's name; a mismatch stops the payment.",
             "where": "The bank check on every new Indonesian payee"},
            {"key": "batch_ewallet", "label": "Batch e-wallet payouts", "status": "planned", "default": False,
             "channels": ["OVO", "DANA", "GoPay", "ShopeePay", "LinkAja"],
             "what": "Many e-wallet payouts in one list and one approval (allowances for a field team), each still name-checked and sent once.",
             "where": "Payment → E-wallet payouts (batch)"},
        ],
    },
    "midtrans": {
        "label": "Midtrans", "site": "https://dashboard.sandbox.midtrans.com", "direction": "Money in · rupiah",
        "summary": "Collects rupiah through the methods Indonesian payers use, settled to the company's Indonesian bank account.",
        "products": [
            {"key": "qris", "label": "QRIS collection", "status": "planned", "default": False,
             "channels": ["GoPay", "OVO", "DANA", "ShopeePay", "LinkAja", "any mobile banking app"],
             "what": "One QR code per invoice or donation that any Indonesian bank app or e-wallet pays; the payment notice marks the invoice paid.",
             "where": "Customer invoices and donations"},
            {"key": "virtual_account", "label": "Bank virtual accounts", "status": "planned", "default": False,
             "channels": ["BCA", "BNI", "BRI", "Mandiri", "Permata", "CIMB Niaga"],
             "what": "Each invoice gets its own virtual account number at the payer's bank; the transfer is matched to the invoice without anyone reading bank statements.",
             "where": "Customer invoices"},
            {"key": "retail_cash", "label": "Cash at convenience stores", "status": "planned", "default": False,
             "channels": ["Indomaret", "Alfamart"],
             "what": "A payment code the payer settles in cash at the counter, for payers with no bank account or card.",
             "where": "Customer invoices and donations"},
        ],
    },
}


def _product(provider: str, key: str) -> dict:
    for p in PROVIDERS[provider]["products"]:
        if p["key"] == key:
            return p
    raise ValueError(f"unknown {provider} product {key}")


def chosen(org) -> dict:
    rec = org.get(KIND, ID) or {}
    out = {}
    for prov, d in PROVIDERS.items():
        mine = rec.get(prov) or {}
        out[prov] = {p["key"]: bool(mine.get(p["key"], p["default"])) for p in d["products"]}
    return out


def enabled(org, provider: str, key: str) -> bool:
    return chosen(org)[provider].get(key, False)


def save_products(org, body: dict, by: str) -> dict:
    cur = chosen(org)
    for prov, keys in (body or {}).items():
        if prov not in PROVIDERS:
            raise ValueError(f"unknown provider {prov}")
        for k, on in (keys or {}).items():
            _product(prov, k)
            cur[prov][k] = bool(on)
    org._put(KIND, {"_id": ID} | cur, by)
    return cur


# ---- Midtrans: the connection (sandbox only, like every provider VENTHORA calls) ----
MT_SERVER, MT_SERVER_ENV = "midtrans_server_key", "MIDTRANS_SERVER_KEY"
MT_CLIENT, MT_CLIENT_ENV = "midtrans_client_key", "MIDTRANS_CLIENT_KEY"
MT_API = "https://api.sandbox.midtrans.com"


def midtrans_settings(org) -> dict:
    s = org.get("midtrans", "settings") or {}
    return {"mode": s.get("mode", "off"), "merchant_id": s.get("merchant_id") or "",
            "server_key": secret_values.info(MT_SERVER, MT_SERVER_ENV), "client_key": secret_values.info(MT_CLIENT, MT_CLIENT_ENV),
            "notification_path": "/webhooks/midtrans"}


def save_midtrans(org, mode: str, by: str, merchant_id: str | None = None, server_key: str | None = None, client_key: str | None = None) -> dict:
    if mode not in ("off", "sandbox"):
        raise ValueError("mode is off or sandbox: VENTHORA runs on sandboxes")
    if server_key:
        if not server_key.strip().startswith("SB-Mid-server-"):
            raise ValueError("only a Midtrans sandbox server key (SB-Mid-server-...) is accepted")
        secret_values.put(MT_SERVER, server_key.strip(), by)
    if client_key:
        if not client_key.strip().startswith("SB-Mid-client-"):
            raise ValueError("only a Midtrans sandbox client key (SB-Mid-client-...) is accepted")
        secret_values.put(MT_CLIENT, client_key.strip(), by)
    mid = (merchant_id if merchant_id is not None else (org.get("midtrans", "settings") or {}).get("merchant_id") or "").strip()
    if mid and not (mid[:1].upper() == "G" and mid[1:].isdigit()):
        raise ValueError("a Midtrans merchant ID looks like G123456789")
    if mode == "sandbox" and not secret_values.get(MT_SERVER, MT_SERVER_ENV):
        raise ValueError("sandbox needs the Midtrans server key")
    org._put("midtrans", {"_id": "settings", "mode": mode, "merchant_id": mid.upper()}, by)
    return midtrans_settings(org)


def midtrans_test(client: httpx.Client | None = None) -> dict:
    """Ask Midtrans for the status of an order that cannot exist: a valid server key gets "doesn't exist" (404),
    a wrong one gets 401. Nothing is created or paid."""
    key = secret_values.get(MT_SERVER, MT_SERVER_ENV)
    if not key:
        return {"ok": False, "detail": "no server key set"}
    order = "venthora-connection-check-" + secrets.token_hex(4)
    try:
        r = (client or httpx).get(f"{MT_API}/v2/{order}/status", auth=(key, ""), headers={"Accept": "application/json"}, timeout=20)
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"Midtrans did not answer: {type(exc).__name__}"}
    try:
        body = r.json()
    except ValueError:
        body = {}
    code = str(body.get("status_code") or r.status_code)
    if code == "404":
        return {"ok": True, "detail": "server key accepted by the Midtrans sandbox (the test order does not exist, as expected)"}
    if code == "401" or r.status_code == 401:
        return {"ok": False, "detail": "Midtrans refused the server key (401): copy it again from Settings → Access Keys"}
    return {"ok": False, "detail": f"unexpected answer from Midtrans ({code}): {str(body.get('status_message') or r.text)[:160]}"}


def midtrans_signature_ok(n: dict) -> bool:
    """Midtrans signs each payment notification: SHA-512 of order_id + status_code + gross_amount + server key."""
    key = secret_values.get(MT_SERVER, MT_SERVER_ENV)
    if not key:
        return False
    want = hashlib.sha512(f"{n.get('order_id', '')}{n.get('status_code', '')}{n.get('gross_amount', '')}{key}".encode()).hexdigest()
    return secrets.compare_digest(want, str(n.get("signature_key") or ""))


def _amount(v) -> str:
    try:
        return f"{float(str(v)):.2f}"
    except ValueError:
        return ""


def midtrans_verify(n: dict, client: httpx.Client | None = None) -> dict:
    """Is this notification Midtrans' own? By its signature with VENTHORA's server key, or, when Midtrans signed it with
    another of the merchant's server keys, by reading the order back from Midtrans with VENTHORA's key: the same order,
    status and amount must be there. Returns {ok, how, reason}; nothing is trusted from the body alone."""
    key = secret_values.get(MT_SERVER, MT_SERVER_ENV)
    if not key:
        return {"ok": False, "how": None, "reason": "no server key set"}
    if midtrans_signature_ok(n):
        return {"ok": True, "how": "signature", "reason": None}
    order = str(n.get("order_id") or "")
    if not order or len(order) > 64 or not all(c.isalnum() or c in "-_.~" for c in order):
        return {"ok": False, "how": None, "reason": "signature wrong and no readable order id"}
    try:
        r = (client or httpx).get(f"{MT_API}/v2/{order}/status", auth=(key, ""), headers={"Accept": "application/json"}, timeout=15)
        s = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "how": None, "reason": f"signature wrong and Midtrans could not be read: {type(exc).__name__}"}
    if str(s.get("status_code")) == "404" or r.status_code == 404:
        return {"ok": False, "how": None, "reason": "signature wrong and the order does not exist at Midtrans"}
    same = (s.get("order_id") == order and s.get("transaction_status") == n.get("transaction_status")
            and _amount(s.get("gross_amount")) == _amount(n.get("gross_amount")))
    if same:
        return {"ok": True, "how": "read back from Midtrans", "reason": None}
    return {"ok": False, "how": None, "reason": "signature wrong and Midtrans reports a different status or amount"}


def view(org, xendit_settings: dict) -> dict:
    ch = chosen(org)
    provs = {}
    for k, d in PROVIDERS.items():
        provs[k] = {kk: v for kk, v in d.items() if kk != "products"} | {
            "products": [p | {"on": ch[k][p["key"]]} for p in d["products"]]}
    provs["xendit"]["connection"] = xendit_settings
    provs["midtrans"]["connection"] = midtrans_settings(org)
    return {"gaps": GAPS, "not_here": [{"what": a, "where": b} for a, b in NOT_HERE], "providers": provs}
