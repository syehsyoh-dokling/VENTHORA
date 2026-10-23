"""Optional C++17 reserve gate loaded through ctypes (see native/money_gate.cpp)."""
from __future__ import annotations

import ctypes
from functools import lru_cache

from ..config import env

INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1


@lru_cache(maxsize=1)
def _load():
    path = env("VENTHORA_NATIVE_LIB")
    if not path:
        return None
    fn = ctypes.CDLL(path).venthora_money_gate
    fn.argtypes = [ctypes.c_int64] * 4
    fn.restype = ctypes.c_int
    return fn


def native_gate(available: int, debit: int, reserve: int, stress: int) -> bool | None:
    """True/False from the native gate, or None when it is not configured."""
    fn = _load()
    if fn is None:
        return None
    if any(not INT64_MIN <= v <= INT64_MAX for v in (available, debit, reserve, stress)):
        return False
    return bool(fn(available, debit, reserve, stress))
