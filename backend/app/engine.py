"""The scalp loop. Each cycle:

1. Scan the universe. For each underlying, build a rule-based signal (TAKE a
   call/put, or AVOID with a reason). Once the model is trained, gate TAKEs on
   its predicted win-probability — that's the "where to trade / where not."
2. For each TAKE with no open position and room in the account, open a paper
   ATM-option trade: buy at the ask (+slippage), set a tight take-profit and
   stop-loss off the premium.
3. Mark every open trade to the live option price. Exit on TP, SL, or near
   expiry. Settle through the India charges+tax model → net P&L → account.
4. Train: the model learns per closed trade (did it hit TP?), the SAE learns
   unsupervised on every scanned setup.

Paper only. Fills, marks and exits are simulated against real quotes; nothing
is ever sent to a broker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Callable

from . import charges, features, options, state, strategy
from .model import get_model
from .sae import get_sae

log = logging.getLogger("argona.scalp")

CYCLE_SEC = float(os.environ.get("ARGONA_CYCLE_SEC", "12"))
# KPI refresh is DB-only, so it can run far faster than the trading cycle.
KPI_TICK_SEC = float(os.environ.get("ARGONA_KPI_TICK_SEC", "1"))
RISK_PER_TRADE = float(os.environ.get("ARGONA_RISK_PCT", "0.01"))   # 1% of balance at risk
MAX_OPEN = int(os.environ.get("ARGONA_MAX_OPEN", "6"))
SLIPPAGE = float(os.environ.get("ARGONA_SLIPPAGE", "0.01"))         # 1% worse fill than mid
# Slippage on the UNDERLYING is a different order entirely. 1% of a premium is
# a realistic option fill; 1% of an index PRICE is ~250 points, which would
# wipe out the whole reason for trading the underlying. A liquid index future
# crosses for about a basis point.
SLIPPAGE_UNDERLYING = float(os.environ.get("ARGONA_SLIPPAGE_UND", "0.0001"))
MODEL_WINPROB_GATE = float(os.environ.get("ARGONA_WINPROB_GATE", "0.50"))
MIN_DTE_EXIT = 0.25  # close trades inside ~6h of expiry
# Forward test of the NIFTY/BANKNIFTY pair. Backtest was promising but
# NOT validated (fails Bonferroni and split-half), so this runs to
# generate genuine out-of-sample evidence, not because it is proven.
PAIR_ENABLED = os.environ.get("ARGONA_PAIR", "1") == "1"

# Risk is defined as a move in the UNDERLYING, then converted to a premium
# band via the option's leverage. Setting the band as a flat percentage of
# premium is what broke the desk: a short-dated ATM option runs 30x-160x, so
# the old "4% of premium" stop was a stop on a 0.025%-0.14% move in the index
# — inside the bid-ask spread, so every exit was microstructure noise paid for
# with full round-trip costs. A live ETH trade stopped out 13 seconds after
# entry on a 0.111% move, which is what prompted this.
SL_SPOT_PCT = float(os.environ.get("ARGONA_SL_SPOT", "0.004"))   # 0.40% against us
TP_SPOT_PCT = float(os.environ.get("ARGONA_TP_SPOT", "0.006"))   # 0.60% in our favour
# Beyond this the premium band is wider than the position is worth; the option
# itself becomes the stop and a separate stop adds nothing.
MAX_LEVERAGE = float(os.environ.get("ARGONA_MAX_LEVERAGE", "200"))

# Capital limits, split by what the capital actually is.
#   premium  — money handed over and at risk, so keep it small
#   margin   — collateral on a stop-managed future, so it can be larger
#   deployed — ceiling across the WHOLE open book, which stops three index
#              futures positions from owing more margin than the account has
MAX_PREMIUM_PCT = float(os.environ.get("ARGONA_MAX_PREMIUM_PCT", "0.25"))
MAX_MARGIN_PCT = float(os.environ.get("ARGONA_MAX_MARGIN_PCT", "0.45"))
MAX_DEPLOYED_PCT = float(os.environ.get("ARGONA_MAX_DEPLOYED_PCT", "0.80"))
COOLDOWN_SEC = float(os.environ.get("ARGONA_COOLDOWN_SEC", "180"))  # stand down after a stop-out

# Bars per year for the 1h history the committee reads. Crypto trades every
# hour of every day; NSE runs 6.25h x ~250 sessions; US 6.5h x ~252. Using one
# factor for all three would misstate annualised vol by up to 6x, which is the
# whole point of the number when it is compared against an annualised IV.
_BARS_PER_YEAR = {"crypto": 24 * 365, "india": 6.25 * 250, "us": 6.5 * 252}

# Instruments that are substitutes rather than separate bets. Measured on 124
# sessions of daily returns:
#     DAX <-> CAC   0.83      SPY <-> QQQ   0.93
#     DAX <-> FTSE  0.36      CAC <-> FTSE  0.34
#     NIFTY <-> everything  -0.15 to +0.02
# Holding DAX and CAC together gave three positions worth about 1.49
# independent bets, so the desk was paying three round trips for one and a half
# bets. SPY and QQQ at 0.93 would be worse. Only genuinely redundant pairs are
# grouped: FTSE stays separate at ~0.35, and NIFTY is the real diversifier.
SUBSTITUTE_GROUP = {
    "DAX": "eurozone", "CAC": "eurozone",
    "SPY": "us_broad", "QQQ": "us_broad",
    "BTC": "majorcoin", "ETH": "majorcoin",
}


def _realised_vol(df, source: str) -> float | None:
    """Annualised realised volatility from the 1h closes already in hand.

    `market_structure.analyze()` reports atr/atr_pct/label/percentile and has
    never produced a `realised_vol` key, so the engine's old read of it handed
    the volatility agent None on every cycle.
    """
    if df is None or len(df) < 20:
        return None
    try:
        import math
        closes = [float(c) for c in df["Close"].tolist()[-200:]]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])
                if a > 0 and b > 0]
        if len(rets) < 10:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(_BARS_PER_YEAR.get(source, 252 * 6.5))
    except Exception:
        return None


class Engine:
    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self.on_event = on_event
        self._task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._epoch = 0
        self.model = get_model()
        self.sae = get_sae()
        self.signals: list[dict] = []   # last cycle's signals (for snapshot)
        # Heartbeat: set at the END of every completed cycle. /health reads it
        # so a hung cycle thread is visible to the supervisor, which otherwise
        # only ever saw that FastAPI could still answer.
        self.last_cycle_ts: float = 0.0
        self._cooldown_until: dict[str, float] = {}  # per-underlying stand-down after a stop
        # Remembers which substitute-group skip has already been logged for a
        # symbol, so a standing refusal is reported once instead of every cycle.
        self._sub_skip_logged: dict[str, str] = {}
        self._ohlc_cache: dict[str, tuple[float, object]] = {}  # symbol -> (ts, df)
        self.pair_state: dict = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="argona.scalp")
        self._tick_task = asyncio.create_task(self._ticker(), name="argona.kpi")

    async def stop(self) -> None:
        self._stop.set()
        if self._tick_task:
            try:
                await asyncio.wait_for(self._tick_task, timeout=3)
            except Exception:
                self._tick_task.cancel()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=4)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _ticker(self) -> None:
        """Push KPIs every second, independently of the trading cycle.

        The trading cycle runs every ~12s because it hits yfinance and
        Deribit; dropping it to 1s would just get rate-limited. But the KPI
        payload — balance, net P&L, win rate, charges, tax, gross, trade
        counts, mark-to-market equity — is computed entirely from the local
        database and costs nothing, so it can refresh every second and the
        screen stops feeling stale between cycles.
        """
        while not self._stop.is_set():
            try:
                self.on_event({"kind": "KPI", "body": _kpi_dict(state.compute_kpis())})
                self.on_event({"kind": "TRADES", "body": state.open_trades()})
                self.on_event({"kind": "ACCOUNT", "body": state.account_report()})
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=KPI_TICK_SEC)
            except asyncio.TimeoutError:
                pass

    async def _run(self) -> None:
        state.append_activity("INFO", {"msg": "Argona F&O online — ATM scalp"})
        self._emit()
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.cycle)
            except Exception as e:
                log.exception("cycle failed")
                state.append_activity("INFO", {"msg": f"cycle error: {e!r}"})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CYCLE_SEC)
            except asyncio.TimeoutError:
                pass

    # -- one cycle (runs in a thread; yfinance/httpx are blocking) -----------

    def cycle(self) -> None:
        self._epoch += 1
        kpis = state.compute_kpis()
        strat = strategy.REGISTRY.get(state.get_meta("strategy", "momentum_scalp"),
                                      strategy.REGISTRY["momentum_scalp"])

        # 1) Manage existing open trades first (mark + exit).
        net_cycle = self._manage_open_trades()

        # 1c) Pair book. A pair is one decision producing two legs that must
        # open and close together, so it is managed outside the per-instrument
        # loop — otherwise one leg could stop out alone and leave a naked
        # directional position, the exact thing a pair exists to avoid.
        try:
            from . import pair_engine
            if options.nse_market_open():
                net_cycle += pair_engine.manage_pairs(state, options, sys.modules[__name__], charges, self._emit)
                has_pair = any(t.get("pair_id") for t in state.open_trades())
                if not has_pair and PAIR_ENABLED:
                    un = options.get_underlying("NIFTY", "india", "NIFTY")
                    ub = options.get_underlying("BANKNIFTY", "india", "BANKNIFTY")
                    if un and ub:
                        zi = pair_engine.live_z(un.spot, ub.spot)
                        self.pair_state = zi
                        if zi.get("ok") and zi.get("action") != "none":
                            pair_engine.open_pair(state, options,
                                                  sys.modules[__name__], zi, kpis.balance)
                else:
                    un = options.get_underlying("NIFTY", "india", "NIFTY")
                    ub = options.get_underlying("BANKNIFTY", "india", "BANKNIFTY")
                    if un and ub:
                        self.pair_state = pair_engine.live_z(un.spot, ub.spot)
        except Exception as e:
            log.debug("pair book failed: %r", e)

        # 2) Scan the universe for signals.
        #
        # The session check is PER INSTRUMENT, not global: crypto runs 24/7,
        # the US session is 20:00-02:30 IST, and NSE is 09:15-15:30 IST. A
        # single global gate would either freeze crypto or let a closed venue
        # trade against a stale price. Trading a shut venue is the dangerous
        # direction — with no fresh spot the mark cannot move, so a position
        # would open and exit against a frozen close and invent a track
        # record out of nothing.
        signals: list[dict] = []
        open_underlyings = {t["underlying"] for t in state.open_trades()}
        open_groups = {SUBSTITUTE_GROUP.get(s) for s in open_underlyings}
        open_groups.discard(None)
        # Which venues the desk is allowed to trade. Empty/absent means all of
        # them, so existing behaviour is unchanged when the key was never set.
        allowed = set(state.get_meta("sources", []) or [])
        # A strategy whose premise is market BETA must not be handed single
        # names. `sources: us` includes NVDA and TSLA beside SPY and QQQ, and
        # index_hold duly bought NVDA at the US open -- a single stock carries
        # a large idiosyncratic component (one earnings call, one cancelled
        # order) that an index averages away. That is not the risk this
        # strategy claims to take.
        index_only = bool(getattr(strat, "index_only", False))
        for item in options.get_universe():
            if index_only and not item.get("index"):
                continue
            if allowed and item.get("source") not in allowed:
                continue
            if not options.market_open_for(item["source"], item["symbol"]):
                signals.append(self._avoid_stub(item, f"{item['source']} market closed"))
                continue
            u = options.get_underlying(item["symbol"], item["source"], item["label"])
            if u is None:
                signals.append(self._avoid_stub(item, "quote unavailable"))
                continue

            opt_cache: dict[str, options.OptionQuote | None] = {}

            def opt_for(side: str, _u=u, _cache=opt_cache):
                if side not in _cache:
                    _cache[side] = options.get_atm(_u, side, strat.instrument)
                return _cache[side]

            cscore = self._candle_score(u.symbol) if strat.name == "rapid_candle" else None
            sig = strategy.evaluate(u, opt_for, strat, candle_score=cscore)

            # Agent committee. It runs on every signal so its scorecards fill
            # up whether or not the desk acts, but it only ever REMOVES trades
            # — a gatekeeper veto downgrades a TAKE to AVOID. It cannot create
            # one, because none of its predictors has demonstrated edge yet.
            try:
                sig.committee = self._committee(u, sig, strat, kpis)
                if sig.take and sig.committee.decision == "VETO":
                    sig.decision = "AVOID"
                    sig.checks.append(strategy.Check(
                        "COMMITTEE", False, sig.committee.note))
            except Exception as e:
                log.debug("committee failed: %r", e)

            # Post-stop cooldown: after a stop-out on this underlying, stand
            # down so we don't immediately re-buy the same reverting setup.
            cd = self._cooldown_until.get(u.symbol, 0.0)
            if sig.take and time.time() < cd:
                left = int(cd - time.time())
                sig.decision = "AVOID"
                sig.checks.append(strategy.Check("COOLDOWN", False, f"stood down {left}s after stop"))

            # Model gate + feature snapshot for TAKE candidates.
            feats = None
            if sig.option is not None:
                feats = features.extract(u, sig.option).as_list()
                self.sae.train(feats)
                combined = feats + self.sae.encode(feats)
                # The model was trained on OPTION features — premium, IV,
                # theta, dte, moneyness. An underlying quote has iv=0,
                # dte=365 and strike=spot, which is outside that distribution
                # entirely, so its output there is not a weak signal but a
                # meaningless one (it scored a clean 100%-aligned trend at
                # 25%). Keep training on these samples so it can learn the
                # new regime, but do not GATE on a prediction made from
                # inputs the model has never seen.
                out_of_distribution = options.is_underlying(sig.option)
                if self.model.is_warm() and not out_of_distribution:
                    win_prob = self.model.predict_fair(combined, 0.5)  # P(win)
                    sig.score = win_prob
                    if sig.take and win_prob < MODEL_WINPROB_GATE:
                        sig.decision = "AVOID"
                        sig.checks.append(strategy.Check("MODEL", False,
                            f"win-prob {win_prob*100:.0f}% < {MODEL_WINPROB_GATE*100:.0f}%"))
                    elif sig.take:
                        sig.checks.append(strategy.Check("MODEL", True, f"win-prob {win_prob*100:.0f}%"))
                feats = combined

            sigd = self._sig_dict(sig)
            signals.append(sigd)

            # 3) Act on a TAKE.
            # The substitute check runs only on a signal that would OTHERWISE
            # be acted on. Checking it first logged "skip DAX: already hold a
            # eurozone position" against DAX itself — the position that WAS the
            # eurozone holding — and did it on every cycle.
            # Two separate caps. MAX_OPEN is the book-wide limit and counts
            # everything; a strategy's own max_concurrent counts only ITS
            # positions. Counting all open trades against the per-strategy cap
            # meant a 3-position scalp could never fire while four index_hold
            # positions sat there — a strategy was being blocked by trades it
            # does not own and cannot close.
            own_open = sum(1 for t in state.open_trades()
                           if t.get("strategy") == strat.name)
            actionable = (sig.take and sig.option is not None and feats is not None
                          and sig.underlying not in open_underlyings
                          and len(state.open_trades()) < MAX_OPEN
                          and own_open < getattr(strat, "max_concurrent", MAX_OPEN))
            grp = SUBSTITUTE_GROUP.get(sig.underlying)
            if actionable and grp is not None and grp in open_groups:
                if self._sub_skip_logged.get(sig.underlying) != grp:
                    self._sub_skip_logged[sig.underlying] = grp
                    state.append_activity("INFO", {
                        "msg": (f"skip {sig.underlying}: already hold a '{grp}' "
                                f"position — they move together, so a second one "
                                f"is the same bet twice, not diversification")})
            elif actionable:
                self._open_trade(u, sig, feats, strat, kpis.balance)
                open_underlyings.add(sig.underlying)
                if grp is not None:
                    open_groups.add(grp)

        self.signals = signals
        self.last_cycle_ts = time.time()
        state.record_equity(round(state.compute_kpis().balance, 4), round(net_cycle, 4))
        self._emit()

    def _candle_score(self, symbol: str) -> float | None:
        """Latest candlestick score for an underlying (cached ~3 min).
        Used by the rapid_candle strategy — the one signal that validated
        out-of-sample."""
        now = time.time()
        hit = self._ohlc_cache.get(symbol)
        if not hit or now - hit[0] > 180:
            try:
                from . import backtest as bt
                df = bt.fetch_history(symbol, "1mo", "1h", min_bars=30)
            except Exception:
                df = None
            self._ohlc_cache[symbol] = (now, df)
            hit = self._ohlc_cache[symbol]
        df = hit[1]
        if df is None:
            return None
        try:
            from . import candlestick as cs
            return float(cs.score(df).iloc[-1])
        except Exception:
            return None

    # -- trade lifecycle -----------------------------------------------------

    def _open_trade(self, u, sig, feats, strat, balance) -> None:
        opt = sig.option
        entry = opt.ask if opt.ask > 0 else opt.mid
        slip = SLIPPAGE_UNDERLYING if options.is_underlying(opt) else SLIPPAGE
        entry = entry * (1 + slip)
        if entry <= 0:
            return
        # Convert the intended move in the UNDERLYING into a premium band.
        lev = options.leverage(opt)
        if lev <= 0 or lev > MAX_LEVERAGE:
            state.append_activity("INFO", {
                "msg": f"skip {u.symbol}: leverage {lev:.0f}x outside usable range"})
            return
        tp_band = options.premium_band(opt, getattr(strat, 'tp_spot_pct', TP_SPOT_PCT))
        sl_band = options.premium_band(opt, getattr(strat, 'sl_spot_pct', SL_SPOT_PCT))
        tp = entry * (1 + tp_band)
        sl = entry * (1 - sl_band)

        # Position sizing — risk ~RISK_PER_TRADE of the account per trade, in
        # whole NSE lots. qty is counted in UNITS of the underlying, and a lot
        # is indivisible (NIFTY 65, BANKNIFTY 35, …), so the smallest position
        # is one full lot. If even one lot would risk more than the hard cap,
        # SKIP rather than oversize — a single index lot is a large position
        # against a retail account.
        import math
        min_lot = float(getattr(opt, "lot_size", 1) or 1)
        risk_amt = RISK_PER_TRADE * balance
        per_unit_risk = max(entry - sl, 1e-6)          # ≈ premium * sl_pct
        raw_qty = risk_amt / per_unit_risk
        qty = math.floor(raw_qty / min_lot) * min_lot
        if qty < min_lot:
            one_lot_risk = per_unit_risk * min_lot
            if one_lot_risk > 0.025 * balance:
                return  # can't size this within risk budget — skip the trade
            qty = min_lot
        # Capital caps. What a position consumes is notional x margin_rate:
        # the full premium for an option buyer, ~12% for a margined index
        # future. The two cases deserve different limits — an option premium
        # is money at risk, while futures margin is collateral and the real
        # risk is the stop distance (one NIFTY lot stops out for ~Rs 6,500,
        # about 1.3% of a Rs 5L book, while tying up ~Rs 2L).
        margin_rate = float(getattr(opt, "margin_rate", 1.0) or 1.0)
        is_margined = margin_rate < 1.0
        per_trade_cap = (MAX_MARGIN_PCT if is_margined else MAX_PREMIUM_PCT) * balance

        # Aggregate cap across the whole open book. Without this the desk
        # could open NIFTY + BANKNIFTY + FINNIFTY futures and owe ~Rs 6.2L of
        # margin on a Rs 5L account — each one passing its own per-trade cap
        # while the book as a whole is over-leveraged.
        deployed = 0.0
        for t in state.open_trades():
            mr = 0.12 if (t.get("segment") == "futures") else 1.0
            deployed += float(t["entry_premium"]) * float(t["qty"]) * mr
        room = max(0.0, MAX_DEPLOYED_PCT * balance - deployed)
        if room <= 0:
            state.append_activity("INFO", {
                "msg": (f"skip {u.symbol}: book already uses Rs {deployed:,.0f} of the "
                        f"Rs {MAX_DEPLOYED_PCT * balance:,.0f} capital limit")})
            return

        capital_per_unit = max(entry * margin_rate, 1e-9)
        allowed = min(per_trade_cap, room)
        max_qty = math.floor((allowed / capital_per_unit) / min_lot) * min_lot
        if max_qty < min_lot:
            # One indivisible lot is already more capital than the cap allows.
            # This is a real constraint, not a glitch: a NIFTY futures lot is
            # 65 x spot, so it ties up ~Rs 1.95L of margin — 39% of a Rs 5L
            # book. Say so instead of silently skipping, because a desk that
            # never trades and never explains why looks broken.
            need = capital_per_unit * min_lot
            state.append_activity("INFO", {
                "msg": (f"skip {u.symbol}: one lot ({min_lot:g} units) needs "
                        f"Rs {need:,.0f} capital, available Rs {allowed:,.0f}")})
            return
        qty = min(qty, max_qty)

        # An NSE lot is indivisible, so on a small book the smallest possible
        # position can risk well over the per-trade target: one BANKNIFTY
        # futures lot is 30 x ~61,700 = ~Rs 18.5L notional, and a 0.40% stop
        # on that is ~1.5% of a Rs 5L account before charges. The hard 2.5%
        # cap still allows it, which is defensible — but it must not happen
        # silently, so say so at entry.
        actual_risk = per_unit_risk * qty
        risk_pct = actual_risk / balance * 100 if balance else 0.0
        if risk_pct > RISK_PER_TRADE * 100 * 1.25:
            state.append_activity("INFO", {
                "msg": (f"{u.symbol}: one lot ({min_lot:g} units) risks "
                        f"Rs {actual_risk:,.0f} = {risk_pct:.2f}% of the book, "
                        f"over the {RISK_PER_TRADE*100:.2f}% target — lot size "
                        f"is indivisible at this account size")})

        tid = state.open_trade(
            underlying=u.symbol, source=u.source, kind=opt.kind, strike=opt.strike,
            expiry=opt.expiry, instrument=opt.instrument, strategy=strat.name, qty=qty,
            entry_premium=round(entry, 6), entry_spot=u.spot,
            tp_premium=round(tp, 6), sl_premium=round(sl, 6), features=feats,
            segment="futures" if options.is_underlying(opt) else "options",
        )
        # Remember what each agent said, so the outcome can score them.
        try:
            from . import agents as _ag
            cm = getattr(sig, "committee", None)
            if cm is not None:
                votes = {v["agent"]: v["vote"] for v in cm.views
                         if v.get("kind") == "predictor"}
                pend = state.get_meta("agent_votes", {}) or {}
                pend[str(tid)] = votes
                state.set_meta("agent_votes", pend)
        except Exception:
            pass

        state.append_activity("ENTRY", {
            "id": tid, "underlying": u.symbol, "kind": opt.kind, "strike": opt.strike,
            "instrument": opt.instrument, "entry": round(entry, 4), "qty": qty,
            "tp": round(tp, 4), "sl": round(sl, 4), "strategy": strat.name,
            "score": round(sig.score, 3) if sig.score is not None else None,
        })

    def _manage_open_trades(self) -> float:
        net_cycle = 0.0
        # Trades the operator asked to close out of band (e.g. to free margin
        # locked in a large position). Read once per cycle, and settled through
        # the SAME charges/tax path as TP/SL below -- a separate manual-close
        # routine would drift from the real cost model and quietly flatter the
        # book.
        forced = set(state.get_meta("force_close", []) or [])
        for t in state.open_trades():
            # Pin the mark's IV to the entry premium so the position can only
            # move on spot and time — not on IV re-estimation noise.
            mark = options.requote_mark(
                t["source"], t["underlying"], t["kind"], t["strike"],
                t["expiry"], t["instrument"], self._spot_for(t),
                entry_premium=t.get("entry_premium"),
                entry_spot=t.get("entry_spot"))
            if mark is None or mark <= 0:
                continue

            # A SHORT underlying profits when the price FALLS, but every rule
            # below (TP above entry, SL below, gross = exit - entry) assumes
            # the mark rises when the position is right — which is true for an
            # option premium and false for a short. Mirror the price around
            # the entry so a short's synthetic mark rises as it profits; the
            # rest of the lifecycle then needs no special casing, and the
            # mirror is exact because underlying P&L is linear.
            is_short_underlying = (
                "UNDERLYING" in (t["instrument"] or "") and t["kind"] == "put")
            if is_short_underlying:
                mark = max(1e-9, 2.0 * t["entry_premium"] - mark)

            state.mark_trade(t["id"], round(mark, 6))

            reason = None
            if t["id"] in forced:
                reason = "MANUAL"
            elif mark >= t["tp_premium"]:
                reason = "TP"
            elif mark <= t["sl_premium"]:
                reason = "SL"
            else:
                # near-expiry exit
                try:
                    from datetime import datetime, timezone
                    exp = datetime.fromisoformat(t["expiry"]).replace(tzinfo=timezone.utc)
                    dte = (exp - datetime.now(timezone.utc)).total_seconds() / 86400.0
                    if dte <= MIN_DTE_EXIT:
                        reason = "EXPIRY"
                except Exception:
                    pass
            if reason is None:
                continue

            # Fill at the band price, not the raw (possibly gapped) mark — a
            # real stop/limit fills at ~its trigger. We add a touch of adverse
            # slippage so it isn't free, but this is what caps a stop-loss to
            # the intended risk instead of the full gap-down.
            xslip = (SLIPPAGE_UNDERLYING if "UNDERLYING" in (t["instrument"] or "")
                     else SLIPPAGE)
            if reason == "TP":
                exit_px = t["tp_premium"] * (1 - xslip)
            elif reason == "SL":
                exit_px = t["sl_premium"] * (1 - xslip)
                exit_px = min(exit_px, mark) if mark < t["sl_premium"] else exit_px
                exit_px = max(exit_px, t["sl_premium"] * (1 - 3 * xslip))  # floor the slip
            elif reason == "MANUAL":
                # No band was touched, so there is no trigger price to fill at.
                # Settle at the live mark with the ordinary adverse slip: an
                # operator closing early crosses the spread like anyone else.
                exit_px = max(0.0, mark * (1 - xslip))
            else:  # EXPIRY — settle at the live mark (intrinsic)
                exit_px = max(0.0, mark)
            exit_px = max(0.0, exit_px)
            # Pre-tax settle, then tax the CUMULATIVE net (F&O losses offset
            # gains — taxing each winner in isolation overstates the drag).
            # Futures and options have different rate cards; bill the right one.
            pnl = charges.settle(t["entry_premium"], exit_px, t["qty"],
                                 segment=t.get("segment") or "options",
                                 apply_tax=False)
            cum_pre = float(state.get_meta("cum_pretax", 0.0)) + pnl.net
            paid = float(state.get_meta("tax_paid_total", 0.0))
            owed = charges.tax_on_portfolio(cum_pre)
            inc_tax = max(0.0, owed - paid)
            net_after_tax = pnl.net - inc_tax
            state.set_meta("cum_pretax", round(cum_pre, 6))
            state.set_meta("tax_paid_total", round(paid + inc_tax, 6))

            try:
                from . import agents as _ag
                pend = state.get_meta("agent_votes", {}) or {}
                votes = pend.pop(str(t["id"]), None)
                if votes:
                    _ag.record_outcome(state, votes, won=(net_after_tax > 0))
                    state.set_meta("agent_votes", pend)
            except Exception:
                pass

            state.close_trade(t["id"], round(exit_px, 6), reason,
                              round(pnl.gross, 4), round(pnl.charges, 4),
                              round(inc_tax, 4), round(net_after_tax, 4),
                              exit_spot=self._spot_for(t))
            if reason == "MANUAL":
                # Drop it from the request list so a stale id cannot close a
                # future trade that happens to reuse the number.
                forced.discard(t["id"])
                state.set_meta("force_close", sorted(forced))
            net_cycle += net_after_tax

            # Push the refreshed balance IMMEDIATELY on a close rather than
            # waiting for the end of the cycle. A close is the one moment the
            # settled balance actually moves, so that is exactly when the
            # screen should change — a 12s lag there reads as a frozen desk.
            try:
                self._emit()
            except Exception:
                pass

            # Stand down on this underlying after a stop-out.
            if reason == "SL":
                _cd = getattr(strategy.REGISTRY.get(t.get("strategy", "")), "cooldown_sec", COOLDOWN_SEC)
                self._cooldown_until[t["underlying"]] = time.time() + _cd

            # Train the model: label = 1 if the scalp netted a profit.
            try:
                raw = json.loads(t["features"]) if t["features"] else None
                from .model import N_INPUT
                if isinstance(raw, list) and len(raw) == N_INPUT:
                    self.model.learn(raw, target_mid=(1.0 if net_after_tax > 0 else 0.0), entry_mid=0.5)
            except Exception:
                pass

            state.append_activity("EXIT", {
                "id": t["id"], "underlying": t["underlying"], "kind": t["kind"],
                "instrument": t["instrument"], "reason": reason,
                "entry": round(t["entry_premium"], 4), "exit": round(exit_px, 4),
                "gross": round(pnl.gross, 4), "charges": round(pnl.charges, 4),
                "tax": round(inc_tax, 4), "net": round(net_after_tax, 4),
                "won": net_after_tax > 0,
            })
        return net_cycle

    def _committee(self, u, sig, strat, kpis):
        """Assemble what the agents need and let them deliberate."""
        from . import agents, market_structure as ms, backtest as bt

        struct, ut, pats = {}, None, []
        df = None
        hit = self._ohlc_cache.get(u.symbol)
        if hit and time.time() - hit[0] <= 180:
            df = hit[1]
        else:
            try:
                df = bt.fetch_history(u.symbol, "1mo", "1h", min_bars=30)
                if df is not None:
                    self._ohlc_cache[u.symbol] = (time.time(), df)
            except Exception:
                df = None
        if df is not None and len(df) > 30:
            try:
                struct = ms.analyze(df, "1h")
            except Exception:
                struct = {}
            try:
                ut = float(bt.ut_score(df)[-1])
            except Exception:
                ut = None
            try:
                from . import candlestick as cs
                row = cs.detect(df).iloc[-1]
                pats = [c for c in row.index if bool(row[c])]
            except Exception:
                pats = []

        deployed = 0.0
        for t in state.open_trades():
            mr = 0.12 if (t.get("segment") == "futures") else 1.0
            deployed += float(t["entry_premium"]) * float(t["qty"]) * mr

        ctx = {
            "symbol": u.symbol,
            "strategy": strat.name,
            "structure": struct,
            "ut_score": ut,
            "patterns": pats,
            "candle_score": self._candle_score(u.symbol),
            "iv": getattr(sig.option, "iv", None) if sig.option else None,
            # `regime` exposes atr/atr_pct/label/percentile and has never had a
            # `realised_vol` key, so this read returned None on every single
            # cycle and the volatility agent's premium rich/cheap branch was
            # dead code. Compute it from the bars we already fetched.
            "rv": _realised_vol(df, u.source),
            # The proposed side. Without it the volatility agent could only
            # lean on the STRUCTURAL trend, and market_structure reports
            # "side" most of the time -- so it abstained on nearly every
            # trade and never earned a weight.
            "side": getattr(sig, "side", None),
            "required_win_rate": getattr(sig, "required_win_rate", None),
            "cost_bite_pct": getattr(sig, "cost_bite_pct", None),
            "balance": kpis.balance,
            "deployed": deployed,
            "deployed_cap": MAX_DEPLOYED_PCT * kpis.balance,
            "open_trades": kpis.open_trades,
            "max_open": MAX_OPEN,
            # Nothing in this project has validated out of sample, so this is
            # False for every strategy until something actually earns it.
            "strategy_validated": False,
        }
        return agents.deliberate(ctx, state)

    def _spot_for(self, t) -> float:
        # A fresh spot is what moves the mark — for India because the premium
        # is modelled off it, for crypto because the coin premium is scaled by
        # it. Falling back to the entry spot freezes the mark rather than
        # inventing a move, which is the safe direction.
        try:
            u = options.get_underlying(t["underlying"], t["source"], t["underlying"])
            if u:
                return u.spot
        except Exception:
            pass
        return t["entry_spot"]

    # -- helpers -------------------------------------------------------------

    def _avoid_stub(self, item, reason) -> dict:
        return {
            "underlying": item["symbol"], "source": item["source"], "label": item["label"],
            "spot": None, "side": None, "decision": "AVOID",
            "checks": [{"name": "QUOTE", "ok": False, "reason": reason}],
            "option": None, "score": None, "strategy": "",
        }

    def _sig_dict(self, s: strategy.Signal) -> dict:
        o = s.option
        return {
            "underlying": s.underlying, "source": s.source, "label": s.label,
            "spot": round(s.spot, 2) if s.spot else None,
            "side": s.side, "decision": s.decision, "strategy": s.strategy,
            "score": round(s.score, 3) if s.score is not None else None,
            "checks": [{"name": c.name, "ok": c.ok, "reason": c.reason} for c in s.checks],
            "cost_bite_pct": getattr(s, "cost_bite_pct", None),
            "required_win_rate": getattr(s, "required_win_rate", None),
            "committee": (s.committee.as_dict()
                          if getattr(s, "committee", None) is not None else None),
            "option": None if o is None else {
                "instrument": o.instrument, "kind": o.kind, "strike": o.strike,
                "expiry": o.expiry, "dte": round(o.dte, 2), "mid": round(o.mid, 4),
                "bid": round(o.bid, 4), "ask": round(o.ask, 4), "iv": round(o.iv, 4),
                "spread_pct": round(o.spread_pct, 4), "volume": o.volume, "oi": o.open_interest,
            },
        }

    def _emit(self) -> None:
        self.on_event({"kind": "KPI", "body": _kpi_dict(state.compute_kpis())})
        self.on_event({"kind": "SIGNALS", "body": self.signals})
        self.on_event({"kind": "TRADES", "body": state.open_trades()})
        self.on_event({"kind": "MODEL", "body": self.model.metrics()})
        self.on_event({"kind": "SAE", "body": self.sae.metrics()})
        self.on_event({"kind": "ACCOUNT", "body": state.account_report()})


def _kpi_dict(k) -> dict:
    """KPI payload for the live WebSocket push.

    This MUST carry the same fields as the REST snapshot. It did not: the
    mark-to-market fields were added to the snapshot only, so the UI got live
    equity once on page load and then every WebSocket update overwrote the
    KPIs WITHOUT them. The balance tile fell back to the settled figure and
    looked frozen while the desk was in fact marking every cycle.
    """
    return {
        "balance": k.balance, "initial": k.initial, "total_net": k.total_net,
        "total_pnl_pct": k.total_pnl_pct, "wins": k.wins, "losses": k.losses,
        "win_rate": k.win_rate, "best": k.best, "worst": k.worst, "avg_net": k.avg_net,
        "charges_paid": k.charges_paid, "tax_paid": k.tax_paid, "gross_total": k.gross_total,
        "open_trades": k.open_trades, "total_trades": k.total_trades,
        "unrealized": k.unrealized, "equity": k.equity,
        "open_notional": k.open_notional,
    }
