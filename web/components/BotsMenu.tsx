"use client";
import { useCallback, useEffect, useRef, useState } from "react";

type Obs = { symbol: string; vote: number | null; reason: string; veto: boolean };
type Bot = {
  agent: string; role: string; kind: string; power: string;
  votes_scored: number; hit_rate: number | null; weight: number;
  calibrated: boolean; needs: number; watching: number;
  status: string; now: Obs[];
};

/**
 * Bots menu — who is on the desk and what each one is doing right now.
 *
 * Shows the split that matters: gatekeepers answer with arithmetic and can
 * veto, predictors forecast and only get a weighted vote. A predictor's
 * weight stays at zero until it has enough scored votes, and the menu says
 * how many more it needs rather than implying it is already contributing.
 */
export function BotsMenu() {
  const [open, setOpen] = useState(false);
  const [bots, setBots] = useState<Bot[] | null>(null);
  const [minSamples, setMin] = useState(20);
  const [sel, setSel] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await fetch("/api/agents").then((r) => r.json());
      if (d?.agents) { setBots(d.agents); setMin(d.min_samples ?? 20); }
    } catch { /* leave the last good view up */ }
  }, []);

  useEffect(() => {
    if (!open) {
      if (timer.current) clearInterval(timer.current);
      return;
    }
    load();
    timer.current = setInterval(load, 5000);
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [open, load]);

  const active = bots?.filter((b) => b.status !== "idle").length ?? 0;

  return (
    <div className="bots-wrap">
      <button className={`bots-btn ${open ? "on" : ""}`} onClick={() => setOpen(!open)}>
        BOTS {bots ? `${active}/${bots.length}` : ""}
      </button>

      {open && (
        <div className="bots-menu">
          <div className="bots-head">
            <span>DESK BOTS</span>
            <button className="x" onClick={() => setOpen(false)}>close</button>
          </div>

          {!bots ? (
            <div className="dim tiny" style={{ padding: 10 }}>loading…</div>
          ) : (
            <>
              <div className="bots-note">
                Gatekeepers answer with arithmetic and can veto a trade.
                Predictors forecast and only get a weighted vote — weight stays
                at zero until {minSamples} of their votes have been scored, so
                an uncalibrated bot cannot move the desk.
              </div>

              {["gatekeeper", "predictor"].map((kind) => (
                <div key={kind}>
                  <div className="bots-sec">
                    {kind === "gatekeeper" ? "GATEKEEPERS · can veto" : "PREDICTORS · vote only"}
                  </div>
                  {bots.filter((b) => b.kind === kind).map((b) => (
                    <div key={b.agent}
                      className={`bot ${b.status} ${sel === b.agent ? "open" : ""}`}
                      onClick={() => setSel(sel === b.agent ? null : b.agent)}>
                      <div className="bot-top">
                        <b>{b.agent}</b>
                        <span className={`st ${b.status}`}>{b.status}</span>
                        {b.kind === "predictor" && (
                          <span className="w">
                            {b.calibrated
                              ? `weight ${b.weight.toFixed(2)}`
                              : `needs ${b.needs} more`}
                          </span>
                        )}
                      </div>
                      <div className="bot-role">{b.role}</div>
                      <div className="bot-stats">
                        <span>watching <b>{b.watching}</b></span>
                        {b.kind === "predictor" && (
                          <>
                            <span>scored <b>{b.votes_scored}</b></span>
                            <span>hit <b>{b.hit_rate != null ? `${b.hit_rate}%` : "—"}</b></span>
                          </>
                        )}
                      </div>

                      {sel === b.agent && b.now.length > 0 && (
                        <div className="bot-now">
                          {b.now.map((o) => (
                            <div key={o.symbol} className="bot-obs">
                              <span className="s">{o.symbol}</span>
                              {b.kind === "predictor" && (
                                <span className={`v ${(o.vote ?? 0) > 0 ? "up" : (o.vote ?? 0) < 0 ? "dn" : ""}`}>
                                  {(o.vote ?? 0) > 0 ? "+" : ""}{(o.vote ?? 0).toFixed(2)}
                                </span>
                              )}
                              <span className="r">{o.reason}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
