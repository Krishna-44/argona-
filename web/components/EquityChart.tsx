"use client";
import { EquityPoint, Kpis } from "@/lib/api";
import { useMemo, useState } from "react";
import { inr, inrSigned, inrCompact } from "@/lib/format";

/** Account equity curve (linear). Raw SVG, no chart lib. */
export function EquityChart({ data, kpis }: { data: EquityPoint[]; kpis: Kpis }) {
  const [hover, setHover] = useState<EquityPoint | null>(null);
  const W = 900, H = 240, PAD_L = 52, PAD_R = 12, PAD_T = 14, PAD_B = 22;

  const pts = data.length ? data : [{ i: 0, ts: 0, balance: kpis.initial, net: 0 }];
  const ys = pts.map((p) => p.balance);
  const minY = Math.min(kpis.initial, ...ys);
  const maxY = Math.max(kpis.initial, ...ys);
  const padY = (maxY - minY) * 0.12 || Math.max(1, kpis.initial * 0.02);
  const lo = minY - padY, hi = maxY + padY;
  const spanX = Math.max(pts.length - 1, 1);

  const xAt = (i: number) => PAD_L + (i / spanX) * (W - PAD_L - PAD_R);
  const yAt = (v: number) => PAD_T + (1 - (v - lo) / (hi - lo || 1)) * (H - PAD_T - PAD_B);

  const path = useMemo(
    () => pts.map((p, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)},${yAt(p.balance).toFixed(1)}`).join(" "),
    [pts, lo, hi],
  );
  const area = `${path} L${xAt(pts.length - 1).toFixed(1)},${H - PAD_B} L${xAt(0).toFixed(1)},${H - PAD_B} Z`;

  const ticks = [lo, (lo + hi) / 2, hi];
  const yInit = yAt(kpis.initial);
  const last = pts[pts.length - 1];
  const hi2 = hover ?? last;
  const hx = xAt(pts.findIndex((p) => p.i === hi2.i) < 0 ? pts.length - 1 : pts.findIndex((p) => p.i === hi2.i));

  return (
    <div className="panel col center" style={{ padding: "10px 12px 6px" }}>
      <div className="panel-title">
        <span className="dot" />
        {/* Title + badge + full-precision balance + net overflowed a ~290px
            panel and collided. Compact rupees here; the KPI row above carries
            the exact figures. */}
        EQUITY
        <span className="chart-hover mono">
          {hover ? <>#{hover.i}<b>{inrCompact(hover.balance)}</b></>
                 : <><b>{inrCompact(last.balance)}</b><span className="dim" style={{ marginLeft: 8 }}>{inrCompact(kpis.total_net)}</span></>}
        </span>
      </div>
      <div className="chart-wrap">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
          onMouseLeave={() => setHover(null)}
          onMouseMove={(e) => {
            const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
            const rel = ((e.clientX - r.left) / r.width) * W;
            const idx = Math.max(0, Math.min(pts.length - 1, Math.round(((rel - PAD_L) / (W - PAD_L - PAD_R)) * spanX)));
            setHover(pts[idx]);
          }}>
          <defs>
            <linearGradient id="eq" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0" stopColor="rgba(74,222,128,0.26)" />
              <stop offset="1" stopColor="rgba(74,222,128,0)" />
            </linearGradient>
          </defs>
          {ticks.map((t, i) => (
            <g key={i}>
              <line x1={PAD_L} x2={W - PAD_R} y1={yAt(t)} y2={yAt(t)} stroke="var(--grid)" strokeDasharray="2 4" />
              <text x={PAD_L - 6} y={yAt(t) + 3} textAnchor="end" fontSize="9.5" fill="var(--ink-mute)">{inrCompact(t)}</text>
            </g>
          ))}
          <line x1={PAD_L} x2={W - PAD_R} y1={yInit} y2={yInit} stroke="rgba(210,222,240,0.22)" strokeDasharray="4 4" />
          <text x={W - PAD_R - 4} y={yInit - 4} textAnchor="end" fontSize="9.5" fill="var(--ink-mute)">START ${kpis.initial.toFixed(0)}</text>
          <path d={area} fill="url(#eq)" />
          <path d={path} fill="none" stroke={kpis.total_net >= 0 ? "var(--green)" : "var(--red)"} strokeWidth={1.4} />
          <line x1={hx} x2={hx} y1={PAD_T} y2={H - PAD_B} stroke="var(--grid-b)" strokeDasharray="2 3" />
          <circle cx={hx} cy={yAt(hi2.balance)} r={3} fill={kpis.total_net >= 0 ? "var(--green)" : "var(--red)"} stroke="var(--bg)" strokeWidth={1.5} />
        </svg>
      </div>
    </div>
  );
}
