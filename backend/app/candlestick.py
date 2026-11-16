"""Candlestick pattern recognition — 15 classic patterns, vectorized pandas.

Spec from the `candlestick` skill (HKUDS/Vibe-Trading): 5 single-candle,
6 double-candle, 4 triple-candle, plus a trend-confirmation filter. The skill
ships only the spec (body_pct=0.1, shadow_ratio=2.0) with no formulas, so the
threshold definitions below are the standard textbook ones, stated explicitly
so they can be audited and tuned.

Scoring: bullish patterns score +1, bearish -1, neutral 0. The composite
signal is long when the sum > 0, short when < 0, flat at 0 — optionally
gated by trend confirmation.

No lookahead: a pattern on bar i uses only bars i, i-1, i-2 (all complete at
i's close). The backtester shifts signals one bar before acting on them.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

BULLISH = [
    "hammer", "inverted_hammer", "bullish_engulfing", "bullish_harami",
    "piercing_line", "morning_star", "three_white_soldiers",
]
BEARISH = [
    "shooting_star", "bearish_engulfing", "bearish_harami",
    "dark_cloud_cover", "evening_star", "three_black_crows",
]
NEUTRAL = ["doji", "spinning_top"]
ALL_PATTERNS = BULLISH + BEARISH + NEUTRAL


@dataclass
class CandleParams:
    body_pct: float = 0.10      # doji: body <= body_pct * range
    shadow_ratio: float = 2.0   # "long" shadow = shadow >= shadow_ratio * body
    small_body_pct: float = 0.35  # "small" body = body <= small_body_pct * range
    trend_len: int = 10         # SMA length for trend confirmation
    require_trend: bool = True  # gate reversal patterns on the prior trend


def _parts(df: pd.DataFrame) -> dict[str, pd.Series]:
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    return {
        "o": o, "h": h, "l": l, "c": c,
        "body": body, "rng": rng, "upper": upper, "lower": lower,
        "bull": c > o, "bear": c < o,
        "body_safe": body.replace(0, np.nan),
    }


def detect(df: pd.DataFrame, p: CandleParams | None = None) -> pd.DataFrame:
    """Return a boolean DataFrame: one column per pattern, indexed like df."""
    p = p or CandleParams()
    x = _parts(df)
    o, h, l, c = x["o"], x["h"], x["l"], x["c"]
    body, rng, upper, lower = x["body"], x["rng"], x["upper"], x["lower"]
    bull, bear, bsafe = x["bull"], x["bear"], x["body_safe"]

    small_body = body <= p.small_body_pct * rng
    long_lower = lower >= p.shadow_ratio * bsafe
    long_upper = upper >= p.shadow_ratio * bsafe

    # trend context: where price sits vs a short SMA (prior bar, no lookahead)
    sma = c.rolling(p.trend_len).mean()
    downtrend = (c.shift(1) < sma.shift(1))
    uptrend = (c.shift(1) > sma.shift(1))

    out = pd.DataFrame(index=df.index)

    # ---- single candle -----------------------------------------------------
    # Hammer: small body near the top, long lower shadow, short upper shadow.
    out["hammer"] = (small_body & long_lower & (upper <= body) &
                     (downtrend if p.require_trend else True))
    # Inverted hammer: long upper shadow, short lower, in a downtrend.
    out["inverted_hammer"] = (small_body & long_upper & (lower <= body) &
                              (downtrend if p.require_trend else True))
    # Shooting star: same shape as inverted hammer but after an UPtrend.
    out["shooting_star"] = (small_body & long_upper & (lower <= body) &
                            (uptrend if p.require_trend else True))
    # Doji: open ≈ close.
    out["doji"] = body <= p.body_pct * rng
    # Spinning top: small body, both shadows long-ish and roughly balanced.
    bal = (np.minimum(upper, lower) / np.maximum(upper, lower).replace(0, np.nan)) > 0.6
    out["spinning_top"] = small_body & bal & (upper > body) & (lower > body)

    # ---- double candle -----------------------------------------------------
    po, pc = o.shift(1), c.shift(1)
    pbull, pbear = bull.shift(1, fill_value=False), bear.shift(1, fill_value=False)
    pbody = body.shift(1)
    pmid = (po + pc) / 2

    out["bullish_engulfing"] = pbear & bull & (o <= pc) & (c >= po) & (body > pbody)
    out["bearish_engulfing"] = pbull & bear & (o >= pc) & (c <= po) & (body > pbody)
    # Harami: small current body contained inside the previous (larger) body.
    out["bullish_harami"] = pbear & bull & (o >= pc) & (c <= po) & (body < pbody)
    out["bearish_harami"] = pbull & bear & (o <= pc) & (c >= po) & (body < pbody)
    # Piercing line: opens below prior close, closes back above prior midpoint.
    out["piercing_line"] = pbear & bull & (o < pc) & (c > pmid) & (c < po)
    # Dark cloud: opens above prior close, closes back below prior midpoint.
    out["dark_cloud_cover"] = pbull & bear & (o > pc) & (c < pmid) & (c > po)

    # ---- triple candle -----------------------------------------------------
    o2, c2 = o.shift(2), c.shift(2)
    bull2 = bull.shift(2, fill_value=False)
    bear2 = bear.shift(2, fill_value=False)
    body2 = body.shift(2)
    small_prev = small_body.shift(1, fill_value=False)
    mid2 = (o2 + c2) / 2

    # Morning star: long bearish, small-bodied pause, strong bullish close
    # back above the midpoint of the first candle.
    out["morning_star"] = bear2 & small_prev & bull & (c > mid2) & (body2 > 0)
    # Evening star: mirror image.
    out["evening_star"] = bull2 & small_prev & bear & (c < mid2) & (body2 > 0)
    # Three white soldiers: 3 rising bullish candles, each opening inside the
    # previous body (no runaway gaps).
    out["three_white_soldiers"] = (
        bull2 & pbull & bull &
        (c > pc) & (pc > c2) &
        (o > po) & (o < pc) & (po < pc.shift(1).fillna(po))
    )
    out["three_black_crows"] = (
        bear2 & pbear & bear &
        (c < pc) & (pc < c2) &
        (o < po) & (o > pc) & (po > pc.shift(1).fillna(po))
    )

    return out.fillna(False).astype(bool)


def score(df: pd.DataFrame, p: CandleParams | None = None) -> pd.Series:
    """Composite score: +1 per bullish pattern, -1 per bearish, 0 for neutral."""
    pats = detect(df, p)
    s = pd.Series(0, index=df.index, dtype=int)
    for col in BULLISH:
        s = s + pats[col].astype(int)
    for col in BEARISH:
        s = s - pats[col].astype(int)
    return s


def signal(df: pd.DataFrame, p: CandleParams | None = None) -> pd.Series:
    """1 = long, -1 = short, 0 = stand aside (per the skill's convention)."""
    s = score(df, p)
    return np.sign(s).astype(int)


def summary(df: pd.DataFrame, p: CandleParams | None = None) -> dict:
    """Per-pattern hit counts + the latest bar's detected patterns — what the
    dashboard shows when you load a chart."""
    pats = detect(df, p)
    counts = {c: int(pats[c].sum()) for c in pats.columns}
    last = pats.iloc[-1]
    latest = [c for c in pats.columns if bool(last[c])]
    sc = int(score(df, p).iloc[-1])
    return {
        "counts": counts,
        "latest": latest,
        "latest_score": sc,
        "latest_signal": int(np.sign(sc)),
        "bars": int(len(df)),
        "bullish_patterns": BULLISH,
        "bearish_patterns": BEARISH,
        "neutral_patterns": NEUTRAL,
    }


# ---------------------------------------------------------------------------
# Support / resistance + risk levels — powers the "analyse this chart" button.


def levels(df: pd.DataFrame, lookback: int = 120, atr_len: int = 14,
           sl_atr: float = 1.5, tp_atr: float = 2.5) -> dict:
    """Swing-based support/resistance plus ATR stop-loss and take-profit for
    the current price, in both directions. Pure geometry + volatility — no
    prediction, no claim these levels will hold."""
    d = df.tail(max(lookback, atr_len + 2))
    h, l, c = d["High"], d["Low"], d["Close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(atr_len).mean().iloc[-1])
    price = float(c.iloc[-1])

    # swing highs/lows: local extrema over a 5-bar window
    win = 5
    is_high = (h == h.rolling(win * 2 + 1, center=True).max())
    is_low = (l == l.rolling(win * 2 + 1, center=True).min())
    swing_highs = sorted({round(float(v), 2) for v in h[is_high].dropna().tolist()})
    swing_lows = sorted({round(float(v), 2) for v in l[is_low].dropna().tolist()})

    resistance = [v for v in swing_highs if v > price][:3]
    support = [v for v in swing_lows if v < price][-3:][::-1]

    return {
        "price": round(price, 2),
        "atr": round(atr, 2) if atr == atr else None,
        "resistance": resistance,
        "support": support,
        "long": {
            "entry": round(price, 2),
            "stop_loss": round(price - sl_atr * atr, 2) if atr == atr else None,
            "take_profit": round(price + tp_atr * atr, 2) if atr == atr else None,
            "rr": round(tp_atr / sl_atr, 2),
        },
        "short": {
            "entry": round(price, 2),
            "stop_loss": round(price + sl_atr * atr, 2) if atr == atr else None,
            "take_profit": round(price - tp_atr * atr, 2) if atr == atr else None,
            "rr": round(tp_atr / sl_atr, 2),
        },
        "method": f"swing extrema ({win}-bar) + ATR{atr_len} · SL {sl_atr}×ATR / TP {tp_atr}×ATR",
    }
