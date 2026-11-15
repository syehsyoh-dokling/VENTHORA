# ruff: noqa: E501
"""Keys kept apart from the data they protect: secrets encrypted at rest, the audit chain keyed.

  VENTHORA_SECRETS_KEY   a Fernet key (AES-128-CBC + HMAC-SHA256) that seals every stored secret: bank and sponsor
                         keys, the SMTP password, second-factor secrets. A stored value reads "enc1:<token>".
  VENTHORA_AUDIT_KEY     the HMAC-SHA256 key that seals every audit event's hash: whoever can write the audit file
                         but does not hold this key cannot add, change or remove an event without it showing.

Each is read from the environment first (a secret manager injects it there in production); without one, a key file
is made once next to the data, readable by the service's user only, and named so a backup of the stores leaves it out
(backup_stores.py copies *.db only). The keys never enter a store, a log or an answer.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
from pathlib import Path

from .config import data_path, env

PREFIX = "enc1:"
_LOCK = threading.RLock()
_CACHE: dict[str, bytes] = {}


def _key(name: str, file_env: str, default_file: str, *, fernet: bool) -> bytes:
    with _LOCK:
        if name in _CACHE:
            return _CACHE[name]
        v = env(name)
        if not v:
            p = data_path(file_env, default_file)
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                v = base64.urlsafe_b64encode(os.urandom(32)).decode()
                try:
                    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:  # another process of the same company made it first: use that one
                    v = p.read_text(encoding="utf-8").strip()
                else:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(v)
        raw = v.encode()
        _CACHE[name] = raw if fernet else hashlib.sha256(raw).digest()
        return _CACHE[name]


def secrets_key() -> bytes:
    return _key("VENTHORA_SECRETS_KEY", "VENTHORA_SECRETS_KEY_FILE", "data/venthora_secrets.key", fernet=True)


def audit_key() -> bytes:
    return _key("VENTHORA_AUDIT_KEY", "VENTHORA_AUDIT_KEY_FILE", "data/venthora_audit.key", fernet=False)


def seal(value: str) -> str:
    """A secret as it is stored: encrypted, or as given when it is already sealed."""
    if not value or value.startswith(PREFIX):
        return value
    from cryptography.fernet import Fernet

    return PREFIX + Fernet(secrets_key()).encrypt(value.encode()).decode()


def unseal(stored: str) -> str:
    """The secret itself. A value stored before encryption was switched on is read as it is (and sealed when it is
    next written, or by seal_store)."""
    if not stored or not stored.startswith(PREFIX):
        return stored
    from cryptography.fernet import Fernet

    return Fernet(secrets_key()).decrypt(stored[len(PREFIX):].encode()).decode()


def mac(digest: str) -> str:
    """The audit key's seal on one event's hash."""
    return hmac.new(audit_key(), digest.encode(), hashlib.sha256).hexdigest()


def mac_ok(digest: str, sealed: str) -> bool:
    return hmac.compare_digest(mac(digest), sealed or "")


def reset_cache() -> None:  # tests switch keys between cases
    with _LOCK:
        _CACHE.clear()


__all__ = ["seal", "unseal", "mac", "mac_ok", "secrets_key", "audit_key", "PREFIX", "Path"]
