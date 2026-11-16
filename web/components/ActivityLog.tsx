"use client";
import { ActivityEvent } from "@/lib/api";
import { timeHMS } from "@/lib/format";

export function ActivityLog({ events }: { events: ActivityEvent[] }) {
  return (
    <div className="panel col" style={{ minHeight: 0, flex: 1 }}>
      <div className="panel-title">
        <span className="dot" />
        ACTIVITY
        <span className="badge">{events.length}</span>
      </div>
      <div className="tape">
        {events.map((e) => (
          <div className="line" key={e.id}>
            <span className="t num">{timeHMS(e.ts)}</span>
            <span className={`k ${e.kind}`}>{e.kind}</span>
            <span className="body">{renderBody(e)}</span>
          </div>
        ))}
        {events.length === 0 && <div className="dim" style={{ padding: "16px 4px" }}>waiting…</div>}
      </div>
    </div>
  );
}

function renderBody(e: ActivityEvent): React.ReactNode {
  const b = e.body as any;
  switch (e.kind) {
    case "ENTRY":
      return (
        <>
          <b className="cyan">OPEN {b.kind?.toUpperCase()}</b> {b.underlying}{" "}
          <em>{b.instrument} · {b.qty}× @ {b.entry} · TP {b.tp}/SL {b.sl}
            {b.score != null ? ` · p${(b.score * 100).toFixed(0)}%` : ""}</em>
        </>
      );
    case "EXIT":
      return (
        <>
          <b className={b.won ? "green" : "red"}>{b.reason}</b> {b.underlying}{" "}
          <em>{b.instrument} · net {(b.net >= 0 ? "+" : "") + b.net?.toFixed?.(2)} (chg -{b.charges} tax -{b.tax})</em>
        </>
      );
    case "INFO":
    default:
      return <span className="dim">{b.msg || JSON.stringify(b)}</span>;
  }
}
