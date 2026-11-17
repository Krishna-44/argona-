# Argona F&O — Research Log

Running record of what has been tested on NSE India F&O, what survived, and
what did not. Negative results are kept, not deleted — they are most of the
value here, and re-testing a dead idea because nobody wrote it down is the
most expensive mistake available.

**Standing rules** (earned, each from a specific failure):

1. **Check saturation before reading any sequential backtest.** The desk holds
   one position, so a dense signal gets sampled, not followed. At 10% signal
   utilisation the equity curve measures the position limit.
2. **Compare against random entry, not against zero.** Every TP/SL scheme has
   a built-in win rate. "It won 55% of the time" is not evidence.
3. **A positive result must replicate on a non-overlapping period** before it
   is reported as an edge.
4. **Sweep every unobservable assumption.** If the sweep spans more than the
   result, the headline is the assumption.
5. **Never show a desk ledger without its random-desk p-value and sampled %.**
   A 30–50 trade ledger of a zero-edge signal is positive about half the time.

---

## Status: no validated edge

**~33 strategies tested** across multiple geometries, timeframes and periods.
**Zero have replicated out of sample.**

| Family | Tested | Survived |
|---|---|---|
| Indicator signals (EMA, Donchian, RSI, Supertrend, momentum, candlestick, UT score) | 8 | 0 |
| Hybrids (ut_score × candlestick, 7 formulations) | 7 | 0 |
| NSE session structure (ORB, gap, first/last hour, VWAP, expiry-day) | 8 | 0 |
| Calendar buckets (weekday, turn-of-month, expiry week, holidays) | 10 | 0 |
| Option selling (short straddle at real India VIX) | 3 | 0 (assumption-dominated) |
| Regime switching (variance ratio) | — | 0 (not predictable, and oracle doesn't pay) |

---

## Rapid mode — built, with the price on the label

`rapid_underlying`: 0.15% target / 0.10% stop on the underlying, 20s cooldown
(vs 0.60% / 0.40%, 180s). Toggled by the ⚡ RAPID button.

Tightening the bands does not change the cost, so it changes the win rate the
desk needs. Exactly:

> p × (TP − c) = (1 − p) × (SL + c)  →  **p = (SL + c) / (TP + SL)**

| | target | stop | cost | **win rate needed** |
|---|---|---|---|---|
| NIFTY normal | 0.60% | 0.40% | 0.070% | **47%** |
| BANKNIFTY normal | 0.60% | 0.40% | 0.069% | **47%** |
| SPY normal | 0.60% | 0.40% | 0.133% | **53%** |
| **NIFTY RAPID** | 0.15% | 0.10% | 0.070% | **68%** |
| **SPY RAPID** | 0.15% | 0.10% | 0.133% | **93%** |

Measured win rates anywhere in this project: **44–55%**.

So rapid mode needs 68% on NIFTY and 93% on SPY, against a demonstrated
44–55%. It is not a faster route to the same result — it is a different and
much harder bet, and the arithmetic above is exact rather than estimated.

Every signal now reports this live: `round trip 0.07% = 46% of the 0.15%
target · needs 68% win rate to break even`.

**New hard block:** when the round trip exceeds the target the trade loses
even when it is right, and no win rate rescues it. That is now refused
outright — it caught BTC at normal bands needing a **121%** win rate (cost
0.81% against a 0.60% target).

A 20-path simulation on driftless random walks had both modes losing roughly
their costs (−₹246 normal, −₹254 rapid, 0% of paths profitable either way).
That harness did not reproduce the expected trade-count gap, so the arithmetic
above — not the simulation — is the evidence.

## The benchmark that matters: doing nothing

Every strategy here is 50–71% long and the markets rose. So the real question
is not "did it make money" but "did it beat buying once and sitting still" —
which costs one round trip and needs no signal, no model and no uptime.

Desk simulation (one position at a time) vs buy-and-hold, 5 years daily, both
net of futures-rate costs:

| market | buy & hold | best of 5 strategies | beats hold? |
|---|---|---|---|
| NIFTY | **+38.8%** | −15.2% (candlestick) | no |
| BANKNIFTY | **+57.7%** | +27.0% (ema_crossover) | no |
| SPY | **+72.5%** | +41.2% (candlestick) | no |
| QQQ | +77.5% | **+89.3%** (candlestick) | yes |
| BTC | +2.5% | **+70.7%** (ut_score) | yes |
| ETH | −59.0% | **+71.7%** (momentum) | yes |

In equities, **buy-and-hold wins outright** — the trading destroys value.

The crypto wins look spectacular until you notice what they are measured
against: ETH buy-and-hold lost **59%**. In a falling market, anything that is
sometimes flat or short beats holding. That is reduced exposure, not skill.

Tested directly against random desks of the same trade count:

```
BTC  ut_score   177 trades   Rs 505,151    7.2% of random desks did better   p=0.072
BTC  momentum   207 trades   Rs 173,035   25.9%                              p=0.259
ETH  momentum   269 trades   Rs 511,939   23.8%                              p=0.238
ETH  ut_score   243 trades   Rs 380,085   31.9%                              p=0.319
```

**All four indistinguishable from random.** A desk making the same number of
random trades lands in the same place.

And the table above is already generous: "best of 5" is selected with
hindsight on the same data, so it is an upper bound on what was achievable,
not something anyone could have traded.

## Trading the underlying instead of options

Built and live as strategy `underlying_momo`: the **same momentum signal**,
traded on the index/stock/coin directly instead of an ATM option. Same signal
on purpose — it isolates the instrument choice from the signal, which is the
whole point.

Modelled as a delta-1, no-expiry quote, so `leverage()` returns exactly 1.0
and the existing risk machinery converts a 0.40% intended move into a 0.40%
stop with no special casing. No theta, no gamma, P&L linear in price.

Verified end to end on live data:

```
SPY  LONG   qty 1       Rs 71,014.20 -> Rs 71,433.14  TP
     gross Rs 418.94   charges Rs 66.15 (0.093% of notional)   net Rs 246.95
BTC  LONG   qty 0.019   Rs 6,301,260 -> Rs 6,338,434  TP
     gross Rs 706.30   charges Rs 79.15 (0.066% of notional)   net Rs 439.00
SPY  SHORT  price fell 1%                              TP
     gross Rs 418.94                                    net Rs 246.95
```

**Charges 0.066–0.093% of notional, against 4.5–14% on options.**

Five things had to be fixed for this to be correct:

1. **Short P&L was inverted.** Every rule downstream (TP above entry, SL
   below, gross = exit − entry) assumes the mark rises when the position is
   right — true for an option premium, false for a short. The mark is now
   mirrored around entry for short underlying positions, which is exact
   because underlying P&L is linear.
2. **Slippage was 1% of *premium*.** Applied to an underlying that is 1% of
   the *price* — ~250 NIFTY points — which would have erased the entire cost
   advantage. Underlying fills now cross at 1bp.
3. **Capital cap used raw notional.** Futures are margined (~12%), so a
   single NIFTY lot looked like a ₹16L position against a ₹5L book. The cap
   is now on capital actually tied up.
4. **The trained model gated on out-of-distribution inputs.** It learned on
   option features (IV, theta, dte, moneyness); an underlying quote has
   iv=0, dte=365, strike=spot. It scored a clean 100%-aligned trend at 25%.
   It still *trains* on these samples but no longer gates them.
5. **A `segment` column** so futures trades are billed the futures rate card,
   not the options one.

**A real constraint, surfaced rather than hidden:** one NIFTY futures lot is
65 × spot ≈ ₹16.3L notional, ~₹1.95L margin — **39% of a ₹5L book**, over the
25% cap. The desk refuses it and now says so in the activity log instead of
skipping silently. US shares and fractional crypto size fine.

**What this does and does not do.** It removes a 100x cost handicap. It does
not create an edge — the signals still have none, and the cross-market test
below was run on underlying-style outcomes that never had the option cost in
them. Expect a flatter curve, not a rising one.

## The cost wall — why "just trade better" cannot work here

Round-trip cost measured live, with real lot sizes (spread + 2% slippage +
charges), against the same trade on the underlying:

| | option round trip | underlying round trip | ratio |
|---|---|---|---|
| NIFTY | **5.0%** of premium | 0.049% | 102x |
| BANKNIFTY | **4.5%** | 0.049% | 92x |
| SPY | **4.0%** | 0.113% | 35x |
| ETH | **11.9%** | 0.073% | 163x |
| BTC | **13.6%** | 0.047% | 287x |

**Best measured edge anywhere in this project: ~0.05% per trade, and not
statistically significant.** To profit on a crypto option you must beat 12–14%
per round trip. Nothing here is within two orders of magnitude of that.

The desk was buying short-dated crypto options — the single most expensive
instrument on the board. The ETH trade that lost ₹6,398 paid an 11% spread
before the signal had any chance to be right or wrong.

**Fixed.** Added a `COST` gate: round-trip cost is computed per instrument and
anything above 6% of premium is refused with the reason shown.

```
NIFTY      india   PASS   round trip 5.0% of premium
BANKNIFTY  india   PASS   round trip 4.5%
SPY        us      PASS   round trip 4.0%
TSLA       us      PASS   round trip 3.3%
BTC        crypto  BLOCK  12.2% > 8% (eats the scalp)
ETH        crypto  BLOCK  spread too wide
NVDA       us      BLOCK  crossed book (bid 292.14 >= ask 282.72) — stale quote
```

Two more bugs found while measuring this:

- **Crossed books passed the spread gate.** yfinance returns crossed quotes
  (ask < bid) for US names outside market hours, which makes `spread_pct`
  *negative* — and a negative value sails through a `spread_pct > max` test.
  The desk would have traded on broken prices. Now rejected explicitly, and
  it caught NVDA live on the very next run.
- **US contract multiplier was 1, not 100.** A listed US option covers 100
  shares, so the flat per-order fee looked like ~20% of notional instead of
  ~0.2% and wrongly blocked every US name.

### Honest consequence

On a zero-edge system, the only reliable lever is **trading less and cheaper**.
The cost gate does that. It does not create an edge, and the expected result
is a balance that drifts near flat minus small costs — not one that climbs.

If the goal is a genuinely lower cost base, the measurement says trade the
**underlying, not short-dated options**: ~100x cheaper per round trip.

## Why it loses: the stop was inside the noise

A live overnight crypto trade stopped out **13 seconds after entry** for
−₹6,398. Not a bug — and chasing it produced the most important structural
finding in this project.

An ATM short-dated option is enormously levered to its underlying. Measured
across the live universe:

| | leverage | old 4%-of-premium stop = spot move of |
|---|---|---|
| SPY | 162x | **0.025%** |
| NIFTY | 150x | **0.027%** |
| QQQ | 88x | 0.046% |
| FINNIFTY | 79x | 0.050% |
| BANKNIFTY | 75x | 0.054% |
| BTC | 45x | 0.089% |
| TSLA | 39x | 0.103% |
| ETH | 36x | 0.111% |
| NVDA | 29x | 0.140% |

The desk's stop was set as a flat **4% of premium**. On NIFTY that is a stop
on a **0.027% index move** — a few points, comfortably inside the bid-ask
spread. Every exit was microstructure noise, paid for with a full round trip:
1% entry slippage plus ~0.2% charges.

**That is unwinnable regardless of signal quality**, and it explains the whole
research result — the uniform ~0.03%/trade bleed in the geometry map, the
17/18 losing cells, and the ETH stop-out in 13 seconds on a 0.111% move.

**Fixed.** Risk is now defined as a move in the UNDERLYING and converted to a
premium band through the option's own leverage (`options.leverage()` /
`premium_band()`): SL 0.40% spot, TP 0.60% spot. Positions beyond 200x
leverage are skipped — past that the option *is* the stop.

What changed in practice, on NIFTY:

| | old | new |
|---|---|---|
| SL band | 4% of premium | **60% of premium** (= 0.40% index) |
| TP band | 6% of premium | **90% of premium** (= 0.60% index) |
| position | ₹1,20,711 (26 lots) | **₹4,653 (1 lot)** |
| actual risk | ~₹1.2L exposed to noise | **₹3,546 = 0.71% of equity** |

The old sizing claimed to risk 1% but staked a ₹1.2L position on a
noise-level stop. The new one risks what it says it risks.

## Multi-day horizons — the last untested hypothesis, also empty

Everything else here is intraday, where a round trip costs ~0.03% against
moves of ~0.3%. At a 5–20 day horizon the same cost works against 2–6% moves,
so it should matter ~10x less. This is also the classic trend-following claim.
Tested on **5 years of daily bars** — the best sample in the project — across
six instruments and three venues, split in half.

At first it looked like something:

| horizon | mean net % per signal | cells positive |
|---|---|---|
| 5d | −0.022% | 32% |
| 10d | +0.075% | 57% |
| **20d** | **+0.106%** | **64%** |

A clean monotone trend, positive at 20 days, against a random-entry baseline
that was *negative* (−0.028%). It is not edge.

**The baseline was wrong.** `geometry_map` fires random entries 50/50
long/short, so market drift cancels inside it. The strategies are **50–71%
long** in markets that rose over five years, so they collect that drift. The
correct comparison is a baseline with the **same long/short mix**, which is
what `event_study` uses.

Against the side-matched baseline, on the full **unselected** 30-cell grid:

```
mean edge vs side-matched random : +0.054%
cells with positive edge         : 18/30 (60%, binomial p ~ 0.18)
p <= 0.05                        : 1/30   (expect 1.5 by chance)
p <= Bonferroni 0.0017           : 1/30
```

**One significant cell where chance predicts 1.5.** That one — QQQ `ut_score`
— fails split-half replication anyway (H1 p=0.221, H2 p=0.001: the whole
result lives in one half).

A warning about how this nearly went wrong: an intermediate check showed
"8/8 cells with positive edge, mean +0.442%". Those 8 cells had been **picked
from the positive-in-both-halves list**, so they were positive by
construction. Selecting cells and then reporting how positive they are
measures the selection. Only the unselected grid above counts.

**Methodological rule added: a long-biased strategy must be compared against a
baseline with the same directional mix.** A 50/50 benchmark silently credits
the strategy with market drift and turns beta into apparent alpha.

## Cross-market check: the losses are universal

Three representative strategies (trend / contrarian / pattern) × six
instruments across three venues, 1-hour bars over 2 years, each split in half.
Pre-specified, 18 cells.

| | sign flips between halves | positive in BOTH halves |
|---|---|---|
| India (NIFTY, BANKNIFTY) | 0/6 | **0/6** |
| US (SPY, QQQ) | 1/6 | **0/6** |
| Crypto (BTC, ETH) | 0/6 | **0/6** |
| **Total** | 1/18 | **0/18** |

**17 of 18 cells lose money in both halves.** Mean net per signal ranges
−0.007% to −0.092%, which is the order of the execution cost drag measured
independently in the geometry map.

Correlation between half-one and half-two performance is **+0.463**, and that
number is a trap: it is persistence of *losing*, not exploitable persistence.
Consistently bad strategies stay consistently bad. A correlation on its own
says nothing without the sign of the levels.

**This also corrects an earlier reading.** The "trend/contra regime mirror" was
measured on month-long 5-minute samples at a 96-bar horizon. On two years of
hourly data across six instruments there is no mirror — just uniform negative
returns. The alternation was a small-sample artifact; the cost drag is the
durable part.

That the result is identical in Indian indices, US equities and 24/7 crypto
says this is a property of the strategy class, not of any one market.

## What IS real

### India variance risk premium — measured, not assumed

India VIX (`^INDIAVIX`, available on yfinance) is the market's own 30-day ATM
implied vol. Compared against the realised vol that actually followed:

| Horizon | Implied | Realised | VRP | Windows positive | Worst |
|---|---|---|---|---|---|
| 7d | 14.12% | 11.91% | **+2.20%** | 78.5% | −18.6% |
| 14d | 14.09% | 12.30% | **+1.79%** | 74.8% | −13.4% |
| 30d | 14.07% | 12.50% | **+1.57%** | 72.4% | −13.1% |

Both sides are observations. This is the one option-selling result in the
project that is **not circular** — earlier straddle work assumed an IV, priced
the premium off that assumption, and then "found" that selling was profitable.

**But it is not yet tradeable here.** Converting VRP into P&L needs the actual
premium a weekly straddle trades at. VIX is a 30-day measure, so pricing a
7-day option with it is an unobservable assumption — and the sweep is fatal:

- 7 DTE: returns span **126 percentage points** across iv_scale 0.8–1.3
  (−99.9% at 0.8 — account wiped). Verdict: NOT USABLE.
- 30 DTE: **monotonic** +7.7% → +44.4% straight up the sweep. Verdict: ARTIFACT.

Real weekly option quotes would settle this. That is a SmartAPI credentials
problem, not a modelling problem.

### The trend/contra regime mirror — real, but not exploitable

Every trend-following variant is strongly positive in one period and strongly
negative in the next; every contrarian variant is the exact mirror. At horizon
96 on NIFTY: `hybrid_strict` +0.309 / −0.127, `hybrid_contra` −0.318 / +0.006.
The effect grows with horizon (±0.1%/trade at h=48 → ±0.4% at h=96).

Tested directly and it does **not** survive:

- **Not predictable.** Trailing variance ratio vs forward variance ratio:
  correlations 0.041, −0.102, −0.489, −0.052, −0.071, −0.113. Directional hit
  rate is **below the majority-class baseline in all six tests**.
- **Wouldn't pay even if it were.** always_trend −0.038, always_contra −0.014,
  realizable switch −0.052 (worse than both), and the **oracle** — knowing the
  true forward regime — is still **negative** at −0.010. Oracle gain: +0.004%.

So the mirror is real but variance ratio is not what drives it, and a perfect
VR regime call would not make money. Whatever varies between periods, it is
not "trending vs mean-reverting" in the VR sense.

---

## Dead ends (do not re-test without new data)

**Execution geometry.** 0 of 48 (horizon, TP, SL) combinations break even at
random entry across 4 datasets. The structure leaks 0.02–0.05%/trade
everywhere. Least-bad is wide-TP/tight-SL (rr 2–3, long horizon).
*A previous "break-even geometry" finding was my own sampling error — 400
random draws, error bar larger than the effect. `geometry_map` now averages
every bar exactly.*

**Calendar effects.** 1,234 NIFTY sessions (2021-11 → 2026-11), 10 buckets,
permutation null, Bonferroni threshold 0.005. Best p = 0.0248 (mid_month, 5d).
**0 significant.** Split-half sign agreement **4/10 — below chance**.

**Session structure.** 1h/2y NIFTY + BANKNIFTY, split in halves. 0/8 beat
random on all four. Best (`expiry_day_momo`) 1/4.

**The ledger trap.** `rsi_INV` on NIFTY produced a ledger showing +1.42% and
+0.97% in consecutive months, 51–55% win rate, profit factor >1.1 — i.e. it
looked exactly like a real track record. It is noise: the desk took 7.0% and
6.6% of signals, t = 0.55 and 0.40, and **16% / 14% of random desks of the
same size did better**. `/signal/ledger` now always returns its significance
block.

---

## Hardening for Monday's open (dry run)

The India trade path had **never been executed** — the engine had only ever
opened positions on US/crypto quotes with fractional lots. Dry-run against a
scratch DB with the market-hours gate forced open found two blockers that
would have surfaced live at 09:15 IST:

**1. The liquidity check rejected every signal, permanently.** A model-priced
quote carries no volume or open interest — not "zero liquidity", just no data
without a live chain. Tested against a volume floor, every signal came back
`thin (vol 0, OI 0)`. The desk would have stood down forever, not just
overnight. Fixed: the check is waived for a nearest-expiry ATM **index**
option (NIFTY/BANKNIFTY weeklies are among the most traded contracts in the
world) and the signal is labelled `LIQUIDITY unverified`, not passed off as
measured. Anything else still requires a real chain.

**2. The mark created P&L out of nothing — a money printer.** The dry run
printed the identical `+Rs 4,100.55` profit on every single cycle. Cause: the
entry priced IV from the underlying's own history, while `requote_mark`
re-estimated IV from a realised-vol cache. When the two disagreed the mark
jumped the instant the position opened and tripped TP on estimation noise
rather than a price move. Fixed: IV is now **backed out of the entry premium**
by bisection and pinned, so the mark can only move with spot and time.

Verified after the fix:

| | Result |
|---|---|
| Sizing | 26 whole lots × 65 = 1,690 units, ₹1.2L notional (24% of ₹5L, under the 25% cap) |
| Spot unchanged | mark holds at entry ₹71.59, **no exit**, balance flat |
| Spot +1.5% | exactly **one** TP: ₹71.59 → ₹75.12, gross ₹5,976, charges ₹282, tax ₹1,708, **net ₹3,986** |

**3. The scanner was suggesting contracts that do not exist.** Two separate
invented numbers, both fixed by using the public instrument master instead of
a model:

- *Wrong expiry.* `_pick_expiry` substituted the NIFTY calendar for every
  non-index symbol, so it offered "TCS 17NOV2026 (3 DTE)". NIFTY runs
  weeklies; **BANKNIFTY and all stocks are monthly-only** since NSE withdrew
  the other weeklies. TCS's real next expiry is 23NOV2026 (9 DTE). Each
  symbol now reads its own calendar.
- *Wrong strike.* `op.snap_strike` assumes a step size and gets it wrong —
  it produced RELIANCE **1335** and ICICIBANK **1605** when both trade a
  10-rupee ladder (1330/1340, 1600/1610). Added
  `angelone.strike_ladder()` / `snap_real_strike()`, which snap to a real
  strike and step along the real ladder. Applied to both the scanner and the
  live desk (`india_atm`).

Verified: every scanned strike now exists in the master (0 invalid), every
strike is on the correct side of spot for its label, and the live desk prices
NIFTY 25150 / BANKNIFTY 61000 / FINNIFTY 27850 on real expiries with real lot
sizes (65 / 30 / 60).

Still unverified until credentials land: real premiums, real spreads, real
liquidity. The desk trades a *model* of an NSE option, and says so.

## The binding constraint

Every intraday conclusion rests on **60 days of 5-minute bars** — yfinance's
cap. Daily data has no cap (5 years tested above, and it came back empty).
Hourly reaches 2 years.

AngelOne SmartAPI returns **years** of 5-minute history and live option
premiums. Three lines in `backend/.env` unlock both the intraday sample size
and the weekly straddle premiums that would settle the VRP question:

```
ANGELONE_CLIENT_CODE=
ANGELONE_PIN=
ANGELONE_TOTP_SECRET=
```

(The SmartAPI key shown in chat earlier should be rotated.)

---

## Honest bottom line

The measurement framework is now good enough to tell a real edge from a
flattering backtest — it has caught a circular straddle, a sampling-error
geometry, a saturated harness, a one-month regime artifact and a convincing
but meaningless ledger, several of which were my own.

What it has not found is an edge. On the data available, **nothing in this
catalog beats entering at random after India F&O costs.** The VRP is the one
genuinely promising lead and it needs real option quotes to pursue.

## Vertical credit spreads — the method finally works, the data runs out

Every naked-option result here died the same way: the premium is model-priced,
so the answer tracked the IV assumption and the sensitivity sweep correctly
called it an artifact. A vertical spread is different in two ways:

* Both legs share the IV, so much of the assumption cancels. Measured on
  NIFTY 5 DTE across IV 8–20%: a naked put's value swings **106%**, the
  spread's credit **44%**. Better, not immune.
* **Max loss is fixed by the strike width, not by IV.** The risk is known
  exactly even when the premium is not — which is what makes this honest to
  test without a live chain.

Backtested on 2 years of NIFTY with the premium priced at the **real India
VIX**:

| direction | dte | trades | win% | net | return | PF | t |
|---|---|---|---|---|---|---|---|
| bull | 7 | 61 | 65.6 | −₹20,552 | −4.11% | 0.88 | −0.45 |
| bull | 30 | 15 | 66.7 | −₹743 | −0.15% | 0.98 | −0.03 |
| bear | 7 | 61 | 57.4 | −₹32,404 | −6.48% | 0.82 | −0.70 |
| **bear** | **30** | **15** | **60.0** | **+₹7,358** | **+1.47%** | **1.25** | **0.41** |

The classic credit-spread shape: win often, lose big, profit factor near 1.

**The circularity guard passes for the first time.** Bear 30d across iv_scale
0.8–1.3 returns 1.32% → 1.59% — a span of **0.27%**, verdict *robust to the
IV assumption*. The 7d version correctly fails (span 12.5%, monotonic,
ARTIFACT) because it prices a weekly off a 30-day VIX. So **30 DTE priced at
the 30-day VIX is the one honest option test available here.**

**But the result is not edge, and cannot be made into one with this data:**

* t = 0.41 on 15 trades.
* Split-half fails outright: first half **−₹10,516**, second half **+₹21,028**.
  The entire result lives in one half.
* Power: at this mean/sd, **342 trades** would be needed for t > 1.96. Two
  years of India VIX at 30-day non-overlapping entries can yield **~24**.

That last line is the finding. The question is not open — it is **unanswerable
at this sample size**, and no amount of further testing on the same two years
changes that. It needs a longer VIX history or a real option-quote archive.

## NIFTY/BANKNIFTY pair — the strongest candidate so far, still not validated

Every strategy tested before this is **directional**, and the cross-market work
showed those returns are largely **beta**: 50–71% long in markets that rose,
with the edge vanishing against a side-matched baseline. A pair removes that
entirely — long one index, short the other, so market direction cancels and
only the *relative* move matters. Both legs are futures, so the round trip is
~0.05% rather than the 4.5–14% an option structure pays.

Trade the BANKNIFTY/NIFTY ratio back to its rolling mean. 5 years daily.

| lookback | entry z | trades | win% | total | t | PF | halves |
|---|---|---|---|---|---|---|---|
| 40 | 2.0 | 35 | 60.0 | +8.87% | 0.88 | 1.44 | +6.2 / +2.7 |
| **60** | **2.0** | **27** | **51.9** | **+10.17%** | **1.17** | **1.69** | **+4.8 / +5.4** |
| 90 | 2.5 | 12 | 66.7 | +5.96% | 0.95 | 1.95 | +3.9 / +2.1 |

**9 of 9 parameter settings positive. 6 of 9 positive in both halves.** That
is a robustness profile nothing else here has produced.

**Permutation test** — do the z-score entry *times* beat random dates with the
same side mix?

```
192 signals   real +0.2104%/trade   random -0.0942%   edge +0.3047%   p = 0.0135
```

The first signal in this project to beat a matched random baseline. But:

* **9 configs were tested first**, so the Bonferroni bar is 0.0056. p=0.0135
  does not clear it.
* **Split-half kills it**: first half edge +0.0587% (p=0.3825, not
  significant), second half +0.5068% (p=0.0040). Positive in both, but the
  significance lives entirely in the second half — the same shape that
  invalidated QQQ `ut_score` earlier.
* Overlapping 14-day windows make every p-value here optimistic.

**Verdict: promising, not validated.** It is the best candidate found — market
neutral, cheap, robust across parameters, positive edge in both halves — and
it still fails the bar. It is not going into the live registry as validated.
The honest way to settle it is **forward testing**, which is genuine
out-of-sample data rather than more passes over the same five years.

## Position risk is set by the instrument, not by the risk target

The desk targets 1% of the book per trade. It hits that on neither instrument
class, and the two miss in opposite directions:

```
NIFTY SHORT   qty 65.0000   risk ₹6,554   1.35%   OVER
BTC   SHORT   qty  0.0164   risk ₹  400   0.08%   under
ETH   SHORT   qty  0.5851   risk ₹  400   0.08%   under
```

A 17x spread. Both ends are **structural, not bugs**:

* **India futures — forced UP.** An NSE lot is indivisible. One NIFTY lot is
  65 × ~25,245 = ₹16.4L notional, and a 0.40% stop on that is ₹6,554 ≈ 1.35%
  of a ₹4.85L book. There is no smaller position available.
* **Crypto spot — forced DOWN.** It is cash-funded (margin_rate 1.0) and
  capped at 25% of the book, so the largest position is ~₹1.2L and a 0.40%
  stop on it risks ~₹485 ≈ 0.1%. The 1% target is unreachable without
  leverage the account does not have.

The first India trade lost **₹8,326 = 1.67% of the book** against that 1.00%
target, and nothing on screen said it was going to.

Not papered over: every open position now reports `risk_amount` and
`risk_pct`, and the OPEN TRADES panel shows the real figure per row (red over
1.25%, muted under 0.5%). Equalising the two would mean varying the stop per
instrument, which changes the strategy's win rate and cost ratio — a real
trade-off, not a free fix, so it is being surfaced rather than silently
chosen.

## scalp_momentum — failed validation, activated anyway (and why)

From the user's `scalp` skill: TP 1.0% / SL 0.6%, max 3 positions, traded on
the underlying. Mirrored exactly into `backtest.catalog_signals` so the test
measures what the live desk fires on, not an approximation.

**Validation: FAILED.** Event study vs a side-matched random baseline, 6
instruments × 2 halves, 1h bars over 2 years:

```
cells 12 | positive edge 5/12 | mean edge -0.0048%
p <= 0.05          : 3/12   (expect 0.6 by chance)
p <= Bonferroni 0.0042 : 0/12
positive edge in BOTH halves : 0/6
```

NIFTY H1 and BANKNIFTY H1 both beat random (p=0.019, 0.011) — and both flip
negative in H2, one of them *worse* than random (p=0.997). The same
first-half/second-half shape that has invalidated every other candidate here.

**So it has no demonstrated edge. It was still activated, for a different and
narrower reason: it strictly dominates the strategy it replaced.**

Head to head on the same bars, each at its own live geometry:

| | mean net/trade | edge vs random | break-even win rate |
|---|---|---|---|
| `momentum` (was live) | **−0.0487%** | −0.0004% | 47.0% |
| `scalp_momentum` | **−0.0309%** | +0.0054% | **41.9%** |

The skill's 1.0/0.6 geometry is wider relative to the ~0.07% futures round
trip, so it needs a 41.9% win rate instead of 47.0% and bleeds ~37% less per
trade. Neither has edge; one loses less.

**This is a cost decision, not an edge claim.** The desk trades regardless
while the user practises, so running the variant with the better cost geometry
is strictly better than running the worse one. It is not evidence the strategy
works, and it is not being presented as such.

## 2026-11-16 — On-device LLM wired in (phi3 via Ollama), and the rapid-toggle bug

### The bug that was breaking strategy selection
`meta.strategy` kept reverting. Only one writer exists (`main.py:299`, the
`/strategy` POST), and it logs every change, so the activity table named the
culprit:

```
11:56:39  rapid_underlying     <- RAPID switch ON
11:57:42  underlying_momo      <- switch OFF, 63s later
11:32:30  scalp_momentum       <- armed, then silently wiped
10:50:34  rapid_underlying  ->  10:50:39 underlying_momo   (5 seconds)
```

`Header.tsx` hard-coded `"underlying_momo"` when RAPID was switched off,
discarding whatever was selected. `scalp_momentum` was armed at 11:32 and
destroyed by a toggle at 11:56 with nothing in the UI to show it. Fixed: the
toggle now stores the pre-RAPID strategy in a ref and restores exactly that.

Consequence to be honest about: every claim that "scalp's screen is working on
a quiet tape" was unfounded. Scalp was not selected. Its record remains 0W/1L
from a single ETH call stopped out at -Rs 6,398.50, and it has had no
opportunity to add to that.

### Local LLM — what it is and is not for
`app/llm.py`, Ollama on 127.0.0.1 only, loopback hard-coded so desk positions
and balances cannot leave the machine. phi3:latest (3.8B, Q4, 2.2 GB) is the
default; llama3 (8B, 4.7 GB) is opt-in via ARGONA_LLM because the box has 8 GB
shared with the API, Next.js and a browser.

The model is NOT in the execution path and must never be. It has no price data
the engine lacks, and a call costs 18-31s, which is fatal to a scalper whose
thesis is a 1.0% move. Two material errors in three test calls, both caught:

1. Read `risk_pct_of_book: 0.08` as "8% of the book" -- a 100x misstatement of
   the single number that decides whether a position is sane. Cause: a bare
   float carries no unit, so the model invented one. Fixed with `label_units()`
   which renders ambiguous numerics as unit-bearing strings before prompting.
2. Given "NIFTY ATR at 6th percentile = VERY QUIET", wrote a rationale citing
   "higher-than-usual ATR, which indicates increased market volatility" --
   exactly backwards.

Error 2 is not fixable by prompting and is the reason for the asymmetry below.

Also fixed: `check_numbers_grounded` split "Rs 486,320.29" into 486 and 320.29
and flagged a correctly grounded sentence while real fabrications hid in the
noise. Thousands separators are now stripped from both sides before comparison.

### The asymmetry
The model may suggest what to test. It has no say in what passes.

- `/llm/explain` narrates facts computed elsewhere, then checks every number in
  the reply against those facts and returns the invented ones in `ungrounded`.
  `trustworthy: false` means do not show it to a trader.
- `/llm/propose` emits strategy parameter sets as grammar-constrained JSON
  (prompting alone produced a truncated object with an unterminated string;
  Ollama's `format: "json"` makes malformed output structurally impossible).
  Break-even win rate is computed in Python from the proposal's own TP/SL plus
  an imposed 0.05% round trip -- the model does not get to assume a cost.
  Arithmetically impossible proposals are rejected before they burn a backtest.
  Everything returns marked `UNTESTED`.

First live batch: NIFTY_MOMO_Plus (BE 41.0%), BANKNIFTY_MeanRev (BE 61.0%),
Crypto_MOMO_ETH (BE 43.6%). All propose TP/SL of 2-4%, far wider than the
current 1.0%/0.6%; on a 6th-percentile ATR tape they would almost never fire.
None has been tested. None is armed.

Standing rule unchanged: an LLM does not create edge. 38 strategies tested,
zero validated edge, buy-and-hold still beats all of them on NIFTY/BANKNIFTY
over 5y. A 3.8B model reading the same candles does not change that arithmetic.

## 2026-11-16 14:58 — Capital freed, desk switched to international venues

Two capabilities the desk did not have, both added rather than faked:

**Manual close** (`POST /control/close`). The engine had no way to exit a
position out of band. Rather than settle it in the endpoint -- which would
have created a second accounting path free to drift from the real cost model
-- the endpoint only records ids in a `force_close` meta list. The next engine
cycle closes them through the SAME charges/tax code as a TP or SL, filling at
the live mark minus the ordinary adverse slip, because an operator closing
early crosses the spread like anyone else. Ids are dropped from the list on
settle so a stale id cannot close a future trade that reuses the number.

**Venue filter** (`POST /control/sources`). `UNIVERSE` was a hard-coded list of
9 instruments with no way to restrict it. The engine now filters it by a
`sources` meta key; empty means all venues, so behaviour is unchanged where the
key was never set. The per-source market gate still applies on top -- allowing
a venue does not make it tradeable.

**NIFTY id 7 closed:** short 65 units @ 25,202.35, exit 25,222.85, held 14,597s.
Gross -Rs 1,168.60, charges Rs 482.03, tax Rs 0.00, **net -Rs 1,650.64**. Price
moved 20.5 points against the short. Rs 16,38,153 of notional released; India
index lots (BANKNIFTY Rs 2.21L, FINNIFTY Rs 2.01L) had been unaffordable
against Rs 1.79L free, which was the real reason the desk was only trading
crypto -- not the quiet tape.

Desk now: `scalp_momentum` armed, venues `["us","crypto"]`, open BTC + ETH at
Rs 1L notional each, equity Rs 4,83,913.86, record 6W/9L (40%).

**Honest note on "international":** US is shut until 20:00 IST, so the desk is
crypto-only for the next five hours. Restricting venues does not create
opportunity; it removes India from the scan. And `scalp_momentum` still has
**zero trades ever** -- the 0W/1L belongs to `momentum_scalp`, a different
strategy with a confusingly similar name. Any win rate quoted for scalp before
it has traded is fiction.

## 2026-11-16 ~15:45 — Supervisor could never restart the web UI; dashboard decluttered

**The supervisor bug.** `logs/web.log` was thousands of identical lines:
`nohup: npm: No such file or directory`. launchd hands the supervisor a minimal
PATH that excludes nvm, where this machine's npm lives
(`~/.nvm/versions/node/v20.19.0/bin`). So every 20s tick the supervisor
"restarted" the web UI into nothing, reported WEB DOWN, and looked like it was
working. The API was never affected because it is launched by absolute path
(`./.venv/bin/python`). Fixed by resolving node's bin directory explicitly,
highest version first (`sort -rV` -- the plain glob puts v16 ahead of v20
lexically). Verified in a stripped environment (`env -i`).

**Dashboard.** Removed, with reasons:
- KPI labels were sentences ("LIVE EQUITY (MARK-TO-MARKET)", "GROSS P&L
  (BEFORE COSTS)") with prose sub-captions ("costs deepened the loss by X").
  Now one word each and numbers underneath.
- Deck headings RESEARCH / LIVE DESK duplicated the tabs directly above them.
- Scanner and backtest explainer paragraphs; `TOP HITS (125 bars)`, a frequency
  table of candlestick patterns across history that nobody acts on.
- The option chain rendered 17 rows of "—" without SmartAPI credentials, which
  was the tallest panel on the page. It is now a strike-ladder strip until real
  premiums exist.
- Model internals (weights, SAE latents, go-live checklist) moved off the live
  desk to RESEARCH; they are development diagnostics, not things you trade off.

**Layout bugs found while doing it.** Open-trades and trade-log columns were
overlapping, not merely tight: grid children do not shrink below their content,
so `6052694.74` (BTC in rupees, 13 characters in a ~35px cell) pushed into its
neighbour and the two numbers rendered on top of each other. Fixed with
`min-width: 0` plus ellipsis, a magnitude-aware `price()` formatter, and by
cutting the trade log from seven columns to four -- `net` is already after
charges and tax, so the extra columns cost legibility for nothing. The signals
panel had the same squeeze on its `why` cell, which is the one that says why
the desk is standing aside, so that moved to its own full-width line.

One self-inflicted break worth recording: a `{/* comment */}` placed inside a
`{cond && (...)}` makes two JSX children and fails to compile. `tsc --noEmit`
passed it; only SWC caught it.

## 2026-11-16 ~15:25 — The volatility agent was dead for a second reason

Checked the earlier "fixed" claim against live data and it was wrong: the agent
still voted 0.0 on every trade and still had no entry in `agent_scores` at all.
Two independent faults, both found:

**1. The engine fed it a key that does not exist.**
```python
"rv": (struct.get("regime") or {}).get("realised_vol"),
```
`market_structure.analyze()` returns `atr`, `atr_pct`, `label`, `percentile` --
there has never been a `realised_vol`. So `ctx["rv"]` was None on every cycle
and the agent's premium rich/cheap branch was unreachable code. The earlier fix
rewrote the AGENT to stop reading that key while leaving the ENGINE still
producing it, which is why the symptom survived. Now computed properly in
`_realised_vol()` from the 1h closes already fetched, annualised per venue
(crypto 24x365, NSE 6.25x250, US 6.5x252 -- one shared factor would misstate it
by up to 6x against an annualised IV). Sanity: NIFTY 14.1%, BTC 54.9%,
ETH 75.0%.

**2. It could only lean on the structural trend, which is usually "side".**
```python
lean = 1.0 if direction == "up" else -1.0 if direction == "down" else 0.0
vote = lean * support * 0.6
```
`market_structure` reports `direction == "side"` most of the time, so lean was
0, so the vote was 0, so it was never scored, so its weight stayed 0 forever --
a closed loop that looked like an agent participating. It now falls back to the
side the strategy is proposing when there is no structural trend.

Verified live, and the behaviour is the right shape:
```
BTC   pct 0.75  rv 0.549  ->  +0.300 call / -0.300 put   enough movement
ETH   pct 0.63  rv 0.750  ->  +0.156 / -0.156
NIFTY pct 0.13  rv 0.141  ->  -0.444 both sides          too quiet to reach target
```
NIFTY votes against a trade on both sides because the tape is at the 13th ATR
percentile -- a 1% target is not reachable in the horizon regardless of which
way you point. That is the agent doing its job.

Not yet confirmed end to end: trades 15 and 17 carry vote snapshots taken at
entry, before the fix, so their stored `volatility: 0.0` will not change. The
first trade opened after this restart is the real test.

Calibration meanwhile: momentum 14 scored votes (6 hits), pattern 9 (5), trend
4 (1), volatility 0. All still weight 0 -- 20 scored votes each is the gate.

## 2026-11-16 15:30 — scalp's first trade ever, and the gate bug that produced it

`scalp_momentum` opened NIFTY LONG 65 units @ Rs 25,291.10 at **15:30:37 IST**
-- 37 seconds after the NSE bell. The session gate was off by one minute:

```python
mins = now.hour * 60 + now.minute          # seconds discarded
return _NSE_OPEN_MIN <= mins <= _NSE_CLOSE_MIN
```

At 15:30:37 `mins` is 930 and `930 <= 930` is true, so the whole closing minute
(15:30:00-15:30:59) counted as open. `us_market_open()` had the identical bug,
holding the US desk open through 16:00:59 ET. Both now use `<` on the close.
Verified at the boundary: 15:29:59 open, 15:30:00 closed, 15:30:37 closed;
09:29:59/16:00:00 likewise for US.

The trade was closed rather than carried: a 1.0%/0.6% intraday scalp cannot be
marked again until Tuesday 09:15 and would have been held ~18 hours through an
overnight gap, which is the opposite of what the strategy is. Entry = exit =
25,291.10 (no price move with the market shut), gross -Rs 493.19 on slippage,
charges Rs 483.72, **net -Rs 976.91**. It should not be read as evidence about
scalp's edge -- it was an invalid entry, closed immediately.

**The volatility agent fix is confirmed end to end.** Trade 18 is the first
with a vote snapshot taken after the restart:
```
trend +0.288 | pattern 0.0 | momentum +1.0 | volatility -0.372
```
It voted AGAINST the long, correctly, because NIFTY sits at the 13th ATR
percentile and a 1% target is not reachable there. The committee took the trade
anyway because every predictor weight is still 0 until 20 scored votes.

**Not a bug:** risk came to 2.04% of the book against a 1.00% target. One NIFTY
lot is 65 indivisible units, which risks Rs 9,866 at a Rs 4.84L account. The
engine already logs this at entry rather than hiding it.

### Scalp's parameters do not clear break-even on crypto
Full event study, 6mo of 1h bars, ~2,500 signals per coin, side-matched random
baseline. These are the armed parameters, not a tuned variant:

```
break-even win rate = 40.6%   (sl + cost) / (tp + sl)

BTC  hit 39.55%  mean net -0.0432%/trade  edge vs random +0.0162%  p=0.1189
     exits TP 28.8% / SL 50.8% / timeout 20.5%
ETH  hit 38.39%  mean net -0.0517%/trade  edge vs random +0.0072%  p=0.3157
     exits TP 32.2% / SL 57.0% / timeout 10.8%
```

Both below break-even, both negative per trade, both NO EDGE (indistinguishable
from random). The small positive "edge vs random" is not significant at p=0.12
and p=0.32 and will not be reported as an edge. Stops are hit at roughly twice
the rate of targets -- 0.6% is easy to touch before 1.0% on this tape.

Desk left on all venues (`sources = []`) at the user's request: practice on
whatever is open. The expected outcome on this evidence is slow bleed, ~0.05%
per trade. The value of running it is agent calibration and execution testing,
not profit.

## 2026-11-16 ~15:50 — Geometry map: the execution structure itself is the cost

`geometry_map` is signal-free -- every bar, both sides, every (horizon, TP, SL)
combination -- so there is nothing to curve-fit. It measures what the exit
scheme costs before any signal is applied.

```
48 cells each (horizons 6/12/24, TP and SL in 0.40/0.60/1.00/1.50%)

BTC    4417 bars   positive cells 0/48
       best  h24 tp1.00 sl1.50  -0.0452%     worst tp0.40 sl0.40  -0.0689%
ETH    4417 bars   positive cells 0/48
       best  h24 tp1.50 sl0.60  -0.0334%     worst tp0.40 sl1.50  -0.0853%
NIFTY   875 bars   positive cells 5/48
       best  h24 tp1.50 sl0.40  +0.0352%     worst tp0.40 sl1.50  -0.1440%
```

On crypto the answer is flat: **every single cell is negative**, in a narrow
band of -0.033% to -0.085%, which is the round-trip cost reappearing whatever
geometry you choose. Tighter stops are worse, not better (0.40/0.40 is the
worst cell on BTC) because noise triggers them more often and each round trip
pays full freight. This sets the bar: a signal must find MORE than ~0.05% of
edge per trade to break even. scalp's signal finds +0.016% (p=0.12). The gap,
not the signal, is the problem.

### The NIFTY cell that looked like an edge, and why it is not
5 of 48 positive on the SMALLEST sample is what noise looks like, so it got the
full treatment rather than a headline.

Split-half: best cell in half 1 (h24, TP 1.50, SL 0.40) returned +0.0352%, and
the SAME cell in half 2 returned +0.0277%. 3 cells positive in both halves.
This is the first thing in this project to survive split-half -- and it still
fails:

- **t = 1.92** (+0.03515% / 0.01833 stderr), p ~ 0.055. Not significant even
  uncorrected.
- 48 cells were searched. Bonferroni needs alpha = 0.05/48 = 0.00104, i.e.
  t ~ 3.3. It misses by a wide margin.
- **Buy-and-hold returned +8.26%** over the identical window (23,362 ->
  25,291). Standing rule: always compare against it.
- SL fires 68.0% of the time against TP 18.9%. The entire positive expectancy
  rests on the 18.9% of trades that reach a 1.5% target -- a fat-tail-dependent
  profile, not a reliable one.
- A 3.75:1 asymmetric geometry on a market that rose 8.26% is trend capture
  wearing a strategy's clothes.

Verdict: no edge. Recorded here because the split-half pass was genuine and
would have been easy to report as a win by stopping one step early.

Running tally unchanged: ~38 strategies, 3 venues, intraday and daily, options
and underlying -- zero validated edge.

## 2026-11-16 ~15:55 — Universe 9 -> 18; the 15:30-20:00 IST gap is now covered

The desk sat idle between the NSE close (15:30) and the US open (20:00) with
only BTC and ETH quotable, and both were moving ~0.02% an hour against a 0.15%
scalp threshold. Checked what else has usable 1h data and added the venues that
actually cover that window:

```
Europe       DAX ^GDAXI, FTSE ^FTSE, CAC ^FCHI    13:30-22:00 IST   0.18-0.23%/h
Commodities  GOLD GC=F, CRUDE CL=F, SILVER SI=F   Globex ~23h       0.20-0.42%/h
Crypto       SOL, XRP, DOGE (yfinance)            24/7              0.31-0.43%/h
```

Asia (Nikkei 0.474%/h, Hang Seng 0.230%/h) has data but runs 05:30-11:30 IST,
overlapping NSE rather than filling the gap, so it was left out.

Everything converts to INR on the way in: `fx_to_inr()` generalises `usdinr()`
to EUR/GBP/JPY/HKD, each with a plausibility band, because a bad FX print
silently rescales an entire venue. Verified: DAX Rs 27,67,213 / 103.42 = 26,757
against ^GDAXI's 26,753; CRUDE Rs 8,324 / 93.92 = 88.63 against 88.59.

Session gates are computed in their own timezones -- Europe in CET, since
European DST does not switch on US dates and India has none.

### Three faults found while wiring it
1. **DAX was unsizeable.** One unit is ~Rs 27.7L, whose 0.6% stop risks
   Rs 16.6k against a Rs 4.8L book, so every entry tripped the 2.5% hard cap
   and the venue could never have traded. Europe and commodities are now
   divisible like crypto, minimum Rs 25,000 notional (Rs 150 risk at a 0.6%
   stop). Margin left at 1.0 -- assuming futures leverage would flatter the
   capital caps on a desk that has not earned it.
2. **New symbols fell through to the NSE guesser.** `backtest.py` keeps its own
   `SYMBOLS` map, and an unmapped name tries `<SYM>.NS`, `<SYM>`, `<SYM>.BO`.
   So `FTSE.NS`, `GOLD.NS` and `SOL.NS` 404'd on every cycle and the whole
   expanded universe produced zero signals. Adding the venue to `options.py`
   alone was not enough.
3. **No option chain exists for these venues.** `get_atm()` now returns None
   rather than modelling one. A fabricated chain is how phantom P&L got in
   before.

### Cost of the expansion
Measured from consecutive equity records: cycle period went from ~14.5s to
~18.5s, i.e. cycle work ~2.5s -> ~6.5s for twice the instruments. Acceptable
for a strategy whose target is a 1% move.

### scalp_momentum's first genuine trade
`SOL LONG 14.9853 @ Rs 6,673.19`, notional Rs 1,00,020, **risk 0.12% of book**,
TP Rs 6,741.27 (+1.02%) / SL Rs 6,634.48 (-0.58%). Correct geometry, and sized
properly because SOL is divisible -- unlike the NIFTY lot, which forced 2.04%.
This is the trade the venue expansion was for: BTC and ETH were flat, SOL was
not.

First full scan of all 18 shows the gates behaving: XRP (fast +0.49%, slow
-0.91%), DOGE (+0.31%/-0.51%) and CRUDE (+0.33%/-0.68%) all clear the 0.15%
threshold on the fast leg but are refused because the slow leg disagrees.

## 2026-11-16 ~16:00 — Asia added; universe 18 -> 23, no empty window left

```
Asia      NIKKEI ^N225 JPY, HSI ^HSI HKD, KOSPI ^KS11 KRW,
          ASX ^AXJO AUD, STI ^STI SGD          04:30-14:30 IST
```

Asia was initially skipped as "overlaps NSE", which was wrong: the ASX opens at
04:30 IST and Tokyo/Seoul at 05:30, nearly five hours before the NSE. That
window was crypto-only.

Each venue keeps its own exchange timezone and its own lunch break -- a single
Asia gate would be wrong by hours, since Tokyo and Hong Kong break for lunch
while Seoul, Sydney and Singapore do not. `market_open_for()` now takes an
optional symbol; the engine always passes one, because "Asia is open" is not a
fact any single instrument can act on. The no-symbol form serves the session
banner only.

Boundary table verified by IST hour: ASX first at 05:00, Nikkei/KOSPI 06:00,
STI 07:00, HSI 07:00; Nikkei dark at 08:00 (11:30-12:30 JST lunch) and HSI at
10:00 (12:00-13:00 HKT); all shut Saturday.

Three currencies added (KRW, AUD, SGD), each with a plausibility band.
Conversions checked against source: Nikkei Rs 35,120 / 0.5883 = 59,698 vs
^N225 59,700; HSI Rs 284,364 / 11.971 = 23,754 vs 23,753.

Taiwan (^TWII) had only 95 usable bars and was left out rather than traded on
thin data.

**Caveat recorded against my own earlier numbers:** the "avg 1h move" figures I
quoted for indices (Nikkei 0.474%, KOSPI 0.932%) are inflated. yfinance 1h bars
for a cash index include the lunch break and the overnight gap as if they were
single-hour moves. Crypto's figures do not have this problem because it trades
continuously. Real intraday range on these indices is materially lower, and
they should not be treated as the biggest movers on that basis.

Coverage now continuous: Asia 04:30-14:30, NSE 09:15-15:30, Europe 13:30-22:00,
US 20:00-02:30, commodities ~23h, crypto 24/7.

Standing caveat unchanged: more venues buy more OPPORTUNITIES, not edge. The
geometry map says the execution structure costs ~0.05% per trade whatever the
venue, and no signal here has yet beaten that.

## 2026-11-16 ~16:05 — scalp tested on all 9 new venues BEFORE the open trades resolve

Run deliberately while SOL (id 19) and CRUDE (id 20) were still open, so the
result could not be retrofitted to whatever they do.

```
scalp geometry TP 1.0% / SL 0.6%, horizon 12, 6mo 1h, side-matched baseline

sym     bars  sigs   hit%   net%/trade   vs rand      p
GOLD    2916  1593  42.12     -0.0233   +0.0222  0.0859
FTSE    1155   652  40.03     -0.0871   -0.0503  0.9810
SOL     4417  2477  38.68     -0.0420   +0.0176  0.1189
CAC     1173   643  38.57     -0.1093   -0.0733  0.9980
DAX     1173   643  38.10     -0.0881   -0.0427  0.9451
XRP     4417  2437  37.87     -0.0485   +0.0064  0.3606
SILVER  2915  1605  37.94     -0.0414   +0.0142  0.2238
DOGE    4417  2465  36.11     -0.0754   -0.0120  0.7852
CRUDE   2916  1624  34.91     -0.0890   -0.0311  0.9471
```

**0 of 9 pass, not even uncorrected.** Bonferroni for 9 venues would need
alpha = 0.00556; nothing comes close. DAX, FTSE, CAC, CRUDE and DOGE are
significantly WORSE than a side-matched random entry (p > 0.78).

CRUDE, which the desk opened a scalp on at 15:59:52, is the second worst of
the nine and worse than random at p = 0.9471.

### Correction to my own method
I have been comparing `hit_rate` against the 40.6% break-even. That is only
valid when almost nothing times out. GOLD exposed it:

```
       hit%    TP%    SL%  timeout%
GOLD  42.12   28.7   51.2      20.1
SOL   38.68   35.5   59.8       4.8
CRUDE 34.91   34.5   64.7       0.7
```

GOLD's hit rate sits ABOVE break-even while its mean net is negative, because
`hit_rate` counts trades that timed out slightly green -- they never reached
the 1% target, so they cannot pay for the 51.2% that hit a 0.6% stop. The
comparison must be **TP rate vs break-even**, not hit rate:

```
GOLD   28.7% vs 40.6% needed   -11.9
CRUDE  34.5% vs 40.6%           -6.1
SOL    35.5% vs 40.6%           -5.1
```

All three fail clearly on the correct comparison. Earlier entries in this log
that quote hit rate against break-even (BTC 39.55%, ETH 38.39%) should be read
with this in mind -- their timeout rates were 20.5% and 10.8%, so their true
margins are worse than stated, not better. The conclusion does not change.

Running tally: ~38 strategies across now 6 venues and 23 instruments -- zero
validated edge.

## 2026-11-16 ~16:15 — The LLM proposal -> gauntlet loop, closed end to end

Built `/llm/propose` earlier and claimed "the model proposes, the gauntlet
disposes". Actually ran it rather than leaving that as a design claim.

phi3 (75.9s, 3 proposals, 0 rejected by the arithmetic filter), each tested on
SOL, CRUDE and GOLD with the same event study and side-matched baseline:

```
proposal                 tp%   sl%   BE%  | TP rate    net%       p     sym
Momentum TP1.2 SL0.4    1.20  0.40  28.1  |   23.1   -0.0571  0.5287   SOL
                                           |   22.5   -0.0806  0.9701   CRUDE
                                           |   19.5   -0.0160  0.0549   GOLD
MeanRev  TP0.8 SL0.2    0.80  0.20  25.0  |   16.7   -0.0770  0.9426   SOL
                                           |   18.0   -0.0662  0.8803   CRUDE
                                           |   19.1   -0.0488  0.3416   GOLD
Hybrid   TP0.9 SL0.3    0.90  0.30  29.2  |   22.2   -0.0741  0.8603   SOL
                                           |   23.2   -0.0670  0.9177   CRUDE
                                           |   23.0   -0.0382  0.2968   GOLD

9 tests -> Bonferroni alpha 0.00556.   PASSED: none
```

Every TP rate sits 2-8 points below its own break-even. Every mean net is
negative.

**The interesting part is what the model did with the evidence.** The context
it was given said, verbatim, that "tighter stops are WORSE because noise
triggers them" -- the finding from the geometry map. All three proposals came
back with stops TIGHTER than the current 0.6%: 0.40%, 0.20%, 0.30%. It
reproduced the folk association between scalping and tight stops instead of
reasoning from the measurement in front of it.

That is the third material error from this model in a handful of calls (after
reading 0.08% as 8%, and calling a 6th-percentile ATR tape "higher than usual
volatility"). It is the reason the asymmetry is built the way it is: arming any
of these on the model's recommendation would have lost money faster, not
slower. The proposer is useful as a generator of things to test and worthless
as a source of conclusions.

Pipeline itself verified working: generate -> arithmetic filter -> event study
-> baseline -> multiple-comparison correction -> verdict, with nothing armed.

## 2026-11-16 ~17:10 — Three positions, one and a half bets

Switched the desk to `index_hold` (long equity indices, no timing claim) and it
opened DAX, FTSE and CAC. Checked afterwards whether those were three bets or
one bet with three tickets. Daily returns, 124 sessions:

```
            DAX   FTSE    CAC  NIFTY    SPY    QQQ
DAX        1.00   0.36   0.83  -0.09   0.15   0.14
FTSE       0.36   1.00   0.34  -0.11   0.01   0.03
CAC        0.83   0.34   1.00   0.02   0.06   0.04
NIFTY     -0.09  -0.11   0.02   1.00  -0.15  -0.12
SPY        0.15   0.01   0.06  -0.15   1.00   0.93
QQQ        0.14   0.03   0.04  -0.12   0.93   1.00

average correlation among the three held: 0.51
effective independent bets: ~1.49 of 3
```

DAX and CAC at 0.83 are substitutes. The desk was paying three round trips for
about one and a half bets. Closed CAC, keeping DAX (eurozone) and FTSE (0.35 —
genuinely separate).

**SPY/QQQ at 0.93 was the bigger exposure.** The US session opens at 20:00 IST
tonight and `index_hold` would have taken both, which is the same bet twice at
double the cost. Added a `SUBSTITUTE_GROUP` guard keyed on the measurements
above: eurozone (DAX/CAC), us_broad (SPY/QQQ), majorcoin (BTC/ETH). FTSE is
deliberately NOT grouped, and NIFTY is grouped with nothing — at -0.15 to +0.02
against everything else it is the only real diversifier in the universe, and it
arrives when the NSE opens tomorrow.

Two bugs in my own guard, both found by running it:
1. It tested the group BEFORE checking the signal was otherwise actionable, so
   it logged "skip DAX: already hold a eurozone position" against DAX itself —
   the position that WAS the eurozone holding.
2. It logged on every cycle. A standing refusal is now reported once.

**CAC's exit is worth recording on its own.** Price moved in our favour and the
trade still lost: gross +Rs 22.51, charges Rs 73.77, net **-Rs 51.26**. That is
the ~0.05% round-trip cost the geometry map measured, arriving in the live
ledger exactly as predicted. Being right about direction is not sufficient.

## 2026-11-17 09:11 — The desk died through the NSE open and every health check passed

The engine stopped completing cycles at 09:11:55, four minutes before the bell,
and was still dead at 10:01 when I looked. Fifty minutes. The supervisor did
nothing, the API answered every request, and the dashboard showed yesterday's
signals as if they were current.

Cycle gaps from the equity table, one row per completed cycle:

```
before the restart:  21 cycles, median gap 661s (11 min), worst 3110s (52 min)
after  the restart:  18 cycles, median gap  13s,          worst   50s
last ten gaps: 13.5 12.9 13.4 12.8 13.3 12.8 13.9 12.8 13.2 13.0
```

A healthy cycle is ~13-18s. It had been degrading all night before it stopped.

**The stall itself:** the last engine activity in the log is 09:25:31, an
`httpx.get` for AngelOne's 6.7MB instrument master with `timeout=120`. The
India code path wakes at the NSE open and pulls that file from inside the
trading cycle. A 120s budget inside an 18s loop is wrong on its face.

**Why nothing caught it:** `/health` returned `{"ok": true}` whenever FastAPI
could answer. The cycle runs in a thread via `asyncio.to_thread`, so a hung
cycle is invisible to the event loop and to anything polling HTTP. The
supervisor's whole job is to restart a dead desk and it had no way to see one.

Fixed:
- `engine.last_cycle_ts` set at the end of every completed cycle
- `/health` reports `seconds_since_cycle` and returns **503** past
  `ARGONA_STALL_SEC` (120s, about five missed cycles)
- `supervisor.sh` already uses `curl -f`, which fails on 503, so the restart
  path works now that there is a signal to act on
- scrip-master timeout 120s -> 30s at both call sites; both cache, so a miss
  costs one cycle rather than the day

### Two hypotheses I got wrong, recorded because the method matters
1. **"Rate limiting."** I grepped for `429` and found 23 hits, called the
   suspicion confirmed, and was wrong — the pattern was matching ephemeral
   PORT numbers (`127.0.0.1:61429`). A strict grep for `HTTP Error 429|Too
   Many Requests|rate limit` returns **0**. There was no throttling.
2. **"Closed venues were still being fetched every cycle."** Also wrong: the
   `market_open_for` gate runs BEFORE `get_underlying` and `continue`s, so a
   shut venue is never fetched.

**What I still do not know:** what caused the gradual slowdown through the
night. The final stall is explained by the timestamp; the 11-minute median
before it is not. It cleared completely on restart, which is consistent with
something accumulating in the old process, but I have no evidence for that and
will not record a third guess as a cause.

The fix addresses the CONSEQUENCE rather than the cause, and that is the honest
description of it: whatever makes the loop hang next time, the desk now says so
and gets restarted instead of sitting dead with a green health check.

**Exposure:** the overnight loss of Rs 2,514 was the market falling while held
long — DAX -0.66%, FTSE -0.70%, SPY -1.56% — not this bug. The bug's real
danger was a stop that could not fire. With the engine dead, a position hitting
its 4% stop would have exited only when a human noticed, at whatever price that
happened to be.
