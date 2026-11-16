"""Live two-leg pair trading for the NIFTY/BANKNIFTY ratio.

Why this is separate from the per-instrument loop: a pair is ONE decision that
produces TWO positions which must open together and close together. The main
engine evaluates each instrument independently, so bolting a pair into it
would give two legs that could be stopped out at different times — leaving a
naked directional position, which is exactly what a pair exists to avoid.

Backtest standing (see RESEARCH_LOG): 9/9 parameter settings positive, the
permutation test beat a matched random baseline at p=0.0135, but it fails the
Bonferroni bar for the 9 configs tested (0.0056) and the split-half test puts
all the significance in one half. So this runs live to generate genuine
out-of-sample evidence — it is NOT a validated edge, and the UI says so.
"""
from __future__ import annotations

import time
import uuid

import numpy as np
import pandas as pd

LOOKBACK = 60          # bars of ratio history for the rolling mean/sd
ENTRY_Z = 2.0          # |z| to open
EXIT_Z = 0.5           # |z| to take the reversion
STOP_Z = 4.0           # |z| that says the spread is not reverting
MAX_HOLD_SEC = 20 * 24 * 3600     # time stop, ~20 sessions
RISK_PCT = 0.01        # risk budget for the pair as a whole
_HIST_TTL = 300.0

_cache: tuple[float, object] = (0.0, None)


def _history():
    """Daily closes for both indices, aligned. Cached — this is slow."""
    global _cache
    now = time.time()
    if _cache[1] is not None and now - _cache[0] < _HIST_TTL:
        return _cache[1]
    from . import backtest
    nf = backtest.fetch_history("NIFTY", "1y", "1d", min_bars=120)
    bn = backtest.fetch_history("BANKNIFTY", "1y", "1d", min_bars=120)
    if nf is None or bn is None:
        return None
    a = nf["Close"].copy(); a.index = pd.to_datetime(a.index).tz_localize(None).normalize()
    b = bn["Close"].copy(); b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
    df = pd.concat([a.rename("nifty"), b.rename("bank")], axis=1).dropna()
    _cache = (now, df)
    return df


def live_z(spot_nifty: float | None = None, spot_bank: float | None = None) -> dict:
    """Current z-score of the BANKNIFTY/NIFTY ratio.

    Live spots replace today's close when given, so the signal reflects now
    rather than yesterday. The rolling mean/sd use only PRIOR bars.
    """
    df = _history()
    if df is None or len(df) < LOOKBACK + 5:
        return {"ok": False, "error": "insufficient history"}
    nifty = df["nifty"].to_numpy(dtype=float)
    bank = df["bank"].to_numpy(dtype=float)
    if spot_nifty and spot_bank and spot_nifty > 0:
        ratio_now = spot_bank / spot_nifty
    else:
        ratio_now = bank[-1] / nifty[-1]
    hist = (bank[:-1] / nifty[:-1])[-LOOKBACK:]
    if len(hist) < LOOKBACK // 2:
        return {"ok": False, "error": "insufficient history"}
    mu, sd = float(hist.mean()), float(hist.std(ddof=1))
    if sd <= 0:
        return {"ok": False, "error": "zero dispersion"}
    z = (ratio_now - mu) / sd
    return {"ok": True, "z": round(float(z), 3), "ratio": round(float(ratio_now), 5),
            "mean": round(mu, 5), "sd": round(sd, 6), "lookback": LOOKBACK,
            "entry_z": ENTRY_Z, "exit_z": EXIT_Z, "stop_z": STOP_Z,
            # z > 0 means BANKNIFTY is rich relative to NIFTY
            "action": ("short_bank_long_nifty" if z >= ENTRY_Z else
                       "long_bank_short_nifty" if z <= -ENTRY_Z else "none")}


def open_pair(state, options, engine_mod, z_info: dict, balance: float) -> str | None:
    """Open both legs together, tagged with one pair_id."""
    short_bank = z_info["z"] > 0
    pid = uuid.uuid4().hex[:10]
    legs = [("BANKNIFTY", "put" if short_bank else "call"),
            ("NIFTY", "call" if short_bank else "put")]

    opened = []
    for sym, kind in legs:
        u = options.get_underlying(sym, "india", sym)
        if u is None:
            break
        q = options.underlying_quote(u, kind)
        if q is None or q.mark <= 0:
            break
        slip = engine_mod.SLIPPAGE_UNDERLYING
        entry = q.ask * (1 + slip) if kind == "call" else q.bid * (1 - slip)
        lot = float(q.lot_size or 1)
        # Each leg risks half the pair's budget, sized on its own stop.
        band = options.premium_band(q, engine_mod.SL_SPOT_PCT)
        per_unit = max(entry * band, 1e-9)
        want = (RISK_PCT * balance * 0.5) / per_unit
        qty = max(lot, (want // lot) * lot)
        tp = entry * (1 + options.premium_band(q, engine_mod.TP_SPOT_PCT))
        sl = entry * (1 - band)
        tid = state.open_trade(
            underlying=sym, source="india", kind=kind, strike=q.strike,
            expiry="", instrument=q.instrument, strategy="pair_nb", qty=qty,
            entry_premium=round(entry, 6), entry_spot=u.spot,
            tp_premium=round(tp, 6), sl_premium=round(sl, 6), features=[],
            segment="futures", pair_id=pid)
        opened.append((tid, sym, kind, qty, entry))

    if len(opened) != 2:
        # A half-open pair is a naked directional bet, which is the one thing
        # this strategy must never leave behind. Unwind anything that filled.
        for tid, *_ in opened:
            state.close_trade(tid, 0.0, "PAIR_ABORT", 0.0, 0.0, 0.0, 0.0)
        state.append_activity("INFO", {"msg": "pair aborted — only one leg could be quoted"})
        return None

    state.append_activity("ENTRY", {
        "msg": (f"PAIR {pid} z={z_info['z']:+.2f} " +
                " / ".join(f"{s} {'SHORT' if k == 'put' else 'LONG'} {q:.0f}"
                           for _, s, k, q, _ in opened))})
    return pid


def manage_pairs(state, options, engine_mod, charges, emit=None) -> float:
    """Close both legs together when the pair's own exit fires."""
    opens = [t for t in state.open_trades() if t.get("pair_id")]
    if not opens:
        return 0.0
    groups: dict[str, list] = {}
    for t in opens:
        groups.setdefault(t["pair_id"], []).append(t)

    net_total = 0.0
    for pid, legs in groups.items():
        if len(legs) != 2:
            continue
        nf = next((t for t in legs if t["underlying"] == "NIFTY"), None)
        bn = next((t for t in legs if t["underlying"] == "BANKNIFTY"), None)
        if not nf or not bn:
            continue
        un = options.get_underlying("NIFTY", "india", "NIFTY")
        ub = options.get_underlying("BANKNIFTY", "india", "BANKNIFTY")
        if un is None or ub is None:
            continue
        zi = live_z(un.spot, ub.spot)
        if not zi.get("ok"):
            continue
        z = zi["z"]
        age = time.time() - min(t["ts_open"] for t in legs)

        reason = None
        if abs(z) <= EXIT_Z:
            reason = "REVERT"
        elif abs(z) >= STOP_Z:
            reason = "STOP"
        elif age >= MAX_HOLD_SEC:
            reason = "TIME"
        if reason is None:
            continue

        for t in legs:
            spot = un.spot if t["underlying"] == "NIFTY" else ub.spot
            exit_px = spot if t["kind"] == "call" else (2.0 * t["entry_premium"] - spot)
            exit_px = max(exit_px, 1e-6)
            pnl = charges.settle(t["entry_premium"], exit_px, t["qty"],
                                 segment="futures", apply_tax=False)
            state.close_trade(t["id"], round(exit_px, 6), reason,
                              round(pnl.gross, 4), round(pnl.charges, 4),
                              0.0, round(pnl.net, 4), exit_spot=spot)
            net_total += pnl.net
        state.append_activity("EXIT", {
            "msg": f"PAIR {pid} closed {reason} at z={z:+.2f} net Rs {net_total:,.2f}"})
        # Both legs are booked now, so the balance is final for this pair —
        # refresh the screen rather than leaving it stale until cycle end.
        if emit is not None:
            try:
                emit()
            except Exception:
                pass
    return net_total
