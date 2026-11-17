"""Side-by-side comparison of scalp variants: call/put, ATM/OTM, option/underlying.

The question this answers is not "which variant wins" but "what does each
variant have to achieve before it can win". Those are different questions and
only the second one has an honest answer in advance.

For every variant it reports three things that are known BEFORE any backtest:

  cost_pct   the round trip as a percentage of what you put at risk
  leverage   how far the premium moves for a 1% move in the underlying
  need_win   the win rate required to break even, from the variant's own
             geometry and its own cost

and then one thing that has to be measured:

  tp_rate    how often the signal actually reached its target, from an event
             study against a side-matched random baseline

A variant whose `need_win` exceeds anything observed anywhere is dead on
arithmetic and does not need a backtest. That is most of them.

Leverage is why. A short-dated ATM option moves 30-160x the underlying, so a
"1% take profit on the premium" is a 0.01% move in the index -- inside the
spread. Expressing a scalp's bands in premium terms converts a strategy into a
coin flip that charges rent. The bands here are always set in UNDERLYING terms
and converted through leverage, never the other way round.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import option_pricing as op


# Round-trip cost as a fraction of the capital actually at risk.
# Underlying: brokerage + exchange + GST + stamp on notional, measured on this
# desk at ~0.05%. Options: the bid-ask spread dominates and it is charged on
# the PREMIUM, which is a fraction of notional -- so the same rupee spread is a
# far larger percentage. Measured live on NSE index weeklies at 4.5-5%, and
# 12-14% on crypto options.
COST_UNDERLYING = 0.0005
COST_OPTION_ATM = 0.045
# An OTM option is cheaper in rupees and therefore WIDER in percentage terms:
# the same tick of spread is a bigger share of a smaller premium.
COST_OTM_STEP = 0.015


@dataclass
class Variant:
    name: str
    instrument: str          # "underlying" | "option"
    kind: str                # "call" | "put" | "both"
    offset_steps: int = 0    # 0 = ATM, +1/+2 = further out of the money
    premium: float = 0.0
    leverage: float = 1.0
    cost_pct: float = 0.0
    tp_spot_pct: float = 0.0
    sl_spot_pct: float = 0.0
    tp_premium_pct: float = 0.0
    sl_premium_pct: float = 0.0
    need_win: float = 0.0
    verdict: str = ""
    note: str = ""
    measured: dict[str, Any] = field(default_factory=dict)


def build_variants(spot: float, symbol: str = "NIFTY", dte_days: float = 7.0,
                   iv: float = 0.15, tp_spot_pct: float = 0.010,
                   sl_spot_pct: float = 0.006) -> list[Variant]:
    """Price every variant at the same spot, IV and horizon.

    The TP/SL are moves in the UNDERLYING and identical across variants. That
    is the only way the comparison means anything: each variant is asked to
    capture the same market move, and they differ solely in what they cost and
    how much leverage they apply to it.
    """
    out: list[Variant] = []

    for kind in ("call", "put", "both"):
        v = Variant(name=f"underlying_{kind}", instrument="underlying",
                    kind=kind, premium=spot, leverage=1.0,
                    cost_pct=COST_UNDERLYING,
                    tp_spot_pct=tp_spot_pct, sl_spot_pct=sl_spot_pct)
        # Delta 1: a 1% move in the underlying is a 1% move in the position.
        v.tp_premium_pct = tp_spot_pct
        v.sl_premium_pct = sl_spot_pct
        v.need_win = (v.sl_premium_pct + v.cost_pct) / (v.tp_premium_pct + v.sl_premium_pct)
        out.append(v)

    T = max(dte_days, 0.05) / 365.0
    for offset in (0, 1, 2):
        strike = op.snap_strike(spot, symbol, offset_steps=offset)
        for kind in ("call", "put"):
            # A put's out-of-the-money direction is the opposite of a call's.
            k = strike if offset == 0 else (
                op.snap_strike(spot, symbol, offset_steps=offset) if kind == "call"
                else op.snap_strike(spot, symbol, offset_steps=-offset))
            g = op.bs(spot, k, T, iv, kind)
            prem = max(g.price, 1e-9)
            # Leverage: how far the premium moves for a 1% move in spot.
            lev = abs(g.delta) * spot / prem if prem > 0 else 0.0
            cost = COST_OPTION_ATM + COST_OTM_STEP * offset
            v = Variant(
                name=f"{'ATM' if offset == 0 else f'OTM{offset}'}_{kind}",
                instrument="option", kind=kind, offset_steps=offset,
                premium=prem, leverage=lev, cost_pct=cost,
                tp_spot_pct=tp_spot_pct, sl_spot_pct=sl_spot_pct)
            # The underlying move, converted into premium terms. This is the
            # step that makes option scalping look viable when done backwards.
            v.tp_premium_pct = tp_spot_pct * lev
            v.sl_premium_pct = sl_spot_pct * lev
            denom = v.tp_premium_pct + v.sl_premium_pct
            v.need_win = ((v.sl_premium_pct + v.cost_pct) / denom) if denom > 0 else 9.99
            out.append(v)

    for v in out:
        if v.need_win >= 1.0:
            v.verdict = "IMPOSSIBLE"
            v.note = "cost alone exceeds the whole take-profit; no win rate saves it"
        elif v.need_win > 0.60:
            v.verdict = "needs >60%"
            v.note = "no signal measured on this desk has come close"
        elif v.need_win > 0.45:
            v.verdict = "hard"
            v.note = "above every TP rate measured here (best was 35.5%)"
        else:
            v.verdict = "arithmetically open"
            v.note = "still has to beat a side-matched random baseline"
    return out


def measure(variants: list[Variant], df, horizon: int = 12,
            n_random: int = 400) -> list[Variant]:
    """Attach the measured TP rate to variants that are not already impossible.

    Only the UNDERLYING move is simulated, because that is what the signal is
    predicting. Every variant is capturing the same move; they differ in cost
    and leverage, both already accounted for in `need_win`. Running a separate
    price path per variant would imply the option has its own signal, which it
    does not.
    """
    from . import signal_eval as se
    cache: dict[tuple, Any] = {}
    for v in variants:
        if v.verdict == "IMPOSSIBLE":
            v.measured = {"skipped": "arithmetically impossible"}
            continue
        # A put variant is the inverted signal, not the same signal relabelled.
        # Reusing the long-side result for both would have made call and put
        # show identical measured rates by construction — a property of the
        # code, not of the market.
        invert = (v.kind == "put")
        key = (round(v.tp_spot_pct, 6), round(v.sl_spot_pct, 6), horizon, invert)
        if key not in cache:
            cache[key] = se.event_study(
                df, "momentum", horizon=horizon,
                tp_pct=v.tp_spot_pct, sl_pct=v.sl_spot_pct,
                n_random=n_random, invert=invert)
        r = cache[key]
        side_rate = r.tp_rate
        v.measured = {
            "tp_rate": round(side_rate, 2),
            "mean_net_pct": round(r.mean_net_pct, 4),
            "p_value": round(r.p_value, 4),
            "n_signals": r.n_signals,
            "margin": round(side_rate / 100.0 - v.need_win, 4),
            "passes": bool(side_rate / 100.0 > v.need_win and r.p_value < 0.05),
        }
    return variants


def table(variants: list[Variant]) -> str:
    """One row per variant, sorted by how close it comes to viability."""
    rows = sorted(variants, key=lambda v: v.need_win)
    w = (f"{'variant':16s} {'lever':>6s} {'cost%':>7s} {'TP prem%':>9s} "
         f"{'need win':>9s} {'got':>7s} {'verdict':>20s}")
    lines = [w, "-" * len(w)]
    for v in rows:
        got = v.measured.get("tp_rate")
        lines.append(
            f"{v.name:16s} {v.leverage:>6.1f} {v.cost_pct*100:>7.2f} "
            f"{v.tp_premium_pct*100:>9.1f} {v.need_win*100:>8.1f}% "
            f"{(f'{got:.1f}%' if got is not None else '—'):>7s} {v.verdict:>20s}")
    return "\n".join(lines)
