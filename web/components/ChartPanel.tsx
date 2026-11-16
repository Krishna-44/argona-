"use client";
import { useEffect, useRef, useState } from "react";
import { CandleChart, type Bar, type Mark, type Fill } from "./CandleChart";

type Levels = {
  price: number; atr: number | null; support: number[]; resistance: number[];
  long: { entry: number; stop_loss: number | null; take_profit: number | null; rr: number };
  short: { entry: number; stop_loss: number | null; take_profit: number | null; rr: number };
  method: string;
};
type Analysis = {
  symbol: string; tv_symbol: string; last_close: number; change_pct: number;
  high_52: number; low_52: number;
  patterns: { counts: Record<string, number>; latest: string[]; latest_score: number; latest_signal: number; bars: number };
  levels: Levels;
};

declare global { interface Window { TradingView?: any } }

const QUICK = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK"];

/**
 * India F&O chart desk: search any share name, see its chart + details, and
 * hit ANALYSE to mark support/resistance, stop-loss, take-profit and the
 * candlestick patterns directly on the chart.
 *
 * The chart is rendered natively because TradingView's free embed refuses NSE
 * symbols (exchange-subscription restricted) and cannot be drawn on
 * programmatically. "Open in TradingView" gives you the real TV chart in a tab.
 */
export function ChartPanel() {
  const [input, setInput] = useState("NIFTY");
  const [symbol, setSymbol] = useState("NIFTY");
  const [tv, setTv] = useState("NSE:NIFTY");
  const [a, setA] = useState<Analysis | null>(null);
  const [bars, setBars] = useState<Bar[]>([]);
  const [trend, setTrend] = useState<string[]>([]);
  const [marks, setMarks] = useState<Mark[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [side, setSide] = useState<"long" | "short">("long");
  const [useTV, setUseTV] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const holder = useRef<HTMLDivElement | null>(null);

  useEffect(() => { load("NIFTY"); /* eslint-disable-next-line */ }, []);

  useEffect(() => {
    if (!useTV) return;
    let dead = false;
    function build() {
      if (dead || !holder.current || !window.TradingView) return;
      holder.current.innerHTML = "";
      const id = "tvc_" + Math.random().toString(36).slice(2);
      const div = document.createElement("div");
      div.id = id; div.style.height = "100%";
      holder.current.appendChild(div);
      try {
        new window.TradingView.widget({
          container_id: id, symbol: tv, interval: "60", theme: "dark",
          style: "1", locale: "en", autosize: true, allow_symbol_change: true,
          backgroundColor: "rgba(10,14,21,1)", gridColor: "rgba(90,130,200,0.08)",
        });
      } catch {}
    }
    if (window.TradingView) build();
    else {
      const sc = document.createElement("script");
      sc.src = "https://s3.tradingview.com/tv.js"; sc.async = true; sc.onload = build;
      document.head.appendChild(sc);
    }
    return () => { dead = true; };
  }, [tv, useTV]);

  async function load(sym?: string) {
    const s = (sym ?? input).trim().toUpperCase();
    if (!s) return;
    setBusy(true); setErr(null); setSymbol(s); setInput(s);
    try {
      const [rr, ra, ro] = await Promise.all([
        fetch(`/api/resolve?symbol=${encodeURIComponent(s)}`),
        fetch(`/api/analyze?symbol=${encodeURIComponent(s)}&period=6mo&interval=1d`),
        fetch(`/api/ohlc?symbol=${encodeURIComponent(s)}&period=6mo&interval=1d&bars=140`),
      ]);
      const jr = await rr.json(); const ja = await ra.json(); const jo = await ro.json();
      if (jr.ok) setTv(jr.tv_symbol);
      if (ja.ok) setA(ja.result as Analysis); else { setA(null); setErr(`no data for ${s}`); }
      setBars(jo.ok ? (jo.result.bars as Bar[]) : []);
      setTrend(jo.ok ? ((jo.result.trend ?? []) as string[]) : []);
      setMarks(jo.ok ? ((jo.result.marks ?? []) as Mark[]) : []);
      setFills(jo.ok ? ((jo.result.fills ?? []) as Fill[]) : []);
    } catch {
      setErr("lookup failed");
    } finally { setBusy(false); }
  }

  const sig = a?.patterns.latest_signal ?? 0;
  const last = bars.length ? bars[bars.length - 1] : null;
  const tvUrl = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tv)}`;

  return (
    <div className="panel col chartpanel">
      {/* ---- search ---- */}
      <div className="search-bar">
        <span className="mag">⌕</span>
        <input
          className="sym-input"
          value={input}
          placeholder="Search any share — RELIANCE, TCS, NIFTY, BANKNIFTY…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") load(); }}
          autoComplete="off"
        />
        <button className="go" onClick={() => load()} disabled={busy}>
          {busy ? "…" : "Search"}
        </button>
      </div>
      <div className="quick-syms">
        {QUICK.map((q) => (
          <button key={q} className={q === symbol ? "on" : ""} onClick={() => load(q)}>{q}</button>
        ))}
      </div>

      {/* ---- stock details ---- */}
      {a && (
        <div className="stock-head">
          <div className="sh-left">
            <div className="sh-sym">{a.symbol}</div>
            <div className="sh-price num">
              {a.last_close.toLocaleString()}
              <span className={`sh-chg ${a.change_pct >= 0 ? "green" : "red"}`}>
                {a.change_pct >= 0 ? "▲" : "▼"} {Math.abs(a.change_pct)}%
              </span>
            </div>
          </div>
          <div className="sh-grid">
            {last && <><span>O</span><b className="num">{last.o}</b>
              <span>H</span><b className="num green">{last.h}</b>
              <span>L</span><b className="num red">{last.l}</b>
              <span>C</span><b className="num">{last.c}</b></>}
            <span>52W</span><b className="num">{a.low_52.toLocaleString()}–{a.high_52.toLocaleString()}</b>
            <span>ATR</span><b className="num">{a.levels.atr ?? "—"}</b>
          </div>
          <span className={`verdict-chip ${sig > 0 ? "call" : sig < 0 ? "put" : "flat"}`}>
            {sig > 0 ? "BULLISH" : sig < 0 ? "BEARISH" : "NEUTRAL"}
          </span>
        </div>
      )}
      {err && <div className="dim tiny" style={{ padding: "4px 2px" }}>{err}</div>}

      {/* ---- chart controls ---- */}
      <div className="chart-controls">
        <button className="primary big" onClick={() => load()} disabled={busy}>
          ANALYSE &amp; MARK LEVELS
        </button>
        <button className={side === "long" ? "on" : ""}
          onClick={() => setSide(side === "long" ? "short" : "long")}>
          {side === "long" ? "LONG" : "SHORT"}
        </button>
        <button className={useTV ? "on" : ""} onClick={() => setUseTV(!useTV)}>
          {useTV ? "TV" : "NATIVE"}
        </button>
        <a className="tv-link" href={tvUrl} target="_blank" rel="noreferrer">TradingView ↗</a>
      </div>

      {/* ---- chart ---- */}
      {useTV ? (
        <>
          <div className="tv-holder" ref={holder} />
          <div className="tv-warn">
            TradingView&apos;s free embed blocks NSE symbols (needs a paid exchange
            subscription) and can&apos;t be drawn on. Use NATIVE for marked levels,
            or open the real chart via the link above.
          </div>
        </>
      ) : (
        <div className="tv-holder native">
          <CandleChart bars={bars} levels={a?.levels ?? null} side={side}
            trend={trend} marks={marks} fills={fills} />
        </div>
      )}

      {/* ---- marked levels / patterns ---- */}
      {a && (
        <div className="analysis">
          <div className="an-grid">
            <div className="an-box">
              {/* Only the last bar matters for a decision. The "top hits"
                  frequency table counted patterns across 125 bars of history,
                  which is a statistic about the past, not a signal -- it filled
                  six chips with nothing anyone acts on. */}
              <div className="an-h">PATTERNS</div>
              {a.patterns.latest.length ? (
                <div className="pat-chips">
                  {a.patterns.latest.map((p) => (
                    <span key={p} className="pat-chip">{p.replace(/_/g, " ")}</span>
                  ))}
                </div>
              ) : <div className="mut tiny">none</div>}
            </div>
            <div className="an-box">
              <div className="an-h">LEVELS ({side.toUpperCase()})</div>
              <div className="lvl-row"><span className="mut">RESISTANCE</span>
                <span className="num red">{a.levels.resistance.length ? a.levels.resistance.join(" · ") : "at highs"}</span></div>
              <div className="lvl-row"><span className="mut">SUPPORT</span>
                <span className="num green">{a.levels.support.length ? a.levels.support.join(" · ") : "at lows"}</span></div>
              <div className="lvl-row"><span className="mut">STOP LOSS</span>
                <span className="num red">{a.levels[side].stop_loss}</span></div>
              <div className="lvl-row"><span className="mut">TAKE PROFIT</span>
                <span className="num green">{a.levels[side].take_profit}</span></div>
              <div className="lvl-row"><span className="mut">RISK:REWARD</span>
                <span className="num cyan">1 : {a.levels[side].rr}</span></div>
            </div>
          </div>
          <div className="an-foot">{a.levels.method} · levels are geometry, not a forecast</div>
        </div>
      )}
    </div>
  );
}
