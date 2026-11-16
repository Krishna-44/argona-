#!/usr/bin/env python
"""Argona MCP server — exposes the F&O research desk as tools.

Read-only and research-only by design. There is deliberately NO order-placing
tool: Argona is a paper/research platform and real execution stays with you.

Run (stdio):
    backend/.venv/bin/python backend/mcp_server.py

Register with Claude Code:
    claude mcp add argona -- \
      /Users/krishnagoyal/ai-trading-os/backend/.venv/bin/python \
      /Users/krishnagoyal/ai-trading-os/backend/mcp_server.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT.parent / ".env", override=False)

from mcp.server import MCPServer  # noqa: E402

from app import angelone, backtest, candlestick  # noqa: E402

mcp = MCPServer(
    name="argona",
    instructions=(
        "Argona F&O research desk. Analyse charts (candlestick patterns, "
        "support/resistance, ATR stops), backtest strategies with real India "
        "F&O costs, validate them out-of-sample, and read the India option "
        "chain. Research only — this server cannot place orders."
    ),
)


@mcp.tool()
def analyze_chart(symbol: str, period: str = "6mo", interval: str = "1d") -> dict:
    """Candlestick patterns + support/resistance + ATR stop-loss/take-profit.

    Indian names resolve to NSE automatically (RELIANCE -> RELIANCE.NS).
    """
    df = backtest.fetch_history(symbol, period, interval)
    if df is None:
        return {"ok": False, "error": f"no data for {symbol}"}
    return {
        "ok": True, "symbol": symbol.upper(),
        "last_close": round(float(df["Close"].iloc[-1]), 2),
        "patterns": candlestick.summary(df),
        "levels": candlestick.levels(df),
    }


@mcp.tool()
def backtest_strategy(symbol: str, strategy: str = "candlestick",
                      mode: Literal["futures", "options"] = "futures",
                      period: str = "1y", interval: str = "1h") -> dict:
    """Backtest a strategy on history with real India F&O costs.

    strategy: candlestick | ema_crossover | donchian | momentum | supertrend | rsi | live
    mode=futures trades the underlying. mode=options models an ATM option
    scalp, which is structurally far worse (~40x leverage + theta).
    """
    r = (backtest.run_futures(symbol, strategy, period, interval) if mode == "futures"
         else backtest.run(symbol, None, period, interval, strategy=strategy))
    if r is None:
        return {"ok": False, "error": f"no data for {symbol}"}
    d = r.as_dict()
    d.pop("equity", None)
    return {"ok": True, "mode": mode, "strategy": strategy, "result": d}


@mcp.tool()
def walk_forward(symbol: str, period: str = "1y", interval: str = "1h",
                 splits: int = 4) -> dict:
    """Walk-forward validation: optimise in-sample, validate out-of-sample.

    Only the out-of-sample numbers count as evidence of edge.
    """
    wf = backtest.walk_forward(symbol, backtest.default_grid(), period, interval, splits)
    return {"ok": bool(wf), "result": wf}


@mcp.tool()
def compare_strategies(symbol: str, mode: Literal["futures", "options"] = "futures",
                       period: str = "1y", interval: str = "1h") -> dict:
    """Run every catalog strategy on one symbol and rank them by return."""
    rows = []
    for s in ["live"] + backtest.CATALOG:
        r = (backtest.run_futures(symbol, s, period, interval) if mode == "futures"
             else backtest.run(symbol, None, period, interval, strategy=s))
        if r:
            rows.append({
                "strategy": s, "trades": r.trades, "win_rate": round(r.win_rate, 1),
                "return_pct": round(r.return_pct, 2),
                "profit_factor": round(r.profit_factor, 2),
                "max_dd_pct": round(r.max_dd_pct, 1),
            })
    rows.sort(key=lambda x: -x["return_pct"])
    return {"ok": True, "symbol": symbol.upper(), "mode": mode, "results": rows}


@mcp.tool()
def option_chain(name: str = "NIFTY", expiry: str | None = None,
                 around: int = 10) -> dict:
    """India F&O option chain (NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY).

    Structure comes from AngelOne's public instrument master. Live premiums
    require SmartAPI credentials in .env. Market data only — never orders.
    """
    spot = None
    df = backtest.fetch_history(name, "5d", "1d")
    if df is not None:
        spot = float(df["Close"].iloc[-1])
    return angelone.option_chain(name, expiry, around, spot)


@mcp.tool()
def broker_status() -> dict:
    """Which AngelOne SmartAPI credentials are configured (never their values)."""
    return angelone.creds_status()


if __name__ == "__main__":
    mcp.run(transport="stdio")
