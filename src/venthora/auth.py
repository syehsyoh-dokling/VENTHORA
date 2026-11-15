# ruff: noqa: E501
"""Sign-in for the people of one company (SaaS: each company's own VENTHORA, src/venthora/saas.py).

Who a person is decides everything they may do: once signed in, every request acts with that person's role and name,
whatever the request says (api.py, sessions middleware), so the unit scopes of the organisation hold for real:

  Root            the platform's operator (saas_gateway.py, not here)
  Super admin     the FCO: the whole company
  Country admin   its country office and everything under it
  Branch admin    its branch (under HQ or under a country office) and what is under it
  Project admin   its project finance unit
  Staff           procurement, finance, requesters and the rest: one level, told apart by the menus they see

Nobody types a password for someone else. An admin (or Root, for a new company's FCO) makes a one-time sign-in link for
a person they may manage; the person opens it and chooses their own password. A link works once and expires after
7 days; making a new one cancels the old one, which is also how a forgotten password is reset.

A person may add a second factor: a six-digit code from an authenticator app (totp.py). Once on, the password alone
no longer signs them in. It is the person's own choice to turn it on; turning it off takes a code.

Kept in the company's secrets database (never in the organisation records the API returns): scrypt password hashes,
the sign-in links and sessions, each stored only as a SHA-256 of its token, and the second factor's secret.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import data_path

MIN_PASSWORD = 10
INVITE_DAYS = 7
SESSION_HOURS = 12
MAX_FAILS, LOCK_S = 5, 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_password (user_id TEXT PRIMARY KEY, salt BLOB NOT NULL, hash BLOB NOT NULL, set_at TEXT NOT NULL,
  last_login TEXT);
CREATE TABLE IF NOT EXISTS auth_invite (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires TEXT NOT NULL,
  created_at TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auth_session (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auth_totp (user_id TEXT PRIMARY KEY, secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
  last_step INTEGER NOT NULL DEFAULT -1, created_at TEXT NOT NULL);
"""
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


class AuthError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


class AuthStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_SECRETS_DB", "data/venthora_secrets.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    # -- sign-in links --------------------------------------------------------------------------------------------
    def invite(self, user_id: str, by: str) -> str:
        """A one-time link token for this person; any earlier unused link of theirs stops working."""
        token = secrets.token_urlsafe(32)
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM auth_invite WHERE user_id = ?", (user_id,))
            c.execute("INSERT INTO auth_invite VALUES (?, ?, ?, ?, ?)",
                      (_h(token), user_id, _iso(_now() + timedelta(days=INVITE_DAYS)), _iso(_now()), by))
        return token

    def invite_user(self, token: str) -> str | None:
        """Whose link this is, while it is valid (not used up)."""
        with self._connect() as c:
            r = c.execute("SELECT user_id, expires FROM auth_invite WHERE token_hash = ?", (_h(token or ""),)).fetchone()
        return r["user_id"] if r and r["expires"] > _iso(_now()) else None

    def accept(self, token: str, password: str) -> str:
        """The person chooses their password with their link; the link is used up."""
        check_password(password)
        uid = self.invite_user(token)
        if uid is None:
            raise AuthError("this sign-in link is not valid any more: ask your admin for a new one")
        self.set_password(uid, password)
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM auth_invite WHERE user_id = ?", (uid,))
            c.execute("DELETE FROM auth_session WHERE user_id = ?", (uid,))  # a reset signs the old sessions out
        return uid

    # -- passwords ------------------------------------------------------------------------------------------------
    def set_password(self, user_id: str, password: str) -> None:
        salt = secrets.token_bytes(16)
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO auth_password VALUES (?, ?, ?, ?, NULL) ON CONFLICT(user_id) DO UPDATE SET salt = excluded.salt,"
                      " hash = excluded.hash, set_at = excluded.set_at", (user_id, salt, _scrypt(password, salt), _iso(_now())))

    def verify(self, user_id: str, password: str) -> bool:
        with self._connect() as c:
            r = c.execute("SELECT salt, hash FROM auth_password WHERE user_id = ?", (user_id,)).fetchone()
        if r is None:
            _scrypt(password, b"0" * 16)  # same work either way: the answer time does not say who has an account
            return False
        return hmac.compare_digest(_scrypt(password, r["salt"]), r["hash"])

    def status(self) -> dict[str, dict]:
        """Per person: has a password, last sign-in, a link waiting (never the link itself)."""
        out: dict[str, dict] = {}
        with self._connect() as c:
            for r in c.execute("SELECT user_id, set_at, last_login FROM auth_password"):
                out[r["user_id"]] = {"password_set": True, "set_at": r["set_at"], "last_login": r["last_login"]}
            for r in c.execute("SELECT user_id, expires FROM auth_invite"):
                if r["expires"] > _iso(_now()):
                    out.setdefault(r["user_id"], {"password_set": False})["link_waiting_until"] = r["expires"]
        return out

    # -- sessions -------------------------------------------------------------------------------------------------
    def session_new(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM auth_session WHERE expires <= ?", (_iso(_now()),))
            c.execute("INSERT INTO auth_session VALUES (?, ?, ?, ?)", (_h(token), user_id, _iso(_now() + timedelta(hours=SESSION_HOURS)), _iso(_now())))
            c.execute("UPDATE auth_password SET last_login = ? WHERE user_id = ?", (_iso(_now()), user_id))
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._connect() as c:
            r = c.execute("SELECT user_id, expires FROM auth_session WHERE token_hash = ?", (_h(token),)).fetchone()
        return r["user_id"] if r and r["expires"] > _iso(_now()) else None

    def session_end(self, token: str | None) -> None:
        if token:
            with self._lock, self._connect() as c:
                c.execute("DELETE FROM auth_session WHERE token_hash = ?", (_h(token),))


    # -- the second factor (totp.py): a code from the person's authenticator app, on top of the password ---------

    def totp_begin(self, user_id: str) -> str:
        """A new secret, not in force until the person proves their app shows its codes (totp_confirm)."""
        from . import totp, vault

        with self._lock, self._connect() as c:
            row = c.execute("SELECT enabled FROM auth_totp WHERE user_id = ?", (user_id,)).fetchone()
            if row and row["enabled"]:
                raise AuthError("the second factor is already on; turn it off first, with a code")
            secret = totp.new_secret()
            c.execute("INSERT INTO auth_totp VALUES (?, ?, 0, -1, ?) ON CONFLICT(user_id) DO UPDATE SET secret = excluded.secret,"
                      " enabled = 0, last_step = -1, created_at = excluded.created_at", (user_id, vault.seal(secret), _iso(_now())))
        return secret

    def totp_enabled(self, user_id: str) -> bool:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT enabled FROM auth_totp WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row["enabled"])

    def _totp_use(self, user_id: str, code: str, *, pending: bool, now: float | None = None) -> bool:
        """True when the code is right for this person now and was not used before; the step is then spent."""
        from . import totp, vault

        with self._lock, self._connect() as c:
            row = c.execute("SELECT secret, enabled, last_step FROM auth_totp WHERE user_id = ?", (user_id,)).fetchone()
            if row is None or bool(row["enabled"]) == pending:
                return False
            step = totp.matching_counter(vault.unseal(row["secret"]), code, now, after=row["last_step"])
            if step is None:
                return False
            c.execute("UPDATE auth_totp SET last_step = ?, enabled = 1 WHERE user_id = ?", (step, user_id))
        return True

    def totp_confirm(self, user_id: str, code: str, now: float | None = None) -> bool:
        """The first code from the app turns the second factor on."""
        return self._totp_use(user_id, code, pending=True, now=now)

    def totp_check(self, user_id: str, code: str, now: float | None = None) -> bool:
        return self._totp_use(user_id, code, pending=False, now=now)

    def totp_off(self, user_id: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM auth_totp WHERE user_id = ?", (user_id,))


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"choose a password of at least {MIN_PASSWORD} characters")
    if password.lower() == password or password.upper() == password or not any(ch.isdigit() for ch in password):
        raise AuthError("use upper and lower case letters and at least one digit")


def locked(email: str) -> bool:
    """Too many wrong passwords for this email lately: wait a few minutes."""
    with _fails_lock:
        recent = [t for t in _fails.get(email, []) if t > time.time() - LOCK_S]
        _fails[email] = recent
        return len(recent) >= MAX_FAILS


def failed(email: str) -> None:
    with _fails_lock:
        _fails.setdefault(email, []).append(time.time())


def cleared(email: str) -> None:
    with _fails_lock:
        _fails.pop(email, None)


def find_user(org, email: str) -> dict | None:
    e = (email or "").strip().lower()
    return next((u for u in org.list("user") if (u.get("email") or "").lower() == e and u.get("active", True)), None) if e else None
