// The desk runs in INR — NSE F&O is a rupee market, so every amount the UI
// shows is rupees. Indian digit grouping (1,23,456) via en-IN, because a
// lakh-grouped number is what the numbers will be checked against.

export function inr(n: number, digits = 2): string {
  const sign = n < 0 ? "−" : "";
  const abs = Math.abs(n);
  return `${sign}₹${abs.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function inrSigned(n: number, digits = 2): string {
  const sign = n >= 0 ? "+" : "−";
  const abs = Math.abs(n);
  return `${sign}₹${abs.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** Compact rupees in Indian units — 1.2L, 3.4Cr — for tight spaces. */
export function inrCompact(n: number): string {
  const sign = n < 0 ? "−" : "";
  const a = Math.abs(n);
  if (a >= 1_00_00_000) return `${sign}₹${(a / 1_00_00_000).toFixed(2)}Cr`;
  if (a >= 1_00_000) return `${sign}₹${(a / 1_00_000).toFixed(2)}L`;
  if (a >= 1_000) return `${sign}₹${(a / 1_000).toFixed(1)}K`;
  return `${sign}₹${a.toFixed(0)}`;
}

/** A price sized to its magnitude, for grid cells that cannot grow.
 *  BTC in rupees is 6052694.74 — thirteen characters in a ~35px column, which
 *  is what made the open-trades and trade-log tables collide. Big numbers do
 *  not need paise: 60,52,695 says the same thing in nine. */
export function price(n: number): string {
  const a = Math.abs(n);
  const d = a >= 10_000 ? 0 : a >= 100 ? 1 : 2;
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

// Legacy aliases — the components were written against usd()/usdSigned().
// Kept so nothing silently renders the wrong symbol during the migration.
export const usd = inr;
export const usdSigned = inrSigned;

export function pct(n: number, digits = 1): string {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)}%`;
}

export function num(n: number, digits = 2): string {
  return n.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function compact(n: number): string {
  if (Math.abs(n) >= 1_00_00_000) return `${(n / 1_00_00_000).toFixed(1)}Cr`;
  if (Math.abs(n) >= 1_00_000) return `${(n / 1_00_000).toFixed(1)}L`;
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toFixed(0);
}

export function timeHMS(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" });
}

export function timeHM(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("en-IN", {
    hour12: false, hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata",
  });
}

export function uptimeSince(startTsSec: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - startTsSec));
  const h = Math.floor(s / 3600).toString().padStart(2, "0");
  const m = Math.floor((s % 3600) / 60).toString().padStart(2, "0");
  const ss = (s % 60).toString().padStart(2, "0");
  return `${h}:${m}:${ss}`;
}
