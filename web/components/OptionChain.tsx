"use client";
import { useEffect, useState } from "react";

type Leg = { token: string; symbol: string; ltp: number | null; lotsize: number } | null;
type Row = { strike: number; atm: boolean; CE: Leg; PE: Leg };
type Chain = {
  ok: boolean; name: string; expiry: string; expiries: string[];
  atm: number; spot: number; rows: Row[]; live: boolean; lotsize: number | null;
  creds: { ready: boolean; api_key: boolean; client_code: boolean; pin: boolean; totp_secret: boolean };
  error?: string;
};

const INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"];

/**
 * India F&O option chain. Strikes/expiries/lot sizes come from AngelOne's
 * PUBLIC instrument master, so the chain renders without credentials; live
 * premiums need a SmartAPI session (.env). Market data only — this panel
 * never places an order.
 */
export function OptionChain() {
  const [name, setName] = useState("NIFTY");
  const [expiry, setExpiry] = useState<string>("");
  const [c, setC] = useState<Chain | null>(null);
  const [busy, setBusy] = useState(false);

  async function load(n = name, e = expiry) {
    setBusy(true);
    try {
      const q = new URLSearchParams({ name: n, around: "8" });
      if (e) q.set("expiry", e);
      const r = await fetch(`/api/optionchain?${q}`);
      const j = (await r.json()) as Chain;
      setC(j);
      if (j.ok && !e) setExpiry(j.expiry);
    } finally { setBusy(false); }
  }

  useEffect(() => { load("NIFTY", ""); /* eslint-disable-next-line */ }, []);

  return (
    <div className="panel col optionchain">
      <div className="panel-title">
        <span className="dot" />
        CHAIN
        {c?.ok && (
          <span className={`pill ${c.live ? "live" : "mock"}`}>
            {c.live ? "LIVE PREMIUMS" : "STRUCTURE ONLY"}
          </span>
        )}
      </div>

      <div className="oc-controls">
        <select className="strat-select" value={name}
          onChange={(e) => { setName(e.target.value); setExpiry(""); load(e.target.value, ""); }}>
          {INDICES.map((i) => <option key={i} value={i}>{i}</option>)}
        </select>
        <select className="strat-select" value={expiry}
          onChange={(e) => { setExpiry(e.target.value); load(name, e.target.value); }}>
          {(c?.expiries ?? []).map((e) => <option key={e} value={e}>{e}</option>)}
        </select>
        <button onClick={() => load()} disabled={busy}>{busy ? "…" : "Refresh"}</button>
      </div>

      {!c ? (
        <div className="dim tiny" style={{ padding: 10 }}>loading chain…</div>
      ) : !c.ok ? (
        <div className="dim tiny" style={{ padding: 10 }}>{c.error ?? "unavailable"}</div>
      ) : (
        <>
          <div className="oc-meta">
            <span>spot <b className="num">{Math.round(c.spot).toLocaleString()}</b></span>
            <span>ATM <b className="num cyan">{Math.round(c.atm).toLocaleString()}</b></span>
            <span>lot <b className="num">{c.lotsize ?? "—"}</b></span>
            <span className="mut">{c.expiry}</span>
          </div>

          {/* Without credentials every premium is null, so the full ladder was
              17 rows of "—" occupying the tallest panel on the page. Show the
              strike ladder alone until real premiums arrive. */}
          {c.live ? (
            <>
              <div className="oc-head">
                <span>CALL LTP</span><span>CE</span><span>STRIKE</span><span>PE</span><span>PUT LTP</span>
              </div>
              <div className="oc-rows">
                {c.rows.map((r) => (
                  <div className={`oc-row ${r.atm ? "atm" : ""}`} key={r.strike}>
                    <span className="num green">{r.CE?.ltp ?? "—"}</span>
                    <span className="mut tiny">{r.CE ? "CE" : ""}</span>
                    <span className="num strike">{Math.round(r.strike).toLocaleString()}</span>
                    <span className="mut tiny">{r.PE ? "PE" : ""}</span>
                    <span className="num red">{r.PE?.ltp ?? "—"}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <>
              <div className="oc-strip">
                {c.rows.map((r) => (
                  <span className={`oc-chip ${r.atm ? "atm" : ""}`} key={r.strike}>
                    {Math.round(r.strike).toLocaleString()}
                  </span>
                ))}
              </div>
              <div className="oc-note">no premiums · add SmartAPI creds to .env</div>
            </>
          )}
          <div className="oc-foot">market data only · Argona never places orders</div>
        </>
      )}
    </div>
  );
}
