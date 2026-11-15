"""Time-based one-time passwords (RFC 6238, the codes an authenticator app shows), with the standard library only.

A second factor for the people who can approve a payment: the password is something they know, the code is something
they hold. Six digits, a new one every 30 seconds, SHA-1 (what every authenticator app implements). A code is accepted
for the step it belongs to and one step either side, so a slow clock or a slow typist is not locked out, and never twice.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_S, DIGITS, WINDOW = 30, 6, 1


def new_secret() -> str:
    """160 random bits, in the base32 an authenticator app takes."""
    return base64.b32encode(secrets.token_bytes(20)).decode()


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def counter(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_S)


def matching_counter(secret: str, code: str, now: float | None = None, after: int = -1) -> int | None:
    """The time step this code belongs to, when it is valid now and later than the last step used (`after`); else None."""
    text = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(text) != DIGITS:
        return None
    here = counter(now)
    for step in range(here - WINDOW, here + WINDOW + 1):
        if step > after and hmac.compare_digest(code_at(secret, step), text):
            return step
    return None


def uri(secret: str, account: str, issuer: str = "VENTHORA") -> str:
    """What an authenticator app reads (as a QR code or typed in)."""
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP_S}"
