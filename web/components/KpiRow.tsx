"use client";
import { Kpis } from "@/lib/api";
import { inr, inrSigned, inrCompact, pct } from "@/lib/format";

/* Terminal-style tiles: a label, a number, and at most one line of supporting
   numbers. No sentences. The previous version explained itself in prose
   ("costs deepened the loss by X", "before charges and tax") which reads fine
   once and then wastes the row forever -- a desk is read at a glance, and the
   figures below already say it. */
export function KpiRow({ k }: { k: Kpis }) {
  const pos = k.total_net >= 0;
  // `balance` only moves when a trade closes. `equity` adds the open book
  // marked to market, so it is the number that ticks while a position runs.
  const unreal = k.unrealized ?? 0;
  const equity = k.equity ?? k.balance;
  const live = k.open_trades > 0;
  const upU = unreal >= 0;
  const costs = k.charges_paid + k.tax_paid;
  return (
    <div className="kpi-row">
      <div className={`panel kpi balance ${live ? "mtm" : ""}`}>
        <div className="label">Equity</div>
        <div className="value num">{inr(equity)}</div>
        <div className="sub num">
          {live ? (
            <>
              {inr(k.balance)} <span className="dim">settled</span>{" "}
              <b className={upU ? "green" : "red"}>{inrSigned(unreal)}</b>
            </>
          ) : (
            <>{inrCompact(k.initial)} <span className="dim">start</span></>
          )}
        </div>
      </div>
      <div className={`panel kpi ${pos ? "pnl-pos" : "pnl-neg"}`}>
        <div className="label">Net P&L</div>
        <div className="value num">{inrSigned(k.total_net)}</div>
        <div className="sub num">{pct(k.total_pnl_pct)}</div>
      </div>
      <div className="panel kpi win">
        <div className="label">Win Rate</div>
        <div className="value num">{k.win_rate.toFixed(1)}%</div>
        <div className="sub num">{k.wins}W / {k.losses}L</div>
      </div>
      <div className="panel kpi">
        <div className="label">Costs</div>
        <div className="value num red">−{inr(costs)}</div>
        <div className="sub num">
          {inr(k.charges_paid)} <span className="dim">chg</span>{" "}
          {inr(k.tax_paid)} <span className="dim">tax</span>
        </div>
      </div>
      <div className="panel kpi risk">
        <div className="label">Gross</div>
        <div className="value num">{inrSigned(k.gross_total)}</div>
        <div className="sub num">
          {inrSigned(k.total_net)} <span className="dim">net</span>
        </div>
      </div>
      <div className="panel kpi">
        <div className="label">Open</div>
        <div className="value num">{k.open_trades}</div>
        <div className="sub num">
          {k.total_trades} <span className="dim">total</span>{" "}
          {inrSigned(k.best)} <span className="dim">best</span>
        </div>
      </div>
    </div>
  );
}
