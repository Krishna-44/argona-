"""Feature engineering for the options scalper.

Each candidate trade is (underlying, ATM option on the chosen side). Features
are computed strictly from data available at entry — no look-ahead. The vector
is fixed-width and ordered; FEATURE_NAMES is the contract shared by the model,
the SAE, and the dashboard.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .options import OptionQuote, Underlying

SOURCES = ["us", "crypto"]

FEATURE_NAMES = [
    "mom_fast",        # short-window underlying return
    "mom_slow",        # longer-window underlying return
    "trend",           # sign-weighted momentum agreement
    "realized_vol",    # stdev of underlying returns
    "iv",              # option implied vol
    "iv_minus_rv",     # IV richness vs realized vol
    "moneyness",       # (spot-strike)/spot
    "spread_pct",      # option bid-ask spread as frac of mid
    "log_volume",      # option volume
    "log_oi",          # option open interest
    "dte",             # days to expiry
    "range_pos",       # where spot sits in recent hi-lo range (0..1)
] + [f"src_{s}" for s in SOURCES]

N_FEATURES = len(FEATURE_NAMES)


def _returns(h: list[float]) -> list[float]:
    out = []
    for i in range(1, len(h)):
        if h[i - 1]:
            out.append((h[i] - h[i - 1]) / h[i - 1])
    return out


def _stdev(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


@dataclass
class FeatureVector:
    values: list[float]

    def as_list(self) -> list[float]:
        return self.values

    def named(self) -> dict[str, float]:
        return dict(zip(FEATURE_NAMES, self.values))


def persistence(history: list[float], want_up: bool, lookback: int = 8) -> float:
    """Fraction of the recent returns that agree with the intended direction.
    ~1.0 = a clean one-way move; ~0.5 = chop (noise). Used to reject scalps
    that fire on a single flickering tick."""
    rets = _returns(history)[-lookback:]
    if not rets:
        return 0.0
    agree = sum(1 for r in rets if (r > 0) == want_up)
    return agree / len(rets)


def underlying_momentum(u: Underlying) -> tuple[float, float]:
    """(fast, slow) % returns over the recent window."""
    h = u.history or [u.spot]
    fast_n = min(5, len(h) - 1) if len(h) > 1 else 0
    slow_n = min(20, len(h) - 1) if len(h) > 1 else 0
    fast = (h[-1] - h[-1 - fast_n]) / h[-1 - fast_n] if fast_n else 0.0
    slow = (h[-1] - h[-1 - slow_n]) / h[-1 - slow_n] if slow_n else 0.0
    return fast, slow


def extract(u: Underlying, opt: OptionQuote) -> FeatureVector:
    h = u.history or [u.spot]
    fast, slow = underlying_momentum(u)
    rets = _returns(h)
    rv = _stdev(rets) * math.sqrt(252)  # annualized-ish realized vol
    trend = (1.0 if fast > 0 else -1.0) * (1.0 if slow > 0 else -1.0) * min(abs(fast) * 50, 1.0)
    hi, lo = (max(h), min(h)) if h else (u.spot, u.spot)
    range_pos = ((u.spot - lo) / (hi - lo)) if hi > lo else 0.5

    # When the desk trades the UNDERLYING, the option-specific slots are
    # constants — iv=0, moneyness=0, dte=365, volume/OI are placeholders — so
    # half the vector carried no information and the model was spending
    # weight fitting them (dte, a hard-coded 365, had the second-largest
    # weight). Fill those slots with underlying signals that actually vary,
    # keeping the vector LENGTH identical so saved checkpoints stay loadable.
    is_underlying = opt.expiry == "" and "UNDERLYING" in (opt.instrument or "")
    if is_underlying:
        short = h[-8:] if len(h) >= 8 else h
        rv_short = _stdev(_returns(short)) * math.sqrt(252)
        mean_h = (sum(h) / len(h)) if h else u.spot
        dist_mean = ((u.spot - mean_h) / mean_h) if mean_h else 0.0
        accel = fast - slow            # momentum acceleration
        slot_iv = rv_short             # short-window realised vol
        slot_ivrv = rv_short - rv      # vol expanding vs its own baseline
        slot_money = dist_mean         # stretch from the recent mean
        slot_dte = accel
    else:
        slot_iv = opt.iv
        slot_ivrv = opt.iv - rv
        slot_money = opt.moneyness
        slot_dte = opt.dte

    base = [
        fast,
        slow,
        trend,
        rv,
        slot_iv,
        slot_ivrv,
        slot_money,
        opt.spread_pct,
        math.log10(max(opt.volume, 1.0)),
        math.log10(max(opt.open_interest, 1.0)),
        slot_dte,
        range_pos,
    ]
    onehot = [1.0 if u.source == s else 0.0 for s in SOURCES]
    return FeatureVector(values=base + onehot)
