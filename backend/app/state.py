"""SQLite store for the options scalper — trades, account equity, activity.

The account runs in INR. Balance = initial + sum(net P&L of closed trades),
where net is already after India charges + tax. Everything the dashboard shows
is derived live from these tables.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


DB_PATH = Path(os.environ.get("ARGONA_DB", Path(__file__).parent.parent / "data" / "argona.sqlite"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open       REAL NOT NULL,
    underlying    TEXT NOT NULL,
    source        TEXT NOT NULL,
    kind          TEXT NOT NULL,        -- call | put (direction in both modes)
    segment       TEXT NOT NULL DEFAULT 'options',  -- options | futures
    pair_id       TEXT,                 -- links the two legs of a pair trade
    strike        REAL NOT NULL,
    expiry        TEXT NOT NULL,
    instrument    TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    qty           REAL NOT NULL,        -- units (contracts * lot)
    entry_premium REAL NOT NULL,
    entry_spot    REAL NOT NULL,
    tp_premium    REAL NOT NULL,
    sl_premium    REAL NOT NULL,
    features      TEXT,                 -- JSON feature vector at entry
    mark_premium  REAL,                 -- last mark (live)
    status        TEXT NOT NULL DEFAULT 'open',
    ts_close      REAL,
    exit_premium  REAL,
    exit_spot     REAL,                 -- underlying price at exit, for charting
    exit_reason   TEXT,                 -- TP | SL | EXPIRY | SIGNAL
    gross         REAL,
    charges       REAL,
    tax           REAL,
    net           REAL
);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, balance REAL NOT NULL, net_cycle REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(id DESC);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    return c


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    c = _conn()
    try:
        yield c
    finally:
        c.close()


def _gen_account_id() -> str:
    import secrets
    return "0x" + secrets.token_hex(20)


def init_db(initial_balance: float) -> None:
    with db() as c:
        c.executescript(SCHEMA)
        # Additive migration: a database created before the underlying-trading
        # mode has no `segment`, and charges differ between the two.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(trades)")}
        if "segment" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN segment TEXT NOT NULL DEFAULT 'options'")
        if "exit_spot" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN exit_spot REAL")
        if "pair_id" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN pair_id TEXT")
        existing = {r["key"] for r in c.execute("SELECT key FROM meta")}
        seed = {
            "initial_balance": initial_balance,
            "started_at": time.time(),
            "mode": "SCALP",
            "account_id": _gen_account_id(),
            "strategy": "momentum_scalp",
        }
        for k, v in seed.items():
            if k not in existing:
                c.execute("INSERT INTO meta(key,value) VALUES(?,?)", (k, json.dumps(v)))


def get_meta(key: str, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default


def set_meta(key: str, value) -> None:
    with db() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


# ---------------------------------------------------------------------------


@dataclass
class Kpis:
    balance: float
    initial: float
    total_net: float
    total_pnl_pct: float
    wins: int
    losses: int
    win_rate: float
    best: float
    worst: float
    avg_net: float
    charges_paid: float
    tax_paid: float
    gross_total: float
    open_trades: int
    total_trades: int
    # Live mark-to-market on positions still open. `balance` only moves when a
    # trade CLOSES, so without these the money appears frozen while a position
    # is running. `equity` is what the account is actually worth right now.
    unrealized: float = 0.0
    equity: float = 0.0
    open_notional: float = 0.0


def compute_kpis() -> Kpis:
    initial = float(get_meta("initial_balance", 1000.0))
    with db() as c:
        r = c.execute("""
            SELECT
              COUNT(*) AS n,
              SUM(CASE WHEN net>0 THEN 1 ELSE 0 END) AS w,
              SUM(CASE WHEN net<0 THEN 1 ELSE 0 END) AS l,
              SUM(net) AS net, SUM(charges) AS ch, SUM(tax) AS tx, SUM(gross) AS gross,
              MAX(net) AS best, MIN(net) AS worst, AVG(net) AS avgn
            FROM trades WHERE status='closed'
        """).fetchone()
        n = int(r["n"] or 0)
        wins = int(r["w"] or 0)
        losses = int(r["l"] or 0)
        net = float(r["net"] or 0.0)
        openn = c.execute("SELECT COUNT(*) AS n FROM trades WHERE status='open'").fetchone()["n"]
        totn = c.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
        # Mark-to-market the open book. mark_premium is refreshed every cycle
        # by the engine, and for a SHORT underlying it is already mirrored so
        # that (mark - entry) is the gain either way.
        opens = c.execute("""SELECT qty, entry_premium, mark_premium
                             FROM trades WHERE status='open'""").fetchall()
    unreal = 0.0
    open_not = 0.0
    for o in opens:
        q = float(o["qty"] or 0.0)
        ep = float(o["entry_premium"] or 0.0)
        mp = float(o["mark_premium"] if o["mark_premium"] is not None else ep)
        unreal += (mp - ep) * q
        open_not += ep * q
    decided = wins + losses
    return Kpis(
        balance=initial + net,
        initial=initial,
        total_net=net,
        total_pnl_pct=(net / initial * 100.0) if initial else 0.0,
        wins=wins, losses=losses,
        win_rate=(wins / decided * 100.0) if decided else 0.0,
        best=float(r["best"] or 0.0), worst=float(r["worst"] or 0.0),
        avg_net=float(r["avgn"] or 0.0),
        charges_paid=float(r["ch"] or 0.0), tax_paid=float(r["tx"] or 0.0),
        gross_total=float(r["gross"] or 0.0),
        open_trades=int(openn), total_trades=int(totn),
        unrealized=unreal, equity=initial + net + unreal,
        open_notional=open_not,
    )


# ---------------------------------------------------------------------------
# activity log


def append_activity(kind: str, body: dict) -> dict:
    ts = time.time()
    with db() as c:
        cur = c.execute("INSERT INTO activity(ts,kind,body) VALUES(?,?,?)",
                        (ts, kind, json.dumps(body)))
        return {"id": cur.lastrowid, "ts": ts, "kind": kind, "body": body}


def recent_activity(limit: int = 150) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT id,ts,kind,body FROM activity ORDER BY id DESC LIMIT ?",
                         (limit,)).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "body": json.loads(r["body"])}
                for r in rows]


# ---------------------------------------------------------------------------
# trades


def open_trade(*, underlying, source, kind, strike, expiry, instrument, strategy, qty,
               entry_premium, entry_spot, tp_premium, sl_premium, features,
               segment: str = "options", pair_id: str | None = None) -> int:
    with db() as c:
        cur = c.execute("""
            INSERT INTO trades(ts_open,underlying,source,kind,segment,pair_id,strike,expiry,instrument,strategy,
                               qty,entry_premium,entry_spot,tp_premium,sl_premium,features,mark_premium,status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')
        """, (time.time(), underlying, source, kind, segment, pair_id, strike, expiry, instrument, strategy,
              qty, entry_premium, entry_spot, tp_premium, sl_premium,
              json.dumps(features), entry_premium))
        return int(cur.lastrowid)


def mark_trade(trade_id: int, mark_premium: float) -> None:
    with db() as c:
        c.execute("UPDATE trades SET mark_premium=? WHERE id=?", (mark_premium, trade_id))


def close_trade(trade_id: int, exit_premium: float, reason: str,
                gross: float, charges: float, tax: float, net: float,
                exit_spot: float | None = None) -> None:
    with db() as c:
        c.execute("""
            UPDATE trades SET status='closed', ts_close=?, exit_premium=?, exit_spot=?,
                   exit_reason=?, gross=?, charges=?, tax=?, net=?, mark_premium=?
            WHERE id=?
        """, (time.time(), exit_premium, exit_spot, reason, gross, charges, tax, net,
              exit_premium, trade_id))


def open_trades() -> list[dict]:
    with db() as c:
        rows = c.execute("""
            SELECT id,ts_open,underlying,source,kind,segment,pair_id,strike,expiry,instrument,strategy,qty,
                   entry_premium,entry_spot,tp_premium,sl_premium,features,mark_premium
            FROM trades WHERE status='open' ORDER BY id DESC
        """).fetchall()
        return [dict(r) for r in rows]


def recent_trades(limit: int = 40) -> list[dict]:
    with db() as c:
        rows = c.execute("""
            SELECT id,ts_open,ts_close,underlying,source,kind,segment,pair_id,strike,instrument,strategy,qty,
                   entry_premium,exit_premium,entry_spot,exit_spot,exit_reason,
                   gross,charges,tax,net,status
            FROM trades WHERE status='closed' ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def record_equity(balance: float, net_cycle: float) -> None:
    with db() as c:
        c.execute("INSERT INTO equity(ts,balance,net_cycle) VALUES(?,?,?)",
                  (time.time(), balance, net_cycle))


def equity_history(limit: int = 600) -> list[dict]:
    """The most recent `limit` equity points, oldest-first for charting.

    This used to be `ORDER BY id ASC LIMIT ?`, which returns the OLDEST rows.
    That looks fine until the table passes `limit` rows — after which the
    window never moves again and the equity curve is frozen forever while the
    desk carries on recording behind it. Take the newest rows, then reverse so
    the chart still reads left-to-right in time.
    """
    with db() as c:
        rows = c.execute("SELECT id,ts,balance,net_cycle FROM equity ORDER BY id DESC LIMIT ?",
                         (limit,)).fetchall()
        return [{"i": r["id"], "ts": r["ts"], "balance": r["balance"], "net": r["net_cycle"]}
                for r in reversed(rows)]


def account_report() -> dict:
    k = compute_kpis()
    return {
        "account_id": get_meta("account_id", "0x0"),
        "balance": k.balance,
        "initial": k.initial,
        "net_pnl": k.total_net,
        "net_pnl_pct": k.total_pnl_pct,
        "charges_paid": k.charges_paid,
        "tax_paid": k.tax_paid,
        "gross_total": k.gross_total,
        "strategy": get_meta("strategy", "momentum_scalp"),
    }
