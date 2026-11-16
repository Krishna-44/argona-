"""AngelOne SmartAPI adapter — MARKET DATA ONLY.

Hard rule: this module exposes quotes, candles and the option chain. It does
NOT place, modify or cancel orders, and must never be extended to. Argona is a
paper/research desk; real execution stays in your own hands.

Two tiers:
  * The instrument master is a PUBLIC file (no credentials) — it gives every
    NFO contract: symbol, strike, expiry, token. That alone builds the chain's
    structure and works out of the box.
  * Live prices (LTP / OI) need a SmartAPI session: api key + client code +
    MPIN + TOTP secret, read from .env. Without them the chain still renders,
    just without live premiums.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

SCRIP_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE = Path(__file__).parent.parent / "data" / "nfo_scrips.json"
CACHE.parent.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 12 * 3600  # the master is republished daily

INDEX_NAMES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]


def _env(k: str) -> str:
    return (os.environ.get(k) or "").strip()


def creds_present() -> bool:
    return all(_env(k) for k in
               ("ANGELONE_API_KEY", "ANGELONE_CLIENT_CODE",
                "ANGELONE_PIN", "ANGELONE_TOTP_SECRET"))


def creds_status() -> dict:
    return {
        "api_key": bool(_env("ANGELONE_API_KEY")),
        "client_code": bool(_env("ANGELONE_CLIENT_CODE")),
        "pin": bool(_env("ANGELONE_PIN")),
        "totp_secret": bool(_env("ANGELONE_TOTP_SECRET")),
        "ready": creds_present(),
        "note": "Market data only — Argona never places orders.",
    }


# ---------------------------------------------------------------------------
# Instrument master (public, no auth)


def _load_cache() -> list[dict] | None:
    try:
        if CACHE.exists() and time.time() - CACHE.stat().st_mtime < CACHE_TTL:
            return json.loads(CACHE.read_text())
    except Exception:
        pass
    return None


def nfo_scrips(force: bool = False) -> list[dict]:
    """All NFO index-option contracts, cached locally (the full master is
    ~39 MB; we keep only what the chain needs)."""
    if not force:
        c = _load_cache()
        if c is not None:
            return c
    r = httpx.get(SCRIP_URL, timeout=120)
    r.raise_for_status()
    rows = r.json()
    keep = []
    for d in rows:
        if d.get("exch_seg") != "NFO":
            continue
        itype = d.get("instrumenttype")
        if itype not in ("OPTIDX", "OPTSTK"):
            continue
        if itype == "OPTIDX" and d.get("name") not in INDEX_NAMES:
            continue
        try:
            strike = float(d.get("strike", 0)) / 100.0  # master stores paise
        except Exception:
            continue
        keep.append({
            "token": d.get("token"), "symbol": d.get("symbol"),
            "itype": itype,
            "name": d.get("name"), "expiry": d.get("expiry"),
            "strike": strike,
            "kind": "CE" if str(d.get("symbol", "")).endswith("CE") else "PE",
            "lotsize": int(float(d.get("lotsize", 0) or 0)),
        })
    CACHE.write_text(json.dumps(keep))
    return keep


def fo_lot_size(symbol: str) -> int | None:
    """Actual NSE F&O lot size for an index or stock, from the instrument
    master (stock lots differ wildly: RELIANCE 500, TCS 175, ...)."""
    s = symbol.upper().strip()
    try:
        rows = nfo_scrips()
    except Exception:
        return None
    for r in rows:
        if (r.get("name") or "").upper() == s and r.get("lotsize"):
            return int(r["lotsize"])
    return None


def strike_ladder(name: str, expiry: str | None = None) -> list[float]:
    """Every strike that actually trades for this underlying and expiry.

    Straight from the public instrument master, so it is the real ladder
    rather than an assumed step. Returns [] if the master is unavailable.
    """
    sym = name.upper()
    try:
        rows = nfo_scrips()
    except Exception:
        return []
    if expiry is None:
        exps = expiries(sym)
        expiry = exps[0] if exps else None
    ks = {float(r["strike"]) for r in rows
          if str(r.get("name", "")).upper() == sym
          and (expiry is None or r.get("expiry") == expiry)
          and float(r.get("strike") or 0) > 0}
    return sorted(ks)


def snap_real_strike(name: str, spot: float, steps: int = 0,
                     expiry: str | None = None) -> float | None:
    """Nearest REAL strike to spot, then `steps` rungs up/down the real ladder.

    The modelled grid in option_pricing guesses a step size, and it guesses
    wrong: it produced RELIANCE 1335 and ICICIBANK 1605 when both trade on a
    10-rupee ladder (1330/1340, 1600/1610). Suggesting a strike that does not
    exist is worse than suggesting a bad one — it cannot be placed at all.
    Returns None when the master has nothing, so callers can fall back.
    """
    ks = strike_ladder(name, expiry)
    if not ks or not (spot > 0):
        return None
    i = min(range(len(ks)), key=lambda j: abs(ks[j] - spot))
    return ks[max(0, min(len(ks) - 1, i + steps))]


def _expiry_key(e: str) -> tuple:
    """Sort '17NOV2026' chronologically."""
    months = {m: i for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
    try:
        d, mon, y = int(e[:2]), months[e[2:5]], int(e[5:])
        return (y, mon, d)
    except Exception:
        return (9999, 99, 99)


def expiries(name: str = "NIFTY") -> list[str]:
    rows = [s for s in nfo_scrips() if s["name"] == name.upper()]
    return sorted({s["expiry"] for s in rows}, key=_expiry_key)


# ---------------------------------------------------------------------------
# Authenticated session (optional)

_session: dict | None = None


def login() -> dict | None:
    """Create a SmartAPI session. Returns None when credentials are absent."""
    global _session
    if not creds_present():
        return None
    if _session and time.time() - _session["ts"] < 3600:
        return _session
    try:
        from SmartApi.smartConnect import SmartConnect
        import pyotp
        sc = SmartConnect(api_key=_env("ANGELONE_API_KEY"))
        otp = pyotp.TOTP(_env("ANGELONE_TOTP_SECRET")).now()
        data = sc.generateSession(_env("ANGELONE_CLIENT_CODE"), _env("ANGELONE_PIN"), otp)
        if not data or not data.get("status"):
            return None
        _session = {"sc": sc, "ts": time.time()}
        return _session
    except Exception:
        return None


def ltp_many(tokens: list[str], exchange: str = "NFO",
             symbols: dict[str, str] | None = None) -> dict[str, dict]:
    """Last-traded price per token. Empty dict if not authenticated."""
    s = login()
    if not s:
        return {}
    sc = s["sc"]
    out: dict[str, dict] = {}
    for t in tokens:
        try:
            sym = (symbols or {}).get(t, "")
            res = sc.ltpData(exchange, sym, t)
            if res and res.get("status") and res.get("data"):
                d = res["data"]
                out[t] = {"ltp": d.get("ltp"), "close": d.get("close"),
                          "open": d.get("open"), "high": d.get("high"), "low": d.get("low")}
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Historical intraday candles — the reason to connect a broker at all.
# yfinance caps 5-minute history at 60 days, which is far too short to
# validate an intraday strategy. SmartAPI goes back years.

CASH_CACHE = Path(__file__).parent.parent / "data" / "nse_scrips.json"

# SmartAPI interval names -> (api_name, max days per request)
INTERVALS = {
    "1m": ("ONE_MINUTE", 25), "3m": ("THREE_MINUTE", 60), "5m": ("FIVE_MINUTE", 90),
    "10m": ("TEN_MINUTE", 90), "15m": ("FIFTEEN_MINUTE", 180),
    "30m": ("THIRTY_MINUTE", 180), "1h": ("ONE_HOUR", 365), "1d": ("ONE_DAY", 1800),
}

# Index names as they appear in the NSE cash segment of the master.
INDEX_ALIAS = {
    "NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank",
    "FINNIFTY": "Nifty Fin Service", "MIDCPNIFTY": "NIFTY MID SELECT",
}


def nse_scrips(force: bool = False) -> list[dict]:
    """NSE cash-segment instruments (indices + equities) for token lookup."""
    if not force:
        try:
            if CASH_CACHE.exists() and time.time() - CASH_CACHE.stat().st_mtime < CACHE_TTL:
                return json.loads(CASH_CACHE.read_text())
        except Exception:
            pass
    r = httpx.get(SCRIP_URL, timeout=120)
    r.raise_for_status()
    keep = []
    for d in r.json():
        if d.get("exch_seg") != "NSE":
            continue
        keep.append({"token": d.get("token"), "symbol": d.get("symbol"),
                     "name": d.get("name")})
    CASH_CACHE.write_text(json.dumps(keep))
    return keep


def resolve_token(symbol: str) -> tuple[str, str] | None:
    """(token, tradingsymbol) for an NSE index or equity, or None."""
    s = symbol.upper().strip()
    rows = nse_scrips()
    want_index = INDEX_ALIAS.get(s)
    if want_index:
        for d in rows:
            if (d.get("name") or "").upper() == want_index.upper():
                return str(d["token"]), d["symbol"]
    # equities trade as "RELIANCE-EQ"
    for d in rows:
        if (d.get("symbol") or "").upper() == f"{s}-EQ":
            return str(d["token"]), d["symbol"]
    for d in rows:
        if (d.get("name") or "").upper() == s:
            return str(d["token"]), d["symbol"]
    return None


def candles_by_token(token: str, exchange: str = "NSE", interval: str = "5m",
                     days: int = 365):
    """Historical OHLCV for any instrument token — works for NSE cash AND for
    NFO option contracts, which is what lets us measure the REAL implied-vol
    premium instead of assuming one."""
    import pandas as pd
    from datetime import datetime, timedelta

    s = login()
    if not s:
        return None
    iv = INTERVALS.get(interval)
    if not iv:
        return None
    api_iv, max_days = iv
    sc = s["sc"]

    frames = []
    end = datetime.now()
    remaining = days
    while remaining > 0:
        span = min(remaining, max_days)
        start = end - timedelta(days=span)
        try:
            res = sc.getCandleData({
                "exchange": exchange, "symboltoken": str(token), "interval": api_iv,
                "fromdate": start.strftime("%Y-%m-%d %H:%M"),
                "todate": end.strftime("%Y-%m-%d %H:%M"),
            })
            rows = (res or {}).get("data") or []
            if rows:
                frames.append(pd.DataFrame(
                    rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"]))
            else:
                break
        except Exception:
            break
        end = start - timedelta(minutes=1)
        remaining -= span

    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Close"])


def measure_iv_premium(name: str = "NIFTY", expiry: str | None = None,
                       interval: str = "1d", days: int = 30) -> dict:
    """Measure the ACTUAL implied-vol premium: back out IV from real ATM option
    prices and compare it to the realized vol that followed.

    This is the number that decides whether selling premium has an edge. Our
    backtest currently *assumes* IV = RV x 1.25; breakeven is around x1.12.
    Requires SmartAPI credentials.
    """
    import math
    import numpy as np
    from . import backtest as bt
    from . import option_pricing as op

    if not creds_present():
        return {"ok": False, "error": "SmartAPI credentials required (.env)",
                "why": "needs real historical option premiums"}

    spot_df = bt.fetch_history(name, f"{days+40}d", interval)
    if spot_df is None or len(spot_df) < 20:
        return {"ok": False, "error": "no underlying history"}
    spot = float(spot_df["Close"].iloc[-1])

    ch = option_chain(name, expiry, around=0, spot=spot)
    if not ch.get("ok") or not ch["rows"]:
        return {"ok": False, "error": "no chain"}
    row = ch["rows"][0]
    results = []
    for side in ("CE", "PE"):
        leg = row.get(side)
        if not leg:
            continue
        od = candles_by_token(leg["token"], "NFO", interval, days)
        if od is None or od.empty:
            continue
        prem = float(od["Close"].iloc[-1])
        # crude IV inversion by bisection on the BS price
        K = float(row["strike"])
        T = 7 / 365.0
        kind = "call" if side == "CE" else "put"
        lo_v, hi_v = 0.01, 3.0
        for _ in range(60):
            mid = (lo_v + hi_v) / 2
            if op.bs(spot, K, T, mid, kind).price < prem:
                lo_v = mid
            else:
                hi_v = mid
        results.append({"side": side, "premium": prem, "implied_vol": round((lo_v + hi_v) / 2, 4)})

    if not results:
        return {"ok": False, "error": "no option history returned"}
    rets = spot_df["Close"].pct_change().dropna()
    per_year = 252 if interval == "1d" else 252 * 6
    rv = float(rets.tail(20).std() * math.sqrt(per_year))
    iv_avg = float(np.mean([r["implied_vol"] for r in results]))
    ratio = iv_avg / rv if rv > 0 else None
    return {
        "ok": True, "name": name, "strike": row["strike"], "legs": results,
        "implied_vol": round(iv_avg, 4), "realized_vol": round(rv, 4),
        "iv_premium_ratio": (round(ratio, 3) if ratio else None),
        "breakeven_ratio": 1.12,
        "verdict": (None if not ratio else
                    ("selling has edge at this sample" if ratio > 1.12
                     else "selling has NO edge at this sample")),
        "caveat": "single snapshot — needs many expiries over time to be evidence",
    }


def candles(symbol: str, interval: str = "5m", days: int = 365):
    """Historical OHLCV as a DataFrame (yfinance-compatible columns), or None.

    Chunks requests to respect SmartAPI's per-call day limits and stitches the
    pieces together. Requires credentials.
    """
    import pandas as pd
    from datetime import datetime, timedelta

    s = login()
    if not s:
        return None
    iv = INTERVALS.get(interval)
    tok = resolve_token(symbol)
    if not iv or not tok:
        return None
    api_iv, max_days = iv
    token, _ts = tok
    sc = s["sc"]

    frames = []
    end = datetime.now()
    remaining = days
    while remaining > 0:
        span = min(remaining, max_days)
        start = end - timedelta(days=span)
        try:
            res = sc.getCandleData({
                "exchange": "NSE", "symboltoken": token, "interval": api_iv,
                "fromdate": start.strftime("%Y-%m-%d %H:%M"),
                "todate": end.strftime("%Y-%m-%d %H:%M"),
            })
            rows = (res or {}).get("data") or []
            if rows:
                frames.append(pd.DataFrame(
                    rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"]))
        except Exception:
            break
        end = start - timedelta(minutes=1)
        remaining -= span
        if not frames or len(frames[-1]) == 0:
            break

    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Close"])


def option_chain(name: str = "NIFTY", expiry: str | None = None,
                 around: int = 10, spot: float | None = None) -> dict:
    """Option chain for an index: strikes around ATM with CE/PE paired.

    Structure always works (public master). `live` is True only when a
    SmartAPI session supplied real premiums.
    """
    name = name.upper()
    exps = expiries(name)
    if not exps:
        return {"ok": False, "error": "no contracts found", "name": name}
    expiry = expiry or exps[0]
    rows = [s for s in nfo_scrips() if s["name"] == name and s["expiry"] == expiry]
    if not rows:
        return {"ok": False, "error": f"no contracts for {expiry}", "name": name}

    strikes = sorted({r["strike"] for r in rows})
    if spot is None:
        spot = strikes[len(strikes) // 2]
    atm = min(strikes, key=lambda k: abs(k - spot))
    i = strikes.index(atm)
    window = strikes[max(0, i - around): i + around + 1]

    by = {(r["strike"], r["kind"]): r for r in rows}
    tokens, symmap = [], {}
    for k in window:
        for side in ("CE", "PE"):
            r = by.get((k, side))
            if r:
                tokens.append(str(r["token"]))
                symmap[str(r["token"])] = r["symbol"]

    quotes = ltp_many(tokens, "NFO", symmap) if creds_present() else {}

    chain = []
    for k in window:
        ce, pe = by.get((k, "CE")), by.get((k, "PE"))
        def q(r):
            if not r:
                return None
            d = quotes.get(str(r["token"]))
            return {"token": r["token"], "symbol": r["symbol"],
                    "ltp": (d or {}).get("ltp"), "lotsize": r["lotsize"]}
        chain.append({"strike": k, "atm": k == atm, "CE": q(ce), "PE": q(pe)})

    return {
        "ok": True, "name": name, "expiry": expiry, "expiries": exps[:8],
        "atm": atm, "spot": spot, "rows": chain,
        "live": bool(quotes), "creds": creds_status(),
        "lotsize": (rows[0]["lotsize"] if rows else None),
    }
