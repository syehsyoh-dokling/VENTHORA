# ruff: noqa: E501
"""Platform Setup -> Visa Intelligent Commerce: the Visa Developer project VENTHORA calls, and what it would ask Visa.

* Credentials: the Visa Developer sandbox project's API key and shared secret (X-Pay token authentication), entered
  once and never shown; VIC's own identifiers (consumer, enrolled card token) arrive with Visa's VIC onboarding.
* Connection test: GET sandbox.api.visa.com/vdp/helloworld with an X-Pay token: "xv2:" + timestamp + ":" +
  HMAC-SHA256(shared secret, timestamp + resource path + query + body), hex, as Visa's starter kit builds it.
* Purchase instruction: VENTHORA's mandate for a card-billed vendor, in the shape of Visa's initiate-purchase-
  instruction payload (a mandate with a decline threshold, the preferred merchant, the merchant category, an end
  date): previewed now, sent once the VIC sandbox credentials are there.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import time
import uuid

import httpx

from . import secret_values

SANDBOX = "https://sandbox.api.visa.com"
API_KEY, SHARED_SECRET = "visa.vdp.api_key", "visa.vdp.shared_secret"
CONSUMER_ID, TOKEN_ID = "visa.vic.consumer_id", "visa.vic.token_id"
ENV = {API_KEY: "VISA_API_KEY", SHARED_SECRET: "VISA_SHARED_SECRET", CONSUMER_ID: "VISA_VIC_CONSUMER_ID", TOKEN_ID: "VISA_VIC_TOKEN_ID"}


def credentials_info() -> dict:
    return {k.split(".")[-1]: secret_values.info(k, ENV[k]) for k in (API_KEY, SHARED_SECRET, CONSUMER_ID, TOKEN_ID)}


def save_credentials(values: dict, by: str) -> dict:
    for name in (API_KEY, SHARED_SECRET, CONSUMER_ID, TOKEN_ID):
        v = (values.get(name.split(".")[-1]) or "").strip()
        if v:
            secret_values.put(name, v, by)
    return credentials_info()


# -- getting the X-Pay shared secret from Visa ---------------------------------------------------------------------
# Visa shows a project's shared secret only encrypted with a public key the project uploads (Credentials -> X-Pay Token
# -> Public Key; only a Visa Admin can remove an uploaded key, so VENTHORA keeps one pair and reuses it). VENTHORA makes
# a 2048-bit RSA pair, keeps the private key write-only, gives the public key in the form Visa asks for (the body of
# the PEM, no header, no line breaks), and decrypts what Visa shows (base64, RSA-OAEP SHA-256) straight into the
# secrets store: the shared secret is never shown to anyone.
XPAY_PRIVATE = "visa.xpay.rsa_private_key"


def _private_key():
    from cryptography.hazmat.primitives import serialization

    pem = secret_values.get(XPAY_PRIVATE)
    return serialization.load_pem_private_key(pem.encode(), password=None) if pem else None


def xpay_public_key(by: str) -> dict:
    """VENTHORA's public key for Visa (created once, then always the same)."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key, created = _private_key(), False
    if key is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        secret_values.put(XPAY_PRIVATE, pem, by)
        created = True
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {"public_key": base64.b64encode(der).decode(), "fingerprint": hashlib.sha256(der).hexdigest()[:16], "created": created,
            "kept_since": secret_values.info(XPAY_PRIVATE)["updated_at"]}


def save_encrypted_secret(encrypted: str, by: str) -> dict:
    """Decrypt the shared secret Visa shows (base64) and keep it write-only; the value is never returned."""
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key = _private_key()
    if key is None:
        raise ValueError("create VENTHORA's public key first and upload it to Visa")
    import binascii
    import re

    size = key.key_size // 8  # 256 bytes for a 2048-bit key: what Visa's ciphertext must decode to
    text = "".join(encrypted.split()).strip("'\"")
    blob = None
    # Visa shows base64; a copy may carry URL-safe letters, missing padding, or come as hex: accept the one that
    # decodes to exactly one RSA block. Extra words copied around it (a label, the note) are ignored.
    candidates = [text, *re.findall(r"[A-Za-z0-9+/_=-]{300,}", encrypted)]
    for cand in candidates:
        for decode in (lambda t: base64.b64decode(t + "=" * (-len(t) % 4), validate=True),
                       lambda t: base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)),
                       lambda t: bytes.fromhex(t)):
            try:
                b = decode(cand)
            except (ValueError, binascii.Error):
                continue
            if len(b) == size:
                blob = b
                break
        if blob is not None:
            break
    if blob is None:
        raise ValueError(f"that is not the encrypted shared secret: it should be one block of base64 text (about {len(base64.b64encode(bytes(size)))} "
                         f"characters); {len(text)} characters were pasted")
    plain = None
    for oaep, mgf in ((hashes.SHA256(), hashes.SHA256()), (hashes.SHA256(), hashes.SHA1()), (hashes.SHA1(), hashes.SHA1())):  # Visa: OAEP SHA-256
        try:
            plain = key.decrypt(blob, padding.OAEP(mgf=padding.MGF1(algorithm=mgf), algorithm=oaep, label=None))
            break
        except ValueError:
            continue
    if plain is None:
        raise ValueError("it does not decrypt with VENTHORA's key: was it encrypted with the public key VENTHORA gave?")
    try:
        secret = plain.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("it decrypted, but not to text: was it encrypted with VENTHORA's current public key?") from exc
    if not (8 <= len(secret) <= 200) or any(c.isspace() for c in secret) or not secret.isprintable():
        raise ValueError(f"it decrypted, but not to a shared secret ({len(secret)} characters)")
    secret_values.put(SHARED_SECRET, secret, by)
    return credentials_info()


def xpay_token(resource_path: str, query: str, body: str, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    digest = hmac.new(secret.encode(), f"{ts}{resource_path}{query}{body}".encode(), hashlib.sha256).hexdigest()
    return f"xv2:{ts}:{digest}"


async def hello_world(client: httpx.AsyncClient | None = None) -> dict:
    """Visa's connectivity check for a sandbox project (no VIC onboarding needed)."""
    key, secret = secret_values.get(API_KEY, ENV[API_KEY]), secret_values.get(SHARED_SECRET, ENV[SHARED_SECRET])
    if not key or not secret:
        return {"ok": False, "status": None, "detail": "enter the project's API key and shared secret first"}
    query = f"apikey={key}"
    headers = {"x-pay-token": xpay_token("helloworld", query, "", secret), "accept": "application/json"}
    started = time.perf_counter()
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=20) as c:
                res = await c.get(f"{SANDBOX}/vdp/helloworld?{query}", headers=headers)
        else:
            res = await client.get(f"{SANDBOX}/vdp/helloworld?{query}", headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "detail": f"Visa did not answer: {type(exc).__name__}"}
    ms = round((time.perf_counter() - started) * 1000)
    try:
        body = res.json()
    except ValueError:
        body = {"text": res.text[:200]}
    detail = body.get("message") or body.get("responseStatus", {}).get("message") if isinstance(body, dict) else None
    return {"ok": res.status_code == 200, "status": res.status_code, "ms": ms, "detail": detail or str(body)[:200],
            "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}


# -- Visa Foreign Exchange Rates: Visa's daily rate beside the rate Airwallex charged --------------------------------
ISO_NUMERIC = {"USD": "840", "EUR": "978", "GBP": "826", "IDR": "360", "SGD": "702", "AUD": "036", "JPY": "392", "CNY": "156", "HKD": "344",
               "CAD": "124", "CHF": "756", "INR": "356", "MYR": "458", "PHP": "608", "THB": "764", "VND": "704", "NZD": "554", "AED": "784",
               "SAR": "682", "KRW": "410", "MXN": "484", "BRL": "986", "ZAR": "710", "SEK": "752", "NOK": "578", "DKK": "208", "PLN": "985"}
FX_PATH = "/forexrates/v2/foreignexchangerates"


async def fx_rate(source: str, destination: str, source_amount: str = "100", client: httpx.AsyncClient | None = None) -> dict:
    """Visa's daily card rate (rateProductCode A) to convert source -> destination, signed with the X-Pay token.
    conversionRate is destination units per one source unit, without markup."""
    import json

    key, secret = secret_values.get(API_KEY, ENV[API_KEY]), secret_values.get(SHARED_SECRET, ENV[SHARED_SECRET])
    if not key or not secret:
        return {"ok": False, "status": None, "detail": "enter the project's API key and shared secret first"}
    if source not in ISO_NUMERIC or destination not in ISO_NUMERIC:
        return {"ok": False, "status": None, "detail": f"no ISO number known for {source} or {destination}"}
    body = json.dumps({"rateProductCode": "A", "sourceCurrencyCode": ISO_NUMERIC[source], "destinationCurrencyCode": ISO_NUMERIC[destination],
                       "sourceAmount": str(source_amount)}, separators=(",", ":"))
    query = f"apikey={key}"
    headers = {"x-pay-token": xpay_token(FX_PATH.split("/", 2)[2], query, body, secret), "accept": "application/json", "content-type": "application/json"}
    url = f"{SANDBOX}{FX_PATH}?{query}"
    res = None
    for attempt in range(2):  # the sandbox answers 500 NPPS003 now and then for a pair it served a moment before: one retry
        try:
            if attempt:
                headers["x-pay-token"] = xpay_token(FX_PATH.split("/", 2)[2], query, body, secret)  # a fresh timestamp
            if client is None:
                async with httpx.AsyncClient(timeout=30) as c:
                    res = await c.post(url, content=body, headers=headers)
            else:
                res = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return {"ok": False, "status": None, "detail": f"Visa did not answer: {type(exc).__name__}"}
        if res.status_code != 500:
            break
    try:
        out = res.json()
    except ValueError:
        out = {"text": res.text[:300]}
    if res.status_code != 200:
        err = (out.get("errorResponse") or out.get("responseStatus") or out) if isinstance(out, dict) else out
        return {"ok": False, "status": res.status_code, "detail": str(err)[:300]}
    return {"ok": True, "status": 200, "source": source, "destination": destination, "conversion_rate": out.get("conversionRate"),
            "destination_amount": out.get("destinationAmount"), "rate_table": (out.get("currentFxRateTable") or {}).get("rateTableID"),
            "valid_until": (out.get("currentFxRateTable") or {}).get("rateExpirationDateTime"), "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}


def purchase_instruction(*, payee: str, amount_minor: int, currency: str, mcc: str | None, category: str, description: str,
                         until: str | None = None, recurring: bool = False) -> dict:
    """VENTHORA's mandate for one card-billed vendor, as Visa's initiate-purchase-instruction payload."""
    end = until or (dt.datetime.now(dt.UTC) + dt.timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mandate = {"mandateId": str(uuid.uuid4()), "declineThreshold": {"amount": f"{amount_minor / 100:.2f}", "currencyCode": currency},
               "effectiveUntilTime": end, "preferredMerchantName": payee, "merchantCategory": category,
               "merchantCategoryCode": mcc or "", "quantity": "1", "description": description[:100]}
    if recurring:
        mandate["recurringFrequency"] = "MONTHLY"
    return {"clientReferenceId": str(uuid.uuid4()),
            "consumerId": secret_values.get(CONSUMER_ID, ENV[CONSUMER_ID]) or "<from VIC onboarding>",
            "tokenId": secret_values.get(TOKEN_ID, ENV[TOKEN_ID]) or "<enrolled card token, from VIC onboarding>",
            "mandates": [mandate], "consumerPrompt": f"VENTHORA pays {payee} within its mandate"}


def vic_ready() -> dict:
    """Which pieces of a VIC call are in place (the rest comes with Visa's onboarding)."""
    have = {k: secret_values.info(k, ENV[k])["set"] for k in ENV}
    return {"vdp_project": have[API_KEY] and have[SHARED_SECRET], "consumer": have[CONSUMER_ID], "card_token": have[TOKEN_ID],
            "ready": all(have.values())}
