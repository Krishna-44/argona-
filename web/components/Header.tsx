"use client";
import { Kpis, setStrategy } from "@/lib/api";
import { useEffect, useRef, useState } from "react";
import { BotsMenu } from "./BotsMenu";

type Session = { source: string; label: string; open: boolean; hours: string };

export function Header({ kpis, live, strategy, sessions, fx }: {
  kpis: Kpis; live: boolean; strategy: string;
  sessions?: Session[]; fx?: number;
}) {
  // Optimistic: the server's strategy only reaches us on the next engine
  // broadcast (~12s), so reflect the click immediately and let the live
  // value take over once it arrives.
  const [pending, setPending] = useState<string | null>(null);
  const effective = pending ?? strategy;
  const rapid = effective === "rapid_underlying" || effective === "rapid_candle";
  useEffect(() => {
    if (pending && strategy === pending) setPending(null);
  }, [strategy, pending]);
  // Turning RAPID off used to hard-code "underlying_momo", which silently
  // clobbered whatever strategy was actually selected -- scalp_momentum was
  // armed at 11:32 and wiped by a rapid toggle at 11:56 without a trace in
  // the UI. Remember what was running before RAPID and restore exactly that.
  const preRapid = useRef<string>("underlying_momo");
  async function toggleRapid() {
    let next: string;
    if (rapid) {
      next = preRapid.current;
    } else {
      if (effective !== "rapid_underlying" && effective !== "rapid_candle") {
        preRapid.current = effective;
      }
      next = "rapid_underlying";
    }
    setPending(next);
    try { await setStrategy(next); } catch { setPending(null); }
  }
  const [uptime, setUptime] = useState("00:00:00");
  useEffect(() => {
    const t0 = Date.now();
    const id = setInterval(() => {
      const s = Math.floor((Date.now() - t0) / 1000);
      const h = Math.floor(s / 3600).toString().padStart(2, "0");
      const m = Math.floor((s % 3600) / 60).toString().padStart(2, "0");
      const ss = (s % 60).toString().padStart(2, "0");
      setUptime(`${h}:${m}:${ss}`);
    }, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <header className="panel header">
      <div className="brand">
        <span className="glyph">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.6">
            <path d="M3 17l5-6 4 3 6-8" />
            <path d="M14 6h5v5" />
          </svg>
        </span>
        {/* The venue list wrapped onto four lines at this column width and
            collided with the session chips. The chips already state which
            venues are open, so the strapline was duplicating them badly. */}
        <div>
          <div className="name">ARGONA</div>
          <div className="sub">paper ₹</div>
        </div>
      </div>
      <div className="status">
        <button
          className={`rapid-switch ${rapid ? "on" : ""}`}
          onClick={toggleRapid}
          role="switch"
          aria-checked={rapid}
          aria-label="Rapid scalping mode"
          title={
            rapid
              ? "RAPID SCALPING ON — 0.15% target / 0.10% stop on the underlying, 20s cooldown. Trades far more often. The round trip eats ~46% of each target (89% on SPY), so it needs a 68-93% win rate to break even; measured win rates here are 44-55%."
              : "Normal mode — 0.60% target / 0.40% stop, 180s cooldown. Needs a 47-53% win rate to break even. Click to switch to rapid scalping."
          }
        >
          <span className="track"><span className="knob" /></span>
          <span className="labels">
            <span className="lab">RAPID SCALP {rapid ? "ON" : "OFF"}</span>
            <span className="sub2">
              {rapid ? "0.15% / 0.10% · needs 68% wins" : "0.60% / 0.40% · needs 47% wins"}
            </span>
          </span>
        </button>
        <BotsMenu />
        <span className="mode">{effective.toUpperCase().replace(/_/g, " ")}</span>
        <span className="alive dim uppercase" style={{ fontSize: 10.5 }}>Live</span>
      </div>
      <div className="meta">
        {sessions?.map((s) => (
          <span key={s.source} title={s.hours}>
            {s.label} <b className={s.open ? "green" : "mut"}>{s.open ? "OPEN" : "SHUT"}</b>
          </span>
        ))}
        {fx ? <span>USDINR <b>{fx.toFixed(2)}</b></span> : null}
        <span>Uptime <b>{uptime}</b></span>
        <span>Open <b>{kpis.open_trades}</b></span>
        <span>Feed <b className={live ? "green" : "amber"}>{live ? "LIVE" : "RECONNECT"}</b></span>
      </div>
    </header>
  );
}
