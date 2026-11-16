"use client";
import { useMemo, useState } from "react";

export type Bar = { t: string; o: number; h: number; l: number; c: number; p: string[] };
export type Levels = {
  price: number; atr: number | null; support: number[]; resistance: number[];
  long: { entry: number; stop_loss: number | null; take_profit: number | null; rr: number };
  short: { entry: number; stop_loss: number | null; take_profit: number | null; rr: number };
  method: string;
};

const BULLISH = new Set([
  "hammer", "inverted_hammer", "bullish_engulfing", "bullish_harami",
  "piercing_line", "morning_star", "three_white_soldiers",
]);
const BEARISH = new Set([
  "shooting_star", "bearish_engulfing", "bearish_harami",
  "dark_cloud_cover", "evening_star", "three_black_crows",
]);

/**
 * Native SVG candlestick chart with the markings drawn on: support/resistance
 * lines, ATR stop-loss and take-profit zones, and a dot on every bar where a
 * candlestick pattern fired (green = bullish, red = bearish, grey = neutral).
 *
 * Built natively because TradingView's free widget refuses NSE symbols and
 * cannot be drawn on programmatically.
 */
export type Mark = { i: number; side: "buy" | "sell"; price: number };
export type Fill = {
  i: number; kind: "entry" | "exit"; side: "long" | "short";
  price: number; net?: number | null; reason?: string | null;
};

export function CandleChart({
  bars, levels, side, trend = [], marks = [], fills = [],
}: {
  bars: Bar[]; levels: Levels | null; side: "long" | "short";
  trend?: string[]; marks?: Mark[]; fills?: Fill[];
}) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 900, H = 340, PAD_L = 8, PAD_R = 62, PAD_T = 10, PAD_B = 20;

  const { lo, hi } = useMemo(() => {
    if (!bars.length) return { lo: 0, hi: 1 };
    let lo = Math.min(...bars.map((b) => b.l));
    let hi = Math.max(...bars.map((b) => b.h));
    const cand = [
      ...(levels?.support ?? []), ...(levels?.resistance ?? []),
      levels?.[side]?.stop_loss ?? null, levels?.[side]?.take_profit ?? null,
    ].filter((x): x is number => typeof x === "number");
    for (const v of cand) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    const pad = (hi - lo) * 0.06 || 1;
    return { lo: lo - pad, hi: hi + pad };
  }, [bars, levels, side]);

  if (!bars.length) {
    return <div className="dim" style={{ padding: 20, fontSize: 11 }}>no bars</div>;
  }

  const n = bars.length;
  const plotW = W - PAD_L - PAD_R;
  const cw = plotW / n;
  const bw = Math.max(1.5, Math.min(cw * 0.65, 9));
  const x = (i: number) => PAD_L + i * cw + cw / 2;
  const y = (v: number) => PAD_T + (1 - (v - lo) / (hi - lo || 1)) * (H - PAD_T - PAD_B);

  // Collapse the per-bar trend into contiguous runs so up/down stretches are
  // shaded as bands instead of 127 separate rectangles.
  const bands: { from: number; to: number; dir: string }[] = [];
  for (let i = 0; i < trend.length && i < n; i++) {
    const d = trend[i];
    const last = bands[bands.length - 1];
    if (last && last.dir === d) last.to = i;
    else bands.push({ from: i, to: i, dir: d });
  }

  const lv = levels?.[side];
  const hb = hover != null ? bars[hover] : bars[n - 1];

  function hline(v: number, cls: string, label: string, dash = "4 4") {
    const yy = y(v);
    if (yy < PAD_T || yy > H - PAD_B) return null;
    return (
      <g key={cls + v}>
        <line x1={PAD_L} x2={W - PAD_R} y1={yy} y2={yy} className={`lv ${cls}`} strokeDasharray={dash} />
        <text x={W - PAD_R + 4} y={yy + 3} className={`lvt ${cls}`}>{label} {v.toLocaleString()}</text>
      </g>
    );
  }

  return (
    <div className="candlechart">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const rel = ((e.clientX - r.left) / r.width) * W;
          setHover(Math.max(0, Math.min(n - 1, Math.floor((rel - PAD_L) / cw))));
        }}>
        {/* uptrend / downtrend shading (EMA 9 vs 21 separation) */}
        {bands.filter((b) => b.dir !== "side").map((b, k) => (
          <rect key={`tb${k}`} x={PAD_L + b.from * cw} y={PAD_T}
            width={Math.max(cw, (b.to - b.from + 1) * cw)} height={H - PAD_T - PAD_B}
            fill={b.dir === "up" ? "rgba(52,211,153,0.055)" : "rgba(248,113,113,0.055)"} />
        ))}

        {/* TP / SL shaded zones */}
        {lv?.take_profit != null && (
          <rect x={PAD_L} y={Math.min(y(lv.take_profit), y(lv.entry))}
            width={plotW} height={Math.abs(y(lv.entry) - y(lv.take_profit))}
            fill="rgba(74,222,128,0.07)" />
        )}
        {lv?.stop_loss != null && (
          <rect x={PAD_L} y={Math.min(y(lv.stop_loss), y(lv.entry))}
            width={plotW} height={Math.abs(y(lv.entry) - y(lv.stop_loss))}
            fill="rgba(248,113,113,0.07)" />
        )}

        {/* support / resistance */}
        {(levels?.resistance ?? []).map((v) => hline(v, "res", "R"))}
        {(levels?.support ?? []).map((v) => hline(v, "sup", "S"))}
        {lv?.take_profit != null && hline(lv.take_profit, "tp", "TP", "2 3")}
        {lv?.stop_loss != null && hline(lv.stop_loss, "sl", "SL", "2 3")}

        {/* candles */}
        {bars.map((b, i) => {
          const up = b.c >= b.o;
          const cls = up ? "up" : "dn";
          const yO = y(b.o), yC = y(b.c);
          const top = Math.min(yO, yC);
          const hgt = Math.max(1, Math.abs(yC - yO));
          const pat = b.p.find((p) => BULLISH.has(p) || BEARISH.has(p));
          const pcls = pat ? (BULLISH.has(pat) ? "pb" : "pr") : b.p.length ? "pn" : "";
          return (
            <g key={i} className={`cndl ${cls}`}>
              <line x1={x(i)} x2={x(i)} y1={y(b.h)} y2={y(b.l)} className="wick" />
              <rect x={x(i) - bw / 2} y={top} width={bw} height={hgt} className="body" />
              {pcls && <circle cx={x(i)} cy={y(b.h) - 5} r={2.4} className={pcls} />}
            </g>
          );
        })}

        {/* buy / sell signal arrows from the strategy the desk is running */}
        {marks.filter((m) => m.i >= 0 && m.i < n).map((m, k) => {
          const yy = m.side === "buy" ? y(bars[m.i].l) + 11 : y(bars[m.i].h) - 11;
          const d = m.side === "buy"
            ? `M${x(m.i)},${yy - 7} l4.5,7 h-9 z`
            : `M${x(m.i)},${yy + 7} l4.5,-7 h-9 z`;
          return <path key={`mk${k}`} d={d} className={`sigmark ${m.side}`} />;
        })}

        {/* where the desk actually filled */}
        {fills.filter((f) => f.i >= 0 && f.i < n).map((f, k) => {
          const yy = y(f.price);
          const win = (f.net ?? 0) > 0;
          return (
            <g key={`fl${k}`} className={`fill ${f.kind} ${win ? "win" : "loss"}`}>
              <line x1={x(f.i)} x2={x(f.i)} y1={PAD_T} y2={H - PAD_B} className="fline" />
              {f.kind === "entry" ? (
                <circle cx={x(f.i)} cy={yy} r={4} className="fdot" />
              ) : (
                <g>
                  <line x1={x(f.i) - 4} y1={yy - 4} x2={x(f.i) + 4} y2={yy + 4} className="fx" />
                  <line x1={x(f.i) - 4} y1={yy + 4} x2={x(f.i) + 4} y2={yy - 4} className="fx" />
                </g>
              )}
              <text x={x(f.i) + 6} y={yy - 6} className="ftxt">
                {f.kind === "entry" ? (f.side === "long" ? "BUY" : "SELL") : f.reason ?? "EXIT"}
              </text>
            </g>
          );
        })}

        {/* crosshair */}
        {hover != null && (
          <line x1={x(hover)} x2={x(hover)} y1={PAD_T} y2={H - PAD_B}
            stroke="var(--grid-b)" strokeDasharray="2 3" />
        )}
      </svg>

      <div className="cc-readout">
        <span className="mut">{hb.t}</span>
        <span>O <b>{hb.o}</b></span>
        <span>H <b>{hb.h}</b></span>
        <span>L <b>{hb.l}</b></span>
        <span>C <b className={hb.c >= hb.o ? "green" : "red"}>{hb.c}</b></span>
        {hb.p.length > 0 && (
          <span className="cc-pats">
            {hb.p.map((p) => (
              <span key={p} className={`pat-chip ${BULLISH.has(p) ? "" : BEARISH.has(p) ? "bear" : "mut"}`}>
                {p.replace(/_/g, " ")}
              </span>
            ))}
          </span>
        )}
      </div>
    </div>
  );
}
