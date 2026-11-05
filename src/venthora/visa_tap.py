# ruff: noqa: E501
"""Platform Setup -> Visa Agent Identity (TAP): VENTHORA proves to a merchant that a request comes from it.

Visa's Trusted Agent Protocol signs each request with HTTP Message Signatures (RFC 9421), as Visa's sample agent does
(visa/trusted-agent-protocol, MIT): the signature base is the request's authority and path plus the signature
parameters (created, expires 8 minutes later, keyId, alg ed25519, a one-time nonce, and the tag: agent-browser-auth
to browse, agent-payer-auth to pay), signed with the agent's Ed25519 key and sent as Signature-Input / Signature
(label sig2). The merchant rebuilds the base from the request and checks it with the agent's public key, which
VENTHORA publishes (GET /v1/visa/tap/keys.json; through the gateway at /.well-known/venthora-agent.json).

The private key never leaves this machine's secrets store and is never shown.
"""
from __future__ import annotations

import base64
import datetime as dt
import time
import uuid
from urllib.parse import urlparse

from . import secret_values

PRIVATE = "visa.tap.private_key"
KEY_ID = "visa.tap.key_id"
PUBLIC = "visa.tap.public_key"
TAGS = {"agent-browser-auth": "browse a merchant's pages", "agent-payer-auth": "pay at the merchant's checkout"}
LIFETIME_S = 8 * 60


def generate(by: str) -> dict:
    """A new Ed25519 key pair (the old one stops signing at once; merchants read the new public key)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    priv = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = f"venthora-ed25519-{dt.datetime.now(dt.UTC).strftime('%Y%m%d%H%M%S')}"
    secret_values.put(PRIVATE, base64.b64encode(priv).decode(), by)
    secret_values.put(PUBLIC, base64.b64encode(pub).decode(), by)
    secret_values.put(KEY_ID, key_id, by)
    return public_info()


def public_info(domain: str | None = None) -> dict:
    pub, kid = secret_values.get(PUBLIC), secret_values.get(KEY_ID)
    meta = secret_values.info(PRIVATE)
    keys = [{"key_id": kid, "algorithm": "ed25519", "public_key": pub, "encoding": "base64 raw Ed25519", "created_at": meta["updated_at"],
             "is_active": "true"}] if pub and kid else []
    return {"agent": "VENTHORA treasury agent", "domain": domain, "keys": keys,
            "registry_format": {"name": "VENTHORA treasury agent", "domain": domain or "",
                                "keys": [{"key_id": k["key_id"], "public_key": k["public_key"], "algorithm": "ed25519",
                                          "description": "VENTHORA agent signing key"} for k in keys]}}


def _components(url: str) -> tuple[str, str]:
    u = urlparse(url)
    if not u.scheme or not u.netloc:
        raise ValueError("a full URL is needed, e.g. https://merchant.example/checkout")
    return u.netloc, u.path or "/"


def _params(created: int, expires: int, key_id: str, nonce: str, tag: str) -> str:
    return f'("@authority" "@path"); created={created}; expires={expires}; keyId="{key_id}"; alg="ed25519"; nonce="{nonce}"; tag="{tag}"'


def base_string(authority: str, path: str, params: str) -> str:
    return "\n".join([f'"@authority": {authority}', f'"@path": {path}', f'"@signature-params": {params}'])


def sign(url: str, tag: str = "agent-payer-auth", *, created: int | None = None, nonce: str | None = None) -> dict:
    """The two headers the agent sends with a request to `url`."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if tag not in TAGS:
        raise ValueError(f"the tag is one of {', '.join(TAGS)}")
    priv, kid = secret_values.get(PRIVATE), secret_values.get(KEY_ID)
    if not priv or not kid:
        raise ValueError("no signing key yet: generate one first")
    authority, path = _components(url)
    created = created or int(time.time())
    params = _params(created, created + LIFETIME_S, kid, nonce or str(uuid.uuid4()), tag)
    key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv))
    sig = base64.b64encode(key.sign(base_string(authority, path, params).encode())).decode()
    return {"Signature-Input": f"sig2={params}", "Signature": f"sig2=:{sig}:"}


def verify(url: str, headers: dict, public_key_b64: str, now: int | None = None) -> tuple[bool, str]:
    """What a merchant does: rebuild the base from the request and check it with the agent's public key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

    try:
        authority, path = _components(url)
        params = headers["Signature-Input"].split("=", 1)[1]
        sig = base64.b64decode(headers["Signature"].split("=", 1)[1].strip(":"))
        fields = dict(p.strip().split("=", 1) for p in params.split(";")[1:])
        created, expires = int(fields["created"]), int(fields["expires"])
    except (KeyError, ValueError, IndexError) as exc:
        return False, f"malformed signature headers ({type(exc).__name__})"
    t = now or int(time.time())
    if not created <= t <= expires:
        return False, "the signature is outside its validity window"
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(sig, base_string(authority, path, params).encode())
    except InvalidSignature:
        return False, "the signature does not match the request"
    return True, f"valid: signed by {fields.get('keyId', '').strip(chr(34))} for {authority}{path}, tag {fields.get('tag', '').strip(chr(34))}"
