"use client";
import { Kpis, StrategyInfo, control, setStrategy } from "@/lib/api";
import { inrSigned } from "@/lib/format";
import { useState } from "react";

export function Footer({
  kpis, live, strategies,
}: {
  kpis: Kpis;
  live: boolean;
  strategies: { active: string; available: StrategyInfo[] };
}) {
  const [busy, setBusy] = useState<string | null>(null);
  async function run(a: "pause" | "resume" | "reset") {
    setBusy(a);
    try { await control(a); } finally { setBusy(null); }
  }
  return (
    <footer className="panel footer">
      <div>
        BEST <span className="green num">{inrSigned(kpis.best)}</span>
        <span style={{ margin: "0 12px" }}>·</span>
        WORST <span className="red num">{inrSigned(kpis.worst)}</span>
        <span style={{ margin: "0 12px" }}>·</span>
        AVG <span className="cyan num">{inrSigned(kpis.avg_net)}</span>
      </div>
      <div className="brand-wm">ARGONA F&O</div>
      <div className="controls">
        <select
          className="strat-select"
          value={strategies.active}
          onChange={(e) => setStrategy(e.target.value)}
          title="active strategy"
        >
          {strategies.available.map((s) => (
            <option key={s.name} value={s.name}>{s.name}</option>
          ))}
        </select>
        <button onClick={() => run("pause")} disabled={busy != null}>Pause</button>
        <button onClick={() => run("resume")} disabled={busy != null}>Resume</button>
        <button onClick={() => run("reset")} disabled={busy != null}>Reset</button>
      </div>
    </footer>
  );
}
