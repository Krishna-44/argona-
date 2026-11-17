"use client";
import { OpenTrade } from "@/lib/api";
import { inr, inrSigned, price } from "@/lib/format";

/* Live scalps only. The OPEN TRADES panel mixes every strategy together, so a
   scalp that is about to hit its stop sits next to an index hold that will not
   move for days. This column answers one question — what are the scalps doing
   right now — and shows how far each is from its own bands. */
export function LiveScalps({ trades }: { trades: OpenTrade[] }) {
  const scalps = trades.filter((t) =>
    (t.strategy || "").includes("scalp") || (t.strategy || "").includes("rapid"));

  const unreal = scalps.reduce((s, t) => {
    const mark = t.mark_premium ?? t.entry_premium;
    return s + (mark - t.entry_premium) * t.qty;
  }, 0);
  const up = unreal >= 0;

  return (
    <div className="panel col livescalps">
      <div className="panel-title">
        <span className="dot" />
        LIVE SCALPS
        <span className="badge">{scalps.length}</span>
        <span className={`ls-total num ${up ? "green" : "red"}`}>
          {inrSigned(unreal)}
        </span>
      </div>

      {scalps.length === 0 ? (
        <div className="dim tiny" style={{ padding: "8px 2px" }}>no scalp open</div>
      ) : (
        <div className="ls-rows">
          {scalps.map((t) => {
            const mark = t.mark_premium ?? t.entry_premium;
            const pnl = (mark - t.entry_premium) * t.qty;
            const win = pnl >= 0;
            // Progress of the mark between SL (0) and TP (1). A short
            // underlying is marked mirrored, so this works for both sides
            // without a special case.
            const span = t.tp_premium - t.sl_premium || 1;
            const prog = Math.max(0, Math.min(1, (mark - t.sl_premium) / span));
            // Distance left to each band, as a move in the mark.
            const toTp = ((t.tp_premium - mark) / mark) * 100;
            const toSl = ((mark - t.sl_premium) / mark) * 100;
            return (
              <div className={`ls-row ${win ? "win" : "loss"}`} key={t.id}>
                <div className="ls-head">
                  <span className={`k ${t.kind}`}>{t.kind === "call" ? "L" : "S"}</span>
                  <b>{t.underlying}</b>
                  <span className="mut tiny">{t.strategy.replace(/_/g, " ")}</span>
                  <span className={`num ls-pnl ${win ? "green" : "red"}`}>
                    {inrSigned(pnl)}
                  </span>
                </div>
                <div className="ls-bar">
                  <span className="track">
                    <span className="fill" style={{ width: `${prog * 100}%` }} />
                  </span>
                </div>
                <div className="ls-meta num">
                  <span className="red">SL {price(t.sl_premium)}</span>
                  <span className="mut">{price(mark)}</span>
                  <span className="green">TP {price(t.tp_premium)}</span>
                </div>
                <div className="ls-dist mut">
                  <span>{toSl.toFixed(2)}% to stop</span>
                  <span>{toTp.toFixed(2)}% to target</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <div className="ls-foot mut">
        unrealised · settles only on exit, charges not yet taken
      </div>
    </div>
  );
}
