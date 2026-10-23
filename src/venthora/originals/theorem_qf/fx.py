"""FX spot/forward arithmetic with every convention an explicit keyword.

Pair notation "BASE/QUOTE" = price of 1 BASE in QUOTE units ("EUR/USD" 1.10 = 1.10 USD per EUR,
"USD/JPY" 150 = 150 JPY per USD). "EURUSD", "EUR-USD", "eur usd" are accepted too.
Rates are DECIMALS (5% -> 0.05; divide quoted percents by 100 first). Year fractions are from
the SPOT date to delivery, per currency (see money_market_year_fraction).

Failure modes this module removes: forward points left in price units instead of pips, JPY pips
at 1e4, inverted cross-rate bid/ask, dealer sides built from mids, P&L sign ignoring direction.
"""
from __future__ import annotations

import itertools
import math

import numpy as np

# ----------------------------------------------------------------------------- pairs / pips

def parse_pair(pair) -> tuple[str, str]:
    """'EUR/USD' | 'EURUSD' | 'EUR-USD' | ('EUR','USD') -> ('EUR', 'USD') upper-case."""
    if isinstance(pair, (tuple, list)) and len(pair) == 2:
        return str(pair[0]).upper(), str(pair[1]).upper()
    s = str(pair).strip().upper()
    for sep in ("/", "-", "_", " ", "."):
        if sep in s:
            a, b = [x.strip() for x in s.split(sep, 1)]
            return a, b
    if len(s) == 6:
        return s[:3], s[3:]
    raise ValueError(f"cannot parse FX pair {pair!r}; use 'BASE/QUOTE' such as 'EUR/USD'")


def _pair_key(pair) -> str:
    b, q = parse_pair(pair)
    return f"{b}/{q}"


def pip_multiplier(pair=None, overrides=None) -> float:
    """Pips per 1.0 of price: 1e4, or 1e2 when QUOTE is JPY. pair=None -> 1e4.

    overrides: dict keyed by pair ('USD/HUF') or quote currency ('HUF') -> multiplier; wins over
    the default (pair key checked before currency key)."""
    if pair is None:
        return 1e4
    b, q = parse_pair(pair)
    if overrides:
        norm = {}
        for k, v in overrides.items():
            ks = str(k).upper()
            norm[_pair_key(ks) if len(ks) > 3 else ks] = float(v)
        if f"{b}/{q}" in norm:
            return norm[f"{b}/{q}"]
        if q in norm:
            return norm[q]
    return 1e2 if q == "JPY" else 1e4


def _mult(pair, multiplier):
    return float(multiplier) if multiplier is not None else pip_multiplier(pair)


def _num(x):
    """list/tuple -> float array; scalars stay scalars; numpy/pandas objects pass through."""
    return np.asarray(x, dtype=float) if isinstance(x, (list, tuple)) else x


def _out(x):
    return float(x) if np.ndim(x) == 0 else x


def forward_points(forward, spot, pair=None, multiplier=None):
    """Forward points in PIPS = (F - S) * multiplier (1e4; 1e2 for JPY quote; explicit
    multiplier wins). Scalars, numpy arrays or pandas Series. Points in price units = F - S."""
    return _out((_num(forward) - _num(spot)) * _mult(pair, multiplier))


def outright_from_points(spot, points, pair=None, multiplier=None):
    """Outright forward = spot + points_in_pips / multiplier (inverse of forward_points).
    Quotes where the bid points exceed the ask points ("high/low") are a DISCOUNT: pass them
    negative."""
    return _out(_num(spot) + _num(points) / _mult(pair, multiplier))


# ----------------------------------------------------------------------------- day counts

# Money-market deposit basis by currency (market default). Everything not listed -> ACT/360.
ACT365_CURRENCIES = frozenset({
    "GBP", "AUD", "NZD", "CAD", "HKD", "SGD", "ZAR", "INR", "MYR", "THB", "PLN", "TWD", "KRW",
    "ILS", "PHP",
})
ACT360_CURRENCIES = frozenset({
    "USD", "EUR", "JPY", "CHF", "SEK", "NOK", "DKK", "MXN", "CZK", "HUF", "TRY", "CNY", "IDR",
    "BRL", "RUB",
})


def money_market_basis(currency=None) -> float:
    """Day-count denominator (360 or 365) for a currency's money-market rate.
    ACT/365: GBP AUD NZD CAD HKD SGD ZAR INR MYR THB PLN TWD KRW ILS PHP.
    ACT/360: USD EUR JPY CHF SEK NOK DKK and every other currency (also currency=None)."""
    if currency is None:
        return 360.0
    return 365.0 if str(currency).upper() in ACT365_CURRENCIES else 360.0


def _basis_denominator(basis) -> float:
    if isinstance(basis, (int, float)):
        return float(basis)
    s = str(basis).upper().replace(" ", "").replace("ACTUAL", "ACT")
    if s in ("ACT/360", "A/360", "360"):
        return 360.0
    if s in ("ACT/365", "ACT/365F", "ACT/365FIXED", "A/365", "365"):
        return 365.0
    if len(s) == 3 and s.isalpha():          # a currency code where a basis was expected (6 Oct census)
        return money_market_basis(s)
    raise ValueError(f"unknown money-market basis {basis!r}; use 'ACT/360', 'ACT/365' or 360/365")


def money_market_year_fraction(days, currency=None, basis=None):
    """days / denominator. basis ('ACT/360' | 'ACT/365' | 360 | 365) overrides the currency
    table in money_market_basis (default ACT/360; ACT/365 for GBP AUD NZD CAD ...).
    days = calendar days from SPOT date to delivery. Accepts arrays."""
    den = _basis_denominator(basis) if basis is not None else money_market_basis(currency)
    return _out(_num(days) / den)


# ----------------------------------------------------------------------------- CIP

def _growth(rate, t, compounding):
    if compounding == "simple":
        return 1.0 + rate * t
    if compounding == "continuous":
        return math.exp(rate * t)
    m = int(compounding)
    return (1.0 + rate / m) ** (m * t)


def cip_forward(spot, r_base, r_quote, t_base, t_quote=None, *, compounding="simple"):
    """Covered-interest-parity outright forward (price of 1 BASE in QUOTE for delivery).

    simple (money-market, default): F = S (1 + r_quote t_quote) / (1 + r_base t_base)
    continuous: F = S exp(r_quote t_quote - r_base t_base)  (= S exp((r_q - r_b) T) if equal t)
    int m: annual-type compounding (1 + r/m)^(m t) on each side.
    t_quote=None -> t_quote = t_base. Rates in decimals. sign(F - S) = sign(r_quote - r_base)."""
    tq = t_base if t_quote is None else t_quote
    return float(spot) * _growth(r_quote, tq, compounding) / _growth(r_base, t_base, compounding)


def cip_forward_bid_ask(spot_bid, spot_ask, r_base_bid, r_base_ask, r_quote_bid, r_quote_ask,
                        t_base, t_quote=None):
    """Dealer two-way outright from two-way spot and deposit rates (money-market simple).

    Forward BID (dealer buys BASE forward): the dealer replicates by borrowing BASE now (pays the
    base OFFER/ask rate), selling it spot at spot BID, depositing QUOTE (earns the quote BID rate):
        F_bid = S_bid (1 + r_quote_bid t_quote) / (1 + r_base_ask t_base)
    Forward ASK: borrow QUOTE at its ask rate, buy BASE at spot ASK, deposit BASE at its bid rate:
        F_ask = S_ask (1 + r_quote_ask t_quote) / (1 + r_base_bid t_base)
    (bid rate <= ask rate for each currency, so F_bid <= F_ask). Returns (fwd_bid, fwd_ask).
    If only mid rates exist, pass the same rate as bid and ask."""
    tq = t_base if t_quote is None else t_quote
    if spot_bid > spot_ask or r_base_bid > r_base_ask or r_quote_bid > r_quote_ask:
        raise ValueError("each bid must be <= its ask (spot and both deposit rates)")
    fb = spot_bid * (1.0 + r_quote_bid * tq) / (1.0 + r_base_ask * t_base)
    fa = spot_ask * (1.0 + r_quote_ask * tq) / (1.0 + r_base_bid * t_base)
    return float(fb), float(fa)


def implied_quote_rate(spot, forward, r_base, t_base, t_quote=None):
    """Quote-ccy money-market rate implied by CIP: r_q = [(F/S)(1 + r_b t_b) - 1] / t_q."""
    tq = t_base if t_quote is None else t_quote
    return ((forward / spot) * (1.0 + r_base * t_base) - 1.0) / tq


def implied_base_rate(spot, forward, r_quote, t_base, t_quote=None):
    """Base-ccy money-market rate implied by CIP: r_b = [(S/F)(1 + r_q t_q) - 1] / t_b."""
    tq = t_base if t_quote is None else t_quote
    return ((spot / forward) * (1.0 + r_quote * tq) - 1.0) / t_base


def cip_deviation_bps(f_market, f_cip):
    """CIP deviation in basis points = (F_mkt - F_cip) / F_cip * 1e4.
    Positive = market forward ABOVE the CIP forward (BASE is dearer forward than parity; synthetic
    quote-ccy borrowing via the swap is cheaper than cash). Flip the sign if a task defines it
    the other way."""
    return (float(f_market) - float(f_cip)) / float(f_cip) * 1e4


def implied_basis(f_market, spot, r_base, r_quote, t_base, t_quote=None, *, compounding="simple"):
    """Cross-currency basis b ADDED TO THE BASE-ccy rate that reprices the market forward:
    simple: F_mkt = S (1 + r_q t_q) / (1 + (r_b + b) t_b)  ->  b = [S(1 + r_q t_q)/F_mkt - 1]/t_b - r_b
    continuous: b = ln(F_cip / F_mkt) / t_b.
    Negative b <=> F_mkt > F_cip (base-ccy funding via the swap is cheaper than its cash rate).
    Returned in decimals (x1e4 for bps)."""
    tq = t_base if t_quote is None else t_quote
    if compounding == "simple":
        return (spot * (1.0 + r_quote * tq) / f_market - 1.0) / t_base - r_base
    if compounding == "continuous":
        f_cip = cip_forward(spot, r_base, r_quote, t_base, tq, compounding="continuous")
        return math.log(f_cip / f_market) / t_base
    raise ValueError("compounding must be 'simple' or 'continuous'")


# ----------------------------------------------------------------------------- crosses

def invert_quote(bid, ask):
    """Two-way quote of the inverse pair: (1/ask, 1/bid). E.g. USD/JPY 150.00/150.10 ->
    JPY/USD (1/150.10, 1/150.00)."""
    return 1.0 / float(ask), 1.0 / float(bid)


def _orient(pair, bid, ask, base, quote):
    """Return (bid, ask) of `pair` expressed as base/quote (inverting if needed)."""
    b, q = parse_pair(pair)
    if (b, q) == (base, quote):
        return float(bid), float(ask)
    if (b, q) == (quote, base):
        return invert_quote(bid, ask)
    raise ValueError(f"{pair} is not {base}/{quote} or its inverse")


def cross_rate(pair1, bid1, ask1, pair2, bid2, ask2, target):
    """Cross two-way quote through the currency the two legs share (usually USD).

    Each leg is re-oriented to X/C and C/Y (inversion = (1/ask, 1/bid)), then
        X/Y bid = XC_bid * CY_bid,  X/Y ask = XC_ask * CY_ask,  mid = XC_mid * CY_mid
    which yields the textbook rules: X/USD & Y/USD -> bid = XUSD_bid / YUSD_ask; X/USD & USD/Y ->
    bid = bid*bid; USD/X & USD/Y -> X/Y bid = USDY_bid / USDX_ask. Returns (bid, ask, mid) with
    bid <= ask; mid uses leg mids ((bid+ask)/2, inverted as 1/mid)."""
    x, y = parse_pair(target)
    c1, c2 = set(parse_pair(pair1)), set(parse_pair(pair2))
    common = (c1 & c2) - {x, y}
    if len(common) != 1 or {x, y} != (c1 | c2) - common:
        raise ValueError(f"legs {pair1}, {pair2} do not triangulate to {target}")
    c = common.pop()
    legs = {frozenset(c1): (pair1, bid1, ask1), frozenset(c2): (pair2, bid2, ask2)}
    px, bx, ax = legs[frozenset({x, c})]
    py, by, ay = legs[frozenset({y, c})]
    xc_b, xc_a = _orient(px, bx, ax, x, c)
    cy_b, cy_a = _orient(py, by, ay, c, y)
    bid, ask = xc_b * cy_b, xc_a * cy_a

    def _mid(p, b_, a_, base, quote):
        m = 0.5 * (float(b_) + float(a_))
        return m if parse_pair(p) == (base, quote) else 1.0 / m

    mid = _mid(px, bx, ax, x, c) * _mid(py, by, ay, c, y)
    return float(bid), float(ask), float(mid)


def triangular_arbitrage(bid_ask_quotes, *, min_profit=0.0):
    """Scan every 3-currency cycle for a riskless gain at executable sides.

    bid_ask_quotes: {pair: (bid, ask)}. Converting X->Y with pair X/Y sells X at bid (Y = x*bid);
    with pair Y/X buys Y at ask (Y = x/ask). Returns a list (best first) of dicts
    {cycle: (A, B, C, A), gross: final units of A per 1 A, profit: gross - 1} with profit > min_profit.
    Empty list = no arbitrage (the normal case when crosses sit inside the triangulated spread)."""
    q = {parse_pair(p): (float(b), float(a)) for p, (b, a) in bid_ask_quotes.items()}

    def conv(frm, to):
        if (frm, to) in q:
            return q[(frm, to)][0]
        if (to, frm) in q:
            return 1.0 / q[(to, frm)][1]
        return None

    ccys = sorted({c for pr in q for c in pr})
    out = []
    for a, b, c in itertools.permutations(ccys, 3):
        if a != min(a, b, c):  # each cycle once per direction
            continue
        r1, r2, r3 = conv(a, b), conv(b, c), conv(c, a)
        if None in (r1, r2, r3):
            continue
        gross = r1 * r2 * r3
        if gross - 1.0 > min_profit:
            out.append({"cycle": (a, b, c, a), "gross": gross, "profit": gross - 1.0})
    return sorted(out, key=lambda d: -d["profit"])


# ----------------------------------------------------------------------------- P&L / exposure

_BUY = {"buy", "buy_base", "long", "b", "+1", "1", "bought", "long_base", "buybase", "longbase"}
_SELL = {"sell", "sell_base", "short", "s", "-1", "sold", "short_base", "sellbase", "shortbase"}


def direction_sign(direction) -> int:
    """+1 for buying BASE ('buy', 'buy_base', 'long', +1), -1 for selling BASE ('sell',
    'sell_base', 'short', -1). Anything else raises (no silent default)."""
    if isinstance(direction, (int, float, np.integer, np.floating)) and not isinstance(direction, bool):
        if direction > 0:
            return 1
        if direction < 0:
            return -1
        raise ValueError("direction 0 is ambiguous")
    s = str(direction).strip().lower().replace(" ", "_")
    if s in _BUY:
        return 1
    if s in _SELL:
        return -1
    raise ValueError(f"unknown direction {direction!r}; use 'buy'/'sell' (of BASE) or +1/-1")


def forward_mtm(notional_base, trade_rate, current_forward, direction="buy", *,
                discount_factor_quote=1.0):
    """Mark-to-market of an outstanding forward, in QUOTE ccy:
        V = sign * N_base * (F_now(T) - K) * DF_quote(now, T)
    sign = +1 bought BASE forward, -1 sold. current_forward = today's outright for the SAME
    delivery date. discount_factor_quote=1.0 -> undiscounted (state it if the task discounts).
    Convert to USD with to_usd(V, QUOTE, rates)."""
    s = direction_sign(direction)
    return s * float(notional_base) * (float(current_forward) - float(trade_rate)) * float(discount_factor_quote)


def to_usd(amount, ccy, usd_rates):
    """Convert `amount` of `ccy` to USD with mid rates usd_rates = {pair: mid}, e.g.
    {'EUR/USD': 1.10, 'USD/JPY': 150}. CCY/USD -> amount * mid; USD/CCY -> amount / mid.
    USD passes through. Raises if neither orientation is present (no silent triangulation)."""
    c = str(ccy).upper()
    if c == "USD":
        return float(amount)
    norm = {parse_pair(k): float(v) for k, v in usd_rates.items()}
    if (c, "USD") in norm:
        return float(amount) * norm[(c, "USD")]
    if ("USD", c) in norm:
        return float(amount) / norm[("USD", c)]
    raise KeyError(f"no {c}/USD or USD/{c} rate in usd_rates")


def forward_pnl_attribution(notional_base, direction, spot_then, spot_now, fwd_then, fwd_now,
                            *, total=None):
    """Split a forward position's P&L (QUOTE ccy) into spot and carry (forward-points) parts.
        spot_delta = sign * N * (S_now - S_then)
        carry      = sign * N * ((F_now - S_now) - (F_then - S_then))   # change in points
        total      = sign * N * (F_now - F_then)  (undiscounted), or the `total` you pass
                     (e.g. a discounted MTM change)
        residual   = total - spot_delta - carry   (0 when total is the undiscounted default)
    fwd_then/fwd_now are outrights for the same delivery date. Returns a dict of floats."""
    s = direction_sign(direction)
    n = float(notional_base)
    spot_delta = s * n * (float(spot_now) - float(spot_then))
    carry = s * n * ((float(fwd_now) - float(spot_now)) - (float(fwd_then) - float(spot_then)))
    tot = s * n * (float(fwd_now) - float(fwd_then)) if total is None else float(total)
    return {"spot_delta": spot_delta, "carry": carry, "total": tot,
            "residual": tot - spot_delta - carry}


def net_currency_exposure(trades):
    """Net signed amount per currency from FX trades.

    trades: iterable of dicts (or a pandas DataFrame) with keys pair, notional (BASE units),
    rate, direction; or tuples (pair, direction, notional, rate). A BUY of BASE/QUOTE for N at K
    is +N BASE and -N*K QUOTE; a SELL reverses both. Returns {ccy: amount}."""
    if hasattr(trades, "to_dict") and hasattr(trades, "columns"):
        trades = trades.to_dict("records")
    out: dict[str, float] = {}
    for t in trades:
        if isinstance(t, dict):
            pair, d, n, k = t["pair"], t["direction"], t["notional"], t["rate"]
        else:
            pair, d, n, k = t
        b, q = parse_pair(pair)
        s = direction_sign(d)
        out[b] = out.get(b, 0.0) + s * float(n)
        out[q] = out.get(q, 0.0) - s * float(n) * float(k)
    return out
