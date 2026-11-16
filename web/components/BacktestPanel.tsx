"use client";
import { useState } from "react";

type Fold = {
  fold: number;
  is_return_pct: number;
  oos_return_pct: number;
  oos_trades: number;
  oos_win_rate: number;
  oos_profit_factor: number;
};
export type WalkForward = {
  symbol: string;
  interval: string;
  period: string;
  folds: Fold[];
  is_mean_return_pct: number;
  oos_mean_return_pct: number;
  oos_positive_folds: number;
  oos_total_folds: number;
  edge_verdict: string;
};

/**
 * Historical validation. Forward-testing a handful of live trades proves
 * nothing; this runs the SAME strategy over years of bars and — critically —
 * validates it out-of-sample. In-sample numbers are curve-fit by construction,
 * so only the OOS column is evidence.
 */
export function BacktestPanel({
  wf,
  symbols,
}: {
  wf: WalkForward | null;
  symbols: string[];
}) {
  const [sym, setSym] = useState(wf?.symbol || "NIFTY");
  const [busy, setBusy] = useState(false);
  const [local, setLocal] = useState<WalkForward | null>(wf);

  async function runWf() {
    setBusy(true);
    try {
      const r = await fetch(`/api/walkforward?symbol=${sym}&period=1y&interval=1h&splits=4`);
      const j = await r.json();
      if (j.ok) setLocal(j.result as WalkForward);
    } finally {
      setBusy(false);
    }
  }

  const w = local ?? wf;
  const hasEdge = w ? w.oos_mean_return_pct > 0 && w.oos_positive_folds > w.oos_total_folds / 2 : false;

  return (
    <div className="panel col" style={{ padding: "10px 12px" }}>
      <div className="panel-title">
        <span className="dot" />
        BACKTEST
        {w && (
          <span className={`pill ${hasEdge ? "live" : "mock"}`}>
            {hasEdge ? "EDGE" : "NO EDGE"}
          </span>
        )}
      </div>

      <div className="bt-controls">
        <select className="strat-select" value={sym} onChange={(e) => setSym(e.target.value)}>
          {(symbols?.length ? symbols : ["NIFTY"]).map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <button onClick={runWf} disabled={busy}>
          {busy ? "running…" : "Run walk-forward"}
        </button>
      </div>

      {!w ? (
        <div className="dim" style={{ padding: "6px 2px", fontSize: 11 }}>
          not validated
        </div>
      ) : (
        <>
          <div className="bt-head">
            <span>FOLD</span><span>IN-SAMPLE</span><span>OUT-OF-SAMPLE</span><span>WR</span><span>PF</span>
          </div>
          <div className="bt-rows">
            {w.folds.map((f) => (
              <div className="bt-row" key={f.fold}>
                <span className="mut">#{f.fold}</span>
                <span className={`num ${f.is_return_pct >= 0 ? "dim" : "mut"}`}>
                  {f.is_return_pct >= 0 ? "+" : ""}{f.is_return_pct.toFixed(1)}%
                </span>
                <span className={`num ${f.oos_return_pct >= 0 ? "green" : "red"}`}>
                  {f.oos_return_pct >= 0 ? "+" : ""}{f.oos_return_pct.toFixed(1)}%
                </span>
                <span className="num mut">{f.oos_win_rate}%</span>
                <span className="num mut">{f.oos_profit_factor}</span>
              </div>
            ))}
          </div>
          <div className="bt-summary">
            <div>
              <span className="l">IS mean</span>
              <span className="v num dim">{w.is_mean_return_pct >= 0 ? "+" : ""}{w.is_mean_return_pct}%</span>
            </div>
            <div>
              <span className="l">OOS mean</span>
              <span className={`v num ${w.oos_mean_return_pct >= 0 ? "green" : "red"}`}>
                {w.oos_mean_return_pct >= 0 ? "+" : ""}{w.oos_mean_return_pct}%
              </span>
            </div>
            <div>
              <span className="l">OOS folds +</span>
              <span className="v num">{w.oos_positive_folds}/{w.oos_total_folds}</span>
            </div>
          </div>
          <div className={`bt-verdict ${hasEdge ? "ok" : "no"}`}>{w.edge_verdict}</div>
        </>
      )}
    </div>
  );
}
