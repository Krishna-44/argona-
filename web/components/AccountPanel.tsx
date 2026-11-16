"use client";
import { Account, ChargesConfig } from "@/lib/api";
import { inr, inrSigned } from "@/lib/format";

/** The paper trading account: identity, balance, net-of-tax P&L, and the
 *  India charge/tax rate card in effect. Paper only — no broker, no real money. */
export function AccountPanel({ account, charges }: { account: Account; charges: ChargesConfig }) {
  const short = `${account.account_id.slice(0, 6)}…${account.account_id.slice(-4)}`;
  const pos = account.net_pnl >= 0;
  return (
    <div className="panel account">
      <div className="panel-title">
        <span className="dot" />
        PAPER ACCOUNT
        <span className="badge">USD</span>
      </div>
      <div className="acc-addr mono">{short} · paper · no broker</div>
      <div className="acc-balance">
        <span className="num">{inr(account.balance)}</span>
        <span className={`chg num ${pos ? "green" : "red"}`}>{inrSigned(account.net_pnl)}</span>
      </div>
      <div className="acc-grid">
        <div className="ag"><span className="l">Charges paid</span><span className="v num red">−{inr(account.charges_paid)}</span></div>
        <div className="ag"><span className="l">Tax paid</span><span className="v num red">−{inr(account.tax_paid)}</span></div>
        <div className="ag"><span className="l">Gross P&L</span><span className="v num">{inrSigned(account.gross_total)}</span></div>
        <div className="ag"><span className="l">Tax rate</span><span className="v num">{charges.tax_rate_pct.toFixed(0)}%</span></div>
      </div>
      <div className="acc-foot">{charges.regime}</div>
    </div>
  );
}
