"use client";
import { Header } from "@/components/Header";
import { KpiRow } from "@/components/KpiRow";
import { EquityChart } from "@/components/EquityChart";
import { SignalsPanel } from "@/components/SignalsPanel";
import { OpenTrades } from "@/components/OpenTrades";
import { LiveScalps } from "@/components/LiveScalps";
import { TradeLog } from "@/components/TradeLog";
import { AccountPanel } from "@/components/AccountPanel";
import { ActivityLog } from "@/components/ActivityLog";
import { ModelPanel } from "@/components/ModelPanel";
import { BacktestPanel } from "@/components/BacktestPanel";
import { ChartPanel } from "@/components/ChartPanel";
import { OptionChain } from "@/components/OptionChain";
import { ScannerPanel } from "@/components/ScannerPanel";
import { Footer } from "@/components/Footer";
import { useLive } from "@/lib/useLive";
import { useState } from "react";

export default function Page() {
  const { snap, live } = useLive();
  const [side, setSide] = useState<"both" | "india" | "intl">("both");

  if (!snap) {
    return (
      <main className="app">
        <div className="panel" style={{ textAlign: "center", padding: 40 }}>
          <div className="panel-title" style={{ justifyContent: "center" }}>
            <span className="dot" /> ARGONA F&O · CONNECTING
          </div>
          <div className="dim">waiting for backend @ /api/snapshot…</div>
        </div>
      </main>
    );
  }

  return (
    <main className="app">
      <Header kpis={snap.kpis} live={live} strategy={snap.account?.strategy ?? "momentum_scalp"}
              sessions={snap.sessions} fx={snap.fx_usdinr} />
      <KpiRow k={snap.kpis} />

      <div className="deck-tabs">
        <button className={side === "both" ? "on" : ""} onClick={() => setSide("both")}>BOTH</button>
        <button className={side === "india" ? "on" : ""} onClick={() => setSide("india")}>RESEARCH</button>
        <button className={side === "intl" ? "on" : ""} onClick={() => setSide("intl")}>LIVE DESK</button>
      </div>

      <div className={`decks ${side}`}>
        {/* RESEARCH. No deck heading inside: the tab above already says which
            deck is open, and repeating it cost a full row on every screen. */}
        {side !== "intl" && (
          <section className="deck india">
            <div className="deck-body">
              <ScannerPanel />
              <ChartPanel />
              <OptionChain />
              <BacktestPanel wf={snap.walkforward ?? null} symbols={snap.backtest_symbols ?? []} />
              {snap.model && snap.readiness && (
                <ModelPanel model={snap.model} readiness={snap.readiness} sae={snap.sae} />
              )}
            </div>
          </section>
        )}

        {/* ---------------- LIVE PAPER DESK ---------------- */}
        {side !== "india" && (
          <section className="deck intl">
            <div className="deck-body intl-grid">
              <div className="col">
                <LiveScalps trades={snap.open_trades ?? []} />
                <SignalsPanel signals={snap.signals ?? []} />
                <ActivityLog events={snap.activity} />
              </div>
              <div className="col">
                <EquityChart data={snap.equity ?? []} kpis={snap.kpis} />
                <OpenTrades trades={snap.open_trades ?? []} />
                <TradeLog trades={snap.recent_trades ?? []} activity={snap.activity}
                  initial={snap.kpis.initial} />
              </div>
              <div className="col">
                {snap.account && snap.charges && (
                  <AccountPanel account={snap.account} charges={snap.charges} />
                )}
                {/* Model internals (weights, SAE latents, go-live checklist)
                    are development diagnostics, not things you trade off.
                    They live on the RESEARCH tab now so the desk shows the
                    book and the tape only. */}
              </div>
            </div>
          </section>
        )}
      </div>

      <Footer kpis={snap.kpis} live={live} strategies={snap.strategies ?? { active: "", available: [] }} />
    </main>
  );
}
