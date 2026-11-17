export type Kpis = {
  balance: number;
  initial: number;
  total_net: number;
  total_pnl_pct: number;
  wins: number;
  losses: number;
  win_rate: number;
  best: number;
  worst: number;
  avg_net: number;
  charges_paid: number;
  tax_paid: number;
  gross_total: number;
  open_trades: number;
  total_trades: number;
  unrealized?: number;
  equity?: number;
  open_notional?: number;
};

export type Account = {
  account_id: string;
  balance: number;
  initial: number;
  net_pnl: number;
  net_pnl_pct: number;
  charges_paid: number;
  tax_paid: number;
  gross_total: number;
  strategy: string;
};

export type SignalCheck = { name: string; ok: boolean; reason: string };

export type SignalOption = {
  instrument: string;
  kind: "call" | "put";
  strike: number;
  expiry: string;
  dte: number;
  mid: number;
  bid: number;
  ask: number;
  iv: number;
  spread_pct: number;
  volume: number;
  oi: number;
};

export type Signal = {
  underlying: string;
  source: "india" | "us" | "crypto";
  label: string;
  spot: number | null;
  side: "call" | "put" | null;
  decision: "TAKE" | "AVOID";
  strategy: string;
  score: number | null;
  cost_bite_pct?: number | null;
  required_win_rate?: number | null;
  checks: SignalCheck[];
  option: SignalOption | null;
};

export type OpenTrade = {
  risk_amount?: number;
  risk_pct?: number;
  id: number;
  ts_open: number;
  underlying: string;
  source: string;
  kind: "call" | "put";
  strike: number;
  expiry: string;
  instrument: string;
  strategy: string;
  qty: number;
  entry_premium: number;
  entry_spot: number;
  tp_premium: number;
  sl_premium: number;
  mark_premium: number | null;
};

export type ClosedTrade = {
  entry_spot?: number;
  exit_spot?: number;
  segment?: string;
  id: number;
  ts_open: number;
  ts_close: number;
  underlying: string;
  source: string;
  kind: "call" | "put";
  strike: number;
  instrument: string;
  strategy: string;
  qty: number;
  entry_premium: number;
  exit_premium: number;
  exit_reason: "TP" | "SL" | "EXPIRY" | "SIGNAL";
  gross: number;
  charges: number;
  tax: number;
  net: number;
  status: string;
};

export type EquityPoint = { i: number; ts: number; balance: number; net: number };

export type ActivityEvent = {
  id: number;
  ts: number;
  kind: "ENTRY" | "EXIT" | "INFO";
  body: Record<string, unknown>;
};

export type ModelMetrics = {
  samples: number;
  trained: boolean;
  warmup_remaining: number;
  scored: number;
  mae: number | null;
  brier: number | null;
  directional_accuracy: number | null;
  bias: number;
  top_features: { name: string; weight: number }[];
  updated_at: number;
};

export type SaeMetrics = {
  samples: number;
  trained: boolean;
  warmup_remaining: number;
  latents: number;
  recon_error: number;
  sparsity: number;
  active_now: { name: string; value: number }[];
};

export type ReadinessBar = {
  key: string;
  label: string;
  value: number | null;
  target: number;
  pass: boolean;
};

export type Readiness = {
  bars: ReadinessBar[];
  passed: number;
  total: number;
  live_allowed: boolean;
  places_real_orders: boolean;
  note: string;
};

export type ChargesConfig = {
  brokerage_per_order: number;
  stt_sell_pct: number;
  exchange_txn_pct: number;
  sebi_pct: number;
  stamp_buy_pct: number;
  gst_pct: number;
  tax_rate_pct: number;
  regime: string;
};

export type StrategyInfo = {
  name: string;
  desc: string;
  tp_pct: number;
  sl_pct: number;
  max_spread_pct: number;
  trend_threshold: number;
};

export type Snapshot = {
  account: Account;
  kpis: Kpis;
  signals: Signal[];
  open_trades: OpenTrade[];
  recent_trades: ClosedTrade[];
  equity: EquityPoint[];
  activity: ActivityEvent[];
  model: ModelMetrics;
  sae: SaeMetrics;
  charges: ChargesConfig;
  strategies: { active: string; available: StrategyInfo[] };
  readiness: Readiness;
  universe: { symbol: string; source: string; label: string }[];
  sessions?: { source: string; label: string; open: boolean; hours: string }[];
  fx_usdinr?: number;
  walkforward: WalkForward | null;
  backtest_symbols: string[];
};

export type WalkForward = {
  symbol: string;
  interval: string;
  period: string;
  folds: {
    fold: number; is_return_pct: number; oos_return_pct: number;
    oos_trades: number; oos_win_rate: number; oos_profit_factor: number;
  }[];
  is_mean_return_pct: number;
  oos_mean_return_pct: number;
  oos_positive_folds: number;
  oos_total_folds: number;
  edge_verdict: string;
};

export async function fetchSnapshot(): Promise<Snapshot> {
  const r = await fetch("/api/snapshot", { cache: "no-store" });
  if (!r.ok) throw new Error("snapshot failed");
  return r.json();
}

export async function control(action: "pause" | "resume" | "reset"): Promise<void> {
  await fetch(`/api/control/${action}`, { method: "POST" });
}

export async function setStrategy(name: string): Promise<void> {
  await fetch(`/api/strategy`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

/** Toggle RAPID without naming a strategy. The server remembers what was
 *  running and restores it on the way back, so the toggle cannot clobber a
 *  selection made elsewhere. */
export async function setRapid(on: boolean): Promise<void> {
  await fetch(`/api/control/rapid`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ on }),
  });
}
