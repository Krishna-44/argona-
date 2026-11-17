"""Argona F&O API — options scalper.

    GET  /health
    GET  /snapshot        — everything the dashboard renders
    GET  /signals         — latest TAKE/AVOID signals per underlying
    GET  /model | /sae    — learning metrics
    GET  /charges         — the India charge/tax rate card in effect
    GET  /strategies      — available strategies (+ which is active)
    POST /strategy        — set active strategy, or add a custom momentum strategy
    GET  /readiness       — go-live gate (stays locked until the record proves out)
    POST /control/{pause,resume,reset}
    WS   /ws              — live stream
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

from fastapi.responses import JSONResponse

from dotenv import load_dotenv
from pathlib import Path as _Path
load_dotenv(_Path(__file__).parent.parent.parent / ".env", override=False)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import (agents, angelone, backtest, candlestick, charges, india_fno,
               llm, market_structure, options, scanner, signal_eval, state,
               strategy)
from . import engine as engine_module
from .engine import Engine
from .model import get_model
from .sae import get_sae

INITIAL = float(os.environ.get("ARGONA_INITIAL", "500000"))  # INR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
app = FastAPI(title="Argona F&O", version="0.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Hub:
    def __init__(self):
        self.subs: set[WebSocket] = set()

    async def register(self, ws: WebSocket):
        await ws.accept()
        self.subs.add(ws)

    async def unregister(self, ws: WebSocket):
        self.subs.discard(ws)

    async def broadcast(self, msg: dict):
        data = json.dumps(msg, default=str)
        for ws in list(self.subs):
            try:
                await ws.send_text(data)
            except Exception:
                self.subs.discard(ws)


hub = Hub()
_loop: asyncio.AbstractEventLoop | None = None


def _sync_event(msg: dict):
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(hub.broadcast(msg), _loop)


# Fan activity rows out over the WS too.
_orig_append = state.append_activity


def _hooked_append(kind: str, body: dict) -> dict:
    ev = _orig_append(kind, body)
    with contextlib.suppress(Exception):
        if _loop is not None:
            asyncio.run_coroutine_threadsafe(
                hub.broadcast({"kind": "ACTIVITY", "body": ev}), _loop)
    return ev


state.append_activity = _hooked_append  # type: ignore[assignment]
engine = Engine(on_event=_sync_event)


@app.on_event("startup")
async def _startup():
    global _loop
    _loop = asyncio.get_running_loop()
    state.init_db(initial_balance=INITIAL)
    await engine.start()


@app.on_event("shutdown")
async def _shutdown():
    await engine.stop()
    options.aclose()


# ---------------------------------------------------------------------------


def _walkforward_bar() -> dict:
    """The hardest bar: the strategy must hold up on unseen historical data.
    Forward-testing a handful of live trades proves nothing; this does."""
    wf = state.get_meta("last_walkforward", None)
    if not wf:
        return {"key": "walkforward", "label": "Walk-forward OOS positive",
                "value": None, "target": 1, "pass": False}
    oos = wf.get("oos_mean_return_pct", 0)
    pos = wf.get("oos_positive_folds", 0)
    tot = max(wf.get("oos_total_folds", 1), 1)
    return {
        "key": "walkforward",
        "label": f"Walk-forward OOS positive ({wf.get('symbol','')})",
        "value": oos, "target": 0,
        "pass": bool(oos > 0 and pos > tot / 2),
    }


def _readiness() -> dict:
    k = state.compute_kpis()
    m = get_model().metrics()
    decided = k.wins + k.losses
    # profit factor from recent closed trades
    gross_win = gross_loss = 0.0
    for t in state.recent_trades(limit=500):
        if (t["net"] or 0) > 0:
            gross_win += t["net"]
        else:
            gross_loss += -(t["net"] or 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (gross_win and 99.0 or 0.0)
    bars = [
        {"key": "model", "label": "Model past warmup (≥200)", "value": m["samples"],
         "target": 200, "pass": m["samples"] >= 200},
        {"key": "trades", "label": "Closed trades (≥50)", "value": decided,
         "target": 50, "pass": decided >= 50},
        {"key": "net", "label": "Positive net P&L (after tax)", "value": round(k.total_net, 2),
         "target": 0, "pass": k.total_net > 0},
        {"key": "winrate", "label": "Win rate ≥ 50%", "value": round(k.win_rate, 1),
         "target": 50, "pass": k.win_rate >= 50},
        {"key": "pf", "label": "Profit factor ≥ 1.2", "value": round(pf, 2),
         "target": 1.2, "pass": pf >= 1.2},
        {"key": "winprob", "label": "Model win-prob acc ≥ 55%",
         "value": (round(m["directional_accuracy"] * 100, 1) if m["directional_accuracy"] is not None else None),
         "target": 55, "pass": m["directional_accuracy"] is not None and m["directional_accuracy"] >= 0.55},
        _walkforward_bar(),
    ]
    passed = sum(1 for b in bars if b["pass"])
    return {"bars": bars, "passed": passed, "total": len(bars),
            "live_allowed": passed == len(bars), "places_real_orders": False,
            "note": "Paper only. Argona never sends orders to a broker; even at 6/6, "
                    "executing in a real account is your action, with your credentials."}


def _snapshot() -> dict:
    k = state.compute_kpis()
    return {
        "account": state.account_report(),
        "kpis": _kpis_dict(k),
        "signals": engine.signals,
        "open_trades": _open_with_risk(),
        "recent_trades": state.recent_trades(limit=40),
        "equity": state.equity_history(limit=600),
        "activity": state.recent_activity(limit=120),
        "model": get_model().metrics(),
        "sae": get_sae().metrics(),
        "charges": charges.config(),
        "strategies": _strategies(),
        "readiness": _readiness(),
        "universe": options.get_universe(),
        "sessions": options.session_status(),
        "fx_usdinr": round(options.usdinr(), 4),
        "agents": agents.scorecard(state),
        "pair": getattr(engine, "pair_state", {}),
        "walkforward": state.get_meta("last_walkforward", None),
        "backtest_symbols": list(backtest.SYMBOLS.keys()),
    }


def _open_with_risk() -> list[dict]:
    """Open positions annotated with what each ACTUALLY risks.

    The desk targets 1% of the book per trade but cannot hit it on either
    instrument class: an NSE lot is indivisible so one NIFTY futures lot
    risks ~1.35%, while crypto spot is cash-funded and capped at 25% of the
    book so it risks ~0.08%. Both are structural, in opposite directions.
    Showing the real number per position is more honest than pretending the
    target is being met.
    """
    k = state.compute_kpis()
    bal = k.balance or 1.0
    out = []
    for t in state.open_trades():
        t = dict(t)
        risk = (float(t["entry_premium"]) - float(t["sl_premium"])) * float(t["qty"])
        t["risk_amount"] = round(risk, 2)
        t["risk_pct"] = round(risk / bal * 100, 3)
        out.append(t)
    return out


def _kpis_dict(k) -> dict:
    # Delegate to the engine's serializer so the REST snapshot and the live
    # WebSocket push can never drift apart again.
    return engine_module._kpi_dict(k)


def _strategies() -> dict:
    return {
        "active": state.get_meta("strategy", "momentum_scalp"),
        "available": [
            {"name": s.name, "desc": s.desc, "tp_pct": s.tp_pct, "sl_pct": s.sl_pct,
             "max_spread_pct": s.max_spread_pct, "trend_threshold": s.trend_threshold,
             # The knobs that actually decide how a strategy behaves: what it
             # trades, how wide the bands are, and how fast it re-enters.
             "instrument": s.instrument,
             "tp_spot_pct": s.tp_spot_pct, "sl_spot_pct": s.sl_spot_pct,
             "cooldown_sec": s.cooldown_sec, "max_cost_pct": s.max_cost_pct,
             "min_persistence": s.min_persistence}
            for s in strategy.REGISTRY.values()
        ],
    }


# A cycle is ~18s. Five missed cycles is a stall, not a slow fetch.
STALL_SEC = float(os.environ.get("ARGONA_STALL_SEC", "120"))


@app.get("/health")
async def health():
    """Liveness of the ENGINE, not just of the web server.

    The old version returned ok:true whenever FastAPI could answer, so when the
    cycle thread hung on a blocking fetch (AngelOne's 6.7MB scrip master, the
    first time the India path woke at the NSE bell) the supervisor saw a
    healthy API and did nothing. The desk was dead for 50 minutes through the
    open while every health check passed.

    A cycle takes ~18s. Anything past STALL_SEC means the loop is not running,
    and saying so with a non-200 is what lets the supervisor act.
    """
    last = engine.last_cycle_ts
    age = (time.time() - last) if last else None
    stalled = age is not None and age > STALL_SEC
    body = {
        "ok": not stalled,
        "open_trades": state.compute_kpis().open_trades,
        "seconds_since_cycle": round(age, 1) if age is not None else None,
        "stall_threshold_sec": STALL_SEC,
    }
    if stalled:
        body["error"] = f"engine has not completed a cycle in {age:.0f}s"
        return JSONResponse(body, status_code=503)
    return body


@app.get("/snapshot")
async def snapshot():
    return _snapshot()


@app.get("/signals")
async def signals():
    return {"signals": engine.signals}


@app.get("/model")
async def model():
    return get_model().metrics()


@app.get("/sae")
async def sae():
    return get_sae().metrics()


@app.get("/charges")
async def charges_cfg():
    return charges.config()


@app.get("/strategies")
async def strategies():
    return _strategies()


@app.get("/readiness")
async def readiness():
    return _readiness()


@app.post("/strategy")
async def set_strategy(payload: dict):
    """Set the active strategy, or register a custom momentum strategy.

    Body: {"name": "momentum_scalp"} to switch, or to ADD a custom one:
      {"name": "my_scalp", "base": "momentum"|"meanrev", "tp_pct":0.2,
       "sl_pct":0.1, "max_spread_pct":0.08, "trend_threshold":0.002,
       "min_volume":50, "max_iv":2.5, "desc":"..."}
    """
    name = str(payload.get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "name required"}
    if "tp_pct" in payload or "base" in payload:  # defining/overriding a strategy
        base = payload.get("base", "momentum")
        direction = strategy._meanrev_dir if base == "meanrev" else strategy._momentum_dir
        strat = strategy.Strategy(
            name=name, direction=direction,
            trend_threshold=float(payload.get("trend_threshold", 0.001)),
            max_spread_pct=float(payload.get("max_spread_pct", 0.08)),
            min_volume=float(payload.get("min_volume", 50)),
            min_oi=float(payload.get("min_oi", 10)),
            max_iv=float(payload.get("max_iv", 2.5)),
            tp_pct=float(payload.get("tp_pct", 0.15)),
            sl_pct=float(payload.get("sl_pct", 0.10)),
            desc=str(payload.get("desc", "custom strategy")),
        )
        strategy.register(strat)
    if name not in strategy.REGISTRY:
        return {"ok": False, "error": f"unknown strategy {name}"}
    state.set_meta("strategy", name)
    state.append_activity("INFO", {"msg": f"strategy set → {name}"})
    return {"ok": True, "active": name, "available": _strategies()["available"]}


@app.get("/llm")
async def llm_status():
    """Is the on-device model reachable? Deliberately never raises: the desk
    trades normally whether or not a local LLM is present."""
    return llm.available()


@app.post("/llm/explain")
async def llm_explain(payload: dict):
    """Narrate a decision from facts the engine already computed.

    The reply is checked against those facts and any number the model invented
    is returned in `ungrounded`. A non-empty `ungrounded` means the narration
    should not be shown to a trader -- on its first live test the model turned
    `risk 0.08% of book` into `8% of the book`, a 100x error on the one number
    that decides whether a position is sane.
    """
    facts = payload.get("facts") or {}
    if not facts:
        return {"ok": False, "error": "no facts supplied"}
    r = llm.explain(facts, model=payload.get("model"))
    if not r.ok:
        return {"ok": False, "error": r.error}
    ungrounded = llm.check_numbers_grounded(r.text, llm.label_units(facts))
    return {"ok": True, "text": r.text, "model": r.model,
            "seconds": r.seconds, "ungrounded": ungrounded,
            "trustworthy": not ungrounded}


@app.post("/llm/propose")
async def llm_propose(payload: dict):
    """Generate strategy hypotheses to be TESTED. Nothing here is armed.

    Every proposal comes back marked UNTESTED with a break-even win rate
    computed here, not by the model. Arming one still requires it to survive
    the gauntlet: event study against a side-matched random baseline,
    split-half replication, Bonferroni, and a buy-and-hold comparison.
    """
    k = state.compute_kpis()
    ctx = payload.get("context") or {
        "instruments": ["NIFTY", "BANKNIFTY", "BTC", "ETH"],
        "round_trip_cost_pct": 0.05,
        "current_strategy": state.get_meta("strategy", "momentum_scalp"),
        "desk_record": f"{k.wins}W/{k.losses}L",
        "finding": ("38 strategies tested, zero validated edge; buy-and-hold "
                    "beat every strategy on NIFTY/BANKNIFTY over 5y"),
    }
    out = llm.propose(ctx, n=int(payload.get("n", 3)),
                      model=payload.get("model"))
    out["warning"] = ("Proposals are untested hypotheses from a 3.8B local "
                      "model. An LLM has no price data the engine lacks and "
                      "does not create edge. Backtest before arming.")
    return out


@app.get("/backtest")
async def run_backtest(symbol: str = "NIFTY", period: str = "1y", interval: str = "1h"):
    """Backtest the live strategy on history with real India F&O costs."""
    r = await asyncio.to_thread(backtest.run, symbol, None, period, interval)
    if r is None:
        return {"ok": False, "error": f"no data for {symbol}"}
    return {"ok": True, "result": r.as_dict()}


@app.get("/walkforward")
async def run_walkforward(symbol: str = "NIFTY", period: str = "1y",
                          interval: str = "1h", splits: int = 4):
    """Walk-forward validation — optimise in-sample, validate out-of-sample.
    Only the OOS numbers count as evidence of edge."""
    wf = await asyncio.to_thread(
        backtest.walk_forward, symbol, backtest.default_grid(), period, interval, splits)
    if wf is None:
        return {"ok": False, "error": f"insufficient data for {symbol}"}
    state.set_meta(f"wf_{symbol.upper()}", wf)
    state.set_meta("last_walkforward", wf)
    return {"ok": True, "result": wf}


@app.get("/backtest/symbols")
async def backtest_symbols():
    return {"symbols": list(backtest.SYMBOLS.keys()), "catalog": backtest.CATALOG}


@app.get("/catalog/test")
async def catalog_test(symbol: str = "NIFTY", period: str = "1y",
                       interval: str = "1h", mode: str = "futures"):
    """Run every catalog strategy on one symbol, options vs futures structure.

    Each row carries its saturation diagnostic. The sequential backtest holds
    one position at a time, so a signal that fires while a trade is open is
    dropped — when exposure is high and utilisation low, the return column is
    measuring the position limit, not the signal, and must not be ranked on.
    """
    def _work():
        rows = []
        df = backtest.fetch_history(symbol, period, interval)
        for s in ["live"] + backtest.CATALOG:
            r = (backtest.run_futures(symbol, s, period, interval) if mode == "futures"
                 else backtest.run(symbol, None, period, interval, strategy=s))
            if not r:
                continue
            row = {
                "strategy": s, "trades": r.trades, "win_rate": round(r.win_rate, 1),
                "return_pct": round(r.return_pct, 2), "profit_factor": round(r.profit_factor, 2),
                "max_dd_pct": round(r.max_dd_pct, 1), "sharpe": round(r.sharpe, 2),
            }
            if df is not None and s != "live":
                try:
                    e = signal_eval.exposure(df, s)
                    row.update(signals_fired=e["signals_fired"],
                               utilisation_pct=e["utilisation_pct"],
                               exposure_pct=e["exposure_pct"],
                               saturated=e["saturated"])
                except Exception:
                    pass
            rows.append(row)
        return rows
    rows = await asyncio.to_thread(_work)
    rows.sort(key=lambda x: -x["return_pct"])
    n_sat = sum(1 for r in rows if r.get("saturated"))
    return {
        "ok": True, "symbol": symbol.upper(), "mode": mode, "results": rows,
        "saturated_count": n_sat,
        "warning": (
            f"{n_sat} of {len(rows)} strategies are SATURATED — the desk is in a "
            "position most of the time, so entries are 'first signal after the "
            "last exit' rather than the best signal, and these returns do not "
            "measure selectivity. Use /signal/compare for a verdict."
        ) if n_sat else "",
    }


@app.get("/signal/eventstudy")
async def signal_eventstudy(symbol: str = "NIFTY", strategy: str = "ut_score",
                            period: str = "1mo", interval: str = "5m",
                            horizon: int = 12, tp: float = 0.010, sl: float = 0.006,
                            invert: bool = False):
    """Per-signal edge vs a random-entry baseline, overlaps allowed.

    Answers the only question that matters: does this signal's TIMING beat
    firing the same TP/SL rules on random bars? Any TP/SL scheme has a built-in
    win rate, so raw returns and hit rates are not evidence on their own.
    """
    df = await asyncio.to_thread(backtest.fetch_history, symbol, period, interval)
    if df is None:
        return {"ok": False, "error": f"no data for {symbol} {interval}/{period}"}
    st = await asyncio.to_thread(signal_eval.event_study, df, strategy,
                                 horizon, tp, sl, 2000, 7, invert)
    return {"ok": True, "symbol": symbol.upper(), "bars": len(df), **st.as_dict()}


@app.get("/signal/compare")
async def signal_compare(symbol: str = "NIFTY", period: str = "1mo",
                         interval: str = "5m", horizon: int = 12,
                         tp: float = 0.010, sl: float = 0.006):
    """Event-study the whole catalog on one symbol and rank by edge over random.

    Includes the Bonferroni threshold: testing k strategies on one dataset means
    the best p-value must clear 0.05/k, not 0.05, before it means anything.
    """
    df = await asyncio.to_thread(backtest.fetch_history, symbol, period, interval)
    if df is None:
        return {"ok": False, "error": f"no data for {symbol} {interval}/{period}"}
    rows = await asyncio.to_thread(signal_eval.compare, df, backtest.CATALOG,
                                   horizon, tp, sl, 2000)
    winners = [r for r in rows if r.get("significant_after_correction")]
    return {
        "ok": True, "symbol": symbol.upper(), "bars": len(df),
        "tests_run": len(rows), "results": rows,
        "validated": [r["strategy"] for r in winners],
        "note": ("No strategy clears the corrected threshold — nothing here has "
                 "demonstrated edge on this data."
                 if not winners else
                 "Clears the in-sample threshold. Still needs replication on a "
                 "different period and instrument before it means anything."),
    }


@app.get("/signal/ledger")
async def signal_ledger(symbol: str = "NIFTY", strategy: str = "rsi",
                        period: str = "1mo", interval: str = "5m",
                        horizon: int = 48, tp: float = 0.008, sl: float = 0.008,
                        invert: bool = False, mode: str = "desk",
                        capital: float = 500_000.0, limit: int = 200):
    """Trade-by-trade ledger: entry, exit, bars held, exit reason, charges, net.

    Always returned WITH its significance test. A sequential ledger has
    timestamps and an equity curve, so it reads like proof — but when the desk
    takes only a few percent of the signals, a few dozen trades of a zero-edge
    signal land positive about half the time. The `significance` block says
    whether a random desk of the same size would have done as well.
    """
    df = await asyncio.to_thread(backtest.fetch_history, symbol, period, interval)
    if df is None:
        return {"ok": False, "error": f"no data for {symbol} {interval}/{period}"}
    log = await asyncio.to_thread(signal_eval.trade_log, df, strategy,
                                  horizon, tp, sl, invert, mode, capital)
    sig = None
    if mode == "desk":
        sig = await asyncio.to_thread(signal_eval.desk_significance, df, strategy,
                                      horizon, tp, sl, invert, capital, 5000)
    log["trades_list"] = log["trades_list"][:limit]
    return {"ok": True, "symbol": symbol.upper(), **log, "significance": sig}


@app.post("/scan/start")
async def scan_start(venue: str = "all", period: str = "6mo", interval: str = "1d"):
    """Begin a full market sweep in the background.

    The sweep is 30 symbols, each pulling history and a chain, so it runs for
    minutes. As one blocking request it would time out and show nothing until
    the end; as a job the UI can stream partial results and a progress count
    while it takes as long as it needs.
    """
    job_id = await asyncio.to_thread(scanner.start_scan, venue, period, interval)
    return {"ok": True, "job_id": job_id, "venue": venue,
            "total": len(scanner.universe_for(venue))}


@app.get("/scan/status")
async def scan_status(job_id: str):
    """Progress + whatever the sweep has found so far."""
    return await asyncio.to_thread(scanner.scan_status, job_id)


@app.get("/scan/venues")
async def scan_venues():
    return {"ok": True,
            "venues": {v: scanner.universe_for(v) for v in ("all", "india", "us", "crypto")}}


@app.get("/agents")
async def agents_report():
    """Agent committee scorecards.

    Predictors earn influence only from realised accuracy — below
    MIN_SAMPLES scored votes their weight is exactly 0, so a fresh or
    mediocre agent cannot move the desk on confidence alone.
    """
    roster = [fn({"structure": {}, "strategy": "-"}) for fn in agents.AGENTS]
    scores = agents.scorecard(state)

    # What each agent said on the most recent pass over the universe, so the
    # menu can show not just who they are but what they are doing right now.
    latest: dict[str, list[dict]] = {}
    for sig in (engine.signals or []):
        cm = sig.get("committee") or {}
        for v in (cm.get("views") or []):
            latest.setdefault(v["agent"], []).append({
                "symbol": sig.get("underlying"),
                "vote": v.get("vote"),
                "reason": v.get("reason"),
                "veto": v.get("veto", False),
            })

    out = []
    for v in roster:
        sc = scores.get(v.agent, {})
        obs = latest.get(v.agent, [])
        out.append({
            "agent": v.agent, "role": v.role, "kind": v.kind,
            "power": "veto" if v.kind == "gatekeeper" else "vote",
            "votes_scored": sc.get("votes_scored", 0),
            "hit_rate": sc.get("hit_rate"),
            "weight": sc.get("weight", 0.0),
            "calibrated": sc.get("calibrated", False),
            "needs": sc.get("needs", agents.MIN_SAMPLES),
            "watching": len(obs),
            "now": obs,
            # A gatekeeper never votes, so "abstaining" would misdescribe it:
            # it is either blocking something or it is clear.
            "status": (
                "vetoing" if any(o["veto"] for o in obs)
                else ("clear" if v.kind == "gatekeeper" else "abstaining")
                if obs and all(abs(float(o["vote"] or 0)) < 1e-9 for o in obs)
                else "voting" if obs else "idle"
            ),
        })
    return {"ok": True, "min_samples": agents.MIN_SAMPLES,
            "strategy": state.get_meta("strategy", "-"), "agents": out}


@app.get("/india/vrp")
async def india_vrp(period: str = "2y"):
    """Variance risk premium: India VIX vs the realised vol that followed.

    Both sides are observations — VIX is published by NSE, realised vol comes
    from index prices. Nothing is assumed, which is what makes this the one
    option-selling result in the project that is not circular.
    """
    return await asyncio.to_thread(india_fno.vrp_study, period)


@app.get("/india/straddle")
async def india_straddle(period: str = "2y", dte: int = 7,
                         sl_mult: float = 2.0, sensitivity: bool = True):
    """Short ATM straddle priced at the real India VIX, with its IV sweep.

    The sweep is returned by default and should be read FIRST: VIX is a 30-day
    measure, so pricing a weekly with it carries term-structure error, and if
    the sweep spans more than the result then the headline is an artifact of
    that assumption rather than a finding.
    """
    r = await asyncio.to_thread(india_fno.short_straddle, period, dte, india_fno.DEFAULT_LOT, sl_mult)
    if isinstance(r, dict) and r.get("ok"):
        r["trades_list"] = r["trades_list"][:100]
        if sensitivity:
            r["iv_sensitivity"] = await asyncio.to_thread(
                india_fno.straddle_iv_sensitivity, period, dte)
    return r


@app.get("/pair")
async def pair_status():
    """Live NIFTY/BANKNIFTY pair state and its open legs.

    Forward test only. The backtest was the strongest candidate found here
    (9/9 configs positive, permutation p=0.0135) but it fails the Bonferroni
    bar for the configs tested and the split-half test, so this is running to
    generate out-of-sample evidence — not because it is a proven edge.
    """
    # `engine` here is the Engine INSTANCE; PAIR_ENABLED is a module-level
    # constant, so reach it through the module rather than the object.
    from . import pair_engine
    from . import engine as engine_mod
    def _work():
        un = options.get_underlying("NIFTY", "india", "NIFTY")
        ub = options.get_underlying("BANKNIFTY", "india", "BANKNIFTY")
        z = pair_engine.live_z(un.spot if un else None, ub.spot if ub else None)
        legs = [t for t in state.open_trades() if t.get("pair_id")]
        closed = [t for t in state.recent_trades(200) if t.get("pair_id")]
        net = sum(float(t.get("net") or 0) for t in closed)
        return {
            "ok": True, "enabled": engine_mod.PAIR_ENABLED, "z": z,
            "open_legs": legs, "closed_legs": len(closed),
            "realised_net": round(net, 2),
            "validated": False,
            "note": ("Forward test. Backtest: 9/9 configs positive, permutation "
                     "p=0.0135 vs a matched random baseline, but it fails "
                     "Bonferroni (bar 0.0056 for 9 configs) and split-half "
                     "(significance only in the second half). Running live to "
                     "produce out-of-sample evidence."),
        }
    return await asyncio.to_thread(_work)


@app.get("/india/spread")
async def india_spread(dte: int = 30, direction: str = "bear",
                       period: str = "2y", sensitivity: bool = True):
    """Vertical credit spread backtest, premium priced at the real India VIX.

    The only option structure here that survives the circularity guard: both
    legs share the IV so much of the assumption cancels, and max loss is fixed
    by the strike width rather than by IV. Read `iv_sensitivity` first — at
    7 DTE a 30-day VIX still produces an artifact.
    """
    r = await asyncio.to_thread(india_fno.vertical_spread, "NIFTY", period, dte,
                                4, 2, 1.0, india_fno.DEFAULT_LOT, 500_000.0, direction)
    if isinstance(r, dict) and r.get("ok"):
        r["trades_list"] = r["trades_list"][:60]
        if sensitivity:
            r["iv_sensitivity"] = await asyncio.to_thread(
                india_fno.spread_iv_sensitivity, "NIFTY", period, dte, direction)
    return r


@app.get("/india/regime")
async def india_regime(symbol: str = "NIFTY", period: str = "2y",
                       interval: str = "1h", window: int = 120):
    """Is the regime predictable, and would knowing it even help?

    Returns both halves: whether the trailing variance ratio predicts the next
    one, and what a perfect regime call would have been worth. A large oracle
    gain with no predictability means a real ceiling out of reach; a small
    oracle gain means the whole idea is not worth building.
    """
    pred = await asyncio.to_thread(india_fno.regime_predictability,
                                   symbol, period, interval, window)
    switch = await asyncio.to_thread(india_fno.regime_switch_study,
                                     symbol, period, interval, window)
    return {"ok": True, "predictability": pred, "switching": switch}


@app.get("/signal/geometry")
async def signal_geometry(symbol: str = "NIFTY", period: str = "1mo",
                          interval: str = "5m"):
    """What the EXIT STRUCTURE costs before any signal is applied.

    Fires random entries across a grid of (horizon, TP, SL). There is no signal
    and therefore nothing to curve-fit: this is the house edge of the execution
    scheme. If random loses X% per trade, a signal must find more than X% of
    edge just to break even.
    """
    df = await asyncio.to_thread(backtest.fetch_history, symbol, period, interval)
    if df is None:
        return {"ok": False, "error": f"no data for {symbol} {interval}/{period}"}
    rows = await asyncio.to_thread(signal_eval.geometry_map, df)
    neutral = [r for r in rows if r["mean_net_pct"] > -0.005]
    return {
        "ok": True, "symbol": symbol.upper(), "bars": len(df),
        "tested": len(rows), "results": rows,
        "near_breakeven": len(neutral),
        "best": rows[0] if rows else None,
        "note": (f"{len(neutral)} of {len(rows)} geometries are near break-even "
                 "at random entry. The rest are structurally negative before a "
                 "signal is even applied."),
    }


@app.get("/analyze")
async def analyze(symbol: str = "NIFTY", period: str = "6mo", interval: str = "1d"):
    """Chart analysis for whatever symbol is loaded: candlestick patterns
    detected, support/resistance, and ATR-based stop-loss / take-profit."""
    def _work():
        df = backtest.fetch_history(symbol, period, interval)
        if df is None:
            return None
        return {
            "symbol": symbol.upper(),
            "tv_symbol": tradingview_symbol(symbol),
            "period": period, "interval": interval,
            "patterns": candlestick.summary(df),
            "levels": candlestick.levels(df),
            "last_close": round(float(df["Close"].iloc[-1]), 2),
            "change_pct": round(float((df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1) * 100), 2),
            "high_52": round(float(df["High"].max()), 2),
            "low_52": round(float(df["Low"].min()), 2),
        }
    res = await asyncio.to_thread(_work)
    if res is None:
        return {"ok": False, "error": f"no data for {symbol}"}
    return {"ok": True, "result": res}


def tradingview_symbol(symbol: str) -> str:
    """Map a symbol to a TradingView ticker so the chart widget can load it."""
    s = symbol.upper().strip()
    special = {
        "NIFTY": "NSE:NIFTY", "BANKNIFTY": "NSE:BANKNIFTY",
        "FINNIFTY": "NSE:CNXFINANCE", "SENSEX": "BSE:SENSEX",
        "BTC": "BINANCE:BTCUSDT", "ETH": "BINANCE:ETHUSDT",
    }
    if s in special:
        return special[s]
    if s in {"SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL"}:
        return f"NASDAQ:{s}" if s in {"QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL"} else f"AMEX:{s}"
    # default: assume an Indian NSE equity (what the F&O side is for)
    return f"NSE:{s}"


@app.get("/ohlc")
async def ohlc(symbol: str = "NIFTY", period: str = "6mo", interval: str = "1d",
               bars: int = 140):
    """OHLC bars + per-bar candlestick patterns + levels, for the native chart.
    Works for Indian symbols (NSE via yfinance) where TradingView's free
    widget refuses to load exchange-restricted data."""
    def _work():
        # A one-month daily window is ~21 bars, well under fetch_history's
        # 60-bar default floor, so the chart came back empty for any short
        # period. The chart only needs enough bars to draw.
        df = backtest.fetch_history(symbol, period, interval, min_bars=15)
        if df is None:
            return None
        d = df.tail(bars)
        pats = candlestick.detect(d)
        out = []
        for ts, row in d.iterrows():
            hits = [c for c in pats.columns if bool(pats.loc[ts, c])]
            out.append({
                "t": str(ts)[:16],
                "o": round(float(row["Open"]), 2), "h": round(float(row["High"]), 2),
                "l": round(float(row["Low"]), 2), "c": round(float(row["Close"]), 2),
                "p": hits,
            })
        # Per-bar trend, so the chart can shade where the market was trending
        # up or down rather than leaving the reader to eyeball it.
        # EMA 9 vs 21, with a separation floor so a flat market is called
        # sideways instead of flickering on tiny crossovers.
        # (market_structure.adx returns a 3-tuple of scalars for the LAST bar,
        # not a per-bar series — indexing it per bar silently produced
        # "sideways" for every candle.)
        trend: list[str] = []
        try:
            close = df["Close"].to_numpy(dtype=float)
            fast = backtest._ema(close, 9)
            slow = backtest._ema(close, 21)
            for i in range(len(df)):
                f, s_, px = fast[i], slow[i], close[i]
                if not (px > 0) or f != f or s_ != s_:
                    trend.append("side")
                    continue
                sep = (f - s_) / px
                if sep > 0.0015:
                    trend.append("up")
                elif sep < -0.0015:
                    trend.append("down")
                else:
                    trend.append("side")
        except Exception:
            trend = ["side"] * len(df)
        trend = trend[-len(d):] if len(trend) >= len(d) else ["side"] * len(d)

        # Buy/sell markers from the strategy the desk is actually running, so
        # the chart shows the signals that would have fired, not a different
        # indicator chosen for looking good.
        marks: list[dict] = []
        try:
            active = state.get_meta("strategy", "momentum_scalp")
            cat = {"rapid_candle": "candlestick", "momentum_scalp": "momentum",
                   "underlying_momo": "momentum", "rapid_underlying": "momentum",
                   "mean_reversion": "rsi"}.get(active, "momentum")
            lo_s, sh_s = backtest.catalog_signals(df, cat)
            base = len(df) - len(d)
            for i in range(base, len(df)):
                if lo_s[i]:
                    marks.append({"i": i - base, "side": "buy", "price": float(df["Low"].iloc[i])})
                elif sh_s[i]:
                    marks.append({"i": i - base, "side": "sell", "price": float(df["High"].iloc[i])})
        except Exception:
            marks = []

        # Where the desk actually filled. These are real paper trades on this
        # symbol, matched onto the bar whose window contains the fill.
        fills: list[dict] = []
        try:
            import datetime as _dt
            ts_list = [t.to_pydatetime().timestamp() for t in d.index]
            def _bar_of(ts: float) -> int | None:
                if not ts or ts < ts_list[0]:
                    return None
                for j in range(len(ts_list) - 1, -1, -1):
                    if ts >= ts_list[j]:
                        return j
                return None
            for t in state.recent_trades(limit=200):
                if str(t.get("underlying", "")).upper() != symbol.upper():
                    continue
                bi = _bar_of(t.get("ts_open") or 0)
                if bi is not None:
                    fills.append({"i": bi, "kind": "entry",
                                  "side": "long" if t.get("kind") == "call" else "short",
                                  "price": float(t.get("entry_spot") or 0),
                                  "net": t.get("net")})
                bx = _bar_of(t.get("ts_close") or 0)
                if bx is not None:
                    fills.append({"i": bx, "kind": "exit",
                                  "side": "long" if t.get("kind") == "call" else "short",
                                  "price": float(t.get("exit_spot") or t.get("entry_spot") or 0),
                                  "net": t.get("net"), "reason": t.get("exit_reason")})
        except Exception:
            fills = []

        return {
            "symbol": symbol.upper(), "interval": interval, "period": period,
            "bars": out, "levels": candlestick.levels(df),
            "patterns": candlestick.summary(df),
            "trend": trend, "marks": marks, "fills": fills,
        }
    res = await asyncio.to_thread(_work)
    if res is None:
        return {"ok": False, "error": f"no data for {symbol}"}
    return {"ok": True, "result": res}


@app.get("/optionchain")
async def optionchain(name: str = "NIFTY", expiry: str | None = None, around: int = 10):
    """India F&O option chain. Structure comes from AngelOne's public instrument
    master; live premiums require SmartAPI credentials in .env.
    MARKET DATA ONLY — Argona never places orders."""
    def _work():
        spot = None
        # A 5-day daily window is 5 bars, far below fetch_history's 60-bar
        # default floor, so this returned None and the chain fell back to a
        # stale internal default — it was centring NIFTY strikes on 24,400
        # while spot was 25,144. Ask for only as many bars as we need.
        df = backtest.fetch_history(name, "1mo", "1d", min_bars=3)
        if df is not None and len(df):
            spot = float(df["Close"].iloc[-1])
        return angelone.option_chain(name, expiry, around, spot)
    return await asyncio.to_thread(_work)


@app.get("/iv-premium")
async def iv_premium(name: str = "NIFTY", interval: str = "1d", days: int = 30):
    """Measure the REAL implied-vol premium from actual option prices.
    This is the number that decides whether selling premium has an edge —
    our backtest otherwise has to assume it. Needs SmartAPI credentials."""
    return await asyncio.to_thread(angelone.measure_iv_premium, name, None, interval, days)


@app.get("/straddle")
async def straddle(symbol: str = "NIFTY", period: str = "1y", interval: str = "1h",
                   initial: float = 500000, model_vrp: bool = True,
                   sl_mult: float = 2.0, otm_steps: int = 0):
    """Short-premium (straddle/strangle) backtest.
    NOTE: with model_vrp=True the result is highly sensitive to the ASSUMED
    IV premium — see /iv-premium to measure the real one."""
    def _work():
        sp = backtest.SellParams(model_vrp=model_vrp, sl_mult=sl_mult, otm_steps=otm_steps)
        r = backtest.run_straddle(symbol, period, interval, initial, sp)
        if r is None:
            return {"ok": False, "error": f"no data for {symbol}"}
        d = r.as_dict(); d.pop("equity", None)
        return {"ok": True, "result": d, "assumption_warning":
                "returns scale directly with the assumed IV premium; breakeven ~1.12"}
    return await asyncio.to_thread(_work)


@app.get("/scan")
async def scan_market(symbols: str | None = None, interval: str = "1d",
                      period: str = "6mo", min_conviction: int = 0):
    """Scan the market and return concrete option trade ideas:
    which symbol, CE or PE, which expiry, which strike (ATM/OTM/ITM)."""
    syms = [x.strip().upper() for x in symbols.split(",")] if symbols else None
    return await asyncio.to_thread(scanner.scan, syms, period, interval, min_conviction)


@app.get("/broker/status")
async def broker_status():
    return angelone.creds_status()


@app.get("/resolve")
async def resolve(symbol: str):
    """Resolve a typed share name to a TradingView symbol + yfinance ticker."""
    s = symbol.upper().strip()
    yf_t = backtest.SYMBOLS.get(s, f"{s}.NS" if s not in backtest.SYMBOLS else s)
    return {"ok": True, "symbol": s, "tv_symbol": tradingview_symbol(s), "yf_ticker": yf_t}


@app.post("/control/close")
async def control_close(payload: dict):
    """Ask the engine to close open trades at the live mark.

    This does not settle anything here. It records the ids and lets the next
    engine cycle close them through the same charges/tax path as a TP or SL,
    so a manual exit cannot drift from the real cost model.
    """
    ids = payload.get("ids")
    if ids is None and payload.get("id") is not None:
        ids = [payload["id"]]
    if not ids:
        return {"ok": False, "error": "no ids supplied"}
    try:
        want = {int(i) for i in ids}
    except (TypeError, ValueError):
        return {"ok": False, "error": "ids must be integers"}

    open_ids = {int(t["id"]) for t in state.open_trades()}
    unknown = sorted(want - open_ids)
    target = sorted(want & open_ids)
    if not target:
        return {"ok": False, "error": "none of those ids are open",
                "open": sorted(open_ids)}

    queued = sorted(set(state.get_meta("force_close", []) or []) | set(target))
    state.set_meta("force_close", queued)
    state.append_activity("INFO", {"msg": f"manual close requested for {target}"})
    return {"ok": True, "closing": target, "not_open": unknown,
            "note": "settles on the next engine cycle at the live mark"}


@app.post("/control/sources")
async def control_sources(payload: dict):
    """Restrict which venues the desk trades: india, us, crypto.

    An empty list means all venues. Note that a venue being allowed does not
    make it tradeable -- the per-source market gate still applies, so US only
    trades 20:00-02:30 IST and india only 09:15-15:30 IST Mon-Fri.
    """
    # Kept in step with options.UNIVERSE rather than hard-coded: this set was
    # written when there were three venues and silently rejected "europe"
    # after six existed.
    valid = {str(i["source"]) for i in options.get_universe()}
    raw = payload.get("sources")
    if raw is None:
        return {"ok": True, "sources": state.get_meta("sources", []) or [],
                "meaning": "empty = all venues"}
    if isinstance(raw, str):
        raw = [raw]
    picked = [str(x).lower().strip() for x in raw]
    bad = [x for x in picked if x not in valid]
    if bad:
        return {"ok": False, "error": f"unknown source(s): {bad}",
                "valid": sorted(valid)}
    state.set_meta("sources", picked)
    state.append_activity("INFO", {"msg": f"venues set to {picked or 'all'}"})
    return {"ok": True, "sources": picked,
            "sessions": options.session_status()}


@app.post("/control/pause")
async def pause():
    await engine.stop()
    return {"ok": True, "state": "paused"}


@app.post("/control/resume")
async def resume():
    await engine.start()
    return {"ok": True, "state": "running"}


@app.post("/control/reset")
async def reset(keep_model: bool = False):
    await engine.stop()
    p = state.DB_PATH
    if p.exists():
        p.unlink()
    if not keep_model:
        get_model().reset()
        get_sae().reset()
    state.init_db(initial_balance=INITIAL)
    await engine.start()
    return {"ok": True, "state": "reset", "kept_model": keep_model}


@app.websocket("/ws")
async def ws(ws: WebSocket):
    await hub.register(ws)
    try:
        await ws.send_text(json.dumps({"kind": "SNAPSHOT", "body": _snapshot()}, default=str))
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(ws)
