"""India F&O charges + tax model, applied per round-trip trade.

**The account runs in INR.** Every amount here is rupees: brokerage is the
real flat ₹20 per executed order, and the percentage legs are the NSE rate
card applied to rupee premium turnover.

Rates reflect the standard NSE options rate card (2024-ish). All configurable
via env so you can dial them to your broker.

  * Brokerage        : flat ₹20 per executed order (buy + sell = 2 orders)
  * STT              : on the SELL side only (options: 0.0625% of premium)
  * Exchange txn     : both sides (NSE options ~0.03503% of premium)
  * SEBI             : both sides (0.0001%)
  * Stamp duty       : BUY side only (0.003%)
  * GST              : 18% on (brokerage + exchange txn + SEBI)

Tax: F&O profit in India is non-speculative business income, taxed at the
slab rate — we model it as a configurable flat rate on NET realized gains
(default 30%). Losses carry no tax (and in reality offset gains; we tax only
positive net P&L per trade here, which is the conservative display).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# NSE F&O **options** rate card (premium turnover basis).
BROKERAGE_PER_ORDER = float(os.environ.get("ARGONA_BROKERAGE", "20.0"))  # flat ₹20 per executed order
STT_SELL = float(os.environ.get("ARGONA_STT", "0.001"))                  # 0.10% on SELL premium
EXCH_TXN = float(os.environ.get("ARGONA_EXCH_TXN", "0.0003553"))         # 0.03553% both sides
SEBI = float(os.environ.get("ARGONA_SEBI", "0.000001"))                  # 0.0001%
STAMP_BUY = float(os.environ.get("ARGONA_STAMP", "0.00003"))             # 0.003% on BUY
GST = float(os.environ.get("ARGONA_GST", "0.18"))                        # 18% on (brokerage+txn+sebi)
TAX_RATE = float(os.environ.get("ARGONA_TAX_RATE", "0.30"))              # 30% slab (F&O = business income)

# Futures differ from options — kept here so the backtester can price both.
FUT_STT_SELL = float(os.environ.get("ARGONA_FUT_STT", "0.0002"))         # 0.02% on SELL
FUT_EXCH_TXN = float(os.environ.get("ARGONA_FUT_EXCH", "0.0000183"))     # 0.00183%
FUT_STAMP_BUY = float(os.environ.get("ARGONA_FUT_STAMP", "0.00002"))     # 0.002% on BUY


@dataclass
class ChargeBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    total: float

    def as_dict(self) -> dict:
        return {
            "brokerage": round(self.brokerage, 4),
            "stt": round(self.stt, 4),
            "exchange": round(self.exchange, 4),
            "sebi": round(self.sebi, 4),
            "stamp": round(self.stamp, 4),
            "gst": round(self.gst, 4),
            "total": round(self.total, 4),
        }


def round_trip_charges(entry_premium: float, exit_premium: float, qty: float,
                       segment: str = "options") -> ChargeBreakdown:
    """entry/exit premium are per-unit; qty = units (contracts * lot size).
    Buy turnover = entry*qty, Sell turnover = exit*qty.
    `segment` is "options" (premium turnover) or "futures" (contract turnover)."""
    buy_turn = max(0.0, entry_premium) * qty
    sell_turn = max(0.0, exit_premium) * qty

    if segment == "futures":
        stt_rate, exch_rate, stamp_rate = FUT_STT_SELL, FUT_EXCH_TXN, FUT_STAMP_BUY
    else:
        stt_rate, exch_rate, stamp_rate = STT_SELL, EXCH_TXN, STAMP_BUY

    brokerage = BROKERAGE_PER_ORDER * 2
    stt = stt_rate * sell_turn
    exchange = exch_rate * (buy_turn + sell_turn)
    sebi = SEBI * (buy_turn + sell_turn)
    stamp = stamp_rate * buy_turn
    gst = GST * (brokerage + exchange + sebi)
    total = brokerage + stt + exchange + sebi + stamp + gst
    return ChargeBreakdown(brokerage, stt, exchange, sebi, stamp, gst, total)


def tax_on_gain(net_gain_after_charges: float) -> float:
    """Tax on a positive net gain (after charges).

    NOTE: F&O income in India is taxed on the NET for the year — losses offset
    gains. Applying this per-trade overstates the drag badly (a losing year can
    still show tax on its individual winners). Use `tax_on_portfolio` for
    anything that aggregates trades; this per-trade form is only a display
    convenience for a single round trip.
    """
    return TAX_RATE * net_gain_after_charges if net_gain_after_charges > 0 else 0.0


def tax_on_portfolio(total_net_pre_tax: float) -> float:
    """Correct treatment: tax only the net profit across all trades."""
    return TAX_RATE * total_net_pre_tax if total_net_pre_tax > 0 else 0.0


@dataclass
class TradePnl:
    gross: float          # (exit - entry) * qty, before anything
    charges: float
    tax: float
    net: float            # gross - charges - tax  (what actually hits the account)
    breakdown: ChargeBreakdown


def settle(entry_premium: float, exit_premium: float, qty: float,
           segment: str = "options", apply_tax: bool = True) -> TradePnl:
    """Settle one round trip. `apply_tax=False` returns pre-tax figures so a
    portfolio can tax the NET across trades (the correct treatment)."""
    gross = (exit_premium - entry_premium) * qty
    ch = round_trip_charges(entry_premium, exit_premium, qty, segment)
    after_charges = gross - ch.total
    tax = tax_on_gain(after_charges) if apply_tax else 0.0
    net = after_charges - tax
    return TradePnl(gross=gross, charges=ch.total, tax=tax, net=net, breakdown=ch)


def config() -> dict:
    return {
        "brokerage_per_order": BROKERAGE_PER_ORDER,
        "stt_sell_pct": STT_SELL * 100,
        "exchange_txn_pct": EXCH_TXN * 100,
        "sebi_pct": SEBI * 100,
        "stamp_buy_pct": STAMP_BUY * 100,
        "gst_pct": GST * 100,
        "tax_rate_pct": TAX_RATE * 100,
        "regime": "India F&O (NSE rate card) · INR",
    }
