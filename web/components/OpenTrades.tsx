"use client";
import { OpenTrade } from "@/lib/api";
import { inr, inrCompact, price } from "@/lib/format";

/** Live open option positions with TP/SL progress and unrealized P&L. */
export function OpenTrades({ trades }: { trades: OpenTrade[] }) {
  return (
    <div className="panel" style={{ padding: "10px 12px" }}>
      <div className="panel-title">
        <span className="dot" />
        OPEN TRADES
        <span className="badge">{trades.length} LIVE</span>
      </div>
      {trades.length === 0 ? (
        <div className="dim" style={{ padding: "10px 4px" }}>flat</div>
      ) : (
        <div className="otrades">
          <div className="ot-head">
            <span>CONTRACT</span><span>ENTRY</span><span>MARK</span><span>RISK</span><span>TP / SL</span><span>UNREAL</span>
          </div>
          {trades.map((t) => {
            const mark = t.mark_premium ?? t.entry_premium;
            const unreal = (mark - t.entry_premium) * t.qty;
            const up = unreal >= 0;
            // progress of mark between SL (0) and TP (1)
            const span = t.tp_premium - t.sl_premium || 1;
            const prog = Math.max(0, Math.min(1, (mark - t.sl_premium) / span));
            return (
              <div className="ot-row" key={t.id}>
                <div className="c">
                  <span className={`k ${t.kind}`}>{t.kind === "call" ? "C" : "P"}</span>
                  <b>{t.underlying}</b>
                  <span className="inst">{t.instrument}</span>
                </div>
                <div className="num">{price(t.entry_premium)}</div>
                <div className="num">{price(mark)}</div>
                <div className={`num ${(t.risk_pct ?? 0) > 1.25 ? "red" : (t.risk_pct ?? 0) < 0.5 ? "mut" : ""}`}
                  title="what this position actually risks if the stop fills — target is 1.00%">
                  {t.risk_pct != null ? `${t.risk_pct.toFixed(2)}%` : "—"}
                </div>
                <div className="tpsl">
                  <span className="bar"><span className="fill" style={{ width: `${prog * 100}%` }} /></span>
                  <span className="mut num">{price(t.sl_premium)}–{price(t.tp_premium)}</span>
                </div>
                <div className={`num ${up ? "green" : "red"}`}>{inrCompact(unreal)}</div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
