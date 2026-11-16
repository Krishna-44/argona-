"""Emit one line per trade the moment it closes.

Lives in the repo rather than a scratch directory: the previous copy was in a
session scratchpad, which is cleared between sessions, so the watcher died
with the session it was written in.
"""
import json
import time
import urllib.request

API = "http://127.0.0.1:4110/snapshot"
seen: set[int] = set()
seen_open: set[int] = set()
primed = False
fails = 0

while True:
    try:
        with urllib.request.urlopen(API, timeout=10) as r:
            d = json.load(r)
        fails = 0
    except Exception as e:
        fails += 1
        # Silence must never be ambiguous between "no trades" and "desk down".
        if fails in (3, 12):
            print(f"DESK UNREACHABLE ({fails} polls): {e!r}", flush=True)
        time.sleep(20)
        continue

    k = d.get("kpis", {})
    trades = d.get("recent_trades") or []

    opens = d.get("open_trades") or []

    if not primed:
        seen = {t["id"] for t in trades}
        seen_open = {t["id"] for t in opens}
        primed = True
        print(f"watching — settled Rs {k.get('balance', 0):,.2f}, "
              f"{k.get('total_trades', 0)} trades so far", flush=True)
        time.sleep(20)
        continue

    # Report ENTRIES too — knowing a position opened matters as much as
    # knowing it closed, especially while watching whether a newly activated
    # strategy actually fires.
    for t in sorted((x for x in opens if x["id"] not in seen_open),
                    key=lambda x: x.get("ts_open") or 0):
        seen_open.add(t["id"])
        ts = time.strftime("%H:%M:%S", time.localtime(t.get("ts_open") or time.time()))
        side = "LONG" if t.get("kind") == "call" else "SHORT"
        px = float(t.get("entry_spot") or t.get("entry_premium") or 0)
        notional = float(t.get("entry_premium") or 0) * float(t.get("qty") or 0)
        print(
            f"TRADE OPENED {ts} | {t.get('strategy')} | {t.get('underlying')} {side} "
            f"qty {t.get('qty', 0):.4f} @ Rs {px:,.2f} | notional Rs {notional:,.0f} | "
            f"risk {t.get('risk_pct', 0):.2f}% of book | "
            f"TP Rs {float(t.get('tp_premium') or 0):,.2f} SL Rs {float(t.get('sl_premium') or 0):,.2f}",
            flush=True)

    for t in sorted((x for x in trades if x["id"] not in seen),
                    key=lambda x: x.get("ts_close") or 0):
        seen.add(t["id"])
        ts = time.strftime("%H:%M:%S", time.localtime(t.get("ts_close") or time.time()))
        side = "LONG" if t.get("kind") == "call" else "SHORT"
        # A SHORT underlying is marked by mirroring the price around entry so
        # the P&L maths works, but that mirrored number is NOT a price. Shown
        # raw it reads as "price rose and the short made money", which is
        # nonsense. Display the real spot for underlying trades.
        is_und = "UNDERLYING" in (t.get("instrument") or "")
        if is_und and t.get("entry_spot") and t.get("exit_spot"):
            px_in, px_out = float(t["entry_spot"]), float(t["exit_spot"])
        else:
            px_in, px_out = float(t.get("entry_premium") or 0), float(t.get("exit_premium") or 0)
        held = int((t.get("ts_close") or 0) - (t.get("ts_open") or 0))
        print(
            f"TRADE CLOSED {ts} | {t.get('underlying')} {side} "
            f"qty {t.get('qty', 0):.4f} | {t.get('exit_reason')} after {held}s | "
            f"Rs {px_in:,.2f} -> Rs {px_out:,.2f} | "
            f"gross Rs {t.get('gross', 0):+,.2f} charges Rs {t.get('charges', 0):,.2f} "
            f"tax Rs {t.get('tax', 0):,.2f} | NET Rs {t.get('net', 0):+,.2f} | "
            f"balance Rs {k.get('balance', 0):,.2f} | "
            f"record {k.get('wins', 0)}W/{k.get('losses', 0)}L "
            f"({k.get('win_rate', 0):.0f}%)",
            flush=True)

    time.sleep(20)
