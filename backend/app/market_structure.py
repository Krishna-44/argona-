"""Market structure — trend, support/resistance zones, volatility regime and
multi-timeframe alignment.

This is the context layer the rest of the desk keys off: you don't buy calls
into a confirmed downtrend, you don't fade a strong trend, and you size
differently in a high-volatility regime. Everything here is computed from
completed bars only (no lookahead).

Pieces:
  * trend()      — direction + strength from EMA slope, ADX, and swing
                   structure (higher-highs/higher-lows vs lower-highs/lower-lows)
  * sr_zones()   — swing extrema clustered into zones, scored by how many
                   times price respected them (touches) and how recent
  * regime()     — ATR percentile: quiet / normal / volatile
  * multi_timeframe() — the same trend read on higher timeframes, plus whether
                   they agree (the classic top-down filter)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

# How each base interval rolls up for multi-timeframe analysis.
ROLLUP = {
    "1m": ["5min", "15min", "1h"],
    "5m": ["15min", "1h", "1D"],
    "15m": ["1h", "4h", "1D"],
    "30m": ["2h", "1D"],
    "1h": ["4h", "1D", "1W"],
    "1d": ["1W", "1ME"],
    "1wk": ["1ME"],
}


# ---------------------------------------------------------------------------


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> tuple[float, float, float]:
    """Wilder ADX / +DI / -DI on the last bar. ADX>25 = trending."""
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    a = dx.ewm(alpha=1 / n, adjust=False).mean()
    f = lambda s: float(s.iloc[-1]) if len(s) and s.iloc[-1] == s.iloc[-1] else 0.0
    return f(a), f(pdi), f(mdi)


def swings(df: pd.DataFrame, win: int = 5) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Local swing highs and lows as (bar_index, price)."""
    h, l = df["High"], df["Low"]
    hi_mask = h == h.rolling(win * 2 + 1, center=True).max()
    lo_mask = l == l.rolling(win * 2 + 1, center=True).min()
    highs = [(i, float(h.iloc[i])) for i in range(len(df)) if bool(hi_mask.iloc[i])]
    lows = [(i, float(l.iloc[i])) for i in range(len(df)) if bool(lo_mask.iloc[i])]
    return highs, lows


# ---------------------------------------------------------------------------


@dataclass
class Trend:
    direction: str          # "up" | "down" | "side"
    strength: float         # 0..1
    adx: float
    plus_di: float
    minus_di: float
    ema_slope_pct: float
    structure: str          # "HH-HL" | "LH-LL" | "mixed"
    above_ema200: bool | None


def trend(df: pd.DataFrame) -> Trend:
    c = df["Close"]
    e20, e50 = _ema(c, 20), _ema(c, 50)
    look = min(10, max(2, len(c) // 10))
    slope = float((e20.iloc[-1] - e20.iloc[-1 - look]) / e20.iloc[-1 - look] * 100) if len(e20) > look else 0.0
    a, pdi, mdi = adx(df)

    # swing structure: are the last two highs and lows rising or falling?
    hi, lo = swings(df)
    struct = "mixed"
    if len(hi) >= 2 and len(lo) >= 2:
        hh = hi[-1][1] > hi[-2][1]
        hl = lo[-1][1] > lo[-2][1]
        lh = hi[-1][1] < hi[-2][1]
        ll = lo[-1][1] < lo[-2][1]
        if hh and hl:
            struct = "HH-HL"
        elif lh and ll:
            struct = "LH-LL"

    votes = 0
    votes += 1 if e20.iloc[-1] > e50.iloc[-1] else -1
    votes += 1 if slope > 0 else -1
    votes += 1 if pdi > mdi else -1
    votes += 1 if struct == "HH-HL" else (-1 if struct == "LH-LL" else 0)

    if a < 20:
        direction = "side"
    elif votes >= 2:
        direction = "up"
    elif votes <= -2:
        direction = "down"
    else:
        direction = "side"

    strength = max(0.0, min(1.0, (abs(votes) / 4) * min(a / 40.0, 1.0)))
    above200 = None
    if len(c) >= 200:
        above200 = bool(c.iloc[-1] > _ema(c, 200).iloc[-1])

    return Trend(direction=direction, strength=round(strength, 3), adx=round(a, 1),
                 plus_di=round(pdi, 1), minus_di=round(mdi, 1),
                 ema_slope_pct=round(slope, 3), structure=struct, above_ema200=above200)


# ---------------------------------------------------------------------------


@dataclass
class Zone:
    price: float
    kind: str        # support | resistance
    touches: int
    strength: float  # 0..1
    bars_ago: int

    def as_dict(self) -> dict:
        return asdict(self)


def sr_zones(df: pd.DataFrame, win: int = 5, max_zones: int = 4) -> list[Zone]:
    """Cluster swing extrema into zones and score them by touch count +
    recency. A level price revisited many times matters more than a one-off
    spike."""
    if len(df) < win * 3:
        return []
    atr_ = float(_atr(df).iloc[-1] or 0) or float(df["Close"].iloc[-1]) * 0.01
    tol = atr_ * 0.75  # cluster width
    price = float(df["Close"].iloc[-1])
    n = len(df)

    hi, lo = swings(df, win)
    pts = [(i, p, "resistance") for i, p in hi] + [(i, p, "support") for i, p in lo]
    if not pts:
        return []

    clusters: list[dict] = []
    for i, p, kind in sorted(pts, key=lambda x: x[1]):
        for cl in clusters:
            if abs(cl["price"] - p) <= tol:
                # merge into the existing zone (running mean, newest touch wins)
                cl["price"] = (cl["price"] * cl["n"] + p) / (cl["n"] + 1)
                cl["n"] += 1
                cl["last"] = max(cl["last"], i)
                break
        else:
            clusters.append({"price": p, "n": 1, "last": i, "kind": kind})

    out: list[Zone] = []
    for cl in clusters:
        kind = "resistance" if cl["price"] > price else "support"
        recency = 1.0 - min((n - 1 - cl["last"]) / max(n, 1), 1.0)
        strength = min(1.0, (cl["n"] / 4.0) * 0.7 + recency * 0.3)
        out.append(Zone(price=round(cl["price"], 2), kind=kind, touches=cl["n"],
                        strength=round(strength, 2), bars_ago=int(n - 1 - cl["last"])))

    sup = sorted([z for z in out if z.kind == "support"], key=lambda z: -z.price)[:max_zones]
    res = sorted([z for z in out if z.kind == "resistance"], key=lambda z: z.price)[:max_zones]
    return sup + res


# ---------------------------------------------------------------------------


@dataclass
class Regime:
    atr: float
    atr_pct: float        # ATR as % of price
    percentile: float     # where current ATR sits in its own history (0..1)
    label: str            # quiet | normal | volatile


def regime(df: pd.DataFrame) -> Regime:
    a = _atr(df).dropna()
    if a.empty:
        return Regime(0.0, 0.0, 0.5, "normal")
    cur = float(a.iloc[-1])
    pct = float((a < cur).mean())
    price = float(df["Close"].iloc[-1])
    label = "quiet" if pct < 0.33 else ("volatile" if pct > 0.75 else "normal")
    return Regime(atr=round(cur, 2), atr_pct=round(cur / price * 100, 2),
                  percentile=round(pct, 2), label=label)


# ---------------------------------------------------------------------------


def multi_timeframe(df: pd.DataFrame, interval: str = "1h") -> dict:
    """Trend on higher timeframes + whether they agree with the base."""
    base = trend(df)
    out = {"base": {"tf": interval, **asdict(base)}, "higher": [], "aligned": False,
           "alignment": "none"}
    rules = ROLLUP.get(interval, ["1D"])
    agree = 0
    total = 0
    for tf in rules:
        try:
            r = df.resample(tf).agg({"Open": "first", "High": "max", "Low": "min",
                                     "Close": "last", "Volume": "sum"}).dropna()
            if len(r) < 30:
                continue
            t = trend(r)
            out["higher"].append({"tf": tf, **asdict(t)})
            total += 1
            if t.direction == base.direction and base.direction != "side":
                agree += 1
        except Exception:
            continue
    if total:
        if agree == total and base.direction != "side":
            out["alignment"] = f"full ({base.direction})"
            out["aligned"] = True
        elif agree:
            out["alignment"] = f"partial ({agree}/{total})"
        else:
            out["alignment"] = "conflict"
    return out


def analyze(df: pd.DataFrame, interval: str = "1h") -> dict:
    """Full market-structure read for a symbol."""
    t = trend(df)
    z = sr_zones(df)
    r = regime(df)
    mtf = multi_timeframe(df, interval)
    price = float(df["Close"].iloc[-1])
    nearest_sup = max([x.price for x in z if x.kind == "support"], default=None)
    nearest_res = min([x.price for x in z if x.kind == "resistance"], default=None)
    return {
        "price": round(price, 2),
        "trend": asdict(t),
        "regime": asdict(r),
        "zones": [x.as_dict() for x in z],
        "nearest_support": nearest_sup,
        "nearest_resistance": nearest_res,
        "room_to_resistance_pct": (round((nearest_res / price - 1) * 100, 2) if nearest_res else None),
        "room_to_support_pct": (round((1 - nearest_sup / price) * 100, 2) if nearest_sup else None),
        "multi_timeframe": mtf,
        "bias": _bias(t, mtf),
    }


def _bias(t: Trend, mtf: dict) -> str:
    """What this structure says you should be doing."""
    if t.direction == "side":
        return "RANGE — fade extremes, avoid breakout entries"
    if mtf.get("aligned"):
        return f"STRONG {t.direction.upper()}TREND — trade with it, avoid counter-trend"
    if mtf.get("alignment", "").startswith("conflict"):
        return f"{t.direction.upper()} on base but higher timeframes disagree — low conviction"
    return f"{t.direction.upper()}TREND — partial timeframe agreement"
