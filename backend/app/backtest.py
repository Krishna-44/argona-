"""Historical backtesting + walk-forward validation for the F&O scalp.

Why this exists: forward-testing live gives you ~5 trades a day — statistically
meaningless. You cannot tell edge from noise at that sample size. This module
runs the *same* signal logic over years of history (thousands of bars), prices
it with the real India F&O cost model, and validates it out-of-sample.

Methodology follows the vectorbt-backtesting skills:
  * **No lookahead** — every indicator used for a decision is `.shift(1)`ed, so
    a bar's signal only sees data strictly before it.
  * **Realistic costs** — India F&O options rate card on premium turnover
    (STT 0.1% sell, exch 0.03553%, GST, stamp, ₹20/order), applied per trade.
  * **Walk-forward** — optimise on in-sample, validate on unseen out-of-sample.
    Only OOS numbers are evidence of edge.
  * **Robustness** — parameter sweeps report whether good results sit in a wide
    stable region (real) or a lone spike (overfit).

Option modelling: we backtest the directional signal, then price it as an ATM
option scalp. An ATM option's premium is a small fraction of spot, so a small
underlying move is a large premium move:

    premium_move% ≈ underlying_move% × (spot / premium) × delta

with theta decay charged per bar. This is deliberately conservative — if the
signal has no edge on the underlying, it certainly has none after paying
option spread + decay.
"""
from __future__ import annotations

import os

import math
from dataclasses import dataclass, asdict, field
from typing import Iterable

import numpy as np
import pandas as pd

from . import charges

# Friendly name -> yfinance ticker. NIFTY/BANKNIFTY are the real Indian F&O
# underlyings; the rest mirror the live universe.
# Adverse fill assumption on entry/exit (1% of premium).
SLIPPAGE = 0.01

SYMBOLS: dict[str, str] = {
    # India
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MIDCAP_100.NS",
    # International — research runs on both books
    "SPY": "SPY",
    "QQQ": "QQQ",
    "AAPL": "AAPL",
    "NVDA": "NVDA",
    "TSLA": "TSLA",
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    # Europe / commodities / the coins Deribit does not list. These MUST be
    # listed here: an unmapped symbol falls through to the NSE-first guesses
    # (`FTSE.NS`, `GOLD.NS`, `SOL.NS`), which 404 on every cycle and left the
    # whole expanded universe producing zero signals.
    "DAX": "^GDAXI",
    "FTSE": "^FTSE",
    "CAC": "^FCHI",
    "GOLD": "GC=F",
    "CRUDE": "CL=F",
    "SILVER": "SI=F",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "DOGE": "DOGE-USD",
    "NIKKEI": "^N225",
    "HSI": "^HSI",
    "KOSPI": "^KS11",
    "ASX": "^AXJO",
    "STI": "^STI",
}


@dataclass
class BTParams:
    """Mirrors the live strategy knobs so the backtest validates what runs."""
    fast: int = 5                  # fast momentum lookback (bars)
    slow: int = 20                 # slow momentum lookback (bars)
    trend_threshold: float = 0.0003
    min_persistence: float = 0.62
    persist_lookback: int = 8
    tp_pct: float = 0.06           # take-profit on OPTION premium
    sl_pct: float = 0.04           # stop-loss on OPTION premium
    max_hold_bars: int = 12        # time-stop
    premium_frac: float = 0.012    # ATM premium as fraction of spot (~1.2%)
    delta: float = 0.5             # ATM option delta
    theta_per_bar: float = 0.004   # premium decay per bar (fraction of premium)
    risk_pct: float = 0.01         # fraction of equity risked per trade


@dataclass
class Trade:
    i_entry: int
    i_exit: int
    side: str
    entry_spot: float
    exit_spot: float
    entry_premium: float
    exit_premium: float
    qty: float
    gross: float
    charges_paid: float
    tax: float
    net: float
    reason: str


@dataclass
class BTResult:
    symbol: str
    bars: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    return_pct: float
    cagr_pct: float
    sharpe: float
    max_dd_pct: float
    profit_factor: float
    avg_net: float
    charges_paid: float
    tax_paid: float
    gross_pnl: float
    equity: list[float] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["equity"] = [round(x, 2) for x in self.equity[-400:]]  # cap payload
        return d


# ---------------------------------------------------------------------------
# data


def _period_days(period: str) -> int:
    """'2y' -> 730, '6mo' -> 180, '60d' -> 60."""
    p = period.strip().lower()
    try:
        if p.endswith("y"):
            return int(float(p[:-1]) * 365)
        if p.endswith("mo"):
            return int(float(p[:-2]) * 30)
        if p.endswith("d"):
            return int(float(p[:-1]))
        if p.endswith("wk"):
            return int(float(p[:-2]) * 7)
    except Exception:
        pass
    return 365


def fetch_history(symbol: str, period: str = "2y", interval: str = "1h",
                  min_bars: int = 60):
    """OHLCV for a friendly symbol. Returns a DataFrame or None.

    Unknown symbols are tried as-is and then with the NSE suffix, so typing a
    plain Indian share name (RELIANCE, TCS, INFY) resolves to RELIANCE.NS etc.
    """
    import yfinance as yf
    s = symbol.upper().strip()

    # Prefer AngelOne for Indian symbols on intraday intervals: yfinance caps
    # 5-minute history at 60 days, which is far too short to validate an
    # intraday strategy. SmartAPI returns years. Silently falls back below.
    if interval in ("1m", "3m", "5m", "10m", "15m", "30m", "1h"):
        try:
            from . import angelone
            if angelone.creds_present():
                days = _period_days(period)
                df = angelone.candles(s, interval, days)
                if df is not None and len(df) >= min_bars:
                    return df
        except Exception:
            pass

    candidates: list[str] = []
    if s in SYMBOLS:
        candidates.append(SYMBOLS[s])
    else:
        # NSE first: on an India F&O desk a bare "INFY" means the NSE listing,
        # not the US ADR (which yfinance would otherwise match).
        candidates.extend([f"{s}.NS", s, f"{s}.BO"])
    for ticker in candidates:
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval)
            if df is None or df.empty or len(df) < min_bars:
                continue
            return df.dropna(subset=["Close"])
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# signal generation — NO LOOKAHEAD


def _shift1(a: np.ndarray) -> np.ndarray:
    out = np.roll(a, 1).astype(float)
    out[0] = np.nan
    return out


def _ema(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if len(a) == 0:
        return out
    k = 2 / (n + 1)
    acc = a[0]
    for i, v in enumerate(a):
        acc = v if i == 0 else v * k + acc * (1 - k)
        out[i] = acc
    return out


def fib_pivots(df) -> pd.DataFrame:
    """Fibonacci pivot levels from the PREVIOUS day's OHLC, broadcast onto
    each intraday bar (no lookahead — today uses yesterday's range).

        P  = (H + L + C) / 3
        R1/S1 = P ± 0.382 * (H - L)
        R2/S2 = P ± 0.618 * (H - L)
        R3/S3 = P ± 1.000 * (H - L)
    """
    d = df.copy()
    day = d.index.normalize() if hasattr(d.index, "normalize") else pd.Series(range(len(d)))
    daily = d.groupby(day).agg({"High": "max", "Low": "min", "Close": "last"})
    prev = daily.shift(1)
    rng = prev["High"] - prev["Low"]
    piv = pd.DataFrame(index=prev.index)
    piv["P"] = (prev["High"] + prev["Low"] + prev["Close"]) / 3
    for name, f in (("R1", 0.382), ("R2", 0.618), ("R3", 1.0)):
        piv[name] = piv["P"] + f * rng
    for name, f in (("S1", 0.382), ("S2", 0.618), ("S3", 1.0)):
        piv[name] = piv["P"] - f * rng
    return piv.reindex(day).set_index(d.index)


def _ut_bot(close: np.ndarray, atr: np.ndarray, sensitivity: float = 2.0) -> np.ndarray:
    """UT Bot ATR trailing stop (ported from the Indian F&O terminal).
    Returns the trailing stop line; position is +1 when close is above it."""
    n = len(close)
    traj = np.zeros(n)
    x = atr * sensitivity
    traj[0] = close[0] - (x[0] if x[0] == x[0] else 0.0)
    for i in range(1, n):
        prev, curr, cx = traj[i - 1], close[i], x[i]
        if cx != cx:
            traj[i] = prev
            continue
        if curr > prev and close[i - 1] > prev:
            traj[i] = max(prev, curr - cx)
        elif curr < prev and close[i - 1] < prev:
            traj[i] = min(prev, curr + cx)
        elif curr > prev:
            traj[i] = curr - cx
        else:
            traj[i] = curr + cx
    return traj


def _vwap(df) -> np.ndarray:
    """Session VWAP — resets each day (that is what makes it VWAP, not a
    cumulative average over the whole history)."""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df["Volume"].replace(0, np.nan).fillna(1.0)
    try:
        day = df.index.normalize()
    except Exception:
        day = pd.Series(0, index=df.index)
    pv = (tp * vol).groupby(day).cumsum()
    vv = vol.groupby(day).cumsum()
    return (pv / vv).to_numpy()


def _supertrend_dir(df, length: int = 10, mult: float = 3.0) -> np.ndarray:
    """Supertrend direction: +1 bullish, -1 bearish."""
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / length, adjust=False).mean()
    hl2 = (h + l) / 2
    ub = (hl2 + mult * atr).to_numpy()
    lb = (hl2 - mult * atr).to_numpy()
    cl = c.to_numpy()
    n = len(cl)
    fub, flb = ub.copy(), lb.copy()
    for i in range(1, n):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or cl[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or cl[i - 1] < flb[i - 1]) else flb[i - 1]
    d = np.ones(n)
    for i in range(1, n):
        if cl[i] > fub[i - 1]:
            d[i] = 1
        elif cl[i] < flb[i - 1]:
            d[i] = -1
        else:
            d[i] = d[i - 1]
    return d


def _rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(close, prepend=close[0])
    gain = pd.Series(np.where(d > 0, d, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    loss = pd.Series(np.where(d < 0, -d, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).to_numpy()


def ut_score(df) -> np.ndarray:
    """Composite score from the Indian F&O terminal's model:
    UT Bot +/-2, Supertrend +/-2, VWAP +/-1, RSI +/-1  -> range -6..+6."""
    close = df["Close"].to_numpy(dtype=float)
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr10 = tr.rolling(10).mean().to_numpy()

    ut = _ut_bot(close, atr10)
    score = np.where(close > ut, 2.0, -2.0)
    score = score + np.where(_supertrend_dir(df) > 0, 2.0, -2.0)
    vw = _vwap(df)
    score = score + np.where(close > vw, 1.0, -1.0)
    r = _rsi(close)
    score = score + np.where((r > 55) & (r < 70), 1.0,
                             np.where((r > 30) & (r < 45), -1.0, 0.0))
    return score


def catalog_signals(df, name: str) -> tuple[np.ndarray, np.ndarray]:
    """Signals for the strategy catalog, all shifted one bar (no lookahead).

    Implements the catalog strategies from the vectorbt skills, plus the
    candlestick pattern engine, so they can be tested head-to-head on the
    same costs and the same walk-forward machinery.
    """
    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    n = len(close)
    longs = np.zeros(n, dtype=bool)
    shorts = np.zeros(n, dtype=bool)

    # NSE session-structure strategies live in india_fno but are dispatched
    # here so they inherit the same costs, event study and random baseline as
    # everything else — a new strategy must not get an easier exam.
    from . import india_fno
    if name in india_fno.INDIA_SESSION:
        return india_fno.india_signals(df, name)

    if name == "ema_crossover":
        f, s = _ema(close, 10), _ema(close, 20)
        fs, ss = _shift1(f), _shift1(s)
        up = (fs > ss)
        longs = up & ~np.r_[False, up[:-1]]
        shorts = (~up) & np.r_[False, up[:-1]]

    elif name == "donchian":
        per = 20
        up = pd.Series(high).rolling(per).max().shift(1).to_numpy()
        dn = pd.Series(low).rolling(per).min().shift(1).to_numpy()
        longs = np.nan_to_num(close > up, nan=0).astype(bool)
        shorts = np.nan_to_num(close < dn, nan=0).astype(bool)

    elif name == "momentum":
        L = 12
        mom = np.full(n, np.nan)
        mom[L:] = close[L:] - close[:-L]
        m1 = _shift1(mom)
        m2 = _shift1(_shift1(mom))
        longs = np.nan_to_num((m1 > 0) & (m1 > m2), nan=0).astype(bool)
        shorts = np.nan_to_num((m1 < 0) & (m1 < m2), nan=0).astype(bool)

    elif name == "supertrend":
        per, mult = 10, 3.0
        h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
        pc = c.shift(1)
        tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr = tr.rolling(per).mean()
        hl2 = (h + l) / 2
        upper = (hl2 + mult * atr).shift(1).to_numpy()
        lower = (hl2 - mult * atr).shift(1).to_numpy()
        longs = np.nan_to_num(close > upper, nan=0).astype(bool)
        shorts = np.nan_to_num(close < lower, nan=0).astype(bool)

    elif name == "scalp_momentum":
        # Mirrors strategy._scalp_dir exactly, so the validation tests the
        # signal the live desk would actually fire on — not an approximation.
        # fast = 5-bar return, slow = 20-bar return, both shifted so no bar
        # sees itself.
        move = float(os.environ.get("ARGONA_SCALP_MOVE", "0.0015"))
        c = pd.Series(close)
        fast = (c / c.shift(5) - 1.0).shift(1).to_numpy()
        slow = (c / c.shift(20) - 1.0).shift(1).to_numpy()
        fast = np.nan_to_num(fast, nan=0.0)
        slow = np.nan_to_num(slow, nan=0.0)
        longs = (fast >= move) & (slow >= 0)
        shorts = (fast <= -move) & (slow <= 0)

    elif name == "rsi":
        per = 14
        d = np.diff(close, prepend=close[0])
        gain = pd.Series(np.where(d > 0, d, 0.0)).rolling(per).mean()
        loss = pd.Series(np.where(d < 0, -d, 0.0)).rolling(per).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100 / (1 + rs)).shift(1).to_numpy()
        longs = np.nan_to_num(rsi < 30, nan=0).astype(bool)
        shorts = np.nan_to_num(rsi > 70, nan=0).astype(bool)


    elif name == "ema_pivot_break":
        # User strategy: 5-min chart, EMA(9) + Fibonacci pivots.
        # LONG  : a GREEN candle that breaks above BOTH the EMA and a pivot
        #         level in the same bar and CLOSES above both.
        # SHORT : a RED candle that breaks below both and closes below both.
        o = df["Open"].to_numpy(dtype=float)
        ema9 = _ema(close, 9)
        piv = fib_pivots(df)
        levels = [piv[c].to_numpy(dtype=float) for c in ("P", "R1", "R2", "R3", "S1", "S2", "S3")]
        prev_c = np.roll(close, 1); prev_c[0] = close[0]
        prev_e = np.roll(ema9, 1); prev_e[0] = ema9[0]
        green = close > o
        red = close < o
        # fresh break of the EMA in this candle
        up_ema = (prev_c <= prev_e) & (close > ema9)
        dn_ema = (prev_c >= prev_e) & (close < ema9)
        up_piv = np.zeros(n, dtype=bool)
        dn_piv = np.zeros(n, dtype=bool)
        for lv in levels:
            with np.errstate(invalid="ignore"):
                up_piv |= np.nan_to_num((prev_c <= lv) & (close > lv), nan=0).astype(bool)
                dn_piv |= np.nan_to_num((prev_c >= lv) & (close < lv), nan=0).astype(bool)
        longs = green & up_ema & up_piv
        shorts = red & dn_ema & dn_piv


    elif name == "ut_score":
        # Indian F&O terminal model: enter LONG at score >= +4, SHORT at <= -4.
        sc = ut_score(df)
        s1 = np.roll(sc, 1).astype(float); s1[0] = 0.0   # no lookahead
        longs = s1 >= 4.0
        shorts = s1 <= -4.0


    elif name in ("hybrid", "hybrid_strict", "hybrid_loose"):
        # Candlestick pattern = the TIMING signal (a specific bar says
        # reverse/continue). ut_score = the STATE confirmation (UT Bot +
        # Supertrend + VWAP + RSI agreeing on direction). Take the pattern
        # only when the stack agrees with it — classic pattern+confirmation.
        from . import candlestick as cs_mod
        conf = {"hybrid_loose": 1.0, "hybrid": 2.0, "hybrid_strict": 4.0}[name]
        cs = cs_mod.signal(df).to_numpy().astype(float)
        ut = ut_score(df)
        cs1 = np.roll(cs, 1).astype(float); cs1[0] = 0.0      # no lookahead
        ut1 = np.roll(ut, 1).astype(float); ut1[0] = 0.0
        longs = (cs1 > 0) & (ut1 >= conf)
        shorts = (cs1 < 0) & (ut1 <= -conf)


    elif name in ("hybrid_contra", "hybrid_contra_strict"):
        # Contrarian pairing: take the candlestick REVERSAL pattern when the
        # indicator stack is still leaning the OTHER way (i.e. the prevailing
        # move is exhausting). The trend-confirming version buys reversals
        # late into an established move, which tested badly — this is the
        # hypothesis that pattern edge lives where the pattern disagrees.
        from . import candlestick as cs_mod
        conf = 2.0 if name == "hybrid_contra" else 4.0
        cs = cs_mod.signal(df).to_numpy().astype(float)
        ut = ut_score(df)
        cs1 = np.roll(cs, 1).astype(float); cs1[0] = 0.0
        ut1 = np.roll(ut, 1).astype(float); ut1[0] = 0.0
        longs = (cs1 > 0) & (ut1 <= -conf)
        shorts = (cs1 < 0) & (ut1 >= conf)


    elif name in ("hybrid_event", "hybrid_event_strict"):
        # The selective formulation: the candlestick pattern must be a FRESH
        # EVENT (score crosses from <=0 to >0), not a lingering state, and the
        # indicator stack must already agree. State-vs-state pairing saturates
        # the holding window and destroys selectivity; event+state does not.
        from . import candlestick as cs_mod
        conf = 2.0 if name == "hybrid_event" else 4.0
        cs = cs_mod.signal(df).to_numpy().astype(float)
        ut = ut_score(df)
        cs1 = np.roll(cs, 1).astype(float); cs1[0] = 0.0
        cs2 = np.roll(cs, 2).astype(float); cs2[:2] = 0.0
        ut1 = np.roll(ut, 1).astype(float); ut1[0] = 0.0
        fresh_up = (cs1 > 0) & (cs2 <= 0)
        fresh_dn = (cs1 < 0) & (cs2 >= 0)
        longs = fresh_up & (ut1 >= conf)
        shorts = fresh_dn & (ut1 <= -conf)

    elif name == "candlestick":
        from . import candlestick as cs_mod
        sig = cs_mod.signal(df).to_numpy()
        s1 = np.roll(sig, 1).astype(float); s1[0] = 0
        longs = s1 > 0
        shorts = s1 < 0

    return longs, shorts


CATALOG = ["scalp_momentum", "ema_crossover", "donchian", "momentum", "supertrend", "rsi", "candlestick", "ema_pivot_break", "ut_score",
           "hybrid_loose", "hybrid", "hybrid_strict",
           "hybrid_contra", "hybrid_contra_strict",
           "hybrid_event", "hybrid_event_strict",
           # NSE session structure (defined in india_fno)
           "orb_break", "orb_fade", "gap_follow", "gap_fade",
           "first_hour_momo", "last_hour_momo", "vwap_revert",
           "expiry_day_momo"]


def build_signals(close: np.ndarray, p: BTParams) -> tuple[np.ndarray, np.ndarray]:
    """Return (long_signal, short_signal) booleans per bar.

    Every input is computed from data strictly BEFORE the bar it acts on: we
    compute momentum/persistence on the series and then shift by one bar.
    """
    n = len(close)
    fast_ret = np.full(n, np.nan)
    slow_ret = np.full(n, np.nan)
    persist_up = np.full(n, np.nan)

    rets = np.diff(close, prepend=close[0]) / np.where(close != 0, close, 1)

    for i in range(n):
        if i >= p.fast:
            fast_ret[i] = (close[i] - close[i - p.fast]) / close[i - p.fast]
        if i >= p.slow:
            slow_ret[i] = (close[i] - close[i - p.slow]) / close[i - p.slow]
        if i >= p.persist_lookback:
            window = rets[i - p.persist_lookback + 1: i + 1]
            persist_up[i] = float(np.mean(window > 0))

    # SHIFT by 1 — decisions on bar i may only use information up to i-1.
    f = np.roll(fast_ret, 1); f[0] = np.nan
    s = np.roll(slow_ret, 1); s[0] = np.nan
    pu = np.roll(persist_up, 1); pu[0] = np.nan

    strong = np.abs(f) >= p.trend_threshold
    long_dir = (f > 0) & (s >= 0)
    short_dir = (f < 0) & (s <= 0)
    long_sig = strong & long_dir & (pu >= p.min_persistence)
    short_sig = strong & short_dir & ((1.0 - pu) >= p.min_persistence)
    return np.nan_to_num(long_sig, nan=0).astype(bool), np.nan_to_num(short_sig, nan=0).astype(bool)


# ---------------------------------------------------------------------------
# simulation


def simulate(df, p: BTParams, initial: float = 500_000.0,
             strategy: str | None = None) -> BTResult:
    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float) if "High" in df else close
    low = df["Low"].to_numpy(dtype=float) if "Low" in df else close
    n = len(close)
    if strategy and strategy != 'live':
        long_sig, short_sig = catalog_signals(df, strategy)
    else:
        long_sig, short_sig = build_signals(close, p)

    equity = initial
    curve: list[float] = []
    trades: list[Trade] = []
    i = 0
    # Leverage of an ATM option on the underlying, in % terms.
    lev = (1.0 / p.premium_frac) * p.delta

    while i < n:
        curve.append(equity)
        if equity <= 0:
            break
        side = "call" if long_sig[i] else ("put" if short_sig[i] else None)
        if side is None:
            i += 1
            continue

        entry_spot = close[i]
        entry_prem = entry_spot * p.premium_frac
        # size so a full stop ≈ risk_pct of equity
        per_unit_risk = max(entry_prem * p.sl_pct, 1e-9)
        qty = max(0.0, (p.risk_pct * equity) / per_unit_risk)
        if qty <= 0:
            i += 1
            continue

        exit_i, exit_prem, reason = i, entry_prem, "TIME"
        for j in range(i + 1, min(i + 1 + p.max_hold_bars, n)):
            # underlying move in our favour (puts profit when spot falls)
            move_hi = (high[j] - entry_spot) / entry_spot
            move_lo = (low[j] - entry_spot) / entry_spot
            fav = move_hi if side == "call" else -move_lo
            adv = move_lo if side == "call" else -move_hi
            bars_held = j - i
            decay = p.theta_per_bar * bars_held

            prem_best = entry_prem * (1 + fav * lev - decay)
            prem_worst = entry_prem * (1 + adv * lev - decay)
            tp_px = entry_prem * (1 + p.tp_pct)
            sl_px = entry_prem * (1 - p.sl_pct)

            # Conservative ordering: if both could trigger in the bar, take the stop.
            if prem_worst <= sl_px:
                exit_i, exit_prem, reason = j, sl_px, "SL"
                break
            if prem_best >= tp_px:
                exit_i, exit_prem, reason = j, tp_px, "TP"
                break
            close_move = (close[j] - entry_spot) / entry_spot
            fav_close = close_move if side == "call" else -close_move
            exit_i = j
            exit_prem = max(0.0, entry_prem * (1 + fav_close * lev - decay))

        # Pre-tax per trade; tax is applied to the NET across all trades below
        # (F&O losses offset gains — per-trade tax wildly overstates the drag).
        pnl = charges.settle(entry_prem, exit_prem, qty, apply_tax=False)
        equity += pnl.net
        trades.append(Trade(
            i_entry=i, i_exit=exit_i, side=side,
            entry_spot=entry_spot, exit_spot=close[min(exit_i, n - 1)],
            entry_premium=entry_prem, exit_premium=exit_prem, qty=qty,
            gross=pnl.gross, charges_paid=pnl.charges, tax=pnl.tax,
            net=pnl.net, reason=reason,
        ))
        i = exit_i + 1

    # Portfolio-level tax: only on net profit across the whole run.
    total_pre_tax = float(sum(t.net for t in trades))
    port_tax = charges.tax_on_portfolio(total_pre_tax)
    equity -= port_tax
    return _metrics(df, trades, curve, equity, initial, p, port_tax)


def _metrics(df, trades: list[Trade], curve: list[float], equity: float,
             initial: float, p: BTParams, port_tax: float = 0.0) -> BTResult:
    nets = np.array([t.net for t in trades]) if trades else np.array([])
    wins = int((nets > 0).sum()) if nets.size else 0
    losses = int((nets < 0).sum()) if nets.size else 0
    gross_win = float(nets[nets > 0].sum()) if nets.size else 0.0
    gross_loss = float(-nets[nets < 0].sum()) if nets.size else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else (99.0 if gross_win > 0 else 0.0)

    eq = np.array(curve if curve else [initial], dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / np.where(peak > 0, peak, 1)
    max_dd = float(dd.min() * 100) if dd.size else 0.0

    # periods/year from the index spacing
    try:
        secs = (df.index[-1] - df.index[0]).total_seconds()
        years = max(secs / (365.25 * 86400), 1e-6)
    except Exception:
        years = max(len(df) / (252 * 7), 1e-6)
    ret_pct = (equity - initial) / initial * 100
    cagr = ((equity / initial) ** (1 / years) - 1) * 100 if equity > 0 and years > 0 else -100.0

    if nets.size > 1 and nets.std() > 1e-12:
        per_year = len(nets) / years
        sharpe = float(nets.mean() / nets.std() * math.sqrt(max(per_year, 1e-6)))
    else:
        sharpe = 0.0

    return BTResult(
        symbol="", bars=len(df), trades=len(trades), wins=wins, losses=losses,
        win_rate=(wins / (wins + losses) * 100) if (wins + losses) else 0.0,
        net_pnl=float(nets.sum()) if nets.size else 0.0,
        return_pct=ret_pct, cagr_pct=cagr, sharpe=sharpe, max_dd_pct=max_dd,
        profit_factor=pf, avg_net=float(nets.mean()) if nets.size else 0.0,
        charges_paid=float(sum(t.charges_paid for t in trades)),
        tax_paid=float(port_tax),
        gross_pnl=float(sum(t.gross for t in trades)),
        equity=[float(x) for x in eq], params=asdict(p),
    )


def simulate_futures(df, p: BTParams, initial: float = 500_000.0,
                     strategy: str | None = None,
                     tp_pct: float = 0.010, sl_pct: float = 0.006) -> BTResult:
    """Same signals, traded as FUTURES on the underlying instead of ATM options.

    This isolates signal quality from option structure. Options apply ~40x
    leverage to the underlying and bleed theta every bar, so a tight premium
    stop is hit by ordinary noise. Futures carry no decay and far cheaper
    charges — if a signal can't profit here, the option version has no hope.
    tp/sl are percentages of the UNDERLYING price.
    """
    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    n = len(close)
    if strategy and strategy != "live":
        long_sig, short_sig = catalog_signals(df, strategy)
    else:
        long_sig, short_sig = build_signals(close, p)

    equity = initial
    curve: list[float] = []
    trades: list[Trade] = []
    i = 0
    while i < n:
        curve.append(equity)
        if equity <= 0:
            break
        side = "call" if long_sig[i] else ("put" if short_sig[i] else None)
        if side is None:
            i += 1
            continue
        entry = close[i]
        qty = max(0.0, (p.risk_pct * equity) / max(entry * sl_pct, 1e-9))
        exit_i, exit_px, reason = i, entry, "TIME"
        for j in range(i + 1, min(i + 1 + p.max_hold_bars, n)):
            fav = (high[j] - entry) / entry if side == "call" else (entry - low[j]) / entry
            adv = (low[j] - entry) / entry if side == "call" else (entry - high[j]) / entry
            if adv <= -sl_pct:
                exit_i, exit_px, reason = j, entry * (1 - sl_pct) if side == "call" else entry * (1 + sl_pct), "SL"
                break
            if fav >= tp_pct:
                exit_i, exit_px, reason = j, entry * (1 + tp_pct) if side == "call" else entry * (1 - tp_pct), "TP"
                break
            exit_i, exit_px = j, close[j]
        # directional P&L on the underlying
        move = (exit_px - entry) if side == "call" else (entry - exit_px)
        eff_exit = entry + move  # express as a long-equivalent for the cost model
        pnl = charges.settle(entry, eff_exit, qty, segment="futures", apply_tax=False)
        equity += pnl.net
        trades.append(Trade(i_entry=i, i_exit=exit_i, side=side, entry_spot=entry,
                            exit_spot=exit_px, entry_premium=entry, exit_premium=eff_exit,
                            qty=qty, gross=pnl.gross, charges_paid=pnl.charges,
                            tax=0.0, net=pnl.net, reason=reason))
        i = exit_i + 1

    total_pre_tax = float(sum(t.net for t in trades))
    port_tax = charges.tax_on_portfolio(total_pre_tax)
    equity -= port_tax
    return _metrics(df, trades, curve, equity, initial, p, port_tax)


def simulate_chain(df, p: BTParams, initial: float = 500_000.0,
                   strategy: str | None = None, symbol: str = "NIFTY",
                   dte_days: float = 7.0, otm_steps: int = 0,
                   trend_filter: bool = False, interval: str = "1h",
                   short_premium: bool = False) -> BTResult:
    """Chain-aware option backtest.

    Unlike `simulate` (which uses a crude premium-fraction model), this prices
    a REAL strike with Black-Scholes at every bar: strikes snap to the actual
    NSE grid, lot sizes are the real contract sizes, IV is estimated from
    realized vol plus a variance-risk premium, and theta decay comes out of
    the model. Charges use the India options rate card on premium turnover.

    `trend_filter` applies the market-structure read: only buy calls when the
    structure is up, puts when it's down. That is the "know the trend" gate.
    """
    from . import market_structure as ms
    from . import option_pricing as op

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    n = len(close)
    if strategy and strategy != "live":
        long_sig, short_sig = catalog_signals(df, strategy)
    else:
        long_sig, short_sig = build_signals(close, p)

    # bars per day (for converting holding bars -> time decay)
    try:
        per_day = max(1.0, 86400.0 / max((df.index[1] - df.index[0]).total_seconds(), 1.0))
    except Exception:
        per_day = 1.0

    # rolling realized vol (annualized), shifted to avoid lookahead
    rets = pd.Series(close).pct_change()
    rv = (rets.rolling(20).std() * np.sqrt(252 * per_day)).shift(1).to_numpy()

    # trend direction per bar (computed on a rolling window, no lookahead)
    trend_dir = np.array([None] * n, dtype=object)
    if trend_filter:
        step = max(1, n // 200)  # recompute periodically; carry forward between
        last = "side"
        for i in range(0, n):
            if i >= 60 and i % step == 0:
                try:
                    last = ms.trend(df.iloc[max(0, i - 250):i]).direction
                except Exception:
                    pass
            trend_dir[i] = last

    lot = op.lot_size(symbol)
    equity = initial
    curve: list[float] = []
    trades: list[Trade] = []
    i = 0
    while i < n:
        curve.append(equity)
        if equity <= 0:
            break
        side = "call" if long_sig[i] else ("put" if short_sig[i] else None)
        if side is None:
            i += 1
            continue
        if trend_filter:
            d = trend_dir[i]
            if (side == "call" and d != "up") or (side == "put" and d != "down"):
                i += 1
                continue

        spot = close[i]
        sigma = float(rv[i]) if rv[i] == rv[i] and rv[i] > 0 else 0.20
        K = op.snap_strike(spot, symbol, otm_steps if side == "call" else -otm_steps)
        iv = op.iv_estimate(sigma, dte_days)
        T0 = dte_days / 365.0
        g0 = op.bs(spot, K, T0, iv, side)
        entry_prem = g0.price * (1 + SLIPPAGE)
        if entry_prem <= 0.05:
            i += 1
            continue

        qty = lot * max(1, int((p.risk_pct * equity) / max(entry_prem * p.sl_pct * lot, 1e-9)))
        if qty * entry_prem > 0.25 * equity:
            qty = lot * max(1, int((0.25 * equity) / (entry_prem * lot)))
        if qty * entry_prem > equity:
            i += 1
            continue

        if short_premium:
            # Selling: profit when the premium DECAYS. TP = buy back cheaper,
            # SL = buy back dearer (risk is the premium rising against us).
            tp_px = entry_prem * (1 - p.tp_pct)
            sl_px = entry_prem * (1 + p.sl_pct)
        else:
            tp_px = entry_prem * (1 + p.tp_pct)
            sl_px = entry_prem * (1 - p.sl_pct)
        exit_i, exit_prem, reason = i, entry_prem, "TIME"

        for j in range(i + 1, min(i + 1 + p.max_hold_bars, n)):
            T = max((dte_days - (j - i) / per_day) / 365.0, 1e-6)
            best_spot = high[j] if side == "call" else low[j]
            worst_spot = low[j] if side == "call" else high[j]
            p_best = op.bs(best_spot, K, T, iv, side).price
            p_worst = op.bs(worst_spot, K, T, iv, side).price
            if short_premium:
                # worst case for a seller = premium spikes (favourable spot move
                # for the option holder); best case = premium collapses.
                if p_best >= sl_px:
                    exit_i, exit_prem, reason = j, sl_px * (1 + SLIPPAGE), "SL"
                    break
                if p_worst <= tp_px:
                    exit_i, exit_prem, reason = j, tp_px * (1 + SLIPPAGE), "TP"
                    break
            else:
                if p_worst <= sl_px:
                    exit_i, exit_prem, reason = j, sl_px * (1 - SLIPPAGE), "SL"
                    break
                if p_best >= tp_px:
                    exit_i, exit_prem, reason = j, tp_px * (1 - SLIPPAGE), "TP"
                    break
            exit_i = j
            exit_prem = op.bs(close[j], K, T, iv, side).price
            if T * 365 <= 0.5:
                reason = "EXPIRY"
                break

        if short_premium:
            pnl = charges.settle(exit_prem, entry_prem, qty, apply_tax=False)
        else:
            pnl = charges.settle(entry_prem, exit_prem, qty, apply_tax=False)
        equity += pnl.net
        trades.append(Trade(i_entry=i, i_exit=exit_i, side=side, entry_spot=spot,
                            exit_spot=close[min(exit_i, n - 1)], entry_premium=entry_prem,
                            exit_premium=exit_prem, qty=qty, gross=pnl.gross,
                            charges_paid=pnl.charges, tax=0.0, net=pnl.net, reason=reason))
        i = exit_i + 1

    total_pre_tax = float(sum(t.net for t in trades))
    port_tax = charges.tax_on_portfolio(total_pre_tax)
    equity -= port_tax
    return _metrics(df, trades, curve, equity, initial, p, port_tax)


@dataclass
class SellParams:
    """Short-premium (straddle/strangle) parameters."""
    dte_days: float = 7.0
    otm_steps: int = 0          # 0 = straddle (ATM); >0 = strangle, N strikes out
    tp_frac: float = 0.50       # buy back once 50% of the credit has decayed
    sl_mult: float = 2.0        # stop if the position doubles against us
    max_hold_bars: int = 40
    risk_pct: float = 0.02      # of equity, against the stop distance
    require_range: bool = True  # only sell when structure is sideways
    max_adx: float = 22.0       # "sideways" definition
    min_iv_rank: float = 0.4    # only sell when vol is rich vs its own history
    exit_dte: float = 1.0       # never hold into the last day (gamma risk)
    model_vrp: bool = True      # mark at REALIZED vol, not the entry IV


def simulate_straddle(df, sp: SellParams, initial: float = 500_000.0,
                      symbol: str = "NIFTY", interval: str = "1h") -> BTResult:
    """Short ATM straddle (or OTM strangle) — the classic premium-selling
    structure, where the edge is the variance risk premium: implied vol is
    usually richer than what actually gets realized.

    The two filters are what keep sellers alive:
      * `require_range` — don't sell into a trend; that's how sellers blow up.
      * `min_iv_rank`  — only sell when vol is rich relative to its own history.

    Risk is real and asymmetric: max gain is the credit, loss is unbounded
    until the stop. The stop is enforced on the bar's WORST excursion, not the
    close, so a gap through it is not silently forgiven.
    """
    from . import market_structure as ms
    from . import option_pricing as op

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    n = len(close)
    try:
        per_day = max(1.0, 86400.0 / max((df.index[1] - df.index[0]).total_seconds(), 1.0))
    except Exception:
        per_day = 1.0

    rets = pd.Series(close).pct_change()
    rv_s = (rets.rolling(20).std() * np.sqrt(252 * per_day)).shift(1)
    rv = rv_s.to_numpy()
    iv_rank = rv_s.rolling(250, min_periods=40).rank(pct=True).to_numpy()

    # periodic structure read (carried forward; no lookahead)
    adx_arr = np.full(n, np.nan)
    step = max(1, n // 250)
    last_adx = 50.0
    for i in range(n):
        if i >= 60 and i % step == 0:
            try:
                last_adx = ms.adx(df.iloc[max(0, i - 250):i])[0]
            except Exception:
                pass
        adx_arr[i] = last_adx

    lot = op.lot_size(symbol)
    equity = initial
    curve: list[float] = []
    trades: list[Trade] = []
    i = 0
    while i < n:
        curve.append(equity)
        if equity <= 0:
            break
        sigma = float(rv[i]) if rv[i] == rv[i] and rv[i] > 0 else 0.0
        if sigma <= 0:
            i += 1
            continue
        if sp.require_range and adx_arr[i] > sp.max_adx:
            i += 1
            continue
        r_iv = iv_rank[i]
        if r_iv == r_iv and r_iv < sp.min_iv_rank:
            i += 1
            continue

        spot = close[i]
        iv = op.iv_estimate(sigma, sp.dte_days)
        Kc = op.snap_strike(spot, symbol, sp.otm_steps)
        Kp = op.snap_strike(spot, symbol, -sp.otm_steps)
        T0 = sp.dte_days / 365.0
        # Sell at the rich implied vol; mark the position at the vol that
        # actually realizes. That difference IS the variance risk premium —
        # the seller's entire edge. With model_vrp=False both use entry IV,
        # which captures theta only and understates (or flatters) nothing.
        mark_iv = sigma if sp.model_vrp else iv
        credit = (op.bs(spot, Kc, T0, iv, "call").price +
                  op.bs(spot, Kp, T0, iv, "put").price) * (1 - SLIPPAGE)
        if credit <= 1.0:
            i += 1
            continue

        stop_val = credit * sp.sl_mult
        tp_val = credit * (1 - sp.tp_frac)
        risk_per_unit = max(stop_val - credit, 1e-9)
        qty = lot * max(1, int((sp.risk_pct * equity) / max(risk_per_unit * lot, 1e-9)))
        # margin sanity: a short straddle needs real margin; cap exposure
        if qty * credit > 0.30 * equity:
            qty = lot * max(1, int((0.30 * equity) / (credit * lot)))

        exit_i, exit_val, reason = i, credit, "TIME"
        for j in range(i + 1, min(i + 1 + sp.max_hold_bars, n)):
            dte_left = sp.dte_days - (j - i) / per_day
            T = max(dte_left / 365.0, 1e-6)
            # worst case = the extreme furthest from the strikes
            v_hi = (op.bs(high[j], Kc, T, mark_iv, "call").price +
                    op.bs(high[j], Kp, T, mark_iv, "put").price)
            v_lo = (op.bs(low[j], Kc, T, mark_iv, "call").price +
                    op.bs(low[j], Kp, T, mark_iv, "put").price)
            worst = max(v_hi, v_lo)
            best = min(v_hi, v_lo)
            if worst >= stop_val:
                exit_i, exit_val, reason = j, stop_val * (1 + SLIPPAGE), "SL"
                break
            if best <= tp_val:
                exit_i, exit_val, reason = j, tp_val * (1 + SLIPPAGE), "TP"
                break
            v_close = (op.bs(close[j], Kc, T, mark_iv, "call").price +
                       op.bs(close[j], Kp, T, mark_iv, "put").price)
            exit_i, exit_val = j, v_close
            if dte_left <= sp.exit_dte:
                reason = "EXPIRY"
                break

        # seller: profit = credit received - cost to buy back
        pnl = charges.settle(exit_val, credit, qty, apply_tax=False)
        equity += pnl.net
        trades.append(Trade(i_entry=i, i_exit=exit_i, side="straddle",
                            entry_spot=spot, exit_spot=close[min(exit_i, n - 1)],
                            entry_premium=credit, exit_premium=exit_val, qty=qty,
                            gross=pnl.gross, charges_paid=pnl.charges, tax=0.0,
                            net=pnl.net, reason=reason))
        i = exit_i + 1

    total_pre_tax = float(sum(t.net for t in trades))
    port_tax = charges.tax_on_portfolio(total_pre_tax)
    equity -= port_tax
    p_dummy = BTParams(tp_pct=sp.tp_frac, sl_pct=sp.sl_mult, max_hold_bars=sp.max_hold_bars)
    return _metrics(df, trades, curve, equity, initial, p_dummy, port_tax)


def run_straddle(symbol: str = "NIFTY", period: str = "1y", interval: str = "1h",
                 initial: float = 500_000.0, sp: SellParams | None = None) -> BTResult | None:
    df = fetch_history(symbol, period, interval)
    if df is None:
        return None
    r = simulate_straddle(df, sp or SellParams(), initial, symbol, interval)
    r.symbol = symbol.upper()
    return r


def run_chain(symbol: str, strategy: str = "candlestick", period: str = "1y",
              interval: str = "1h", initial: float = 500_000.0,
              dte_days: float = 7.0, otm_steps: int = 0,
              trend_filter: bool = False, short_premium: bool = False,
              params: BTParams | None = None) -> BTResult | None:
    df = fetch_history(symbol, period, interval)
    if df is None:
        return None
    r = simulate_chain(df, params or BTParams(), initial, strategy, symbol,
                       dte_days, otm_steps, trend_filter, interval, short_premium)
    r.symbol = symbol.upper()
    return r


def run_futures(symbol: str, strategy: str, period: str = "1y", interval: str = "1h",
                initial: float = 500_000.0) -> BTResult | None:
    df = fetch_history(symbol, period, interval)
    if df is None:
        return None
    r = simulate_futures(df, BTParams(), initial, strategy)
    r.symbol = symbol.upper()
    return r


def run(symbol: str, p: BTParams | None = None, period: str = "2y",
        interval: str = "1h", initial: float = 500_000.0,
        strategy: str | None = None) -> BTResult | None:
    df = fetch_history(symbol, period, interval)
    if df is None:
        return None
    r = simulate(df, p or BTParams(), initial, strategy)
    r.symbol = symbol.upper()
    return r


# ---------------------------------------------------------------------------
# walk-forward — the only numbers that count as evidence


def walk_forward(symbol: str, grid: Iterable[dict], period: str = "2y",
                 interval: str = "1h", splits: int = 4,
                 initial: float = 500_000.0) -> dict | None:
    """Rolling walk-forward: for each split, optimise params on the in-sample
    window, then trade them on the next (unseen) out-of-sample window.

    Returns in-sample vs out-of-sample aggregates. A strategy is only credible
    if the OOS numbers hold up — IS numbers are curve-fit by construction.
    """
    df = fetch_history(symbol, period, interval)
    if df is None:
        return None
    grid = list(grid)
    n = len(df)
    fold = n // (splits + 1)
    if fold < 120:
        return None

    folds = []
    for k in range(splits):
        is_df = df.iloc[k * fold:(k + 1) * fold]
        oos_df = df.iloc[(k + 1) * fold:(k + 2) * fold]
        if len(oos_df) < 60:
            continue
        best, best_ret = None, -1e18
        for g in grid:
            p = BTParams(**g)
            r = simulate(is_df, p, initial)
            if r.trades >= 3 and r.return_pct > best_ret:
                best_ret, best = r.return_pct, g
        if best is None:
            continue
        oos = simulate(oos_df, BTParams(**best), initial)
        folds.append({
            "fold": k + 1,
            "params": best,
            "is_return_pct": round(best_ret, 2),
            "oos_return_pct": round(oos.return_pct, 2),
            "oos_trades": oos.trades,
            "oos_win_rate": round(oos.win_rate, 1),
            "oos_profit_factor": round(oos.profit_factor, 2),
            "oos_max_dd_pct": round(oos.max_dd_pct, 2),
        })

    if not folds:
        return None
    oos_rets = [f["oos_return_pct"] for f in folds]
    is_rets = [f["is_return_pct"] for f in folds]
    positive = sum(1 for r in oos_rets if r > 0)
    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "period": period,
        "folds": folds,
        "is_mean_return_pct": round(float(np.mean(is_rets)), 2),
        "oos_mean_return_pct": round(float(np.mean(oos_rets)), 2),
        "oos_positive_folds": positive,
        "oos_total_folds": len(folds),
        "edge_verdict": (
            "EDGE (OOS positive in majority of folds)"
            if positive > len(folds) / 2 and float(np.mean(oos_rets)) > 0
            else "NO EDGE (out-of-sample does not hold up)"
        ),
    }


def default_grid() -> list[dict]:
    """A deliberately small grid — the skills warn that sweeping hundreds of
    combos invites data-snooping. Few params, broad steps."""
    out = []
    for tt in (0.0003, 0.001, 0.002):
        for mp in (0.55, 0.62, 0.75):
            for tp, sl in ((0.06, 0.04), (0.10, 0.06), (0.15, 0.10)):
                out.append({"trend_threshold": tt, "min_persistence": mp,
                            "tp_pct": tp, "sl_pct": sl})
    return out
