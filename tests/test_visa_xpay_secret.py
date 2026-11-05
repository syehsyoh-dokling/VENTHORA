# ruff: noqa: E501
"""Visa shows a project's X-Pay shared secret only encrypted with the project's public key: VENTHORA makes the key
pair once (the same public key every time after), decrypts what Visa shows (RSA-OAEP SHA-256) and keeps the secret
write-only, never returned."""
import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from venthora import secret_values, visa_vic


@pytest.fixture
def vault(monkeypatch):
    store = {}
    monkeypatch.setattr(secret_values, "get", lambda k, env=None: store.get(k, ""))
    monkeypatch.setattr(secret_values, "put", lambda k, v, by: store.__setitem__(k, v))
    monkeypatch.setattr(secret_values, "info", lambda k, env=None: {"set": k in store, "hint": None, "source": "store" if k in store else None, "updated_at": "now", "updated_by": "x"})
    return store


def visa_encrypts(public_body: str, secret: str) -> str:
    """What Visa does: the shared secret encrypted with the uploaded public key, base64."""
    pub = serialization.load_der_public_key(base64.b64decode(public_body))
    return base64.b64encode(pub.encrypt(secret.encode(), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()


def test_one_key_pair_and_the_secret_kept_without_being_returned(vault):
    first = visa_vic.xpay_public_key("Rosa")
    again = visa_vic.xpay_public_key("Rosa")
    assert first["created"] and not again["created"] and first["public_key"] == again["public_key"]
    assert "BEGIN" not in first["public_key"] and "\n" not in first["public_key"]  # the form Visa asks for
    enc = visa_encrypts(first["public_key"], "Sh4red-Secret_from+Visa")
    info = visa_vic.save_encrypted_secret("\n" + enc[:40] + "\n" + enc[40:] + "  ", "Rosa")  # line breaks from a copy are fine
    assert vault[visa_vic.SHARED_SECRET] == "Sh4red-Secret_from+Visa" and "Sh4red" not in str(info)


def test_the_secret_is_read_from_any_copy_visa_s_page_gives(vault):
    pub = visa_vic.xpay_public_key("Rosa")["public_key"]
    for i, shape in enumerate((lambda e: e.replace("+", "-").replace("/", "_").rstrip("="),  # URL-safe, no padding
                               lambda e: "Encrypted Shared Secret\n" + e + "\nNote: Shared Secret is Encrypted using RSA/ECB/OAEPWithSHA-256AndMGF1Padding",
                               lambda e: base64.b64decode(e).hex())):  # hex
        secret = f"Visa-Secret-{i}-abcdef"
        visa_vic.save_encrypted_secret(shape(visa_encrypts(pub, secret)), "Rosa")
        assert vault[visa_vic.SHARED_SECRET] == secret


def test_a_wrong_paste_is_refused_and_nothing_is_kept(vault):
    visa_vic.xpay_public_key("Rosa")
    with pytest.raises(ValueError, match="base64"):
        visa_vic.save_encrypted_secret("not base64 at all !!!", "Rosa")
    other = serialization.load_pem_private_key(
        __import__("cryptography.hazmat.primitives.asymmetric.rsa", fromlist=["rsa"]).generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), password=None)
    der = other.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(ValueError, match="does not decrypt"):
        visa_vic.save_encrypted_secret(visa_encrypts(base64.b64encode(der).decode(), "x" * 20), "Rosa")
    assert visa_vic.SHARED_SECRET not in vault
