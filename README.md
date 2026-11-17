# Argona

A paper-trading research desk for Indian F&O and world markets, built to answer
one question honestly: **does this strategy actually have an edge?**

So far, across 38+ strategies, 6 venues and 23 instruments, the answer has been
**no** — every time. This repository is the harness that produced that answer
and the log that records it.

> **Paper only.** Argona never sends an order to a broker. There is no
> order-placing code path, and the MCP server deliberately exposes no order
> tool. Market data in, simulated fills out.

---

## Why a negative result is the point

Most retail trading projects start from a strategy and look for evidence that
it works. That ordering is what loses money: with enough parameters, enough
instruments and enough date ranges, something always looks profitable.

Argona is built the other way round. A signal has to survive a gauntlet before
it is allowed to trade:

| Test | What it catches |
|---|---|
| **Side-matched random baseline** | A strategy that is just long a rising market. The baseline draws random entries with the *same long/short mix*, so beating "random" means beating the drift too. |
| **Permutation p-value** | Effects that are the right sign but inside the noise. |
| **Split-half replication** | Fits to one half of history. Run on the first half, tested unchanged on the second. |
| **Bonferroni correction** | The multiple-comparisons trap. Search 48 geometries, find 5 positive, and that is what noise looks like — not a discovery. |
| **Buy-and-hold comparison** | Strategies that underperform doing nothing. |
| **Exact geometry map** | Whether the *exit structure alone* is profitable before any signal is applied. |
| **Break-even arithmetic** | `p = (SL + cost) / (TP + SL)`. A geometry that needs a 68% win rate is rejected before it burns a backtest. |

Nothing is armed on a backtest alone, and no test is loosened to let a
candidate through.

### What that produced

```
Geometry map, crypto, 1h bars, every bar, both sides, no signal:
  BTC   0 of 48 (TP/SL, horizon) cells profitable
  ETH   0 of 48
  range -0.033% to -0.085% per trade

The execution structure costs ~0.05% per round trip whatever the geometry.
A signal must find MORE than that before it breaks even. None has.
```

```
Momentum scalp (TP 1.0% / SL 0.6%), 11 instruments, 6 months of 1h bars:
  break-even win rate  40.6%
  best observed TP rate 35.5% (SOL)    worst 19.5% (GOLD)
  0 of 11 pass, uncorrected
  5 of 11 significantly WORSE than a random entry
```

Full working notes, including the mistakes and the corrections, are in
[`RESEARCH_LOG.md`](RESEARCH_LOG.md).

---

## What it runs on

| Venue | Instruments | IST hours |
|---|---|---|
| Asia | Nikkei, Hang Seng, KOSPI, ASX 200, Straits Times | 04:30 – 14:30 |
| India (NSE) | Nifty, Bank Nifty, Fin Nifty | 09:15 – 15:30 |
| Europe | DAX, FTSE, CAC | 13:30 – 22:00 |
| US | SPY, QQQ, NVDA, TSLA | 20:00 – 02:30 |
| Commodities | Gold, Crude, Silver (Globex) | ~23h |
| Crypto | BTC, ETH, SOL, XRP, DOGE | 24/7 |

Every venue keeps its own exchange timezone, its own session gate and its own
lunch break. All prices are converted to INR on the way in, each currency pair
checked against a plausibility band — a bad FX print silently rescales a whole
venue otherwise.

Settlement runs the real **India F&O charge model** (STT, brokerage, exchange
txn, GST, SEBI, stamp) plus 30% tax on cumulative net gains, so losses offset
gains rather than each winner being taxed in isolation. The ledger shows
net-of-tax, never gross.

---

## Architecture

```
FastAPI :4110  ──  engine loop (~18s)  ──  SQLite
    │                   │
    │                   ├── strategy registry (pluggable)
    │                   ├── 7-agent committee (gatekeepers can veto;
    │                   │   predictors vote but carry zero weight until
    │                   │   20 scored votes each)
    │                   ├── online model + sparse autoencoder
    │                   └── two-leg pair book (z-score)
    │
Next.js :3110  ──  dashboard, 1s KPI push over WebSocket
launchd        ──  supervisor with HTTP health checks, survives reboot
```

**The agent committee splits responsibilities deliberately.** Gatekeepers
(cost, risk, validation) can veto a trade outright. Predictors (trend, pattern,
momentum, volatility) only vote, and their weight is `max(0, hit_rate − 0.5) ×
2 × min(1, n/100)` — which is zero until they have been scored on real
outcomes. An agent cannot influence a trade on the strength of its own opinion.

**A local LLM (Ollama, loopback only) can propose strategies but cannot
decide.** It emits parameter sets as grammar-constrained JSON; break-even
arithmetic is computed in Python, not by the model; every proposal returns
marked `UNTESTED`. On its first nine proposals, zero passed. It also misread
`0.08%` as `8%` and called a 6th-percentile ATR tape "higher than usual
volatility" — which is exactly why it is kept out of the execution path.

---

## Run

```bash
cd backend && python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 4110 --app-dir .
```

```bash
cd web && npm install && npm run dev        # http://localhost:3110
```

Optional, for the proposal generator:

```bash
ollama pull phi3
```

Copy `.env.example` to `.env` for broker market-data credentials. They are
optional — Argona runs on free data without them, and the credentials are for
**reading quotes only**.

Config via env: `ARGONA_INITIAL`, `ARGONA_RISK_PCT`, `ARGONA_CYCLE_SEC`,
`ARGONA_TAX_RATE`, `ARGONA_MAX_OPEN`, `ARGONA_LLM`.

---

## Honest limits

- **No edge has been found.** If you are looking for a profitable strategy to
  run, this repository does not contain one. It contains the apparatus that
  keeps proving there isn't one yet.
- Free data is delayed (US ~15 min). This is research, not low-latency
  execution.
- A handful of live paper trades proves nothing in either direction. The live
  ledger is for testing the execution path, not for evaluating a strategy.
- `index_hold` is **not a strategy**. It is long equity indices and captures
  market beta (~+0.03%/day), and it exists only because every timing signal
  measured here has negative expectancy while churn costs ~0.05% per round
  trip. It is labelled that way in the code. Beta runs both ways: held
  overnight on 2026-11-16 it lost Rs 2,514 as DAX, FTSE and SPY all fell
  together.
- The active strategy is set at runtime and persists in the database, so
  whatever is running is whatever was last selected — the code has no
  meaningful "default" beyond the fallback used when the key is absent.

## Licence

MIT.
