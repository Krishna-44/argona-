"""Signal engine — decides, per underlying, whether to TAKE a scalp (and which
side) or AVOID, with a written reason for every check. This is the panel that
tells you where to trade and where not to.

Strategies are pluggable: each is a set of parameters + a `direction` rule.
`REGISTRY` ships two built-ins (momentum scalp, mean-reversion); add your own
by registering another `Strategy` (see `register`). The engine then applies the
same rule gates (quote live, liquidity, spread, IV sanity) plus, once trained,
the learned model's win-probability gate.
"""
from __future__ import annotations

import os

from dataclasses import dataclass, field
from typing import Callable, Literal

from .features import persistence, underlying_momentum
from .options import OptionQuote, Underlying

Decision = Literal["TAKE", "AVOID"]


# Momentum threshold for the scalp skill. The skill asks for a 3% 24h move;
# the desk's `fast` return is measured over a much shorter window, so the
# equivalent screen is proportionally smaller. Set from the skill's intent
# (strong directional move), not copied literally from a different timeframe.
SCALP_MOVE = float(os.environ.get("ARGONA_SCALP_MOVE", "0.0015"))

# Entry+exit slippage assumption, as a percentage of premium. Mirrors
# engine.SLIPPAGE (1% each way) so the cost gate sees what the desk pays.
SLIPPAGE_PCT = 2.0


@dataclass
class Check:
    name: str
    ok: bool
    reason: str


@dataclass
class Signal:
    underlying: str
    source: str
    label: str
    spot: float
    side: str | None            # "call" | "put" | None
    decision: Decision
    checks: list[Check] = field(default_factory=list)
    option: OptionQuote | None = None
    score: float | None = None  # model win-prob once trained
    strategy: str = ""
    # Share of the target move eaten by the round trip. The single number
    # that says whether trading faster can pay for itself.
    cost_bite_pct: float | None = None
    # Win rate needed just to break even at this target/stop/cost.
    required_win_rate: float | None = None
    # Agent-committee verdict attached by the engine.
    committee: object | None = None

    @property
    def take(self) -> bool:
        return self.decision == "TAKE"


@dataclass
class Strategy:
    name: str
    # returns "call" / "put" / None given momentum + range position
    direction: Callable[[float, float, float], str | None]
    trend_threshold: float = 0.0010   # |fast return| needed to act
    min_persistence: float = 0.62     # fraction of recent ticks that must agree
    max_spread_pct: float = 0.08      # reject options wider than this
    min_volume: float = 50.0
    min_oi: float = 10.0
    max_iv: float = 2.5               # reject IV above 250%
    tp_pct: float = 0.06              # take profit: +6% of premium (tight scalp)
    sl_pct: float = 0.04              # stop loss: -4% of premium (tight scalp)
    # Max round-trip cost (spread + slippage + fees) as a % of premium.
    # Measured live: ~4.5-5% on NSE index weeklies, 12-14% on crypto options.
    # No signal in this project has shown edge anywhere near those, so an
    # instrument above this is a fee, not a trade.
    max_cost_pct: float = 6.0
    # "option" trades an ATM option; "underlying" trades the index/stock/coin
    # directly — ~100x cheaper per round trip, linear P&L, no theta.
    instrument: str = "option"
    # Risk bands expressed as a move in the UNDERLYING (converted to a
    # premium band via option leverage). Per-strategy so a rapid mode can run
    # tighter without touching the rest of the desk.
    tp_spot_pct: float = 0.006        # 0.60% in our favour
    sl_spot_pct: float = 0.004        # 0.40% against us
    cooldown_sec: float = 180.0       # stand down after a stop-out
    # Max concurrent positions for this strategy. The scalp skill caps
    # at 3; the desk default is wider.
    max_concurrent: int = 6
    desc: str = ""


def _scalp_dir(fast: float, slow: float, rng: float) -> str | None:
    """Momentum scalp entry, from the user's `scalp` skill.

    The skill screens for strong 24h movers (>3% up for longs, <-3% for
    shorts). `fast` here is the recent return over the momentum window, which
    is the same idea measured on the data this desk actually has.
    """
    if fast >= SCALP_MOVE and slow >= 0:
        return "call"
    if fast <= -SCALP_MOVE and slow <= 0:
        return "put"
    return None


def _hold_dir(fast: float, slow: float, rng: float) -> str | None:
    """Long, always. This makes NO timing claim and is not a signal.

    Every timing signal tested in this project has negative measured
    expectancy, and each round trip costs ~0.05%. So the way to maximise the
    EXPECTED balance is to stop paying that cost repeatedly and hold an asset
    with positive expected drift instead. That drift is market beta -- the
    equity risk premium -- not edge. It is small (roughly +0.03%/day on an
    index) and it is NOT reliable over a few days, where variance dominates
    completely.
    """
    return "call"


def _momentum_dir(fast: float, slow: float, rng: float) -> str | None:
    if fast > 0 and slow >= 0:
        return "call"
    if fast < 0 and slow <= 0:
        return "put"
    return None


def _meanrev_dir(fast: float, slow: float, rng: float) -> str | None:
    # fade extremes: overbought (high in range, pushing up) -> buy puts
    if rng > 0.8 and fast > 0:
        return "put"
    if rng < 0.2 and fast < 0:
        return "call"
    return None


def _candle_dir(fast: float, slow: float, rng: float) -> str | None:
    """Direction comes from the candlestick engine, which is the only signal
    that held up out-of-sample (4/5 folds positive on NIFTY *and* BANKNIFTY).
    The engine passes the pattern score in via `rng` when this strategy is
    active — positive score = bullish, negative = bearish."""
    if rng > 0:
        return "call"
    if rng < 0:
        return "put"
    return None


REGISTRY: dict[str, Strategy] = {
    # Rapid mode: faster turnover, tighter targets. Only honest to run on a
    # signal with demonstrated edge — on a losing signal this just loses faster.
    "rapid_candle": Strategy(
        name="rapid_candle", direction=_candle_dir, trend_threshold=0.0,
        min_persistence=0.0,          # the pattern IS the filter
        tp_pct=0.05, sl_pct=0.035,    # tight, quick cycling
        max_spread_pct=0.06,
        desc="RAPID: enters on candlestick patterns (the only out-of-sample "
             "validated signal), tight TP/SL, fast re-entry.",
    ),
    "momentum_scalp": Strategy(
        name="momentum_scalp", direction=_momentum_dir, trend_threshold=0.0003,
        desc="Buy ATM call with up-momentum, put with down-momentum. Trend-following scalp.",
    ),
    "underlying_momo": Strategy(
        name="underlying_momo", direction=_momentum_dir, trend_threshold=0.0003,
        instrument="underlying",
        # No premium to pay a spread on and nothing to decay, so the round
        # trip costs ~0.05% of notional instead of 4.5-14%. The signal is the
        # same one the option version uses — this isolates the instrument
        # choice from the signal, which is the whole point of running it.
        max_cost_pct=1.0,
        desc="Same momentum signal, traded on the UNDERLYING instead of an "
             "ATM option: ~100x cheaper per round trip, linear P&L, no theta.",
    ),
    "scalp_momentum": Strategy(
        name="scalp_momentum", direction=_scalp_dir,
        instrument="underlying",
        # Straight from the user's `scalp` skill: TP 1.0%, SL 0.6%, max 3
        # concurrent positions, risk 1-2% per trade. Traded on the UNDERLYING
        # because the skill's 1.0%/0.6% are moves in the PRICE, and paying a
        # 4.5-14% option round trip to chase a 1% move is unwinnable.
        tp_spot_pct=0.010,
        sl_spot_pct=0.006,
        cooldown_sec=120.0,
        min_persistence=0.55,
        max_concurrent=3,
        trend_threshold=0.0,      # the direction fn does its own screen
        max_cost_pct=1.0,
        desc="SCALP (from the scalp skill): momentum screen, TP 1.0% / SL 0.6% "
             "on the underlying, max 3 positions. Needs ~42% win rate to break "
             "even on India futures. NOTE: the skill's orderbook confirmation "
             "and 10x leverage are not implemented - no L2 feed, and this desk "
             "is paper-only.",
    ),
    "index_hold": Strategy(
        name="index_hold", direction=_hold_dir,
        instrument="underlying",
        # No screen at all: there is nothing to screen FOR. Both gates are
        # opened deliberately rather than by oversight.
        trend_threshold=0.0,
        min_persistence=0.0,
        # Bands wide enough that ordinary movement does not close the
        # position. An index moving 4% inside a week is rare; the point is to
        # pay the round trip ONCE instead of fifty times.
        tp_spot_pct=0.060,
        sl_spot_pct=0.040,
        # 24h, not the hour I first wrote. A stop-out here means the index
        # fell 4%, i.e. the only premise this strategy has -- positive drift --
        # just failed on that instrument. Re-entering an hour later would buy
        # the same falling index repeatedly and pay the round trip each time,
        # which is exactly the churn this strategy exists to avoid. One entry
        # per instrument per day is the cap.
        cooldown_sec=86400.0,
        max_concurrent=4,
        max_cost_pct=1.0,
        desc="HOLD, not a strategy: long equity indices and sit. Captures market "
             "beta (~+0.03%/day), which is NOT edge and NOT reliable over days. "
             "Chosen only because every timing signal tested here has negative "
             "measured expectancy and churn costs ~0.05% per round trip.",
    ),
    "rapid_underlying": Strategy(
        name="rapid_underlying", direction=_momentum_dir,
        instrument="underlying",
        # Fires on a smaller move and exits sooner, so it trades far more
        # often. Bands are deliberately only ~4x the round-trip cost rather
        # than the ~10x the normal mode runs — that ratio is the whole
        # trade-off and it is reported, not hidden.
        trend_threshold=0.0001,
        min_persistence=0.50,
        tp_spot_pct=0.0015,          # 0.15% in our favour
        sl_spot_pct=0.0010,          # 0.10% against us
        cooldown_sec=20.0,
        max_cost_pct=0.5,            # a rapid mode cannot afford a wide book
        desc="RAPID on the UNDERLYING: 0.15%/0.10% bands, 20s cooldown. Many "
             "more trades — and cost is ~45% of each target instead of ~12%, "
             "so it needs a real edge to survive, not just a faster clock.",
    ),
    "mean_reversion": Strategy(
        name="mean_reversion", direction=_meanrev_dir, trend_threshold=0.0,
        desc="Fade extremes of the recent range. Buy puts when overbought, calls when oversold.",
    ),
}


def register(strat: Strategy) -> None:
    REGISTRY[strat.name] = strat


def evaluate(u: Underlying, opt_for: Callable[[str], OptionQuote | None], strat: Strategy,
             candle_score: float | None = None) -> Signal:
    """Build a rule-based signal. `opt_for(side)` fetches the ATM option for a
    side (so we only hit the chain for the side we actually want)."""
    fast, slow = underlying_momentum(u)
    h = u.history or [u.spot]
    hi, lo = max(h), min(h)
    rng = ((u.spot - lo) / (hi - lo)) if hi > lo else 0.5

    # Strategies driven by the candlestick engine receive its score in place
    # of the range position (the direction callback's third argument).
    if candle_score is not None:
        rng = candle_score

    sig = Signal(underlying=u.symbol, source=u.source, label=u.label,
                 spot=u.spot, side=None, decision="AVOID", strategy=strat.name)

    # 1) Market open?
    if not u.market_open:
        sig.checks.append(Check("MARKET", False, "market closed — no live quotes"))
        return sig
    sig.checks.append(Check("MARKET", True, "open"))

    # 2) Trend strong enough to pick a side?
    side = strat.direction(fast, slow, rng)
    strong = abs(fast) >= strat.trend_threshold
    if side is None or not strong:
        sig.checks.append(Check("TREND", False, f"no setup (fast {fast*100:+.2f}%, slow {slow*100:+.2f}%)"))
        return sig
    sig.side = side
    sig.checks.append(Check("TREND", True, f"{side.upper()} · fast {fast*100:+.2f}%"))

    # Persistence: the recent move must be mostly one-way, not a flickering
    # tick. This is what stops the desk from buying noise tops that revert.
    agree = persistence(h, side == "call")
    if agree < strat.min_persistence:
        sig.checks.append(Check("PERSIST", False, f"choppy · {agree*100:.0f}% aligned"))
        sig.side = None
        return sig
    sig.checks.append(Check("PERSIST", True, f"{agree*100:.0f}% aligned"))

    # Fetch the option for that side.
    opt = opt_for(side)
    sig.option = opt
    if opt is None or opt.mid <= 0 or (opt.bid <= 0 and opt.ask <= 0):
        sig.checks.append(Check("QUOTE", False, "no live option quote"))
        sig.side = None
        return sig
    sig.checks.append(Check("QUOTE", True, f"{opt.instrument} @ {opt.mid:.2f}"))

    # 3) Liquidity
    #
    # A model-priced quote carries no volume or open interest — not "zero
    # liquidity", simply no data, because there is no live chain without
    # SmartAPI credentials. Testing it against a volume floor rejected every
    # signal forever, so the desk would have stood down permanently rather
    # than only outside market hours.
    #
    # For an ATM NSE INDEX weekly the liquidity question is not genuinely
    # open: NIFTY and BANKNIFTY weeklies are among the most heavily traded
    # option contracts in the world. So the check is waived for exactly that
    # case — nearest-expiry ATM on an index — and the signal is labelled
    # UNVERIFIED rather than passed off as measured.
    if not getattr(opt, "quoted", True):
        is_index = opt.underlying.upper() in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
        near_atm = opt.spot > 0 and abs(opt.strike - opt.spot) / opt.spot <= 0.01
        if is_index and near_atm and opt.dte <= 35:
            sig.checks.append(Check(
                "LIQUIDITY", True,
                "unverified — model-priced; ATM index weekly assumed liquid"))
        else:
            sig.checks.append(Check(
                "LIQUIDITY", False,
                "unverified and not an ATM index weekly — needs a live chain"))
            return sig
    elif opt.volume < strat.min_volume and opt.open_interest < strat.min_oi:
        sig.checks.append(Check("LIQUIDITY", False, f"thin (vol {opt.volume:.0f}, OI {opt.open_interest:.0f})"))
        return sig
    else:
        sig.checks.append(Check("LIQUIDITY", True, f"vol {opt.volume:.0f} · OI {opt.open_interest:.0f}"))

    # 4) Spread
    #
    # A CROSSED quote (ask <= bid) is not a tight spread, it is stale or
    # broken data — yfinance returns crossed books for US names outside
    # market hours. spread_pct goes NEGATIVE there, which sailed straight
    # through a `spread_pct > max` test and would have let the desk trade on
    # garbage prices at whatever "mid" that implies.
    if opt.quoted and opt.bid > 0 and opt.ask > 0 and opt.ask <= opt.bid:
        sig.checks.append(Check(
            "SPREAD", False,
            f"crossed book (bid {opt.bid:.2f} >= ask {opt.ask:.2f}) — stale quote"))
        return sig
    if opt.spread_pct > strat.max_spread_pct:
        sig.checks.append(Check("SPREAD", False, f"{opt.spread_pct*100:.1f}% > {strat.max_spread_pct*100:.0f}% (eats the scalp)"))
        return sig
    sig.checks.append(Check("SPREAD", True, f"{opt.spread_pct*100:.1f}%"))

    # 4b) Cost vs plausible edge.
    #
    # The round trip on a short-dated option is spread + slippage + charges.
    # Measured live that is ~4.5-5% of premium on NSE index weeklies and
    # 12-14% on crypto. No signal tested in this project has shown an edge
    # anywhere near that — the best measured is ~0.05% per trade and it is not
    # statistically significant. An instrument whose round trip costs more
    # than any plausible edge is not a trade, it is a fee.
    try:
        from . import charges as _ch
        from . import options as _opts
        lot = float(getattr(opt, "lot_size", 1) or 1)
        notional = opt.mark * lot
        seg = "futures" if _opts.is_underlying(opt) else "options"
        if notional > 0:
            fee_pct = _ch.round_trip_charges(opt.mark, opt.mark, lot,
                                             segment=seg).total / notional * 100
            spread_pct = max(opt.spread_pct * 100, 0.0)
            # Underlying fills cross ~1bp, not 1% of premium.
            slip_pct = 0.02 if seg == "futures" else SLIPPAGE_PCT
            cost_pct = spread_pct + slip_pct + fee_pct
            if cost_pct > strat.max_cost_pct:
                sig.checks.append(Check(
                    "COST", False,
                    f"round trip {cost_pct:.2f}% of premium > {strat.max_cost_pct:.2f}% "
                    f"(spread {spread_pct:.2f} + slip {slip_pct:.2f} + fees {fee_pct:.2f})"))
                return sig
            # The number that actually decides whether a faster clock can
            # work: how much of the target move the round trip eats. At 12%
            # the signal needs to be right a bit more than half the time; at
            # 45% it needs a genuine edge, which nothing here has shown.
            tp_band = _opts.premium_band(opt, strat.tp_spot_pct) * 100
            sl_band = _opts.premium_band(opt, strat.sl_spot_pct) * 100
            bite = (cost_pct / tp_band * 100) if tp_band > 0 else 0.0
            sig.cost_bite_pct = round(bite, 1)

            # Win rate needed just to break even, given this target, this stop
            # and this cost:
            #     p*(TP - c) = (1-p)*(SL + c)  ->  p = (SL + c) / (TP + SL)
            # Tightening the bands raises this fast, because the cost stays
            # the same while the target shrinks. It is the honest price of a
            # faster clock, and measured win rates in this project sit in the
            # 44-55% band.
            denom = tp_band + sl_band
            need = ((sl_band + cost_pct) / denom * 100) if denom > 0 else 100.0
            sig.required_win_rate = round(need, 1)
            if need >= 100.0:
                # The round trip costs more than the target is worth, so the
                # trade loses even when it is right. No win rate rescues it.
                sig.checks.append(Check(
                    "COST", False,
                    f"needs {need:.0f}% win rate — impossible: round trip "
                    f"{cost_pct:.2f}% exceeds the {tp_band:.2f}% target"))
                return sig
            sig.checks.append(Check(
                "COST", True,
                f"round trip {cost_pct:.2f}% = {bite:.0f}% of the {tp_band:.2f}% "
                f"target · needs {need:.0f}% win rate to break even"))
    except Exception:
        pass

    # 5) IV sanity
    if opt.iv > strat.max_iv:
        sig.checks.append(Check("IV", False, f"{opt.iv*100:.0f}% too rich"))
        return sig
    sig.checks.append(Check("IV", True, f"{opt.iv*100:.0f}%"))

    # All rule gates pass — the model gate (if trained) is applied by the engine.
    sig.decision = "TAKE"
    return sig
