"""Black-Scholes pricing + greeks, so the backtester can value any strike on
any historical bar.

Historical option chains aren't freely available — you can't download what
NIFTY 24400CE traded at every hour last year. But you CAN reconstruct it: the
underlying path is known, the chain's real strikes/expiries/lot sizes come
from AngelOne's instrument master, and Black-Scholes prices the contract at
each step. Theta decay then falls out of the model instead of being a
hand-waved constant.

Caveat worth stating: BS assumes constant vol and no skew, so reconstructed
premiums are an approximation — good enough to compare strategies honestly,
not a substitute for real tick data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Standard strike intervals on NSE (used to snap to a tradeable strike).
STRIKE_STEP = {
    "NIFTY": 50.0, "BANKNIFTY": 100.0, "FINNIFTY": 50.0, "MIDCPNIFTY": 25.0,
    "SENSEX": 100.0,
}
LOT_SIZE = {
    "NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120, "SENSEX": 20,
}
RISK_FREE = 0.065  # ~India 10y


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float   # per DAY
    vega: float    # per 1 vol point


def bs(S: float, K: float, T_years: float, sigma: float, kind: str = "call",
       r: float = RISK_FREE) -> Greeks:
    """Black-Scholes price + greeks. T in years, sigma annualized."""
    kind = kind.lower()
    if T_years <= 1e-9 or sigma <= 1e-9 or S <= 0 or K <= 0:
        intrinsic = max(0.0, S - K) if kind in ("call", "ce") else max(0.0, K - S)
        return Greeks(intrinsic, 1.0 if intrinsic > 0 else 0.0, 0.0, 0.0, 0.0)

    sqrtT = math.sqrt(T_years)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T_years) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    disc = math.exp(-r * T_years)

    if kind in ("call", "ce"):
        price = S * _ncdf(d1) - K * disc * _ncdf(d2)
        delta = _ncdf(d1)
        theta = (-(S * _npdf(d1) * sigma) / (2 * sqrtT) - r * K * disc * _ncdf(d2))
    else:
        price = K * disc * _ncdf(-d2) - S * _ncdf(-d1)
        delta = _ncdf(d1) - 1.0
        theta = (-(S * _npdf(d1) * sigma) / (2 * sqrtT) + r * K * disc * _ncdf(-d2))

    gamma = _npdf(d1) / (S * sigma * sqrtT)
    vega = S * _npdf(d1) * sqrtT / 100.0
    return Greeks(price=max(0.0, price), delta=delta, gamma=gamma,
                  theta=theta / 365.0, vega=vega)


def snap_strike(spot: float, symbol: str = "NIFTY", offset_steps: int = 0) -> float:
    """Nearest tradeable strike, optionally N steps OTM/ITM."""
    step = STRIKE_STEP.get(symbol.upper(), max(round(spot * 0.01 / 5) * 5, 1.0))
    atm = round(spot / step) * step
    return float(atm + offset_steps * step)


def lot_size(symbol: str) -> int:
    return LOT_SIZE.get(symbol.upper(), 1)


def iv_estimate(realized_vol: float, dte_days: float) -> float:
    """Implied vol proxy. Options systematically price above realized vol
    (the variance risk premium), and short-dated contracts carry more.
    """
    base = max(realized_vol, 0.05)
    premium = 1.15 + (0.25 if dte_days <= 3 else 0.10 if dte_days <= 7 else 0.0)
    return min(base * premium, 2.5)
