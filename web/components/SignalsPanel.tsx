"use client";
import { Signal } from "@/lib/api";

/**
 * The "where to trade / where not to trade" panel. One row per underlying with
 * a TAKE (call/put) or AVOID verdict, the reason, and — once the model is
 * trained — its win-probability score. This is the advisory half you act on in
 * your own account.
 */
export function SignalsPanel({ signals }: { signals: Signal[] }) {
  const takes = signals.filter((s) => s.decision === "TAKE").length;
  return (
    <div className="panel col" style={{ minHeight: 0, flex: 1 }}>
      <div className="panel-title">
        <span className="dot" />
        SIGNALS
        <span className="badge">{takes} TAKE / {signals.length}</span>
      </div>
      <div className="signals">
        <div className="sig-head">
          <span>UNDERLYING</span>
          <span>SPOT</span>
          <span>VERDICT</span>
        </div>
        <div className="sig-rows">
          {signals.map((s) => {
            const fail = s.checks.find((c) => !c.ok);
            const take = s.decision === "TAKE";
            const reason = take
              ? `${s.side?.toUpperCase()} ${s.option?.instrument ?? ""}`
              : fail
              ? `${fail.name}: ${fail.reason}`
              : "—";
            return (
              // Five columns could not fit a ~290px panel, and `why` -- the
              // one cell that says why the desk is standing aside -- was the
              // worst squeezed. Symbol/spot/verdict on the grid line, the
              // reason on its own full-width line below it.
              <div className={`sig-item ${take ? "take" : "avoid"}`} key={s.underlying}>
                <div className="sig-row">
                  <div className="u">
                    <span className={`src ${s.source}`}>
                      {s.source === "us" ? "US" : s.source === "india" ? "IN" : "CR"}
                    </span>
                    <b>{s.underlying}</b>
                  </div>
                  <div className="num">{s.spot == null ? "—" : s.spot.toLocaleString()}</div>
                  <div className={`verdict ${take ? (s.side === "put" ? "put" : "call") : "avoid"}`}>
                    {take ? `TAKE ${s.side?.toUpperCase()}` : "AVOID"}
                    {s.score != null && ` ${(s.score * 100).toFixed(0)}%`}
                  </div>
                </div>
                <div className="sig-why" title={reason}>{reason}</div>
              </div>
            );
          })}
          {signals.length === 0 && (
            <div className="dim" style={{ padding: "16px 4px" }}>scanning the universe…</div>
          )}
        </div>
      </div>
    </div>
  );
}
