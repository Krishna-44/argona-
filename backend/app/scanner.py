"""Market scanner — turns structure + patterns into a concrete, placeable
option trade: which symbol, CE or PE, which expiry, which strike.

The strike/expiry choice is not arbitrary. Two rules drive it:

  * **Expiry** — buying anything under ~3 DTE is a theta/gamma meat grinder,
    so the scanner skips to the next weekly. Slower, positional setups get a
    longer expiry so the thesis has room to play out.
  * **Strike** — the underlying must actually be able to REACH it. Expected
    move over the holding window is ATR x sqrt(days); a strike beyond that is
    a lottery ticket, so the scanner won't suggest one.

Conviction blends trend strength, multi-timeframe alignment, the candlestick
score, and whether there's room to the next S/R level.

Honest framing carried into every idea: our own backtests show BUYING options
directionally is a losing structure (~-96% on NIFTY over a year). These are
ranked candidates with their risks stated, not predictions — and they are
suggestions for YOU to place, never orders Argona sends.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field

from . import angelone, backtest, candlestick, market_structure as ms
from . import option_pricing as op

# What gets scanned, by venue. Indices first — they carry the best option
# liquidity — then the liquid single names, then the international book.
UNIVERSE_INDIA = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "TCS", "INFY",
                  "HDFCBANK", "ICICIBANK", "SBIN", "TATAMOTORS",
                  "AXISBANK", "KOTAKBANK", "LT", "MARUTI", "WIPRO",
                  "BHARTIARTL", "ITC", "HINDUNILVR", "BAJFINANCE", "ADANIENT"]
UNIVERSE_US = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META"]
UNIVERSE_CRYPTO = ["BTC", "ETH"]

VENUES = {"india": UNIVERSE_INDIA, "us": UNIVERSE_US, "crypto": UNIVERSE_CRYPTO}


def universe_for(venue: str = "all") -> list[str]:
    if venue in VENUES:
        return list(VENUES[venue])
    return UNIVERSE_INDIA + UNIVERSE_US + UNIVERSE_CRYPTO


def venue_of(symbol: str) -> str:
    s = symbol.upper()
    for v, syms in VENUES.items():
        if s in syms:
            return v
    return "india"


# Back-compat for callers that still import UNIVERSE.
UNIVERSE = UNIVERSE_INDIA

INDEX_SET = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
MIN_DTE_BUY = 3.0      # never suggest buying inside 3 days to expiry
IDEAL_DTE = (5.0, 12.0)


@dataclass
class Idea:
    symbol: str
    spot: float
    direction: str            # bullish | bearish | neutral
    option_type: str | None   # CE | PE | None
    expiry: str | None
    dte: float | None
    strike: float | None
    moneyness: str | None     # ATM | OTM1 | OTM2 | ITM1
    premium_est: float | None
    lot_size: int | None
    cost_per_lot: float | None
    delta: float | None
    theta_per_day: float | None
    target: float | None      # on the UNDERLYING
    stop_loss: float | None   # on the UNDERLYING
    expected_move: float | None
    reachable: bool | None
    conviction: int           # 0..100
    trend: str
    adx: float
    regime: str
    mtf: str
    patterns: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Which book this belongs to, and the cost economics that decide whether
    # a trade is POSSIBLE at all rather than merely attractive.
    venue: str = ""
    round_trip_pct: float | None = None     # % of premium: spread + slip + fees
    required_win_rate: float | None = None  # win rate needed just to break even
    tradeable: bool = False
    verdict: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _pick_expiry(symbol: str) -> tuple[str | None, float | None]:
    """Nearest real expiry for THIS symbol with enough time left to buy.

    Each NSE underlying has its own calendar and they are not interchangeable:
    NIFTY runs weeklies, while BANKNIFTY and every stock are monthly-only
    since NSE withdrew the other weeklies. Substituting the NIFTY calendar
    produced suggestions like "TCS 17NOV2026" — a contract that does not
    exist. The instrument master is public, so the symbol's own list is
    always available; falling back to NIFTY only if the symbol returns
    nothing keeps a missing master from stalling the scan.
    """
    import datetime as dt
    name = symbol.upper()
    try:
        exps = angelone.expiries(name)
        if not exps and name not in INDEX_SET:
            exps = angelone.expiries("NIFTY")
    except Exception:
        return None, None
    months = {m: i for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
    today = dt.date.today()
    for e in exps:
        try:
            d = dt.date(int(e[5:]), months[e[2:5]], int(e[:2]))
        except Exception:
            continue
        dte = (d - today).days
        if dte >= MIN_DTE_BUY:
            return e, float(dte)
    return (exps[0] if exps else None), None


def scan_symbol(symbol: str, period: str = "6mo", interval: str = "1d") -> Idea | None:
    df = backtest.fetch_history(symbol, period, interval)
    if df is None or len(df) < 60:
        return None

    struct = ms.analyze(df, interval)
    t = struct["trend"]
    reg = struct["regime"]
    mtf = struct["multi_timeframe"]
    spot = struct["price"]

    cs_score = int(candlestick.score(df).iloc[-1])
    pats = [p for p in candlestick.detect(df).iloc[-1].index
            if bool(candlestick.detect(df).iloc[-1][p])]

    # ---- direction ------------------------------------------------------
    votes = 0
    if t["direction"] == "up":
        votes += 2
    elif t["direction"] == "down":
        votes -= 2
    votes += 1 if cs_score > 0 else (-1 if cs_score < 0 else 0)
    if mtf.get("aligned"):
        votes += 1 if t["direction"] == "up" else -1
    if t.get("above_ema200") is True:
        votes += 1
    elif t.get("above_ema200") is False:
        votes -= 1

    direction = "bullish" if votes >= 2 else ("bearish" if votes <= -2 else "neutral")
    reasons: list[str] = []
    warnings: list[str] = []

    reasons.append(f"trend {t['direction']} (ADX {t['adx']}, {t['structure']})")
    if cs_score:
        reasons.append(f"candlestick score {cs_score:+d}")
    if mtf.get("alignment"):
        reasons.append(f"timeframes: {mtf['alignment']}")

    idea = Idea(symbol=symbol.upper(), spot=spot, direction=direction,
                option_type=None, expiry=None, dte=None, strike=None,
                moneyness=None, premium_est=None, lot_size=None,
                cost_per_lot=None, delta=None, theta_per_day=None,
                target=None, stop_loss=None, expected_move=None, reachable=None,
                conviction=0, trend=t["direction"], adx=t["adx"],
                regime=reg["label"], mtf=mtf.get("alignment", "n/a"),
                patterns=pats, reasons=reasons, warnings=warnings)

    if direction == "neutral":
        idea.warnings.append("no directional edge — range/chop; buying options bleeds theta here")
        idea.conviction = max(0, 25 + t["adx"] // 4)
        return idea

    # ---- expiry ---------------------------------------------------------
    expiry, dte = _pick_expiry(symbol)
    if dte is None:
        dte = 7.0
    if dte < IDEAL_DTE[0]:
        warnings.append(f"only {dte:.0f} DTE — theta/gamma risk is high")
    elif dte > IDEAL_DTE[1]:
        reasons.append(f"{dte:.0f} DTE gives the thesis room")

    # ---- expected move & target ----------------------------------------
    atr = reg["atr"] or (spot * 0.01)
    exp_move = atr * math.sqrt(max(dte, 1.0))
    if direction == "bullish":
        target = struct.get("nearest_resistance") or (spot + exp_move)
        stop = struct.get("nearest_support") or (spot - 1.5 * atr)
    else:
        target = struct.get("nearest_support") or (spot - exp_move)
        stop = struct.get("nearest_resistance") or (spot + 1.5 * atr)

    distance = abs(target - spot)
    reachable = distance <= exp_move * 1.2
    if not reachable:
        warnings.append(f"target {round(distance,1)} away vs expected move {round(exp_move,1)} — "
                        "may not be reached before expiry")

    # ---- strike selection ------------------------------------------------
    # Strong conviction + a reachable target -> ATM (best delta/theta balance).
    # Strong trend + a LOT of expected room -> 1 strike OTM for leverage.
    # Weak conviction -> 1 strike ITM (higher delta, less theta sensitivity).
    strength = float(t["strength"])
    if strength >= 0.6 and distance > exp_move * 0.6:
        steps, moneyness = 1, "OTM1"
    elif strength < 0.35:
        steps, moneyness = -1, "ITM1"
    else:
        steps, moneyness = 0, "ATM"

    sign = 1 if direction == "bullish" else -1
    # Snap to a strike that actually trades. The modelled grid assumes a step
    # size and gets it wrong on several names, producing strikes that cannot
    # be placed; the public instrument master has the real ladder.
    strike = angelone.snap_real_strike(symbol.upper(), spot, steps * sign, expiry)
    if strike is None:
        strike = op.snap_strike(spot, symbol.upper(), steps * sign)
        reasons.append("strike from modelled grid — instrument master unavailable")
    kind = "call" if direction == "bullish" else "put"

    # ---- premium / greeks estimate --------------------------------------
    rets = df["Close"].pct_change().dropna()
    rv = float(rets.tail(20).std() * math.sqrt(252)) if len(rets) > 20 else 0.2
    iv = op.iv_estimate(rv, dte)
    g = op.bs(spot, strike, dte / 365.0, iv, kind)
    lot = angelone.fo_lot_size(symbol.upper()) or (
        op.lot_size(symbol.upper()) if symbol.upper() in INDEX_SET else 1)

    # ---- conviction ------------------------------------------------------
    conv = 35
    conv += int(strength * 25)
    conv += 12 if mtf.get("aligned") else 0
    conv += 8 if abs(cs_score) >= 1 else 0
    conv += 10 if reachable else -12
    conv += 5 if reg["label"] != "volatile" else -5
    conv = max(0, min(100, conv))

    # ---- venue, cost economics and the actual verdict --------------------
    # A direction is not a trade. What decides whether a trade is POSSIBLE is
    # the round trip: spread + slippage + fees, against the move being aimed
    # at. Expressed as the win rate needed just to break even,
    #     p = (SL + cost) / (TP + SL)
    # which is exact. Above 100% the cost exceeds the target and the trade
    # loses even when it is right.
    venue = venue_of(symbol)
    idea.venue = venue
    try:
        from . import charges as _ch
        from . import options as _opt
        # Contract size is venue-specific: NSE lots from the instrument
        # master, a US listed option covers 100 shares, crypto trades in
        # single contracts. Defaulting US to 1 made the flat per-order fee
        # look like 100x its real share of notional (NVDA came out at 1080%).
        if venue == "us":
            lot_units = 100.0
        elif venue == "crypto":
            lot_units = 1.0
        else:
            lot_units = float(lot or 1)

        # The charge model's flat component is in RUPEES, but US and crypto
        # premiums are quoted in dollars here. Without converting, a $28 ETH
        # premium was charged Rs 40 of brokerage and came out at 122%.
        fx = _opt.usdinr() if venue in ("us", "crypto") else 1.0
        prem = float(g.price) * fx
        notional = prem * lot_units
        seg = "options"
        spread_pct = {"india": 2.0, "us": 1.6, "crypto": 9.0}.get(venue, 2.0)
        slip_pct = 2.0
        fee_pct = (_ch.round_trip_charges(prem, prem, lot_units, segment=seg).total
                   / notional * 100) if notional > 0 else 0.0
        cost_pct = spread_pct + slip_pct + fee_pct
        idea.round_trip_pct = round(cost_pct, 2)

        tp_move = abs(target - spot) / spot if spot else 0.0
        sl_move = abs(stop - spot) / spot if spot else 0.0
        lev = (abs(g.delta) * spot / prem) if prem > 0 else 0.0
        tp_band = min(tp_move * lev, 0.90) * 100
        sl_band = min(sl_move * lev, 0.90) * 100
        denom = tp_band + sl_band
        need = ((sl_band + cost_pct) / denom * 100) if denom > 0 else 100.0
        idea.required_win_rate = round(need, 1)

        if need >= 100.0:
            idea.tradeable = False
            idea.verdict = (f"NOT TRADEABLE — round trip {cost_pct:.1f}% exceeds the "
                            f"{tp_band:.0f}% target; it loses even when right")
            idea.warnings.append(idea.verdict)
        elif need >= 75.0:
            idea.tradeable = False
            idea.verdict = f"MARGINAL — needs a {need:.0f}% win rate to break even"
            idea.warnings.append(idea.verdict)
        else:
            idea.tradeable = True
            idea.verdict = (f"TRADEABLE — needs {need:.0f}% win rate; round trip "
                            f"{cost_pct:.1f}% of premium")
    except Exception as e:
        idea.verdict = f"cost not computed ({e!r})"

    idea.option_type = "CE" if kind == "call" else "PE"
    idea.expiry = expiry
    idea.dte = round(dte, 1)
    idea.strike = strike
    idea.moneyness = moneyness
    idea.premium_est = round(g.price, 2)
    # Use the venue-aware contract size and currency worked out above, so the
    # displayed lot and per-lot cost match the economics the verdict is based
    # on. A US contract is 100 shares and its premium is in dollars; showing
    # "lot 1 · per lot Rs 12" for QQQ while costing it as 100 shares in rupees
    # is two different trades on one card.
    try:
        idea.lot_size = int(lot_units)
        idea.cost_per_lot = round(prem * lot_units, 2)
    except Exception:
        idea.lot_size = lot
        idea.cost_per_lot = round(g.price * lot, 2) if lot else None
    idea.delta = round(g.delta, 3)
    idea.theta_per_day = round(g.theta, 3)
    idea.target = round(target, 2)
    idea.stop_loss = round(stop, 2)
    idea.expected_move = round(exp_move, 2)
    idea.reachable = reachable
    idea.conviction = conv
    idea.reasons.append(f"{moneyness} {idea.option_type} @ {strike:.0f} · delta {idea.delta}")
    if g.theta < 0:
        idea.warnings.append(f"theta -{abs(g.theta):.2f}/day — time works against a buyer")
    return idea


# ---------------------------------------------------------------------------
# Background scan job.
#
# A full sweep is 30 symbols, each pulling history and an option chain, so it
# runs for minutes. As a single blocking request that is a timeout waiting to
# happen and the UI can show nothing until the very end. Running it as a job
# lets the scan take as long as it needs while the dashboard streams partial
# results and a progress count.

import threading

_JOBS: dict[str, dict] = {}
_JOB_LOCK = threading.Lock()


def start_scan(venue: str = "all", period: str = "6mo",
               interval: str = "1d") -> str:
    """Kick off a scan in the background and return its job id."""
    import uuid
    syms = universe_for(venue)
    job_id = uuid.uuid4().hex[:12]
    with _JOB_LOCK:
        _JOBS[job_id] = {
            "id": job_id, "venue": venue, "state": "running",
            "total": len(syms), "done": 0, "current": None,
            "started": __import__("time").time(), "finished": None,
            "ideas": [], "no_trade": [], "errors": [],
        }

    def _run():
        for s in syms:
            with _JOB_LOCK:
                _JOBS[job_id]["current"] = s
            try:
                idea = scan_symbol(s, period, interval)
            except Exception as e:
                idea = None
                with _JOB_LOCK:
                    _JOBS[job_id]["errors"].append(f"{s}: {e!r}")
            with _JOB_LOCK:
                j = _JOBS[job_id]
                if idea:
                    (j["ideas"] if idea.option_type else j["no_trade"]).append(idea.as_dict())
                j["done"] += 1
        with _JOB_LOCK:
            j = _JOBS[job_id]
            j["ideas"].sort(key=lambda i: (not i.get("tradeable"), -i.get("conviction", 0)))
            j["state"] = "done"
            j["current"] = None
            j["finished"] = __import__("time").time()
        # keep only the few most recent jobs
        with _JOB_LOCK:
            if len(_JOBS) > 6:
                for k in sorted(_JOBS, key=lambda k: _JOBS[k]["started"])[:-6]:
                    _JOBS.pop(k, None)

    threading.Thread(target=_run, daemon=True, name=f"scan-{job_id}").start()
    return job_id


def scan_status(job_id: str) -> dict:
    with _JOB_LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return {"ok": False, "error": "unknown job id"}
        snap = dict(j)
        snap["ideas"] = list(j["ideas"])
        snap["no_trade"] = list(j["no_trade"])
        snap["errors"] = list(j["errors"])
    tradeable = [i for i in snap["ideas"] if i.get("tradeable")]
    snap["ok"] = True
    snap["tradeable_count"] = len(tradeable)
    snap["elapsed_sec"] = round(
        (snap["finished"] or __import__("time").time()) - snap["started"], 1)
    snap["note"] = (
        f"{len(tradeable)} of {len(snap['ideas'])} directional setups clear their "
        "round-trip cost. A direction is not a trade — the cost verdict is what "
        "decides whether one is possible."
    )
    return snap


def scan(symbols: list[str] | None = None, period: str = "6mo",
         interval: str = "1d", min_conviction: int = 0) -> dict:
    """Scan the universe and rank concrete option trade ideas."""
    syms = symbols or UNIVERSE
    ideas: list[Idea] = []
    for s in syms:
        try:
            idea = scan_symbol(s, period, interval)
        except Exception:
            idea = None
        if idea:
            ideas.append(idea)

    tradeable = [i for i in ideas if i.option_type and i.conviction >= min_conviction]
    tradeable.sort(key=lambda i: -i.conviction)
    watch = [i for i in ideas if not i.option_type]
    return {
        "ok": True,
        "scanned": len(ideas),
        "ideas": [i.as_dict() for i in tradeable],
        "no_trade": [i.as_dict() for i in watch],
        "note": ("Ranked candidates, not predictions. Our own backtests show "
                 "BUYING options directionally is a losing structure "
                 "(~-96% on NIFTY/1y) — treat these as where to look, and "
                 "size for the theta you are paying. Argona places no orders."),
    }
