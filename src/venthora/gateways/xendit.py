# ruff: noqa: E501
"""Xendit client: the Indonesian rails Airwallex does not cover.

Airwallex pays Indonesian bank accounts (SKN / BI-FAST), and VENTHORA keeps paying them there. Airwallex does not
pay e-wallets (its digital-wallet payouts are Airwallex and PayPal only, checked on the sandbox's beneficiary
schema for ID / IDR) and does not confirm the holder's name of an Indonesian account. Xendit is used for exactly
those two things, and nothing else:

  1. e-wallet payouts   POST /v2/payouts  (channel_code ID_OVO, ID_DANA, ID_GOPAY, ID_SHOPEEPAY, ID_LINKAJA)
  2. name check         the holder's name of an Indonesian bank account or e-wallet, before VENTHORA pays it
                        (Xendit's Name Validator is a separate service Xendit activates on request; until it is
                        active the live check answers "not available" and the bank check stays PASSED_FORMAT)

Modes (Platform Setup → Xendit):
  off        nothing goes to Xendit; e-wallet payouts are refused
  simulator  demo mode only: a local stand-in with Xendit's request and response shapes, so the whole flow runs
             without a Xendit account; every answer says simulated, no money exists or moves
  live       Xendit's API with the organisation's own key. Test-mode keys only (xnd_development_...): VENTHORA
             runs on sandboxes, and a production key is refused

Money: Xendit speaks IDR in whole rupiah; VENTHORA's IDR minor unit is the rupiah too (reporting.MINOR), so amounts
pass through unchanged. Every payout carries VENTHORA's reference as reference_id and an Idempotency-key kept
before the call, so a retry after a timeout returns the first payout instead of paying twice.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

import httpx

API_URL = "https://api.xendit.co"
WALLETS = {"ID_OVO": "OVO", "ID_DANA": "DANA", "ID_GOPAY": "GoPay", "ID_SHOPEEPAY": "ShopeePay", "ID_LINKAJA": "LinkAja"}
BANKS = {"ID_BCA": "Bank Central Asia", "ID_BNI": "Bank Negara Indonesia", "ID_BRI": "Bank Rakyat Indonesia",
         "ID_MANDIRI": "Bank Mandiri", "ID_PERMATA": "Bank Permata", "ID_CIMB": "CIMB Niaga", "ID_BSI": "Bank Syariah Indonesia"}
# the SWIFT code Airwallex keeps for an Indonesian bank -> Xendit's channel for the same bank (for the name check)
SWIFT_TO_CHANNEL = {"CENAIDJA": "ID_BCA", "BNINIDJA": "ID_BNI", "BRINIDJA": "ID_BRI", "BMRIIDJA": "ID_MANDIRI",
                    "BBBAIDJA": "ID_PERMATA", "BNIAIDJA": "ID_CIMB", "BSMDIDJA": "ID_BSI"}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "REVERSED"}
# what an e-wallet can take in one top-up: Xendit refuses a payout that would push the wallet above its limit
# (DESTINATION_MAXIMUM_LIMIT); VENTHORA refuses before asking, with the same words
WALLET_MAX_MINOR = 10_000_000


class XenditError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def channel_name(code: str) -> str:
    return WALLETS.get(code) or BANKS.get(code) or code


def is_wallet(code: str) -> bool:
    return code in WALLETS


class Xendit:
    """One client per request. `org` is needed only by the simulator (its balance and payouts are kept there)."""

    def __init__(self, mode: str, key: str = "", org=None, *, transport: httpx.AsyncBaseTransport | None = None):
        if mode not in ("off", "simulator", "live"):
            raise ValueError("mode is off, simulator or live")
        self.mode, self._key, self._org, self._transport = mode, key, org, transport
        if mode == "live":
            if not key:
                raise XenditError("Xendit is set to live but no secret key is saved", code="NOT_CONFIGURED")
            if not key.startswith("xnd_development_"):
                raise XenditError("only a Xendit test-mode key (xnd_development_...) is accepted: VENTHORA runs on sandboxes", code="PRODUCTION_KEY")

    @property
    def simulated(self) -> bool:
        return self.mode == "simulator"

    # ---- live HTTP ----
    async def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                       headers: dict | None = None) -> dict | list:
        if self.mode == "off":
            raise XenditError("Xendit is switched off (Platform Setup → Xendit)", code="OFF")
        async with httpx.AsyncClient(base_url=API_URL, auth=(self._key, ""), timeout=30, transport=self._transport) as c:
            try:
                r = await c.request(method, path, json=json, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise XenditError(f"Xendit could not be reached: {exc.__class__.__name__}", code="UNREACHABLE") from exc
        try:
            body = r.json()
        except ValueError:
            body = {"message": r.text[:300]}
        if r.status_code >= 400:
            raise XenditError(f"Xendit {r.status_code}: {body.get('message') or body.get('error_code') or 'refused'}",
                              status=r.status_code, code=body.get("error_code"))
        return body

    # ---- simulator state ----
    def _sim(self) -> dict:
        rec = self._org.get("xendit_sim", "state") or {}
        return {"_id": "state", "balance_minor": rec.get("balance_minor", 0), "payouts": rec.get("payouts", {}), "holders": rec.get("holders", {})}

    def _sim_save(self, st: dict) -> None:
        self._org._put("xendit_sim", st, "xendit-simulator")

    def sim_topup(self, amount_minor: int) -> int:
        st = self._sim()
        st["balance_minor"] += int(amount_minor)
        self._sim_save(st)
        return st["balance_minor"]

    # ---- the calls VENTHORA makes ----
    async def channels(self, currency: str = "IDR") -> list[dict]:
        """The payout channels: Xendit's own list when live; the simulator's catalogue otherwise."""
        if self.simulated:
            return ([{"channel_code": k, "channel_name": v, "channel_category": "EWALLET", "currency": "IDR"} for k, v in WALLETS.items()]
                    + [{"channel_code": k, "channel_name": v, "channel_category": "BANK", "currency": "IDR"} for k, v in BANKS.items()])
        rows = await self._request("GET", "/payouts_channels", params={"currency": currency})
        return [{"channel_code": r.get("channel_code"), "channel_name": r.get("channel_name"),
                 "channel_category": r.get("channel_category"), "currency": r.get("currency"),
                 "minimum": (r.get("amount_limits") or {}).get("minimum"), "maximum": (r.get("amount_limits") or {}).get("maximum")}
                for r in (rows if isinstance(rows, list) else rows.get("data", []))]

    async def balance(self, currency: str = "IDR") -> int:
        if self.simulated:
            return self._sim()["balance_minor"]
        r = await self._request("GET", "/balance", params={"account_type": "CASH", "currency": currency})
        return int(r.get("balance") or 0)

    async def check_name(self, channel_code: str, account_number: str, name: str) -> dict:
        """The holder's name of an account or wallet. {available, resolved_name, result: MATCH|MISMATCH|NOT_FOUND}."""
        if not self.simulated:
            # Xendit's Name Validator is activated per business on request (help@xendit.co); until it is, nothing
            # here claims a name was confirmed
            return {"available": False, "detail": "Xendit's name validator is not active for this account: ask Xendit to activate it"}
        acct = "".join(ch for ch in account_number if ch.isdigit())
        if acct.endswith("000"):
            return {"available": True, "simulated": True, "result": "NOT_FOUND", "resolved_name": None}
        # the simulator's register: the first name an account is checked with becomes its holder, so the same
        # number under another name is a mismatch (as at a bank); a number ending 999 belongs to someone else
        st = self._sim()
        holders = st.setdefault("holders", {})
        resolved = "BUDI SANTOSO" if acct.endswith("999") else holders.setdefault(f"{channel_code}|{acct}", name.upper().strip())
        self._sim_save(st)
        return {"available": True, "simulated": True, "resolved_name": resolved,
                "result": "MATCH" if _same_name(resolved, name) else "MISMATCH"}

    async def create_payout(self, *, reference_id: str, idempotency_key: str, channel_code: str, account_number: str,
                            account_holder_name: str, amount_minor: int, currency: str = "IDR", description: str = "") -> dict:
        body = {"reference_id": reference_id, "channel_code": channel_code, "currency": currency, "amount": int(amount_minor),
                "description": description[:100] or reference_id,
                "channel_properties": {"account_holder_name": account_holder_name, "account_number": account_number}}
        if not self.simulated:
            return _payout(await self._request("POST", "/v2/payouts", json=body, headers={"Idempotency-key": idempotency_key}))
        st = self._sim()
        for p in st["payouts"].values():  # the same key returns the first payout, as Xendit does
            if p["idempotency_key"] == idempotency_key:
                return _payout(p)
        if int(amount_minor) > st["balance_minor"]:
            raise XenditError("Xendit 400: INSUFFICIENT_BALANCE (simulated): top up the Xendit balance", status=400, code="INSUFFICIENT_BALANCE")
        pid = "disb-sim-" + secrets.token_hex(8)
        st["balance_minor"] -= int(amount_minor)
        st["payouts"][pid] = body | {"id": pid, "status": "ACCEPTED", "created": _now(), "updated": _now(), "idempotency_key": idempotency_key}
        self._sim_save(st)
        return _payout(st["payouts"][pid])

    async def get_payout(self, payout_id: str) -> dict:
        if not self.simulated:
            return _payout(await self._request("GET", f"/v2/payouts/{payout_id}"))
        st = self._sim()
        p = st["payouts"].get(payout_id)
        if p is None:
            raise XenditError("Xendit 404: DATA_NOT_FOUND (simulated)", status=404, code="DATA_NOT_FOUND")
        age = (datetime.now(UTC) - datetime.fromisoformat(p["created"])).total_seconds()
        if p["status"] == "ACCEPTED" and age >= 2:  # the channel answers a moment later
            acct = "".join(ch for ch in p["channel_properties"]["account_number"] if ch.isdigit())
            if acct.endswith("13"):
                p |= {"status": "FAILED", "failure_code": "INVALID_DESTINATION"}
                st["balance_minor"] += p["amount"]  # a failed payout comes back to the balance
            else:
                p["status"] = "SUCCEEDED"
            p["updated"] = _now()
            self._sim_save(st)
        return _payout(p)

    async def cancel_payout(self, payout_id: str) -> dict:
        if not self.simulated:
            return _payout(await self._request("POST", f"/v2/payouts/{payout_id}/cancel"))
        st = self._sim()
        p = st["payouts"].get(payout_id)
        if p is None or p["status"] != "ACCEPTED":
            raise XenditError("Xendit 400: only an ACCEPTED payout can be cancelled (simulated)", status=400, code="INVALID_STATUS")
        p |= {"status": "CANCELLED", "updated": _now()}
        st["balance_minor"] += p["amount"]
        self._sim_save(st)
        return _payout(p)


def _payout(p: dict) -> dict:
    """Xendit's payout, reduced to what VENTHORA keeps (never the full account number)."""
    cp = p.get("channel_properties") or {}
    acct = str(cp.get("account_number") or "")
    return {"id": p.get("id"), "reference_id": p.get("reference_id"), "status": p.get("status"),
            "channel_code": p.get("channel_code"), "amount_minor": int(p.get("amount") or 0), "currency": p.get("currency"),
            "account_holder_name": cp.get("account_holder_name"), "account_masked": "•••• " + acct[-4:] if acct else None,
            "failure_code": p.get("failure_code"), "created": p.get("created"), "updated": p.get("updated"),
            "estimated_arrival_time": p.get("estimated_arrival_time")}


def _same_name(a: str | None, b: str | None) -> bool:
    norm = lambda s: " ".join("".join(ch for ch in (s or "").upper() if ch.isalnum() or ch == " ").replace("PT ", "").split())  # noqa: E731
    return bool(a) and norm(a) == norm(b)


def fingerprint(channel_code: str, account_number: str, name: str) -> str:
    acct = "".join(ch for ch in account_number if ch.isdigit())
    return hashlib.sha256(f"{channel_code}|{acct}|{name.strip().upper()}".encode()).hexdigest()[:24]
