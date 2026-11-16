"use client";
import { useEffect, useRef, useState } from "react";
import { inr } from "@/lib/format";

type Idea = {
  symbol: string; spot: number; direction: string; venue?: string;
  option_type: string | null; expiry: string | null; dte: number | null;
  strike: number | null; moneyness: string | null;
  premium_est: number | null; lot_size: number | null; cost_per_lot: number | null;
  delta: number | null; theta_per_day: number | null;
  target: number | null; stop_loss: number | null;
  expected_move: number | null; reachable: boolean | null;
  conviction: number; trend: string; adx: number; regime: string; mtf: string;
  patterns: string[]; reasons: string[]; warnings: string[];
  round_trip_pct?: number | null; required_win_rate?: number | null;
  tradeable?: boolean; verdict?: string;
};
type Job = {
  ok: boolean; id: string; state: string; total: number; done: number;
  current: string | null; elapsed_sec: number; tradeable_count: number;
  ideas: Idea[]; no_trade: Idea[]; errors: string[]; note: string;
};

const VENUES = [
  { id: "all", label: "ALL MARKETS" },
  { id: "india", label: "INDIA" },
  { id: "us", label: "US" },
  { id: "crypto", label: "CRYPTO" },
];

/**
 * Full-market sweep. Runs as a background job so it can take as long as it
 * needs — the panel streams partial results and a progress count rather than
 * blocking on one long request and showing nothing until the end.
 *
 * The headline answer is not "which way" but "where is a trade POSSIBLE":
 * every idea carries its round-trip cost and the win rate that cost demands,
 * and anything whose cost exceeds its target is called out as untradeable.
 */
export function ScannerPanel() {
  const [venue, setVenue] = useState("all");
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);

  async function run() {
    if (poll.current) clearInterval(poll.current);
    setBusy(true);
    setJob(null);
    try {
      const r = await fetch(`/api/scan/start?venue=${venue}`, { method: "POST" });
      const { job_id } = await r.json();
      poll.current = setInterval(async () => {
        try {
          const s: Job = await fetch(`/api/scan/status?job_id=${job_id}`).then((x) => x.json());
          setJob(s);
          if (s.state === "done") {
            if (poll.current) clearInterval(poll.current);
            setBusy(false);
          }
        } catch { /* keep polling through a blip */ }
      }, 1200);
    } catch {
      setBusy(false);
    }
  }

  const pct = job && job.total ? Math.round((job.done / job.total) * 100) : 0;
  const tradeable = (job?.ideas ?? []).filter((i) => i.tradeable);
  const rejected = (job?.ideas ?? []).filter((i) => !i.tradeable);

  return (
    <div className="panel col scanner">
      <div className="panel-title">
        <span className="dot" />
        SCAN
        {job && <span className="badge">{job.tradeable_count} tradeable</span>}
      </div>

      <div className="venue-row">
        {VENUES.map((v) => (
          <button key={v.id}
            className={venue === v.id ? "on" : ""}
            onClick={() => setVenue(v.id)}
            disabled={busy}>{v.label}</button>
        ))}
      </div>

      <button className="scan-btn" onClick={run} disabled={busy}>
        {busy ? `SCANNING ${job?.done ?? 0}/${job?.total ?? "…"}…` : "SCAN MARKET"}
      </button>

      {busy && job && (
        <div className="scan-prog">
          <span className="bar"><span className="fill" style={{ width: `${pct}%` }} /></span>
          <span className="tiny mut">
            {job.current ? `reading ${job.current}` : "starting"} · {job.elapsed_sec}s
          </span>
        </div>
      )}

      {!job ? (
        <div className="dim tiny" style={{ padding: "6px 2px" }}>
          idle
        </div>
      ) : (
        <>
          {tradeable.length > 0 && (
            <div className="scan-sec">TRADEABLE ({tradeable.length})</div>
          )}
          <div className="scan-list">
            {tradeable.map((i) => (
              <IdeaCard key={i.symbol} i={i} open={open} setOpen={setOpen} />
            ))}
          </div>

          {rejected.length > 0 && (
            <>
              <div className="scan-sec warn">COST RULES THESE OUT ({rejected.length})</div>
              <div className="scan-list">
                {rejected.map((i) => (
                  <IdeaCard key={i.symbol} i={i} open={open} setOpen={setOpen} />
                ))}
              </div>
            </>
          )}

          {(job.no_trade?.length ?? 0) > 0 && (
            <div className="no-trade">
              <div className="an-h">NO DIRECTIONAL SETUP ({job.no_trade.length})</div>
              <div className="tiny mut">
                {job.no_trade.map((i) => i.symbol).join(" · ")}
              </div>
            </div>
          )}
          <div className="scan-note">{job.note}</div>
        </>
      )}
    </div>
  );
}

function IdeaCard({ i, open, setOpen }: {
  i: Idea; open: string | null; setOpen: (s: string | null) => void;
}) {
  const bull = i.direction === "bullish";
  const id = i.symbol;
  const need = i.required_win_rate ?? null;
  return (
    <div className={`idea ${bull ? "bull" : "bear"} ${i.tradeable ? "" : "blocked"}`}
      onClick={() => setOpen(open === id ? null : id)}>
      <div className="idea-top">
        <span className="conv" title="conviction">{i.conviction}</span>
        <b className="sym">{i.symbol}</b>
        <span className={`vn ${i.venue}`}>{i.venue}</span>
        <span className={`ot ${i.option_type === "CE" ? "ce" : "pe"}`}>
          {i.moneyness} {i.option_type}
        </span>
        <span className="strike num">{i.strike?.toLocaleString("en-IN")}</span>
        <span className="exp mut">{i.expiry} · {i.dte}d</span>
      </div>

      <div className={`verdict-line ${i.tradeable ? "ok" : "no"}`}>
        {i.verdict}
      </div>

      <div className="idea-row">
        <span>cost <b className="num amber">{i.round_trip_pct?.toFixed(1)}%</b></span>
        <span>needs <b className={`num ${need && need < 55 ? "green" : "red"}`}>
          {need?.toFixed(0)}%</b> wins</span>
        <span>lot <b className="num">{i.lot_size}</b></span>
        <span>per lot <b className="num cyan">{inr(i.cost_per_lot ?? 0, 0)}</b></span>
      </div>
      <div className="idea-row">
        <span>spot <b className="num">{i.spot?.toLocaleString("en-IN")}</b></span>
        <span>tgt <b className="num green">{i.target?.toLocaleString("en-IN")}</b></span>
        <span>SL <b className="num red">{i.stop_loss?.toLocaleString("en-IN")}</b></span>
        <span className={i.reachable ? "green" : "red"}>
          {i.reachable ? "reachable" : "far"}
        </span>
      </div>

      {open === id && (
        <div className="idea-why">
          <div className="an-h">WHY</div>
          {i.reasons.map((r, k) => <div key={k} className="tiny">· {r}</div>)}
          {i.patterns.length > 0 && (
            <div className="pat-chips" style={{ marginTop: 4 }}>
              {i.patterns.map((p) => (
                <span key={p} className="pat-chip">{p.replace(/_/g, " ")}</span>
              ))}
            </div>
          )}
          {i.warnings.length > 0 && (
            <>
              <div className="an-h" style={{ marginTop: 5 }}>RISKS</div>
              {i.warnings.map((w, k) => (
                <div key={k} className="tiny amber">! {w}</div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
