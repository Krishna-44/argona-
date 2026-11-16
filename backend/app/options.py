"""Option-chain adapters. **India NSE F&O only** — everything is in INR.

  * India (NIFTY, BANKNIFTY, FINNIFTY) — spot from yfinance (^NSEI etc.),
    expiry calendar, strike grid and lot sizes from AngelOne's PUBLIC
    instrument master (no credentials needed), premium from Black-Scholes on
    realised vol.

    The premium is therefore MODEL-PRICED, not a live quote, and every quote
    says so (``quoted=False``). It is the one piece that needs SmartAPI
    credentials; strikes, expiries and lot sizes are all real today. A model
    price has no bid/ask spread, so a synthetic spread is applied rather than
    letting the desk pretend it fills at mid.

US equities and crypto adapters were removed — this is an India F&O desk.
We never place orders; these are read-only quote feeds.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
# Europe runs its own DST calendar — it does not switch on the same dates as
# the US, and India has none — so the European session is computed in CET
# rather than offset from either of the others.
CET = ZoneInfo("Europe/Berlin")
TOKYO = ZoneInfo("Asia/Tokyo")
HK = ZoneInfo("Asia/Hong_Kong")
SEOUL = ZoneInfo("Asia/Seoul")
SYDNEY = ZoneInfo("Australia/Sydney")
SG = ZoneInfo("Asia/Singapore")

# Half-spread applied to the modelled premium, as a fraction. Real NSE index
# weeklies run ~0.5-2% wide on ATM strikes; 1% each side is the honest middle.
MODEL_HALF_SPREAD = 0.01

# Smallest crypto position worth opening, in rupees. The cost model charges a
# flat Rs 20 per order, so a position has to be large enough that the flat
# component is a rounding error rather than the whole trade.
MIN_CRYPTO_NOTIONAL = float(os.environ.get("ARGONA_MIN_CRYPTO_NOTIONAL", "25000"))

# The tradeable universe. `source` picks the adapter.
#
# The book is denominated in INR, so US and crypto premiums are converted at
# the live USD/INR rate. That is a real modelling choice with a real
# consequence: an unchanged dollar premium still moves the rupee P&L when the
# rupee moves, which is exactly what a rupee-funded account would experience.
UNIVERSE = [
    {"symbol": "NIFTY",     "source": "india",  "label": "Nifty 50"},
    {"symbol": "BANKNIFTY", "source": "india",  "label": "Bank Nifty"},
    {"symbol": "FINNIFTY",  "source": "india",  "label": "Fin Nifty"},
    {"symbol": "SPY",       "source": "us",     "label": "S&P 500 ETF"},
    {"symbol": "QQQ",       "source": "us",     "label": "Nasdaq 100 ETF"},
    {"symbol": "NVDA",      "source": "us",     "label": "Nvidia"},
    {"symbol": "TSLA",      "source": "us",     "label": "Tesla"},
    {"symbol": "BTC",       "source": "crypto", "label": "Bitcoin"},
    {"symbol": "ETH",       "source": "crypto", "label": "Ethereum"},
    # Europe covers 13:30-22:00 IST — the window where NSE has shut (15:30)
    # and the US has not opened (20:00), which the desk previously sat out.
    {"symbol": "DAX",       "source": "europe", "label": "DAX 40"},
    {"symbol": "FTSE",      "source": "europe", "label": "FTSE 100"},
    {"symbol": "CAC",       "source": "europe", "label": "CAC 40"},
    # Globex metals/energy run nearly 23h, so they cover the gap too.
    {"symbol": "GOLD",      "source": "commodity", "label": "Gold"},
    {"symbol": "CRUDE",     "source": "commodity", "label": "Crude Oil"},
    {"symbol": "SILVER",    "source": "commodity", "label": "Silver"},
    # BTC and ETH have been moving ~0.01-0.04% an hour, far under the 0.15%
    # a scalp needs. These move ~0.3-0.43% and are open 24/7.
    {"symbol": "SOL",       "source": "crypto", "label": "Solana"},
    {"symbol": "XRP",       "source": "crypto", "label": "XRP"},
    {"symbol": "DOGE",      "source": "crypto", "label": "Dogecoin"},
    # Asia opens BEFORE the NSE -- ASX 04:30 IST, Tokyo and Seoul 05:30 --
    # so it covers the early window that was crypto-only, not just the
    # NSE overlap.
    {"symbol": "NIKKEI",    "source": "asia",   "label": "Nikkei 225"},
    {"symbol": "HSI",       "source": "asia",   "label": "Hang Seng"},
    {"symbol": "KOSPI",     "source": "asia",   "label": "KOSPI"},
    {"symbol": "ASX",       "source": "asia",   "label": "ASX 200"},
    {"symbol": "STI",       "source": "asia",   "label": "Straits Times"},
]

# Yahoo ticker and quote currency for the venues served by yf_underlying().
# Prices arrive in the local currency and are converted to INR on the way in.
_YF_VENUE = {
    "DAX":    {"ticker": "^GDAXI",   "ccy": "EUR"},
    "FTSE":   {"ticker": "^FTSE",    "ccy": "GBP"},
    "CAC":    {"ticker": "^FCHI",    "ccy": "EUR"},
    "GOLD":   {"ticker": "GC=F",     "ccy": "USD"},
    "CRUDE":  {"ticker": "CL=F",     "ccy": "USD"},
    "SILVER": {"ticker": "SI=F",     "ccy": "USD"},
    "SOL":    {"ticker": "SOL-USD",  "ccy": "USD"},
    "XRP":    {"ticker": "XRP-USD",  "ccy": "USD"},
    "DOGE":   {"ticker": "DOGE-USD", "ccy": "USD"},
    "NIKKEI": {"ticker": "^N225",    "ccy": "JPY"},
    "HSI":    {"ticker": "^HSI",     "ccy": "HKD"},
    "KOSPI":  {"ticker": "^KS11",    "ccy": "KRW"},
    "ASX":    {"ticker": "^AXJO",    "ccy": "AUD"},
    "STI":    {"ticker": "^STI",     "ccy": "SGD"},
}

_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(12.0, connect=6.0),
                               headers={"user-agent": "argona-fno/0.1"})
    return _client


# ---------------------------------------------------------------------------


@dataclass
class Underlying:
    symbol: str
    source: str
    label: str
    spot: float
    market_open: bool
    # short-window price history (most recent last), for momentum features
    history: list[float] = field(default_factory=list)


@dataclass
class OptionQuote:
    underlying: str
    source: str
    kind: str            # "call" | "put"
    strike: float
    expiry: str          # ISO date
    dte: float           # days to expiry
    bid: float           # USD premium per unit
    ask: float
    mark: float
    iv: float            # implied vol (fraction, e.g. 0.42)
    volume: float
    open_interest: float
    spot: float
    instrument: str = ""  # "NIFTY 2026-11-20 CE 25100"
    lot_size: float = 1   # NSE contracts trade in lots; premium x lot = rupees
    quoted: bool = True   # False => premium is model-priced, not a live quote
    # Capital actually tied up, as a fraction of notional. An option buyer
    # pays the full premium (1.0); an index future is margined at roughly
    # 12% SPAN+exposure. Without this a single NIFTY futures lot looks like a
    # Rs 16L position against a Rs 5L book and is rejected outright, when in
    # reality it ties up about Rs 2L.
    margin_rate: float = 1.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.mark or self.ask or self.bid

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if m <= 0 or self.bid <= 0 or self.ask <= 0:
            return 1.0
        return (self.ask - self.bid) / m

    @property
    def moneyness(self) -> float:
        # (spot - strike)/spot ; ~0 at the money
        return (self.spot - self.strike) / self.spot if self.spot else 0.0


# ---------------------------------------------------------------------------
# India NSE F&O.

_spot_cache: dict[str, tuple[float, list[float]]] = {}
_SPOT_TTL = 20.0
_YF = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS"}

# NSE holidays are not worth hardcoding — a closed day simply returns no fresh
# bars, and the desk marks the market shut.
_NSE_OPEN_MIN = 9 * 60 + 15
_NSE_CLOSE_MIN = 15 * 60 + 30


def nse_market_open(now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    # Strictly before the close. `mins` drops the seconds, so `<=` treated the
    # whole 15:30:00-15:30:59 minute as open and let scalp_momentum open NIFTY
    # id 18 at 15:30:37 -- 37 seconds after the bell. Last tradeable minute is
    # 15:29.
    return _NSE_OPEN_MIN <= mins < _NSE_CLOSE_MIN


def india_underlying(symbol: str, label: str) -> Underlying | None:
    """Index spot + a short 5-minute history for the momentum features."""
    sym = symbol.upper()
    now = time.time()
    hit = _spot_cache.get(sym)
    if hit and now - hit[0] < _SPOT_TTL:
        closes = hit[1]
    else:
        try:
            import yfinance as yf
            hist = yf.Ticker(_YF.get(sym, f"{sym}.NS")).history(period="5d", interval="5m")
            closes = [float(x) for x in hist["Close"].dropna().tolist()][-60:]
        except Exception:
            closes = []
        if not closes:
            return None
        _spot_cache[sym] = (now, closes)
    return Underlying(symbol=sym, source="india", label=label, spot=closes[-1],
                      market_open=nse_market_open(), history=closes)


def _realised_vol(history: list[float]) -> float:
    """Annualised realised vol from the recent 5-minute closes."""
    if len(history) < 12:
        return 0.15
    rets = [math.log(history[i] / history[i - 1])
            for i in range(1, len(history)) if history[i - 1] > 0]
    if len(rets) < 8:
        return 0.15
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    # 75 five-minute bars per NSE session, 252 sessions
    return max(0.05, min(1.5, math.sqrt(var * 75 * 252)))


_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _expiry_code(iso: str | None) -> str | None:
    """'2026-11-17' -> '17NOV2026', the instrument master's own format."""
    if not iso:
        return None
    try:
        import datetime as dt
        d = dt.date.fromisoformat(iso)
        return f"{d.day:02d}{_MONTHS[d.month - 1]}{d.year}"
    except Exception:
        return None


def _next_expiry(symbol: str) -> tuple[str | None, float]:
    """Real NSE expiry from AngelOne's public instrument master.

    Needs no credentials. Falls back to the next Thursday if the master is
    unreachable, which keeps the desk running rather than stalling it.
    """
    import datetime as dt
    today = dt.date.today()
    try:
        from . import angelone
        months = {m: i for i, m in enumerate(
            ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
             "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
        for e in angelone.expiries(symbol.upper()):
            try:
                d = dt.date(int(e[5:]), months[e[2:5]], int(e[:2]))
            except Exception:
                continue
            dte = (d - today).days
            if dte >= 0:
                return d.isoformat(), max(float(dte), 0.05)
    except Exception:
        pass
    ahead = (3 - today.weekday()) % 7          # Thursday
    d = today + dt.timedelta(days=ahead or 7)
    return d.isoformat(), max(float((d - today).days), 0.05)


def india_atm(symbol: str, spot: float, side: str,
              history: list[float] | None = None) -> OptionQuote | None:
    """ATM option on the nearest real expiry, priced with Black-Scholes.

    Strike grid, expiry and lot size are real (public instrument master). The
    PREMIUM is modelled — flagged ``quoted=False`` so nothing downstream can
    mistake it for a live fill.
    """
    from . import angelone
    from . import option_pricing as op

    sym = symbol.upper()
    expiry, dte = _next_expiry(sym)
    if expiry is None or spot <= 0:
        return None
    iv = op.iv_estimate(_realised_vol(history or []), dte)
    # Snap to a strike that actually trades. The modelled grid assumes a step
    # size and is wrong on several underlyings; the public instrument master
    # carries the real ladder, so use it and fall back only if it is missing.
    strike = None
    try:
        exp_code = _expiry_code(expiry)
        strike = angelone.snap_real_strike(sym, spot, 0, exp_code)
    except Exception:
        strike = None
    if strike is None:
        try:
            strike = float(op.snap_strike(spot, sym, 0))
        except Exception:
            return None
    strike = float(strike)
    kind = "call" if side == "call" else "put"
    try:
        g = op.bs(spot, strike, max(dte, 0.05) / 365.0, iv, kind)
    except Exception:
        return None
    mark = float(g.price)
    if not (mark > 0) or mark != mark:
        return None
    try:
        lot = int(angelone.fo_lot_size(sym) or op.lot_size(sym) or 1)
    except Exception:
        lot = 1
    half = mark * MODEL_HALF_SPREAD
    return OptionQuote(
        underlying=sym, source="india", kind=kind, strike=strike,
        expiry=expiry, dte=dte, bid=max(0.05, mark - half), ask=mark + half,
        mark=mark, iv=iv, volume=0.0, open_interest=0.0, spot=spot,
        instrument=f"{sym} {expiry} {'CE' if kind == 'call' else 'PE'} {strike:.0f}",
        lot_size=lot, quoted=False,
    )


# ---------------------------------------------------------------------------
# FX — the book is in rupees, so dollar instruments convert.

_fx_cache: tuple[float, float] = (0.0, 0.0)
_FX_TTL = 300.0
_FX_FALLBACK = 94.0


def usdinr() -> float:
    """Live USD/INR, cached 5 minutes. Falls back rather than failing a cycle."""
    global _fx_cache
    now = time.time()
    if _fx_cache[0] and now - _fx_cache[0] < _FX_TTL:
        return _fx_cache[1]
    try:
        import yfinance as yf
        d = yf.Ticker("USDINR=X").history(period="5d", interval="1d")
        rate = float(d["Close"].dropna().iloc[-1])
        if 50.0 < rate < 200.0:
            _fx_cache = (now, rate)
            return rate
    except Exception:
        pass
    return _fx_cache[1] or _FX_FALLBACK


# Quote currency -> INR, for venues that are not priced in USD. The desk
# settles in rupees, so every spot and premium has to arrive converted or the
# risk sizing is nonsense (one DAX "point" is not one rupee).
_FX_PAIRS = {"USD": "USDINR=X", "EUR": "EURINR=X", "GBP": "GBPINR=X",
             "JPY": "JPYINR=X", "HKD": "HKDINR=X", "KRW": "KRWINR=X",
             "AUD": "AUDINR=X", "SGD": "SGDINR=X"}
# Plausibility bands. A bad FX print silently rescales an entire venue, so a
# rate outside its band is rejected in favour of the last good one.
_FX_BAND = {"USD": (50, 200), "EUR": (60, 220), "GBP": (70, 260),
            "JPY": (0.3, 2.0), "HKD": (5, 30), "KRW": (0.02, 0.3),
            "AUD": (35, 110), "SGD": (40, 130)}
_fx_multi: dict[str, tuple[float, float]] = {}


def fx_to_inr(ccy: str) -> float | None:
    """Live <ccy>/INR, cached like usdinr(). None when it cannot be trusted."""
    ccy = (ccy or "INR").upper()
    if ccy == "INR":
        return 1.0
    if ccy == "USD":
        return usdinr()
    pair = _FX_PAIRS.get(ccy)
    if not pair:
        return None
    now = time.time()
    hit = _fx_multi.get(ccy)
    if hit and now - hit[0] < _FX_TTL:
        return hit[1]
    try:
        import yfinance as yf
        d = yf.Ticker(pair).history(period="5d", interval="1d")
        rate = float(d["Close"].dropna().iloc[-1])
        lo, hi = _FX_BAND.get(ccy, (0, 1e9))
        if lo < rate < hi:
            _fx_multi[ccy] = (now, rate)
            return rate
    except Exception:
        pass
    return hit[1] if hit else None


# ---------------------------------------------------------------------------
# US equities / ETFs via yfinance. Chains are TTL-cached — Yahoo rate-limits.

_us_cache: dict[str, tuple[float, object]] = {}
_US_TTL = 25.0


def us_market_open(now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    # Strictly before the close, for the same reason as the NSE gate above:
    # `mins` has no seconds, so `<=` kept the desk open through 16:00:59 ET.
    return 9 * 60 + 30 <= mins < 16 * 60


def _yf_ticker(symbol: str):
    import yfinance as yf
    return yf.Ticker(symbol)


def us_underlying(symbol: str, label: str) -> Underlying | None:
    """Spot in INR, plus a short history for the momentum features."""
    try:
        fx = usdinr()
        t = _yf_ticker(symbol)
        hist = t.history(period="2d", interval="5m")
        closes = [float(x) * fx for x in hist["Close"].dropna().tolist()][-60:]
        if not closes:
            closes = [float(t.history(period="1d")["Close"].iloc[-1]) * fx]
        return Underlying(symbol=symbol, source="us", label=label,
                          spot=closes[-1], market_open=us_market_open(),
                          history=closes)
    except Exception:
        return None


def us_atm(symbol: str, spot: float, side: str) -> OptionQuote | None:
    """Nearest-expiry ATM call/put, premium converted to INR.

    `spot` arrives in INR, so it is converted back to USD to match the chain's
    own strikes, and the resulting premium is converted forward again.
    """
    try:
        fx = usdinr()
        spot_usd = spot / fx if fx else spot
        t = _yf_ticker(symbol)
        exps = t.options
        if not exps:
            return None
        expiry = exps[0]
        now = time.time()
        ck = f"{symbol}:{expiry}"
        cached = _us_cache.get(ck)
        if cached and now - cached[0] < _US_TTL:
            chain = cached[1]
        else:
            chain = t.option_chain(expiry)
            _us_cache[ck] = (now, chain)
        df = chain.calls if side == "call" else chain.puts
        if df is None or df.empty:
            return None
        df = df.assign(_d=(df["strike"] - spot_usd).abs()).sort_values("_d")
        row = df.iloc[0]
        bid = float(row.get("bid") or 0.0) * fx
        ask = float(row.get("ask") or 0.0) * fx
        last = float(row.get("lastPrice") or 0.0) * fx
        exp_dt = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=NY)
        dte = max(0.0, (exp_dt - datetime.now(NY)).total_seconds() / 86400.0)
        return OptionQuote(
            underlying=symbol, source="us", kind=side,
            strike=float(row["strike"]) * fx, expiry=expiry, dte=dte,
            bid=bid, ask=ask,
            mark=(bid + ask) / 2 if bid > 0 and ask > 0 else last,
            iv=float(row.get("impliedVolatility") or 0.0),
            volume=float(row.get("volume") or 0.0),
            open_interest=float(row.get("openInterest") or 0.0), spot=spot,
            instrument=f"{symbol} {expiry} {'C' if side == 'call' else 'P'}{row['strike']:.0f}",
            # A US listed option contract covers 100 shares. Leaving this at 1
            # made the flat per-order fee look like ~20% of notional instead of
            # ~0.2%, which wrongly blocked every US name on the cost gate.
            lot_size=100, quoted=True,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Crypto via Deribit. Premiums quote in coin; multiply by index, then by FX.

_DERIBIT = "https://www.deribit.com/api/v2/public"
_deribit_cache: dict[str, tuple[float, list[dict]]] = {}
_DBT_TTL = 20.0


def _deribit(method: str, params: dict) -> dict:
    r = _http().get(f"{_DERIBIT}/{method}", params=params)
    r.raise_for_status()
    return r.json()["result"]


def crypto_underlying(symbol: str, label: str) -> Underlying | None:
    try:
        fx = usdinr()
        idx = float(_deribit("get_index_price",
                             {"index_name": f"{symbol.lower()}_usd"})["index_price"])
        hist = [idx * fx]
        try:
            tr = _deribit("get_last_trades_by_instrument",
                          {"instrument_name": f"{symbol}-PERPETUAL", "count": "40"})
            prices = [float(x["price"]) * fx for x in tr.get("trades", [])]
            if prices:
                hist = prices[-40:] + [idx * fx]
        except Exception:
            pass
        return Underlying(symbol=symbol, source="crypto", label=label,
                          spot=idx * fx, market_open=True, history=hist)
    except Exception:
        return None


def _deribit_instruments(symbol: str) -> list[dict]:
    now = time.time()
    c = _deribit_cache.get(symbol)
    if c and now - c[0] < _DBT_TTL:
        return c[1]
    inst = _deribit("get_instruments",
                    {"currency": symbol, "kind": "option", "expired": "false"})
    _deribit_cache[symbol] = (now, inst)
    return inst


def crypto_atm(symbol: str, spot: float, side: str) -> OptionQuote | None:
    try:
        fx = usdinr()
        spot_usd = spot / fx if fx else spot
        inst = _deribit_instruments(symbol)
        want = "call" if side == "call" else "put"
        cand = [i for i in inst if i.get("option_type") == want]
        if not cand:
            return None
        now_ms = time.time() * 1000
        for i in cand:
            i["_dte"] = max(0.0, (i["expiration_timestamp"] - now_ms) / 86400000.0)
        # Prefer >= 1.5 days: ultra-short-dated options have violent gamma and
        # theta, and their marks gap, which is murder on a tight scalp.
        min_dte = min((i["_dte"] for i in cand if i["_dte"] > 1.5),
                      default=min(i["_dte"] for i in cand))
        near = [i for i in cand if abs(i["_dte"] - min_dte) < 0.01] or cand
        near.sort(key=lambda i: abs(i["strike"] - spot_usd))
        chosen = near[0]
        tk = _deribit("ticker", {"instrument_name": chosen["instrument_name"]})
        bid_c = tk.get("best_bid_price") or 0.0
        ask_c = tk.get("best_ask_price") or 0.0
        mark_c = tk.get("mark_price") or 0.0
        stats = tk.get("stats") or {}
        exp_iso = datetime.fromtimestamp(
            chosen["expiration_timestamp"] / 1000, tz=timezone.utc).date().isoformat()
        # coin premium -> USD via the index -> INR via FX
        unit = spot_usd * fx
        return OptionQuote(
            underlying=symbol, source="crypto", kind=side,
            strike=float(chosen["strike"]) * fx, expiry=exp_iso,
            dte=float(chosen["_dte"]),
            bid=float(bid_c) * unit, ask=float(ask_c) * unit,
            mark=float(mark_c) * unit,
            iv=float(tk.get("mark_iv") or 0.0) / 100.0,
            volume=float(stats.get("volume") or 0.0),
            open_interest=float(tk.get("open_interest") or 0.0), spot=spot,
            instrument=chosen["instrument_name"], lot_size=1, quoted=True,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Unified entry points used by the engine.


def underlying_quote(u: "Underlying", side: str) -> "OptionQuote | None":
    """The UNDERLYING itself, expressed as a quote the desk can trade.

    Why this exists: a round trip on a short-dated option costs 4.5-14% of
    premium (spread + slippage + fees), while the same trade on the underlying
    costs ~0.05% of notional — roughly 100x cheaper. No signal measured in this
    project earns anything close to 4.5%, so the option wrapper is a fee, not
    leverage.

    Modelled as a delta-1, no-expiry instrument, which makes
    ``leverage()`` return exactly 1.0 — so the existing risk machinery
    converts a 0.40% intended move into a 0.40% stop with no special casing.
    There is no theta and no gamma: P&L is linear in the underlying.

    `side` keeps the call/put vocabulary so the rest of the engine is
    unchanged — "call" is long, "put" is short.
    """
    if u is None or u.spot <= 0:
        return None
    # Index/FX spreads on a liquid future are a basis point or two.
    half = u.spot * 0.0001

    # Tradeable unit and capital tied up differ by venue:
    #   india  — the index FUTURE, which trades in NSE lots and is margined
    #   us     — shares, bought for cash
    #   crypto — spot, divisible, bought for cash
    if u.source == "india":
        try:
            from . import angelone
            lot = float(angelone.fo_lot_size(u.symbol) or 1)
        except Exception:
            lot = 1.0
        margin = 0.12
    elif u.source == "crypto":
        # Crypto spot is divisible, so the "lot" is a sensible minimum trade
        # size rather than an exchange constraint — and it has to be big
        # enough to carry the flat per-order fee. At 0.001 ETH the position
        # was ~Rs 180 and the Rs 20-a-side brokerage came to 22% of it, which
        # blocked every crypto trade on the cost gate. Size the minimum so
        # the flat fee is a fraction of a percent.
        lot = max(1e-6, MIN_CRYPTO_NOTIONAL / u.spot)
        margin = 1.0
    elif u.source in ("europe", "commodity", "asia"):
        # Divisible, for the same reason crypto is: these are quoted as index
        # and futures prices, not share lots. Left indivisible at 1.0 they
        # were unsizeable -- one unit of DAX is ~Rs 27.7L, whose 0.6% stop
        # risks ~Rs 16.6k against a Rs 4.8L book, so every entry tripped the
        # 2.5% hard cap and the venue could never trade at all.
        # Margin stays 1.0 (full cash): assuming futures leverage here would
        # flatter the capital caps on a desk that has not earned it.
        lot = max(1e-9, MIN_CRYPTO_NOTIONAL / u.spot)
        margin = 1.0
    else:
        lot, margin = 1.0, 1.0

    return OptionQuote(
        underlying=u.symbol, source=u.source, kind=side,
        strike=u.spot,                 # delta 1 => strike is irrelevant
        expiry="", dte=365.0,          # no expiry; nothing decays
        bid=u.spot - half, ask=u.spot + half, mark=u.spot,
        iv=0.0, volume=1e9, open_interest=1e9, spot=u.spot,
        instrument=f"{u.symbol} UNDERLYING {'LONG' if side == 'call' else 'SHORT'}",
        lot_size=lot, quoted=True, margin_rate=margin,
    )


def europe_market_open(now: datetime | None = None) -> bool:
    """Frankfurt/Paris/London cash session, 09:00-17:30 CET.

    In IST that is roughly 13:30-22:00, which is precisely the window the desk
    used to sit idle in: NSE shuts at 15:30 and the US does not open until
    20:00. Expressed in CET so it tracks European DST on its own schedule,
    which does not change on the same dates as India (India has none) or the US.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(CET)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 <= mins < 17 * 60 + 30


def commodity_market_open(now: datetime | None = None) -> bool:
    """CME Globex metals/energy: 18:00-17:00 ET next day, Sun-Fri, with a
    one-hour maintenance break. Treated as the 17:00-18:00 ET gap plus the
    weekend, which is the part that matters for not quoting a dead tape."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    wd, mins = now.weekday(), now.hour * 60 + now.minute
    if wd == 5:                                    # Saturday: shut all day
        return False
    if wd == 6 and mins < 18 * 60:                 # Sunday before the 18:00 reopen
        return False
    if wd == 4 and mins >= 17 * 60:                # Friday after the 17:00 close
        return False
    return not (17 * 60 <= mins < 18 * 60)         # daily maintenance hour


# Asian cash sessions, each in its OWN exchange timezone with its own lunch
# break. A single "Asia" gate would be wrong by hours: the ASX opens at 04:30
# IST while Hong Kong does not start until 07:00, and Tokyo and Hong Kong both
# shut for lunch while Seoul and Sydney do not.
#   (tz, open_min, close_min, lunch_from, lunch_to)   minutes, local time
_ASIA_SESSION = {
    "NIKKEI": (TOKYO,  9 * 60,      15 * 60,      11 * 60 + 30, 12 * 60 + 30),
    "HSI":    (HK,     9 * 60 + 30, 16 * 60,      12 * 60,      13 * 60),
    "KOSPI":  (SEOUL,  9 * 60,      15 * 60 + 30, None,         None),
    "ASX":    (SYDNEY, 10 * 60,     16 * 60,      None,         None),
    "STI":    (SG,     9 * 60,      17 * 60,      None,         None),
}


def asia_market_open(symbol: str | None = None, now: datetime | None = None) -> bool:
    """One Asian venue, or (without a symbol) whether ANY of them is trading.

    The no-symbol form exists only for the session banner; the engine always
    passes a symbol, because "Asia is open" is not a fact any single
    instrument can act on.
    """
    syms = [symbol] if symbol else list(_ASIA_SESSION)
    base = now or datetime.now(timezone.utc)
    for s in syms:
        spec = _ASIA_SESSION.get(s)
        if not spec:
            continue
        tz, o, c, lf, lt = spec
        local = base.astimezone(tz)
        if local.weekday() >= 5:
            continue
        mins = local.hour * 60 + local.minute
        if not (o <= mins < c):
            continue
        if lf is not None and lf <= mins < lt:
            continue
        return True
    return False


def market_open_for(source: str, symbol: str | None = None) -> bool:
    """Per-source session check.

    This must stay per-instrument. A single global gate keyed on NSE would
    freeze crypto, which trades 24/7, and would hold the US desk shut through
    its entire session (20:00-02:30 IST).
    """
    if source == "crypto":
        return True
    if source == "us":
        return us_market_open()
    if source == "europe":
        return europe_market_open()
    if source == "commodity":
        return commodity_market_open()
    if source == "asia":
        return asia_market_open(symbol)
    return nse_market_open()


def session_status() -> list[dict]:
    """Which venues are open right now — for the UI and the activity log."""
    return [
        {"source": "india", "label": "NSE", "open": nse_market_open(),
         "hours": "09:15-15:30 IST Mon-Fri"},
        {"source": "us", "label": "US", "open": us_market_open(),
         "hours": "09:30-16:00 ET (20:00-02:30 IST) Mon-Fri"},
        {"source": "europe", "label": "Europe", "open": europe_market_open(),
         "hours": "09:00-17:30 CET (13:30-22:00 IST) Mon-Fri"},
        {"source": "commodity", "label": "Commodities", "open": commodity_market_open(),
         "hours": "Globex 18:00-17:00 ET Sun-Fri"},
        {"source": "asia", "label": "Asia", "open": asia_market_open(),
         "hours": "ASX 04:30 / Tokyo-Seoul 05:30 / SG 06:30 / HK 07:00 IST"},
        {"source": "crypto", "label": "Crypto", "open": True, "hours": "24/7"},
    ]


def get_universe() -> list[dict]:
    return UNIVERSE


def yf_underlying(symbol: str, source: str, label: str) -> Underlying | None:
    """Generic yfinance spot + short history, converted to INR.

    Covers every venue that is just "a price on Yahoo": European indices,
    Globex metals and energy, and the coins Deribit does not list. The
    venue-specific adapters above stay as they are because they do more than
    fetch a price (India models its own chain, crypto reads Deribit's index).
    """
    meta = _YF_VENUE.get(symbol)
    if not meta:
        return None
    ticker, ccy = meta["ticker"], meta["ccy"]
    fx = fx_to_inr(ccy)
    if not fx:
        # Better to skip the symbol than to size a position off a guessed
        # exchange rate.
        return None
    try:
        t = _yf_ticker(ticker)
        hist = t.history(period="5d", interval="5m")
        closes = [float(x) * fx for x in hist["Close"].dropna().tolist()][-60:]
        if not closes:
            closes = [float(t.history(period="1d")["Close"].iloc[-1]) * fx]
        return Underlying(symbol=symbol, source=source, label=label,
                          spot=closes[-1], market_open=market_open_for(source, symbol),
                          history=closes)
    except Exception:
        return None


def get_underlying(symbol: str, source: str, label: str) -> Underlying | None:
    if source in ("europe", "commodity", "asia"):
        return yf_underlying(symbol, source, label)
    if source == "crypto":
        # Deribit lists BTC and ETH only; the rest come from Yahoo.
        if symbol in _YF_VENUE:
            return yf_underlying(symbol, source, label)
        return crypto_underlying(symbol, label)
    if source == "us":
        return us_underlying(symbol, label)
    return india_underlying(symbol, label)


def get_atm(u: Underlying, side: str, instrument: str = "option") -> OptionQuote | None:
    """Tradeable instrument for a side.

    `instrument="underlying"` trades the index/stock/coin directly instead of
    an ATM option — ~100x cheaper per round trip, with linear P&L and no theta.
    """
    if instrument == "underlying":
        return underlying_quote(u, side)
    if u.source in ("europe", "commodity", "asia") or u.symbol in _YF_VENUE:
        # No options chain is modelled for these venues. Refuse rather than
        # fabricate a premium -- a made-up chain is how phantom P&L got in
        # last time.
        return None
    if u.source == "crypto":
        return crypto_atm(u.symbol, u.spot, side)
    if u.source == "us":
        return us_atm(u.symbol, u.spot, side)
    return india_atm(u.symbol, u.spot, side, u.history)


def is_underlying(q: "OptionQuote | None") -> bool:
    """True for a quote that represents the underlying rather than an option."""
    return bool(q is not None and q.expiry == "" and "UNDERLYING" in (q.instrument or ""))


def leverage(q: "OptionQuote") -> float:
    """How much the PREMIUM moves for a 1% move in the underlying.

    This is the number that decides whether a premium-based stop is a stop or
    a coin flip. A short-dated ATM option runs 30x-160x, so a "4% stop on
    premium" is really a stop on a 0.025%-0.14% move in the underlying —
    comfortably inside the bid-ask spread and sub-second noise. Risk has to be
    expressed in UNDERLYING terms and converted, never set as a flat
    percentage of premium.
    """
    from . import option_pricing as op
    try:
        if q is None or q.mark <= 0 or q.spot <= 0:
            return 0.0
        # The underlying is delta 1 by definition — don't route it through
        # Black-Scholes, which has no meaningful answer at iv = 0.
        if is_underlying(q):
            return 1.0
        iv = q.iv if q.iv and q.iv > 0 else 0.5
        g = op.bs(q.spot, q.strike, max(q.dte, 0.05) / 365.0, iv, q.kind)
        d = abs(float(g.delta))
        lev = d * q.spot / q.mark
        return float(lev) if lev == lev and lev > 0 else 0.0
    except Exception:
        return 0.0


def premium_band(q: "OptionQuote", spot_move_pct: float,
                 cap: float = 0.90) -> float:
    """Premium move (as a fraction) implied by a given move in the underlying.

    Capped, because deep leverage would otherwise ask for a stop beyond a
    total loss of premium — at which point the option itself is the stop.
    """
    lev = leverage(q)
    if lev <= 0:
        return cap
    return float(min(spot_move_pct * lev, cap))


def implied_vol(price: float, spot: float, strike: float, dte: float,
                kind: str, lo: float = 0.01, hi: float = 3.0) -> float | None:
    """Back the IV out of a known premium by bisection on Black-Scholes."""
    from . import option_pricing as op
    if not (price > 0 and spot > 0 and strike > 0 and dte > 0):
        return None
    T = dte / 365.0
    try:
        if float(op.bs(spot, strike, T, hi, kind).price) < price:
            return hi
        if float(op.bs(spot, strike, T, lo, kind).price) > price:
            return lo
        for _ in range(60):
            mid = (lo + hi) / 2
            if float(op.bs(spot, strike, T, mid, kind).price) < price:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2
    except Exception:
        return None


def requote_mark(source: str, underlying: str, kind: str, strike: float,
                 expiry: str, instrument: str, spot: float,
                 entry_premium: float | None = None,
                 entry_spot: float | None = None,
                 entry_dte: float | None = None) -> float | None:
    """Current INR premium for an already-open option, for marking TP/SL.

    Re-prices the SAME strike and expiry, so theta is charged honestly as the
    position ages instead of the mark being frozen.

    **The IV is pinned to the entry.** Previously the mark re-estimated IV from
    a realised-vol cache while the entry had used the underlying's own history.
    When those two disagreed — different windows, a cold cache, a 20s TTL — the
    mark jumped the instant the position opened and tripped TP or SL on nothing
    but estimation noise. In a dry run that printed the identical +Rs 4,100
    "profit" on every cycle, which is the signature of P&L created out of a
    model inconsistency rather than a price move.

    Pinning the entry IV means the mark can only move with SPOT and TIME, which
    is what the position's P&L actually depends on. Falls back to the realised
    -vol estimate only when the entry premium is unavailable.
    """
    try:
        import datetime as dt
        from . import option_pricing as op

        # An underlying position marks at the underlying. No decay, no model.
        if "UNDERLYING" in (instrument or ""):
            return float(spot) if spot and spot > 0 else None

        # US and crypto have real chains, so mark from the live quote rather
        # than a model. Only India is model-priced.
        if source == "crypto":
            fx = usdinr()
            tk = _deribit("ticker", {"instrument_name": instrument})
            bid_c = tk.get("best_bid_price") or 0.0
            ask_c = tk.get("best_ask_price") or 0.0
            coin = ((bid_c + ask_c) / 2) if (bid_c and ask_c) else (tk.get("mark_price") or 0.0)
            px = float(coin) * spot            # spot already in INR
            return px if px > 0 else None
        if source == "us":
            fx = usdinr()
            t = _yf_ticker(underlying)
            chain = t.option_chain(expiry)
            _us_cache[f"{underlying}:{expiry}"] = (time.time(), chain)
            df = chain.calls if kind == "call" else chain.puts
            k_usd = strike / fx if fx else strike
            row = df.loc[(df["strike"] - k_usd).abs().idxmin()]
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
            px = ((bid + ask) / 2 if bid > 0 and ask > 0
                  else float(row.get("lastPrice") or 0.0)) * fx
            return px if px > 0 else None

        sym = underlying.upper()
        try:
            d = dt.date.fromisoformat(expiry)
            dte = max((d - dt.date.today()).days, 0.05)
        except Exception:
            dte = 1.0

        iv = None
        if entry_premium and entry_premium > 0 and entry_spot and entry_spot > 0:
            iv = implied_vol(entry_premium, entry_spot,
                             strike, entry_dte or dte, kind)
        if iv is None:
            hist = _spot_cache.get(sym, (0.0, []))[1]
            iv = op.iv_estimate(_realised_vol(hist), dte)

        g = op.bs(spot, strike, dte / 365.0, iv, kind)
        px = float(g.price)
        return px if px > 0 and px == px else None
    except Exception:
        return None


def aclose() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
