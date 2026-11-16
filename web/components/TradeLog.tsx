"use client";
import { ActivityEvent, ClosedTrade } from "@/lib/api";
import { inr, inrSigned, timeHMS, inrCompact } from "@/lib/format";

/**
 * Closed-trade ledger. Shows the full economics of each scalp: gross move,
 * India charges, tax, and the NET that actually hit the account — so the
 * "small profit" is always shown after costs, never before.
 */
export function TradeLog({ trades, activity, initial = 0 }: {
  trades: ClosedTrade[]; activity: ActivityEvent[]; initial?: number;
}) {
  // Running balance after each trade, so the account can be watched walking
  // up and down rather than only as a final number. `trades` arrives newest
  // first, so accumulate from the oldest end and map back.
  const oldestFirst = [...trades].reverse();
  const balAfter = new Map<number, number>();
  let run = initial;
  for (const t of oldestFirst) { run += t.net; balAfter.set(t.id, run); }
  return (
    <div className="panel col" style={{ minHeight: 0, flex: 1 }}>
      <div className="panel-title">
        <span className="dot" />
        TRADES
        <span className="badge">{trades.length}</span>
      </div>
      <div className="tlog">
        <div className="tl-head">
          <span>CONTRACT</span><span>EXIT</span><span>NET</span><span>BALANCE</span>
        </div>
        <div className="tl-rows">
          {trades.map((t) => {
            const win = t.net > 0;
            // A short underlying's "exit_premium" is a mirrored mark, not a
            // price — showing it makes a winning short look like the price
            // moved against it. Use the real spot where we have it.
            const isUnd = (t.instrument ?? "").includes("UNDERLYING");
            const pxIn = isUnd && t.entry_spot ? t.entry_spot : t.entry_premium;
            const pxOut = isUnd && t.exit_spot ? t.exit_spot : t.exit_premium;
            return (
              <div className={`tl-row ${win ? "win" : "loss"}`} key={t.id}>
                <div className="c">
                  <span className={`k ${t.kind}`}>{t.kind === "call" ? "C" : "P"}</span>
                  <b>{t.underlying}</b>
                  <span className="inst">{t.instrument}</span>
                </div>
                <div className={`reason ${t.exit_reason?.toLowerCase()}`}>{t.exit_reason}</div>
                <div className={`num net ${win ? "green" : "red"}`}>{inrSigned(t.net)}</div>
                <div className="num bal">{inrCompact(balAfter.get(t.id) ?? 0)}</div>
              </div>
            );
          })}
          {trades.length === 0 && (
            <div className="dim" style={{ padding: "14px 4px" }}>
              no closed trades yet — it opens on a TAKE, exits at TP/SL, repeats.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
