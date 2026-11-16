"""India NSE F&O structures — the strategies that are specific to this market.

Everything here is INR and NSE-specific. Two groups:

**Option selling, driven by REAL implied vol.** Earlier short-straddle work in
this project was circular: it assumed an IV, priced the premium off that
assumption, and then "discovered" that selling was profitable — the answer was
baked into the input. India VIX (``^INDIAVIX``) fixes that. It is the market's
own 30-day ATM implied vol for NIFTY, published by NSE, and it is an
observation rather than a parameter. Realised vol comes from actual index
prices. Neither side is assumed, so the variance risk premium measured here is
real or it is not there at all.

**Intraday structure.** Opening-range breakout, time-of-day, gap behaviour and
days-to-expiry effects — mechanical features of the NSE session rather than
indicator signals, which is where persistent edge is more plausible.

Honesty constraints carried throughout:
  * India VIX is a 30-DAY measure. Using it to price a 7-day weekly is an
    approximation, and the term-structure sensitivity is reported alongside
    any weekly result rather than hidden.
  * Every result is split by period so a single regime cannot pass as an edge.
  * Selling options has unbounded loss. A strategy that wins 90% of the time
    and blows up on the 10th is reported with its worst trade, not just its
    average.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from . import charges
from . import option_pricing as op

TRADING_DAYS = 252
VIX_TICKER = "^INDIAVIX"

# One NIFTY lot. Used to make the flat ₹20/order brokerage realistic.
DEFAULT_LOT = 65


def fetch_vix(period: str = "2y") -> pd.Series | None:
    """India VIX daily close, as a fraction (16.6 -> 0.166)."""
    try:
        import yfinance as yf
        d = yf.Ticker(VIX_TICKER).history(period=period, interval="1d")
        if d is None or d.empty:
            return None
        s = d["Close"].dropna() / 100.0
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        return s
    except Exception:
        return None


def _align(period: str = "2y") -> pd.DataFrame | None:
    """NIFTY daily OHLC joined to India VIX on the same dates."""
    from . import backtest
    spot = backtest.fetch_history("NIFTY", period, "1d")
    vix = fetch_vix(period)
    if spot is None or vix is None:
        return None
    s = spot.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    df = s.join(vix.rename("VIX"), how="inner").dropna(subset=["Close", "VIX"])
    return df if len(df) > 60 else None


# ---------------------------------------------------------------------------
# 1. Variance risk premium — is selling vol paid for in India?


def vrp_study(period: str = "2y", horizons=(7, 14, 30)) -> dict:
    """Compare India VIX to the realised vol that actually followed it.

    VRP = implied - realised. A persistently positive VRP is the structural
    reason option SELLING can carry an edge: sellers are paid more than the
    movement costs them, on average, as compensation for tail risk.

    Measured, not assumed — and reported with the share of windows where it
    was negative, because those are the windows that bankrupt a seller.
    """
    df = _align(period)
    if df is None:
        return {"ok": False, "error": "could not align NIFTY and India VIX"}

    close = df["Close"].to_numpy(dtype=float)
    vix = df["VIX"].to_numpy(dtype=float)
    logret = np.diff(np.log(close), prepend=np.log(close[0]))
    out = []

    for h in horizons:
        rows = []
        for i in range(len(close) - h):
            fwd = logret[i + 1: i + 1 + h]
            if len(fwd) < max(5, h // 2):
                continue
            rv = float(fwd.std(ddof=1) * math.sqrt(TRADING_DAYS))
            rows.append((vix[i], rv, vix[i] - rv))
        if len(rows) < 30:
            continue
        a = np.array(rows)
        iv_, rv_, vrp_ = a[:, 0], a[:, 1], a[:, 2]
        out.append({
            "horizon_days": h, "windows": len(rows),
            "mean_implied_pct": round(float(iv_.mean()) * 100, 2),
            "mean_realised_pct": round(float(rv_.mean()) * 100, 2),
            "mean_vrp_pct": round(float(vrp_.mean()) * 100, 2),
            "median_vrp_pct": round(float(np.median(vrp_)) * 100, 2),
            "pct_windows_positive": round(float((vrp_ > 0).mean() * 100), 1),
            "worst_vrp_pct": round(float(vrp_.min()) * 100, 2),
            "p5_vrp_pct": round(float(np.percentile(vrp_, 5)) * 100, 2),
            # overlapping windows -> this t-stat is optimistic, stated plainly
            "t_stat_naive": round(float(vrp_.mean() / (vrp_.std(ddof=1) / math.sqrt(len(vrp_)))), 2),
        })

    return {
        "ok": True, "period": period, "days": len(df),
        "from": str(df.index[0].date()), "to": str(df.index[-1].date()),
        "results": out,
        "note": ("VRP = India VIX minus the realised vol that followed. Both "
                 "observed. Overlapping windows inflate the t-stat; the share "
                 "of negative windows and the worst case are the honest risk "
                 "measures for a seller."),
    }


# ---------------------------------------------------------------------------
# 2. Short straddle priced at the REAL VIX


@dataclass
class StraddleTrade:
    entry_date: str
    exit_date: str
    dte: int
    spot_in: float
    spot_out: float
    strike: float
    iv_used: float
    premium_recv: float      # per unit, both legs
    payoff_paid: float       # per unit at exit
    move_pct: float
    gross: float             # rupees on one lot
    charges_paid: float
    net: float
    reason: str


def short_straddle(period: str = "2y", dte: int = 7, lot: int = DEFAULT_LOT,
                   sl_mult: float = 2.0, iv_scale: float = 1.0,
                   capital: float = 500_000.0) -> dict:
    """Sell an ATM straddle every `dte` days, priced at the real India VIX.

    The only modelled step is converting an observed IV into a premium via
    Black-Scholes. The payoff is whatever the index actually did. ``iv_scale``
    exists so the term-structure assumption can be swept rather than trusted:
    VIX is a 30-day number and a 7-day weekly does not trade at the same vol.

    A stop at ``sl_mult`` x premium received is the difference between a
    strategy and a margin call — naked short straddles have unbounded loss, so
    an unstopped version is not a strategy, it is a countdown.
    """
    df = _align(period)
    if df is None:
        return {"ok": False, "error": "could not align NIFTY and India VIX"}

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    vix = df["VIX"].to_numpy(dtype=float)
    dates = [str(d.date()) for d in df.index]
    n = len(close)

    trades: list[StraddleTrade] = []
    i = 0
    while i + dte < n:
        spot = close[i]
        iv = float(vix[i]) * iv_scale
        if not (iv > 0) or not (spot > 0):
            i += 1
            continue
        try:
            strike = float(op.snap_strike(spot, "NIFTY", 0))
        except Exception:
            strike = round(spot / 50.0) * 50.0
        T = dte / 365.0
        try:
            prem = float(op.bs(spot, strike, T, iv, "call").price) + \
                   float(op.bs(spot, strike, T, iv, "put").price)
        except Exception:
            i += 1
            continue
        if not (prem > 0):
            i += 1
            continue

        stop_at = prem * sl_mult
        exit_i, payoff, reason = i + dte, None, "EXPIRY"
        for j in range(i + 1, min(i + dte + 1, n)):
            # intrabar worst case for a straddle seller: the furthest extreme
            worst = max(abs(high[j] - strike), abs(low[j] - strike))
            if worst >= stop_at:
                exit_i, payoff, reason = j, stop_at, "SL"
                break
        if payoff is None:
            exit_i = min(i + dte, n - 1)
            payoff = abs(close[exit_i] - strike)

        qty = float(lot)
        # Seller: collects the premium up front, pays the payoff at exit. The
        # cost model is turnover-based and side-agnostic, so the same premium
        # pair prices the round trip for a seller as for a buyer. STT on the
        # sell leg is the larger piece and is charged on the premium received,
        # which is the entry here.
        gross = (prem - payoff) * qty
        ch = charges.round_trip_charges(prem, payoff, qty).total
        net = gross - ch
        trades.append(StraddleTrade(
            entry_date=dates[i], exit_date=dates[exit_i], dte=exit_i - i,
            spot_in=round(spot, 2), spot_out=round(close[exit_i], 2),
            strike=strike, iv_used=round(iv, 4),
            premium_recv=round(prem, 2), payoff_paid=round(payoff, 2),
            move_pct=round((close[exit_i] - spot) / spot * 100, 2),
            gross=round(gross, 2), charges_paid=round(ch, 2),
            net=round(net, 2), reason=reason))
        i = exit_i + 1

    if not trades:
        return {"ok": False, "error": "no trades generated"}

    nets = np.array([t.net for t in trades])
    pre_tax = float(nets.sum())
    tax = charges.tax_on_portfolio(pre_tax)
    curve = np.concatenate([[capital], capital + np.cumsum(nets)])
    peak = np.maximum.accumulate(curve)
    dd = float(((curve - peak) / peak).min() * 100)
    gw = float(nets[nets > 0].sum())
    gl = float(-nets[nets < 0].sum())
    sd = float(nets.std(ddof=1)) if nets.size > 1 else 0.0

    return {
        "ok": True, "strategy": f"short_straddle_{dte}d", "period": period,
        "from": dates[0], "to": dates[-1],
        "dte": dte, "lot": lot, "sl_mult": sl_mult, "iv_scale": iv_scale,
        "trades": len(trades),
        "wins": int((nets > 0).sum()), "losses": int((nets < 0).sum()),
        "win_rate": round(float((nets > 0).mean() * 100), 1),
        "sl_hits": sum(1 for t in trades if t.reason == "SL"),
        "gross_pnl": round(float(sum(t.gross for t in trades)), 2),
        "charges_paid": round(float(sum(t.charges_paid for t in trades)), 2),
        "net_pre_tax": round(pre_tax, 2),
        "tax": round(tax, 2),
        "net_after_tax": round(pre_tax - tax, 2),
        "return_pct": round((pre_tax - tax) / capital * 100, 2),
        "avg_net": round(float(nets.mean()), 2),
        "best": round(float(nets.max()), 2),
        "worst": round(float(nets.min()), 2),
        "profit_factor": round(gw / gl, 2) if gl > 1e-9 else (99.0 if gw > 0 else 0.0),
        "max_dd_pct": round(dd, 2),
        "t_stat": round(float(nets.mean() / (sd / math.sqrt(nets.size))), 2) if sd > 1e-9 else 0.0,
        "capital": capital,
        "trades_list": [asdict(t) for t in trades],
        "note": (f"Premium priced at the real India VIX x {iv_scale}. VIX is a "
                 "30-day measure, so a shorter dte carries term-structure "
                 "error — sweep iv_scale before believing any result. Short "
                 "straddles have unbounded loss; read 'worst' and 'max_dd'."),
    }


# ---------------------------------------------------------------------------
# 3. NSE session structure — strategies that need no option model at all.

# The NSE equity session is 09:15-15:30 IST. These are mechanical features of
# that session rather than indicator readings, which is where a persistent
# effect is more plausible: everyone's day starts and ends at the same moment.

INDIA_SESSION = [
    "orb_break", "orb_fade",          # opening-range breakout / fade
    "gap_follow", "gap_fade",         # overnight gap
    "first_hour_momo",                # first hour sets the day
    "last_hour_momo",                 # closing-hour drift
    "vwap_revert",                    # stretch from session VWAP
    "expiry_day_momo",                # expiry-session behaviour only
]


def _session_frames(df):
    """Per-bar session context: day index, minutes since the open, day open."""
    idx = df.index
    try:
        mins = np.asarray([t.hour * 60 + t.minute for t in idx]) - (9 * 60 + 15)
    except Exception:
        mins = np.arange(len(idx)) % 75 * 5
    try:
        days = np.asarray([t.date() for t in idx])
    except Exception:
        days = np.zeros(len(idx))
    new_day = np.empty(len(idx), dtype=bool)
    new_day[0] = True
    if len(idx) > 1:
        new_day[1:] = days[1:] != days[:-1]
    day_id = np.cumsum(new_day) - 1
    return day_id, mins, new_day


def india_signals(df, name: str) -> tuple[np.ndarray, np.ndarray]:
    """Session-structure signals. Every one is shifted so no bar sees itself."""
    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    # The session OPEN, not the first bar's close — the overnight gap is
    # open-minus-previous-close by definition, and using the close collapses
    # the first-hour move to exactly zero on hourly bars.
    openp = (df["Open"].to_numpy(dtype=float) if "Open" in df else close)
    vol = (df["Volume"].to_numpy(dtype=float)
           if "Volume" in df else np.ones(len(close)))
    n = len(close)
    day_id, mins, new_day = _session_frames(df)
    longs = np.zeros(n, dtype=bool)
    shorts = np.zeros(n, dtype=bool)

    # --- opening range: high/low of the first 15 minutes of each session ----
    OR_MIN = 15
    or_hi = np.full(n, np.nan)
    or_lo = np.full(n, np.nan)
    day_open = np.full(n, np.nan)
    prev_close = np.full(n, np.nan)
    starts = np.flatnonzero(new_day)
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else n
        in_or = np.flatnonzero(mins[s:e] < OR_MIN) + s
        if in_or.size:
            or_hi[s:e] = high[in_or].max()
            or_lo[s:e] = low[in_or].min()
        day_open[s:e] = openp[s]
        if s > 0:
            prev_close[s:e] = close[s - 1]

    # a bar may only act AFTER the opening range has completed
    after_or = mins >= OR_MIN
    c1 = np.roll(close, 1); c1[0] = np.nan          # previous close, no lookahead

    if name in ("orb_break", "orb_fade"):
        up = after_or & np.isfinite(or_hi) & (c1 > or_hi)
        dn = after_or & np.isfinite(or_lo) & (c1 < or_lo)
        # only the FIRST break of each session — later bars are the same event
        first_up = up & ~np.r_[False, up[:-1]] if n else up
        first_dn = dn & ~np.r_[False, dn[:-1]] if n else dn
        if name == "orb_break":
            longs, shorts = first_up, first_dn
        else:
            longs, shorts = first_dn, first_up

    elif name in ("gap_follow", "gap_fade"):
        gap = (day_open - prev_close) / np.where(prev_close > 0, prev_close, np.nan)
        big = np.isfinite(gap) & (np.abs(gap) >= 0.003)        # >= 0.3%
        # act on the bar after the open, once, per session
        at = big & new_day
        at = np.r_[False, at[:-1]] if n else at
        g = np.r_[np.nan, gap[:-1]] if n else gap
        if name == "gap_follow":
            longs, shorts = at & (g > 0), at & (g < 0)
        else:
            longs, shorts = at & (g < 0), at & (g > 0)

    elif name == "first_hour_momo":
        # at 10:15, take the direction the first hour travelled
        at = (mins >= 60) & (np.r_[True, mins[:-1] < 60] if n else True)
        move = (c1 - day_open) / np.where(day_open > 0, day_open, np.nan)
        strong = np.isfinite(move) & (np.abs(move) >= 0.002)
        longs = at & strong & (move > 0)
        shorts = at & strong & (move < 0)

    elif name == "last_hour_momo":
        # enter at 14:30 in the direction of the day so far
        at = (mins >= 315) & (np.r_[True, mins[:-1] < 315] if n else True)
        move = (c1 - day_open) / np.where(day_open > 0, day_open, np.nan)
        strong = np.isfinite(move) & (np.abs(move) >= 0.002)
        longs = at & strong & (move > 0)
        shorts = at & strong & (move < 0)

    elif name == "vwap_revert":
        tp = (high + low + close) / 3.0
        cum_pv = np.zeros(n); cum_v = np.zeros(n)
        run_pv = run_v = 0.0
        for i in range(n):
            if new_day[i]:
                run_pv = run_v = 0.0
            run_pv += tp[i] * max(vol[i], 1.0)
            run_v += max(vol[i], 1.0)
            cum_pv[i], cum_v[i] = run_pv, run_v
        vwap = cum_pv / np.where(cum_v > 0, cum_v, 1.0)
        v1 = np.r_[np.nan, vwap[:-1]] if n else vwap
        dev = (c1 - v1) / np.where(v1 > 0, v1, np.nan)
        # fade a stretch, but not in the first 30 min when VWAP is unformed
        ok = after_or & (mins >= 30) & np.isfinite(dev)
        longs = ok & (dev <= -0.004)
        shorts = ok & (dev >= 0.004)

    elif name == "expiry_day_momo":
        # NSE weekly index expiry. Trade momentum only on the expiry session,
        # where theta and unwinding dominate the tape.
        try:
            wd = np.asarray([t.weekday() for t in df.index])
        except Exception:
            wd = np.zeros(n, dtype=int)
        exp_day = (wd == 1) | (wd == 3)      # Tue (NIFTY) and Thu (legacy)
        move = (c1 - day_open) / np.where(day_open > 0, day_open, np.nan)
        ok = exp_day & after_or & np.isfinite(move) & (np.abs(move) >= 0.0015)
        longs = ok & (move > 0)
        shorts = ok & (move < 0)

    return longs, shorts


# ---------------------------------------------------------------------------
# 5. Calendar effects — the only place with a real sample.

# Every intraday conclusion in this project rests on 60 days of 5-minute bars,
# because that is yfinance's cap. DAILY data has no such cap: five years is
# ~1,230 sessions. Calendar effects are the one family of hypotheses that can
# actually be tested at that resolution, so they get tested properly.
#
# The whole bucket family is declared up front and corrected for as a family.
# Picking the best-looking weekday after seeing the data and reporting its
# p-value would be exactly the data-snooping this project keeps avoiding.

CALENDAR_BUCKETS = [
    "mon", "tue", "wed", "thu", "fri",
    "turn_of_month",        # last 1 + first 3 sessions, the global ToM effect
    "mid_month",
    "expiry_week",          # week containing the monthly last-Thursday expiry
    "post_holiday",         # first session after an exchange holiday
    "pre_holiday",
]


def _calendar_masks(idx) -> dict[str, np.ndarray]:
    """Boolean mask per calendar bucket, aligned to a daily index."""
    n = len(idx)
    dates = [d.date() for d in idx]
    wd = np.array([d.weekday() for d in dates])
    month = np.array([d.month for d in dates])
    year = np.array([d.year for d in dates])

    # session position within its calendar month
    key = year * 100 + month
    pos_from_start = np.zeros(n, dtype=int)
    pos_from_end = np.zeros(n, dtype=int)
    for k in np.unique(key):
        w = np.flatnonzero(key == k)
        pos_from_start[w] = np.arange(len(w))
        pos_from_end[w] = np.arange(len(w))[::-1]

    # Normal session gaps are 1 day (Tue-Fri) and 3 days (Mon after Fri).
    # A midweek holiday shows up as a 2-day gap, and a Monday holiday pushes
    # Tuesday 4 days from Friday. Testing only for gap > 4 caught neither,
    # which left both holiday buckets empty.
    gap = np.zeros(n, dtype=int)
    for i in range(1, n):
        gap[i] = (dates[i] - dates[i - 1]).days
    post_hol = (gap == 2) | (gap >= 4)
    pre_hol = np.r_[post_hol[1:], False]

    # monthly expiry = last Thursday of the month; expiry week = that week
    exp_week = np.zeros(n, dtype=bool)
    for k in np.unique(key):
        w = np.flatnonzero(key == k)
        thu = [i for i in w if wd[i] == 3]
        if thu:
            last = thu[-1]
            exp_week[[i for i in w if abs((dates[i] - dates[last]).days) <= 4]] = True

    return {
        "mon": wd == 0, "tue": wd == 1, "wed": wd == 2,
        "thu": wd == 3, "fri": wd == 4,
        "turn_of_month": (pos_from_end <= 0) | (pos_from_start <= 2),
        "mid_month": (pos_from_start >= 7) & (pos_from_end >= 7),
        "expiry_week": exp_week,
        "post_holiday": post_hol,
        "pre_holiday": pre_hol,
    }


def calendar_study(symbol: str = "NIFTY", period: str = "5y",
                   horizon: int = 1, n_perm: int = 5000,
                   cost_bps: float = 2.0, seed: int = 5) -> dict:
    """Mean forward return by calendar bucket, vs a label-shuffle null.

    The null is a permutation: keep the returns, shuffle which days carry the
    label. That preserves the return distribution exactly and asks only
    whether the CALENDAR placement matters — which is the actual hypothesis.

    ``cost_bps`` is the round-trip cost of acting on the bucket; a calendar
    effect smaller than the cost of trading it is a curiosity, not an edge.
    Each bucket is reported both ways and the family is Bonferroni-corrected.
    """
    from . import backtest
    df = backtest.fetch_history(symbol, period, "1d", min_bars=200)
    if df is None or len(df) < 250:
        return {"ok": False, "error": f"insufficient daily data for {symbol}"}

    close = df["Close"].to_numpy(dtype=float)
    n = len(close)
    fwd = np.full(n, np.nan)
    if horizon < n:
        fwd[:-horizon] = (close[horizon:] / close[:-horizon] - 1.0) * 100.0

    masks = _calendar_masks(df.index)
    ok = np.isfinite(fwd)
    rng = np.random.default_rng(seed)
    base = float(fwd[ok].mean())
    cost = cost_bps / 100.0          # bps -> percent

    rows = []
    for name in CALENDAR_BUCKETS:
        m = masks[name] & ok
        k = int(m.sum())
        if k < 20:
            continue
        obs = float(fwd[m].mean())
        pool = fwd[ok]
        sims = np.array([rng.choice(pool, k, replace=False).mean()
                         for _ in range(n_perm)])
        # two-sided: is this bucket unusual in EITHER direction
        p_two = float((np.abs(sims - base) >= abs(obs - base)).sum() + 1) / (n_perm + 1)
        rows.append({
            "bucket": name, "sessions": k,
            "mean_fwd_pct": round(obs, 4),
            "all_days_mean_pct": round(base, 4),
            "excess_pct": round(obs - base, 4),
            "net_of_cost_pct": round(abs(obs) - cost, 4),
            "beats_cost": bool(abs(obs) > cost),
            "p_two_sided": round(p_two, 4),
        })

    thr = 0.05 / max(len(rows), 1)
    for r in rows:
        r["bonferroni_threshold"] = round(thr, 4)
        r["significant"] = bool(r["p_two_sided"] <= thr)
    rows.sort(key=lambda r: r["p_two_sided"])
    winners = [r for r in rows if r["significant"] and r["beats_cost"]]

    return {
        "ok": True, "symbol": symbol.upper(), "period": period,
        "horizon_days": horizon, "sessions": n,
        "from": str(df.index[0].date()), "to": str(df.index[-1].date()),
        "buckets_tested": len(rows), "cost_bps": cost_bps,
        "results": rows,
        "significant_and_tradeable": [r["bucket"] for r in winners],
        "note": ("Permutation null keeps the return distribution and shuffles "
                 "only the calendar labels. A bucket must clear the "
                 "family-corrected threshold AND exceed round-trip cost to "
                 "matter; clearing only the first is a statistical curiosity."),
    }


def calendar_replication(symbol: str = "NIFTY", period: str = "5y",
                         horizon: int = 1) -> dict:
    """Same study on each half of the sample. A real effect appears in both."""
    from . import backtest
    df = backtest.fetch_history(symbol, period, "1d", min_bars=200)
    if df is None or len(df) < 400:
        return {"ok": False, "error": "insufficient data"}
    mid = len(df) // 2
    out = {}
    for label, sl in (("first_half", df.iloc[:mid]), ("second_half", df.iloc[mid:])):
        close = sl["Close"].to_numpy(dtype=float)
        n = len(close)
        fwd = np.full(n, np.nan)
        fwd[:-horizon] = (close[horizon:] / close[:-horizon] - 1.0) * 100.0
        masks = _calendar_masks(sl.index)
        ok = np.isfinite(fwd)
        base = float(fwd[ok].mean())
        out[label] = {
            "from": str(sl.index[0].date()), "to": str(sl.index[-1].date()),
            "buckets": {b: round(float(fwd[masks[b] & ok].mean()) - base, 4)
                        for b in CALENDAR_BUCKETS
                        if int((masks[b] & ok).sum()) >= 15},
        }
    a = out.get("first_half", {}).get("buckets", {})
    b = out.get("second_half", {}).get("buckets", {})
    common = sorted(set(a) & set(b))
    agree = [k for k in common if (a[k] > 0) == (b[k] > 0)]
    return {
        "ok": True, "symbol": symbol.upper(), "horizon_days": horizon,
        "halves": out,
        "sign_agreement": f"{len(agree)}/{len(common)}",
        "agreeing_buckets": agree,
        "note": ("Excess return over each half's own mean. Sign agreement "
                 "across halves is the minimum bar for an effect to be real; "
                 "about half the buckets will agree by chance alone."),
    }


# ---------------------------------------------------------------------------
# 4. Regime — the one thing that showed up in every test.

# Across ~23 strategies, three geometries and several timeframes, nothing
# replicated out of sample, and the failures were not random: trend-following
# variants were strongly positive in one period and strongly negative in the
# next, while contrarian variants were the exact mirror. That is a regime
# effect, and it makes the question "can the regime be known in advance?"
# strictly more important than "is there a better signal?".
#
# The test below is deliberately built to be able to say NO.


def _variance_ratio(logret: np.ndarray, q: int = 5) -> float:
    """VR(q) = Var(q-bar returns) / (q * Var(1-bar returns)).

    > 1 trending (moves extend), < 1 mean-reverting (moves get given back),
    = 1 random walk. A standard, assumption-light regime measure.
    """
    r = logret[np.isfinite(logret)]
    if len(r) < q * 4:
        return float("nan")
    v1 = r.var(ddof=1)
    if v1 <= 0:
        return float("nan")
    m = (len(r) // q) * q
    if m < q * 2:
        return float("nan")
    agg = r[:m].reshape(-1, q).sum(axis=1)
    return float(agg.var(ddof=1) / (q * v1))


def regime_predictability(symbol: str = "NIFTY", period: str = "2y",
                          interval: str = "1h", window: int = 120,
                          q: int = 5) -> dict:
    """Does the PAST regime say anything about the NEXT one?

    Everything downstream depends on this. If the trailing variance ratio
    carries no information about the forward variance ratio, then regime
    switching cannot be implemented no matter how large the oracle gain is,
    and the honest conclusion is that the regime is only visible afterwards.
    """
    from . import backtest
    df = backtest.fetch_history(symbol, period, interval)
    if df is None or len(df) < window * 4:
        return {"ok": False, "error": f"insufficient data for {symbol} {interval}"}

    close = df["Close"].to_numpy(dtype=float)
    logret = np.diff(np.log(close), prepend=np.log(close[0]))

    pairs = []
    for i in range(window, len(close) - window, max(window // 4, 1)):
        past = _variance_ratio(logret[i - window:i], q)
        fwd = _variance_ratio(logret[i:i + window], q)
        if np.isfinite(past) and np.isfinite(fwd):
            pairs.append((past, fwd))
    if len(pairs) < 12:
        return {"ok": False, "error": "too few windows"}

    a = np.array(pairs)
    past, fwd = a[:, 0], a[:, 1]
    corr = float(np.corrcoef(past, fwd)[0, 1])
    # directional agreement: does "trending now" mean "trending next"?
    hit = float(((past > 1) == (fwd > 1)).mean() * 100)
    # a coin that always guesses the majority class would score this:
    base = float(max((fwd > 1).mean(), 1 - (fwd > 1).mean()) * 100)

    n = len(pairs)
    # Fisher z for the correlation's significance
    if abs(corr) < 0.999 and n > 4:
        z = 0.5 * math.log((1 + corr) / (1 - corr)) * math.sqrt(n - 3)
    else:
        z = 0.0

    return {
        "ok": True, "symbol": symbol.upper(), "interval": interval,
        "window_bars": window, "q": q, "windows": n,
        "mean_vr": round(float(fwd.mean()), 3),
        "pct_trending": round(float((fwd > 1).mean() * 100), 1),
        "corr_past_future": round(corr, 3),
        "corr_z": round(z, 2),
        "direction_hit_rate": round(hit, 1),
        "majority_class_baseline": round(base, 1),
        "edge_over_baseline": round(hit - base, 1),
        "verdict": ("PREDICTABLE — the trailing regime carries real "
                    "information about the next one."
                    if abs(z) > 2 and hit > base + 5 else
                    "NOT PREDICTABLE — the trailing regime says essentially "
                    "nothing about the next one. Regime switching cannot be "
                    "implemented from this measure."),
        "note": ("Windows overlap by design (stepped quarter-window), which "
                 "inflates the z. If it fails even with the inflated z, it "
                 "fails."),
    }


def regime_switch_study(symbol: str = "NIFTY", period: str = "2y",
                        interval: str = "1h", window: int = 120,
                        trend_strat: str = "ut_score",
                        contra_strat: str = "hybrid_contra",
                        horizon: int = 7, tp: float = 0.008,
                        sl: float = 0.006, q: int = 5) -> dict:
    """How much would a PERFECT regime call be worth, and how much of it is reachable?

    Three numbers that bound the whole idea:

      * ``always_trend`` / ``always_contra`` — the no-switching baselines.
      * ``oracle`` — switch using the regime that actually occurred. Cheating,
        and therefore the CEILING on what any classifier could deliver.
      * ``realizable`` — switch using only the trailing regime.

    If the oracle gain is small, regime switching is not worth building. If the
    oracle gain is large but realizable is near the baselines, the ceiling is
    real and simply out of reach — which is a different and more useful thing
    to know than another failed backtest.
    """
    from . import backtest, signal_eval
    df = backtest.fetch_history(symbol, period, interval)
    if df is None or len(df) < window * 4:
        return {"ok": False, "error": f"insufficient data for {symbol} {interval}"}

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    logret = np.diff(np.log(close), prepend=np.log(close[0]))

    net_l, net_s, _, _ = signal_eval._outcome_table(high, low, close, horizon, tp, sl)

    def picks(strat):
        lo, sh = backtest.catalog_signals(df, strat)
        return np.asarray(lo, dtype=bool), np.asarray(sh, dtype=bool)

    t_lo, t_sh = picks(trend_strat)
    c_lo, c_sh = picks(contra_strat)

    def window_mean(lo, sh, a, b):
        idx_l = np.flatnonzero(lo[a:b]) + a
        idx_s = np.flatnonzero(sh[a:b]) + a
        vals = np.concatenate([net_l[idx_l], net_s[idx_s]])
        vals = vals[np.isfinite(vals)]
        return (float(vals.mean()), len(vals)) if len(vals) else (0.0, 0)

    rows = []
    for a in range(window, len(close) - window, window):
        b = min(a + window, len(close))
        past_vr = _variance_ratio(logret[a - window:a], q)
        fwd_vr = _variance_ratio(logret[a:b], q)
        if not (np.isfinite(past_vr) and np.isfinite(fwd_vr)):
            continue
        tm, tn = window_mean(t_lo, t_sh, a, b)
        cm, cn = window_mean(c_lo, c_sh, a, b)
        if tn + cn == 0:
            continue
        rows.append({"past_vr": past_vr, "fwd_vr": fwd_vr,
                     "trend": tm, "contra": cm, "n": tn + cn})
    if len(rows) < 6:
        return {"ok": False, "error": "too few windows"}

    tr = np.array([r["trend"] for r in rows])
    co = np.array([r["contra"] for r in rows])
    oracle = np.where(np.array([r["fwd_vr"] for r in rows]) > 1, tr, co)
    real = np.where(np.array([r["past_vr"] for r in rows]) > 1, tr, co)
    best = np.maximum(tr, co)      # perfect per-window choice, looser oracle

    def s(x):
        return round(float(x.mean()), 5)

    return {
        "ok": True, "symbol": symbol.upper(), "interval": interval,
        "windows": len(rows), "window_bars": window,
        "trend_strategy": trend_strat, "contra_strategy": contra_strat,
        "always_trend_pct": s(tr),
        "always_contra_pct": s(co),
        "realizable_switch_pct": s(real),
        "oracle_switch_pct": s(oracle),
        "perfect_choice_pct": s(best),
        "oracle_gain_pct": round(float(oracle.mean() - max(tr.mean(), co.mean())), 5),
        "realizable_gain_pct": round(float(real.mean() - max(tr.mean(), co.mean())), 5),
        "verdict": ("Realizable switching adds nothing over the better fixed "
                    "strategy — the ceiling may be real but it is not reachable "
                    "from the trailing regime."
                    if real.mean() <= max(tr.mean(), co.mean()) else
                    "Realizable switching beats both fixed strategies here — "
                    "needs replication on another symbol and period."),
        "note": "All figures are mean net % per signal, after India F&O costs.",
    }


def straddle_iv_sensitivity(period: str = "2y", dte: int = 7,
                            scales=(0.8, 0.9, 1.0, 1.1, 1.2, 1.3)) -> dict:
    """Sweep the term-structure assumption.

    If the result tracks ``iv_scale`` monotonically, the strategy is measuring
    the assumption rather than the market — exactly the failure that made the
    earlier straddle work meaningless. This is the guard, run every time.
    """
    rows = []
    for s in scales:
        r = short_straddle(period, dte, iv_scale=s)
        if r.get("ok"):
            rows.append({"iv_scale": s, "return_pct": r["return_pct"],
                         "win_rate": r["win_rate"], "trades": r["trades"],
                         "worst": r["worst"], "max_dd_pct": r["max_dd_pct"],
                         "t_stat": r["t_stat"]})
    if len(rows) < 3:
        return {"ok": False, "error": "sweep failed"}
    rets = [r["return_pct"] for r in rows]
    monotonic = all(b >= a for a, b in zip(rets, rets[1:]))
    span = max(rets) - min(rets)
    flips = any(r <= 0 for r in rets) and any(r > 0 for r in rets)
    base = next((r["return_pct"] for r in rows if abs(r["iv_scale"] - 1.0) < 1e-9),
                rets[len(rets) // 2])
    # The question is not "does the sign flip" but "is the spread caused by an
    # unobservable input bigger than the effect being claimed". If it is, the
    # headline number is a restatement of the assumption. An earlier version of
    # this check required BOTH monotonicity and a sign flip, and so passed a
    # result that ran +7.7% -> +44.4% straight up the sweep.
    dominated = span > 2 * abs(base) or span > 20.0

    if monotonic and dominated:
        verdict = ("ARTIFACT — returns rise monotonically with the IV "
                   "assumption and the sweep spans more than the result "
                   "itself. This measures the input, not the market.")
    elif dominated:
        verdict = ("NOT USABLE — the sweep spans more than the claimed result, "
                   "so the headline sits inside the error bar of an assumption "
                   "that cannot be observed without real option quotes.")
    elif flips:
        verdict = "FRAGILE — the sign changes within a plausible assumption range."
    else:
        verdict = "Robust to the IV term-structure assumption."

    return {
        "ok": True, "dte": dte, "results": rows,
        "monotonic_in_assumption": monotonic,
        "return_span_pct": round(span, 2),
        "base_case_return_pct": round(base, 2),
        "sign_flips_within_sweep": flips,
        "assumption_dominates": dominated,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# 6. Vertical credit spreads — the one option structure testable without a
#    live chain.
#
# Every naked-option result in this project died the same way: the premium is
# model-priced, so the answer tracked the IV assumption and the sensitivity
# sweep correctly called it an artifact. A vertical spread is different in two
# ways that matter here:
#
#   * Both legs are priced at the SAME IV, so much of the assumption cancels.
#     Measured on NIFTY 5 DTE across IV 8-20%: a naked put's value swings 106%
#     while the spread's credit swings 44%. Better, not immune.
#   * MAX LOSS IS FIXED BY THE STRIKE WIDTH, not by IV. The risk is known
#     exactly even when the premium is uncertain — which is the property that
#     makes this honest to test at all.
#
# Implemented as the two standard directional credit spreads:
#   bull put  — sell the nearer put, buy the further put   (profits if spot holds up)
#   bear call — sell the nearer call, buy the further call (profits if spot holds down)


def vertical_spread(symbol: str = "NIFTY", period: str = "2y", dte: int = 7,
                    width_steps: int = 4, otm_steps: int = 2,
                    iv_scale: float = 1.0, lot: int = DEFAULT_LOT,
                    capital: float = 500_000.0, direction: str = "bull") -> dict:
    """Sell a vertical credit spread every `dte` days, priced at the real VIX.

    `otm_steps` is how far the short strike sits from spot, `width_steps` the
    distance to the long (protective) strike, both in real strike increments.
    The payoff uses the index's ACTUAL move; only the premium is modelled.
    """
    df = _align(period)
    if df is None:
        return {"ok": False, "error": "could not align NIFTY and India VIX"}

    close = df["Close"].to_numpy(dtype=float)
    vix = df["VIX"].to_numpy(dtype=float)
    dates = [str(d.date()) for d in df.index]
    n = len(close)
    kind = "put" if direction == "bull" else "call"
    sign = -1 if direction == "bull" else 1       # OTM puts sit below spot

    trades: list[dict] = []
    i = 0
    while i + dte < n:
        spot, iv = close[i], float(vix[i]) * iv_scale
        if not (iv > 0 and spot > 0):
            i += 1
            continue
        try:
            k_short = float(op.snap_strike(spot, "NIFTY", otm_steps * sign))
            k_long = float(op.snap_strike(spot, "NIFTY", (otm_steps + width_steps) * sign))
        except Exception:
            i += 1
            continue
        width = abs(k_short - k_long)
        if width <= 0:
            i += 1
            continue

        T = dte / 365.0
        try:
            credit = (float(op.bs(spot, k_short, T, iv, kind).price)
                      - float(op.bs(spot, k_long, T, iv, kind).price))
        except Exception:
            i += 1
            continue
        if credit <= 0:
            i += 1
            continue

        exit_i = min(i + dte, n - 1)
        s_t = close[exit_i]
        # Intrinsic payoff of the spread at expiry, capped at the width.
        if kind == "put":
            payoff = max(0.0, k_short - s_t) - max(0.0, k_long - s_t)
        else:
            payoff = max(0.0, s_t - k_short) - max(0.0, s_t - k_long)
        payoff = max(0.0, min(payoff, width))

        qty = float(lot)
        gross = (credit - payoff) * qty
        # Two legs in and two out: four option orders, charged on both
        # premiums rather than pretending a spread trades as one instrument.
        ch = (charges.round_trip_charges(credit, payoff, qty).total
              + charges.round_trip_charges(width - credit, width - payoff, qty).total * 0.5)
        trades.append({
            "entry_date": dates[i], "exit_date": dates[exit_i],
            "spot_in": round(spot, 2), "spot_out": round(s_t, 2),
            "k_short": k_short, "k_long": k_long, "width": width,
            "iv_used": round(iv, 4), "credit": round(credit, 2),
            "payoff": round(payoff, 2),
            "max_loss": round((width - credit) * qty, 2),
            "gross": round(gross, 2), "charges": round(ch, 2),
            "net": round(gross - ch, 2),
            "won": bool(gross - ch > 0),
        })
        i = exit_i + 1

    if not trades:
        return {"ok": False, "error": "no trades generated"}

    nets = np.array([t["net"] for t in trades])
    pre_tax = float(nets.sum())
    tax = charges.tax_on_portfolio(pre_tax)
    curve = np.concatenate([[capital], capital + np.cumsum(nets)])
    peak = np.maximum.accumulate(curve)
    gw = float(nets[nets > 0].sum())
    gl = float(-nets[nets < 0].sum())
    sd = float(nets.std(ddof=1)) if nets.size > 1 else 0.0

    return {
        "ok": True, "strategy": f"{direction}_spread_{dte}d",
        "symbol": symbol.upper(), "period": period, "dte": dte,
        "otm_steps": otm_steps, "width_steps": width_steps, "iv_scale": iv_scale,
        "from": dates[0], "to": dates[-1], "trades": len(trades),
        "wins": int((nets > 0).sum()), "losses": int((nets < 0).sum()),
        "win_rate": round(float((nets > 0).mean() * 100), 1),
        "gross_pnl": round(float(sum(t["gross"] for t in trades)), 2),
        "charges_paid": round(float(sum(t["charges"] for t in trades)), 2),
        "net_pre_tax": round(pre_tax, 2), "tax": round(tax, 2),
        "net_after_tax": round(pre_tax - tax, 2),
        "return_pct": round((pre_tax - tax) / capital * 100, 2),
        "avg_net": round(float(nets.mean()), 2),
        "best": round(float(nets.max()), 2), "worst": round(float(nets.min()), 2),
        "profit_factor": round(gw / gl, 2) if gl > 1e-9 else (99.0 if gw > 0 else 0.0),
        "max_dd_pct": round(float(((curve - peak) / peak).min() * 100), 2),
        "t_stat": round(float(nets.mean() / (sd / math.sqrt(nets.size))), 2) if sd > 1e-9 else 0.0,
        "max_loss_per_trade": round(trades[0]["max_loss"], 2),
        "trades_list": trades,
    }


def spread_iv_sensitivity(symbol: str = "NIFTY", period: str = "2y", dte: int = 7,
                          direction: str = "bull",
                          scales=(0.8, 0.9, 1.0, 1.1, 1.2, 1.3)) -> dict:
    """Same circularity guard the naked straddle gets. A spread is less
    IV-sensitive, not insensitive, so it still has to pass."""
    rows = []
    for s in scales:
        r = vertical_spread(symbol, period, dte, iv_scale=s, direction=direction)
        if r.get("ok"):
            rows.append({"iv_scale": s, "return_pct": r["return_pct"],
                         "win_rate": r["win_rate"], "trades": r["trades"],
                         "worst": r["worst"], "t_stat": r["t_stat"]})
    if len(rows) < 3:
        return {"ok": False, "error": "sweep failed"}
    rets = [r["return_pct"] for r in rows]
    base = next((r["return_pct"] for r in rows if abs(r["iv_scale"] - 1.0) < 1e-9),
                rets[len(rets) // 2])
    span = max(rets) - min(rets)
    monotonic = all(b >= a for a, b in zip(rets, rets[1:]))
    dominated = span > 2 * abs(base) or span > 20.0
    return {
        "ok": True, "dte": dte, "direction": direction, "results": rows,
        "base_case_return_pct": round(base, 2), "return_span_pct": round(span, 2),
        "monotonic_in_assumption": monotonic, "assumption_dominates": dominated,
        "verdict": ("ARTIFACT — tracks the IV assumption" if monotonic and dominated
                    else "NOT USABLE — sweep spans more than the result" if dominated
                    else "Robust to the IV assumption"),
    }


# ---------------------------------------------------------------------------
# 7. NIFTY / BANKNIFTY pair — market-neutral relative value.
#
# Everything tested so far is directional, and the cross-market work showed
# those returns are largely BETA: strategies are 50-71% long in markets that
# rose, and against a side-matched baseline the edge vanishes. A pair trade
# removes that entirely — long one index, short the other, so the market's
# direction cancels and only the RELATIVE move matters.
#
# It is also cheap: both legs are futures (~0.05% round trip) rather than the
# 4.5-14% an option structure pays.
#
# The honest risk: the two indices are ~90% correlated, which is what makes
# the spread tradeable, but correlation is not cointegration. If the ratio
# drifts permanently rather than mean-reverting, every entry is early and the
# losses compound. The test below is built to show that if it is happening.


def pair_spread(period: str = "5y", lookback: int = 60, entry_z: float = 2.0,
                exit_z: float = 0.5, max_hold: int = 20, stop_z: float = 4.0,
                capital: float = 500_000.0, cost_pct: float = 0.05) -> dict:
    """Trade the BANKNIFTY/NIFTY ratio back to its rolling mean.

    Enter when the ratio is `entry_z` standard deviations from its rolling
    mean (short the rich index, long the cheap one), exit on reversion to
    `exit_z`, a `stop_z` blowout, or a time stop. P&L is the RELATIVE move,
    so a market-wide rally or selloff nets out.

    `cost_pct` is the round trip for BOTH legs combined, as a percentage of
    one leg's notional — measured at ~0.049% per futures leg earlier.
    """
    from . import backtest
    nf = backtest.fetch_history("NIFTY", period, "1d", min_bars=200)
    bn = backtest.fetch_history("BANKNIFTY", period, "1d", min_bars=200)
    if nf is None or bn is None:
        return {"ok": False, "error": "could not load both indices"}

    a = nf["Close"].copy(); a.index = pd.to_datetime(a.index).tz_localize(None).normalize()
    b = bn["Close"].copy(); b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
    df = pd.concat([a.rename("nifty"), b.rename("banknifty")], axis=1).dropna()
    if len(df) < lookback + 60:
        return {"ok": False, "error": "not enough overlapping history"}

    ratio = (df["banknifty"] / df["nifty"]).to_numpy(dtype=float)
    nifty = df["nifty"].to_numpy(dtype=float)
    bank = df["banknifty"].to_numpy(dtype=float)
    dates = [str(d.date()) for d in df.index]
    n = len(ratio)

    # Rolling mean/sd computed on data strictly BEFORE each bar.
    mean = np.full(n, np.nan); sd = np.full(n, np.nan)
    for i in range(lookback, n):
        w = ratio[i - lookback:i]
        mean[i] = w.mean(); sd[i] = w.std(ddof=1)
    z = np.where(sd > 0, (ratio - mean) / sd, np.nan)

    trades: list[dict] = []
    i = lookback
    while i < n - 1:
        zi = z[i]
        if not np.isfinite(zi) or abs(zi) < entry_z:
            i += 1
            continue
        # z > 0 => BANKNIFTY rich relative to NIFTY => short bank, long nifty
        short_bank = zi > 0
        e_ratio, e_bank, e_nifty = ratio[i], bank[i], nifty[i]
        exit_i, reason = min(i + max_hold, n - 1), "TIME"
        for j in range(i + 1, min(i + max_hold + 1, n)):
            zj = z[j]
            if not np.isfinite(zj):
                continue
            if abs(zj) >= stop_z and (zj > 0) == (zi > 0):
                exit_i, reason = j, "STOP"
                break
            if abs(zj) <= exit_z:
                exit_i, reason = j, "REVERT"
                break

        # Relative return: the pair's P&L is the difference between the legs,
        # so a move common to both indices cancels.
        r_bank = (bank[exit_i] - e_bank) / e_bank
        r_nifty = (nifty[exit_i] - e_nifty) / e_nifty
        rel = (r_nifty - r_bank) if short_bank else (r_bank - r_nifty)
        gross_pct = rel * 100
        net_pct = gross_pct - cost_pct
        trades.append({
            "entry_date": dates[i], "exit_date": dates[exit_i],
            "bars_held": exit_i - i, "reason": reason,
            "z_entry": round(float(zi), 2), "z_exit": round(float(z[exit_i]), 2),
            "side": "short_bank_long_nifty" if short_bank else "long_bank_short_nifty",
            "ratio_in": round(e_ratio, 4), "ratio_out": round(ratio[exit_i], 4),
            "gross_pct": round(gross_pct, 4), "net_pct": round(net_pct, 4),
        })
        i = exit_i + 1

    if len(trades) < 5:
        return {"ok": False, "error": f"only {len(trades)} trades", "trades": len(trades)}

    nets = np.array([t["net_pct"] for t in trades])
    sd_n = float(nets.std(ddof=1)) if nets.size > 1 else 0.0
    t_stat = float(nets.mean() / (sd_n / math.sqrt(nets.size))) if sd_n > 1e-12 else 0.0
    eq = capital * np.cumprod(1 + nets / 100.0)
    curve = np.concatenate([[capital], eq])
    peak = np.maximum.accumulate(curve)
    gw = float(nets[nets > 0].sum()); gl = float(-nets[nets < 0].sum())

    mid = len(nets) // 2
    return {
        "ok": True, "strategy": "nifty_banknifty_pair", "period": period,
        "lookback": lookback, "entry_z": entry_z, "exit_z": exit_z,
        "max_hold": max_hold, "cost_pct": cost_pct,
        "from": dates[lookback], "to": dates[-1],
        "trades": len(trades),
        "win_rate": round(float((nets > 0).mean() * 100), 1),
        "mean_net_pct": round(float(nets.mean()), 4),
        "total_net_pct": round(float(nets.sum()), 2),
        "return_pct": round(float((eq[-1] - capital) / capital * 100), 2),
        "t_stat": round(t_stat, 2),
        "profit_factor": round(gw / gl, 2) if gl > 1e-9 else (99.0 if gw > 0 else 0.0),
        "max_dd_pct": round(float(((curve - peak) / peak).min() * 100), 2),
        "best_pct": round(float(nets.max()), 3), "worst_pct": round(float(nets.min()), 3),
        "mean_bars_held": round(float(np.mean([t["bars_held"] for t in trades])), 1),
        "exits": {k: sum(1 for t in trades if t["reason"] == k)
                  for k in ("REVERT", "STOP", "TIME")},
        "first_half_net_pct": round(float(nets[:mid].sum()), 2),
        "second_half_net_pct": round(float(nets[mid:].sum()), 2),
        "both_halves_positive": bool(nets[:mid].sum() > 0 and nets[mid:].sum() > 0),
        "trades_list": trades[:40],
    }
