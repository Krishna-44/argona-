"""Signal event study — measures a signal's edge WITHOUT the portfolio harness.

Why this module exists
----------------------
The sequential backtest (`backtest.simulate_futures`) walks bar by bar: when a
signal fires it opens a trade, holds, exits, then resumes scanning from the bar
after the exit. Every signal that fires while a position is open is silently
dropped.

That is realistic for a single-position desk, but it destroys the thing we are
trying to measure. With ~300 signals over ~1,570 bars and a 12-bar hold, the
desk is in a position roughly three-quarters of the time, so trade count
saturates at `bars / hold` and the entry becomes "whatever fires first after the
last exit" — nearly a time sample. Two strategies with ZERO overlapping signals
produced almost identical trade counts (97 vs 95) for exactly this reason, which
is how the bug surfaced.

What this does instead
----------------------
Evaluates EVERY signal independently, overlaps allowed. Entry at bar i, same
TP / SL / time-stop rules, same India F&O cost model, and the result is the net
return of that one trade. No position limit, so a selective signal is actually
allowed to be selective.

Two guards against fooling ourselves:

  * **Random-entry baseline.** The question is never "did it make money" — it is
    "did it beat entering at random?" Any TP/SL scheme has a built-in win rate
    (a tight TP and wide SL wins most of the time and still loses money), so the
    only meaningful benchmark is the same rules fired on random bars with the
    same long/short mix. p is the fraction of random draws that matched or beat
    the real signal.

  * **Precomputed outcome table.** Outcomes for entering long/short at every bar
    are computed once, so the actual signal and all bootstrap draws are lookups
    into the same table. The baseline is therefore exact, not a re-simulation
    that could drift from the live path.

Overlapping windows make the naive t-stat optimistic (observations share bars),
so it is reported but the randomization p-value is the number to trust.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field

import numpy as np

from . import charges

# Per-side adverse fill on the UNDERLYING, in basis points. NIFTY futures move
# in 0.05 pt ticks on a ~24,000 index; 1bp (~2.4 pts) is a conservative
# assumption for a market order that crosses the spread.
SLIP_BPS = 1.0

# Notional per trade. The cost model has a flat per-order component (~₹20), so
# charges are only realistic against a real position size — one NIFTY lot.
NOTIONAL = 500_000.0


@dataclass
class EventStats:
    strategy: str
    n_signals: int
    n_long: int
    n_short: int
    horizon: int
    tp_pct: float
    sl_pct: float

    mean_net_pct: float       # per signal, after charges, % of notional
    median_net_pct: float
    std_net_pct: float
    hit_rate: float           # % of signals with net > 0
    t_stat: float             # naive; inflated by overlap
    sum_net_pct: float        # total if you took 1 unit on every signal

    long_mean_pct: float
    short_mean_pct: float
    tp_rate: float
    sl_rate: float
    time_rate: float
    mean_bars_held: float

    # randomization test vs. entering at random with the same side mix
    random_mean_pct: float
    random_p5: float
    random_p95: float
    p_value: float            # P(random >= actual)
    edge_vs_random_pct: float
    verdict: str
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _outcome_table(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                   horizon: int, tp_pct: float, sl_pct: float,
                   slip_bps: float = SLIP_BPS) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Net % return of entering at EVERY bar, for both sides.

    Returns (net_long, net_short, reason_code, bars_held) where reason_code is
    0=TP 1=SL 2=TIME, each shaped (2, n) for [long, short]. Bars with no room
    for a full horizon are marked NaN and excluded everywhere downstream.
    """
    n = len(close)
    net = np.full((2, n), np.nan)
    reason = np.full((2, n), -1, dtype=int)
    held = np.full((2, n), np.nan)
    slip = slip_bps / 10_000.0

    for s, is_long in enumerate((True, False)):
        for i in range(n - 1):
            entry = close[i]
            if not np.isfinite(entry) or entry <= 0:
                continue
            # pay the spread on the way in
            fill = entry * (1 + slip) if is_long else entry * (1 - slip)
            last = min(i + 1 + horizon, n)
            if last <= i + 1:
                continue
            exit_px, code, bars = close[last - 1], 2, last - 1 - i
            for j in range(i + 1, last):
                fav = (high[j] - fill) / fill if is_long else (fill - low[j]) / fill
                adv = (low[j] - fill) / fill if is_long else (fill - high[j]) / fill
                # Conservative: if both TP and SL are inside the same bar, assume
                # the stop filled first — we cannot see intrabar order.
                if adv <= -sl_pct:
                    exit_px = fill * (1 - sl_pct) if is_long else fill * (1 + sl_pct)
                    code, bars = 1, j - i
                    break
                if fav >= tp_pct:
                    exit_px = fill * (1 + tp_pct) if is_long else fill * (1 - tp_pct)
                    code, bars = 0, j - i
                    break
                exit_px, bars = close[j], j - i
            # pay the spread on the way out
            exit_fill = exit_px * (1 - slip) if is_long else exit_px * (1 + slip)
            move = (exit_fill - fill) if is_long else (fill - exit_fill)
            qty = NOTIONAL / fill
            pnl = charges.settle(fill, fill + move, qty, segment="futures", apply_tax=False)
            net[s, i] = pnl.net / NOTIONAL * 100.0
            reason[s, i] = code
            held[s, i] = bars
    return net[0], net[1], reason, held


def event_study(df, strategy: str, horizon: int = 12,
                tp_pct: float = 0.010, sl_pct: float = 0.006,
                n_random: int = 1000, seed: int = 7,
                invert: bool = False) -> EventStats:
    """Measure one strategy's per-signal edge against a random-entry baseline.

    `invert` swaps long/short. A signal that is reliably WORSE than random is
    information too — but only if the inverse survives the same test, because
    TP/SL geometry is asymmetric and flipping the side does not simply flip the
    sign of the return.
    """
    from . import backtest

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    n = len(close)

    long_sig, short_sig = backtest.catalog_signals(df, strategy)
    if invert:
        long_sig, short_sig = short_sig, long_sig
        strategy = f"{strategy}_INV"
    net_l, net_s, reason, held = _outcome_table(high, low, close, horizon, tp_pct, sl_pct)

    valid = np.isfinite(net_l) & np.isfinite(net_s)
    li = np.flatnonzero(np.asarray(long_sig, dtype=bool) & valid)
    si = np.flatnonzero(np.asarray(short_sig, dtype=bool) & valid)
    n_long, n_short = len(li), len(si)
    total = n_long + n_short

    if total < 10:
        return EventStats(
            strategy=strategy, n_signals=total, n_long=n_long, n_short=n_short,
            horizon=horizon, tp_pct=tp_pct, sl_pct=sl_pct,
            mean_net_pct=0.0, median_net_pct=0.0, std_net_pct=0.0, hit_rate=0.0,
            t_stat=0.0, sum_net_pct=0.0, long_mean_pct=0.0, short_mean_pct=0.0,
            tp_rate=0.0, sl_rate=0.0, time_rate=0.0, mean_bars_held=0.0,
            random_mean_pct=0.0, random_p5=0.0, random_p95=0.0, p_value=1.0,
            edge_vs_random_pct=0.0, verdict="TOO FEW SIGNALS",
            note=f"only {total} signals — nothing to measure")

    rets = np.concatenate([net_l[li], net_s[si]])
    codes = np.concatenate([reason[0][li], reason[1][si]])
    bars = np.concatenate([held[0][li], held[1][si]])

    mean = float(rets.mean())
    std = float(rets.std(ddof=1)) if rets.size > 1 else 0.0
    t_stat = mean / (std / math.sqrt(rets.size)) if std > 1e-12 else 0.0

    # ---- randomization baseline -----------------------------------------
    # Same number of longs and shorts, drawn from every bar that had a valid
    # outcome. This asks the only question that matters: is the signal's
    # timing worth anything, or would darts have done as well?
    pool = np.flatnonzero(valid)
    rng = np.random.default_rng(seed)
    sims = np.empty(n_random)
    for k in range(n_random):
        acc = 0.0
        if n_long:
            acc += net_l[rng.choice(pool, n_long, replace=True)].sum()
        if n_short:
            acc += net_s[rng.choice(pool, n_short, replace=True)].sum()
        sims[k] = acc / total
    p_value = float((sims >= mean).sum() + 1) / (n_random + 1)
    rnd_mean = float(sims.mean())

    if p_value <= 0.05 and mean > 0:
        verdict = "BEATS RANDOM"
    elif p_value >= 0.95 and mean < rnd_mean:
        verdict = "WORSE THAN RANDOM"
    else:
        verdict = "NO EDGE (indistinguishable from random)"

    return EventStats(
        strategy=strategy, n_signals=total, n_long=n_long, n_short=n_short,
        horizon=horizon, tp_pct=tp_pct, sl_pct=sl_pct,
        mean_net_pct=round(mean, 5),
        median_net_pct=round(float(np.median(rets)), 5),
        std_net_pct=round(std, 5),
        hit_rate=round(float((rets > 0).mean() * 100), 2),
        t_stat=round(t_stat, 2),
        sum_net_pct=round(float(rets.sum()), 3),
        long_mean_pct=round(float(net_l[li].mean()), 5) if n_long else 0.0,
        short_mean_pct=round(float(net_s[si].mean()), 5) if n_short else 0.0,
        tp_rate=round(float((codes == 0).mean() * 100), 2),
        sl_rate=round(float((codes == 1).mean() * 100), 2),
        time_rate=round(float((codes == 2).mean() * 100), 2),
        mean_bars_held=round(float(bars.mean()), 2),
        random_mean_pct=round(rnd_mean, 5),
        random_p5=round(float(np.percentile(sims, 5)), 5),
        random_p95=round(float(np.percentile(sims, 95)), 5),
        p_value=round(p_value, 4),
        edge_vs_random_pct=round(mean - rnd_mean, 5),
        verdict=verdict,
        note=("Overlapping windows inflate the t-stat; trust the p-value. "
              f"Costs: India F&O futures card + {SLIP_BPS}bp slip/side on "
              f"₹{NOTIONAL:,.0f} notional."),
    )


def exposure(df, strategy: str, horizon: int = 12,
             tp_pct: float = 0.010, sl_pct: float = 0.006) -> dict:
    """How much of the signal does the sequential harness actually trade?

    Low utilisation is the saturation warning: it means the portfolio sim is
    sampling the signal rather than following it, and differences between
    strategies are being washed out by the single-position constraint.
    """
    from . import backtest

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    n = len(close)
    long_sig, short_sig = backtest.catalog_signals(df, strategy)
    fired = int(np.asarray(long_sig, dtype=bool).sum() + np.asarray(short_sig, dtype=bool).sum())

    taken, bars_in = 0, 0
    i = 0
    while i < n:
        is_long = bool(long_sig[i])
        if not (is_long or bool(short_sig[i])):
            i += 1
            continue
        entry = close[i]
        exit_i = i
        for j in range(i + 1, min(i + 1 + horizon, n)):
            fav = (high[j] - entry) / entry if is_long else (entry - low[j]) / entry
            adv = (low[j] - entry) / entry if is_long else (entry - high[j]) / entry
            exit_i = j
            if adv <= -sl_pct or fav >= tp_pct:
                break
        taken += 1
        bars_in += exit_i - i
        i = exit_i + 1

    util = (taken / fired * 100) if fired else 0.0
    expo = (bars_in / n * 100) if n else 0.0
    return {
        "strategy": strategy, "bars": n, "signals_fired": fired,
        "trades_taken": taken, "signals_dropped": fired - taken,
        "utilisation_pct": round(util, 1),
        "exposure_pct": round(expo, 1),
        "saturated": bool(expo > 60.0 and util < 50.0),
        "note": ("SATURATED — the desk is in a position most of the time, so "
                 "entries are 'first signal after the last exit', not the best "
                 "signal. Sequential returns are not measuring selectivity here."
                 if (expo > 60.0 and util < 50.0) else
                 "Utilisation is high enough that sequential results reflect "
                 "the signal rather than the position limit."),
    }


def trade_log(df, strategy: str, horizon: int = 48,
              tp_pct: float = 0.008, sl_pct: float = 0.008,
              invert: bool = False, mode: str = "desk",
              capital: float = 500_000.0) -> dict:
    """The actual trade-by-trade ledger: entry, exit, reason, charges, net.

    Two modes, because they answer different questions:

      * ``event`` — every signal taken, overlaps allowed. This is the SIGNAL's
        record. Not tradeable with one position, but it is what the statistics
        are computed on.
      * ``desk``  — one position at a time, signals that fire while a trade is
        open are dropped. This is what you would actually have traded, with a
        running equity curve.

    One lot of fixed notional per trade, so equity is additive and the ledger
    reads the way a broker statement does. Tax is applied once at the end on
    net profit (F&O losses offset gains), never per trade.
    """
    from . import backtest

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    n = len(close)
    long_sig, short_sig = backtest.catalog_signals(df, strategy)
    if invert:
        long_sig, short_sig = short_sig, long_sig
        strategy = f"{strategy}_INV"
    long_sig = np.asarray(long_sig, dtype=bool)
    short_sig = np.asarray(short_sig, dtype=bool)
    slip = SLIP_BPS / 10_000.0

    def _ts(i: int) -> str:
        try:
            return str(df.index[i])[:16]
        except Exception:
            return str(i)

    def _one(i: int, is_long: bool) -> dict | None:
        entry = close[i]
        if not np.isfinite(entry) or entry <= 0:
            return None
        fill = entry * (1 + slip) if is_long else entry * (1 - slip)
        last = min(i + 1 + horizon, n)
        if last <= i + 1:
            return None
        exit_px, reason, bars = close[last - 1], "TIME", last - 1 - i
        for j in range(i + 1, last):
            fav = (high[j] - fill) / fill if is_long else (fill - low[j]) / fill
            adv = (low[j] - fill) / fill if is_long else (fill - high[j]) / fill
            if adv <= -sl_pct:                       # stop first if both in-bar
                exit_px = fill * (1 - sl_pct) if is_long else fill * (1 + sl_pct)
                reason, bars = "SL", j - i
                break
            if fav >= tp_pct:
                exit_px = fill * (1 + tp_pct) if is_long else fill * (1 - tp_pct)
                reason, bars = "TP", j - i
                break
            exit_px, bars = close[j], j - i
        exit_fill = exit_px * (1 - slip) if is_long else exit_px * (1 + slip)
        move = (exit_fill - fill) if is_long else (fill - exit_fill)
        qty = NOTIONAL / fill
        pnl = charges.settle(fill, fill + move, qty, segment="futures", apply_tax=False)
        return {
            "i_entry": i, "i_exit": i + bars,
            "entry_time": _ts(i), "exit_time": _ts(min(i + bars, n - 1)),
            "side": "LONG" if is_long else "SHORT",
            "entry": round(fill, 2), "exit": round(exit_fill, 2),
            "bars": int(bars), "move_pct": round(move / fill * 100, 4),
            "gross": round(pnl.gross, 2), "charges": round(pnl.charges, 2),
            "net": round(pnl.net, 2), "net_pct": round(pnl.net / NOTIONAL * 100, 4),
            "reason": reason,
        }

    trades: list[dict] = []
    if mode == "desk":
        i = 0
        while i < n:
            is_long = bool(long_sig[i])
            if not (is_long or bool(short_sig[i])):
                i += 1
                continue
            t = _one(i, is_long)
            if t is None:
                i += 1
                continue
            trades.append(t)
            i = t["i_exit"] + 1
    else:
        for i in range(n):
            if long_sig[i]:
                t = _one(i, True)
                if t:
                    trades.append(t)
            if short_sig[i]:
                t = _one(i, False)
                if t:
                    trades.append(t)
        trades.sort(key=lambda t: t["i_entry"])

    eq = capital
    for k, t in enumerate(trades, 1):
        eq += t["net"]
        t["n"] = k
        t["equity"] = round(eq, 2)

    nets = np.array([t["net"] for t in trades]) if trades else np.array([])
    wins = int((nets > 0).sum()) if nets.size else 0
    losses = int((nets < 0).sum()) if nets.size else 0
    gw = float(nets[nets > 0].sum()) if nets.size else 0.0
    gl = float(-nets[nets < 0].sum()) if nets.size else 0.0
    pre_tax = float(nets.sum()) if nets.size else 0.0
    tax = charges.tax_on_portfolio(pre_tax)

    curve = np.array([capital] + [t["equity"] for t in trades], dtype=float)
    peak = np.maximum.accumulate(curve)
    dd = float(((curve - peak) / peak).min() * 100) if curve.size else 0.0

    fired = int(long_sig.sum() + short_sig.sum())
    return {
        "strategy": strategy, "mode": mode, "bars": n,
        "horizon": horizon, "tp_pct": tp_pct, "sl_pct": sl_pct,
        "signals_fired": fired, "trades": len(trades),
        "wins": wins, "losses": losses,
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0,
        "tp_exits": sum(1 for t in trades if t["reason"] == "TP"),
        "sl_exits": sum(1 for t in trades if t["reason"] == "SL"),
        "time_exits": sum(1 for t in trades if t["reason"] == "TIME"),
        "gross_pnl": round(float(sum(t["gross"] for t in trades)), 2),
        "charges_paid": round(float(sum(t["charges"] for t in trades)), 2),
        "net_pre_tax": round(pre_tax, 2),
        "tax": round(tax, 2),
        "net_after_tax": round(pre_tax - tax, 2),
        "return_pct": round((pre_tax - tax) / capital * 100, 3),
        "profit_factor": round(gw / gl, 2) if gl > 1e-9 else (99.0 if gw > 0 else 0.0),
        "avg_net": round(float(nets.mean()), 2) if nets.size else 0.0,
        "best": round(float(nets.max()), 2) if nets.size else 0.0,
        "worst": round(float(nets.min()), 2) if nets.size else 0.0,
        "max_dd_pct": round(dd, 2),
        "capital": capital, "notional_per_trade": NOTIONAL,
        "trades_list": trades,
    }


def desk_significance(df, strategy: str, horizon: int = 48,
                      tp_pct: float = 0.008, sl_pct: float = 0.008,
                      invert: bool = False, capital: float = 500_000.0,
                      n_sims: int = 5000, seed: int = 3) -> dict:
    """Could a desk of random trades have produced this ledger?

    A sequential ledger is seductive: it has timestamps, an equity curve and a
    bottom line, so it reads like evidence. But when the desk only takes a few
    percent of the signals, the ledger is a tiny sample — and a tiny sample of
    a zero-edge signal is positive about half the time.

    This draws the SAME NUMBER of trades at random entry bars and sides, many
    times, and reports where the real ledger falls in that distribution. If a
    coin flip lands here often, the ledger is not evidence of anything.
    """
    log = trade_log(df, strategy, horizon, tp_pct, sl_pct, invert, "desk", capital)
    k = log["trades"]
    if k < 2:
        return {**{m: log[m] for m in ("strategy", "trades", "return_pct")},
                "verdict": "TOO FEW TRADES"}

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    net_l, net_s, _, _ = _outcome_table(high, low, close, horizon, tp_pct, sl_pct)
    ok = np.flatnonzero(np.isfinite(net_l) & np.isfinite(net_s))

    rng = np.random.default_rng(seed)
    sims = np.empty(n_sims)
    for s in range(n_sims):
        idx = rng.choice(ok, k, replace=True)
        side = rng.integers(0, 2, k)
        pct = np.where(side == 0, net_l[idx], net_s[idx])
        sims[s] = pct.sum() * NOTIONAL / 100.0      # currency, pre-tax

    actual = log["net_pre_tax"]
    p = float((sims >= actual).sum() + 1) / (n_sims + 1)
    nets = np.array([t["net"] for t in log["trades_list"]], dtype=float)
    sd = float(nets.std(ddof=1)) if nets.size > 1 else 0.0
    t_stat = float(nets.mean() / (sd / math.sqrt(nets.size))) if sd > 1e-9 else 0.0

    return {
        "strategy": log["strategy"], "trades": k,
        "signals_fired": log["signals_fired"],
        "sampled_pct": round(k / max(log["signals_fired"], 1) * 100, 1),
        "net_pre_tax": actual, "return_pct": log["return_pct"],
        "t_stat": round(t_stat, 2),
        "random_median": round(float(np.median(sims)), 0),
        "random_p5": round(float(np.percentile(sims, 5)), 0),
        "random_p95": round(float(np.percentile(sims, 95)), 0),
        "pct_of_random_desks_beating_it": round(p * 100, 1),
        "p_value": round(p, 4),
        "verdict": ("BEATS A RANDOM DESK" if p <= 0.05 else
                    "INDISTINGUISHABLE FROM A RANDOM DESK"),
        "note": (f"The desk traded {k} of {log['signals_fired']} signals "
                 f"({round(k/max(log['signals_fired'],1)*100,1)}%). At this "
                 "sample size the ledger's bottom line carries almost no "
                 "information about the signal."),
    }


def geometry_map(df, horizons=(6, 12, 24, 48), tps=(0.002, 0.004, 0.008, 0.015),
                 sls=(0.002, 0.004, 0.008, 0.015)) -> list[dict]:
    """What does the EXIT STRUCTURE alone cost, before any signal?

    Evaluates every (horizon, TP, SL) combination over EVERY bar and both
    sides. There is no signal here and therefore nothing to curve-fit — this is
    the house edge of the execution scheme itself.

    It answers the question underneath everything else: if a random entry loses
    X% per trade under a given geometry, then any signal using that geometry has
    to find more than X% of edge before it breaks even. If no geometry is close
    to neutral, the structure is the problem, not the signal.

    This used to draw a random sample of entries, which was a mistake — at 400
    draws the sampling error (~0.05%/trade) was larger than the effect being
    measured, and it manufactured a "break-even geometry" that reversed sign on
    its own dataset when resampled. Averaging every bar is both exact and
    cheaper: the outcome table already holds all of them.
    """
    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    out: list[dict] = []

    for h in horizons:
        for tp in tps:
            for sl in sls:
                net_l, net_s, reason, held = _outcome_table(high, low, close, h, tp, sl)
                ok = np.isfinite(net_l) & np.isfinite(net_s)
                if ok.sum() < 50:
                    continue
                # exact expectation of a random entry: every bar, both sides
                rets = np.concatenate([net_l[ok], net_s[ok]])
                codes = np.concatenate([reason[0][ok], reason[1][ok]])
                bars = np.concatenate([held[0][ok], held[1][ok]])
                mean = float(rets.mean())
                # standard error, so a near-zero reading can be judged honestly
                se = float(rets.std(ddof=1) / math.sqrt(rets.size)) if rets.size > 1 else 0.0
                out.append({
                    "horizon": h, "tp_pct": tp, "sl_pct": sl,
                    "rr": round(tp / sl, 2),
                    "n_entries": int(rets.size),
                    "mean_net_pct": round(mean, 5),
                    "stderr_pct": round(se, 5),
                    "hit_rate": round(float((rets > 0).mean() * 100), 1),
                    "tp_rate": round(float((codes == 0).mean() * 100), 1),
                    "sl_rate": round(float((codes == 1).mean() * 100), 1),
                    "time_rate": round(float((codes == 2).mean() * 100), 1),
                    "mean_bars_held": round(float(bars.mean()), 1),
                    "edge_needed_pct": round(-mean, 5),
                })
    out.sort(key=lambda r: -r["mean_net_pct"])
    return out


def compare(df, strategies: list[str], horizon: int = 12,
            tp_pct: float = 0.010, sl_pct: float = 0.006,
            n_random: int = 1000) -> list[dict]:
    """Event-study every strategy on the same bars and rank by edge over random.

    Bonferroni note: testing k strategies on one dataset means the best-looking
    p-value should be compared against 0.05/k, not 0.05.
    """
    rows = []
    for s in strategies:
        try:
            st = event_study(df, s, horizon, tp_pct, sl_pct, n_random)
            row = st.as_dict()
            row["bonferroni_threshold"] = round(0.05 / max(len(strategies), 1), 4)
            row["significant_after_correction"] = bool(
                st.p_value <= 0.05 / max(len(strategies), 1) and st.mean_net_pct > 0)
            rows.append(row)
        except Exception as e:  # a broken signal must not kill the sweep
            rows.append({"strategy": s, "error": str(e)})
    rows.sort(key=lambda r: -(r.get("edge_vs_random_pct") or -99))
    return rows
