"use client";
import { ModelMetrics, Readiness, SaeMetrics } from "@/lib/api";

/**
 * The model's training status + the go-live readiness gate. This is the panel
 * that answers "is it trained yet, and can it trade real money?" — honestly.
 *
 * Directional accuracy is the headline: >50% means the model calls the
 * forward move better than a coin flip. Brier is calibration (lower better;
 * 0.25 is the always-say-50% baseline). Both are scored BEFORE each example
 * trains the model, so they're not self-graded.
 */
export function ModelPanel({
  model,
  readiness,
  sae,
}: {
  model: ModelMetrics;
  readiness: Readiness;
  sae?: SaeMetrics;
}) {
  const dir = model.directional_accuracy;
  const dirPct = dir == null ? null : dir * 100;
  const warm = model.trained;

  return (
    <div className="panel col" style={{ padding: "10px 12px" }}>
      <div className="panel-title">
        <span className="dot" />
        EDGE MODEL
        <span className={`badge ${warm ? "" : ""}`}>
          {warm ? "TRAINED" : `WARMUP ${model.warmup_remaining} LEFT`}
        </span>
      </div>

      {/* training progress */}
      <div className="model-stats">
        <div className="mstat">
          <span className="l">Samples</span>
          <span className="v num">{model.samples}</span>
        </div>
        <div className="mstat">
          <span className="l">Dir. acc</span>
          <span className={`v num ${dirPct != null && dirPct >= 55 ? "green" : dirPct != null && dirPct < 50 ? "red" : ""}`}>
            {dirPct == null ? "—" : `${dirPct.toFixed(1)}%`}
          </span>
        </div>
        <div className="mstat">
          <span className="l">Brier</span>
          <span className={`v num ${model.brier != null && model.brier < 0.22 ? "green" : ""}`}>
            {model.brier == null ? "—" : model.brier.toFixed(3)}
          </span>
        </div>
        <div className="mstat">
          <span className="l">MAE</span>
          <span className="v num">{model.mae == null ? "—" : model.mae.toFixed(3)}</span>
        </div>
      </div>

      {/* top learned features */}
      {model.top_features?.length > 0 && (
        <>
          <div className="model-sub">TOP SIGNALS (|weight|)</div>
          <div className="feat-bars">
            {model.top_features.slice(0, 5).map((f) => {
              const wt = Number.isFinite(f.weight as number) ? (f.weight as number) : 0;
              const max = Math.max(
                ...model.top_features.map((x) => (Number.isFinite(x.weight as number) ? Math.abs(x.weight as number) : 0)),
                1e-6,
              );
              const w = (Math.abs(wt) / max) * 100;
              return (
                <div className="feat" key={f.name}>
                  <span className="fn">{f.name}</span>
                  <span className="fbar">
                    <span
                      className="ffill"
                      style={{ width: `${w}%`, background: wt >= 0 ? "var(--green)" : "var(--red)" }}
                    />
                  </span>
                  <span className="fw num">{wt >= 0 ? "+" : ""}{wt.toFixed(2)}</span>
                </div>
              );
            })}
          </div>
        </>
      )}

      {/* SAE representation learning */}
      {sae && (
        <>
          <div className="model-sub" style={{ marginTop: 8 }}>
            SPARSE AUTOENCODER
            <span className={`pill ${sae.trained ? "live" : "mock"}`} style={{ marginLeft: 8 }}>
              {sae.trained ? `${sae.latents} LATENTS` : `WARMUP ${sae.warmup_remaining}`}
            </span>
          </div>
          <div className="model-stats" style={{ gridTemplateColumns: "repeat(3,1fr)" }}>
            <div className="mstat"><span className="l">Recon</span><span className="v num">{sae.recon_error.toFixed(3)}</span></div>
            <div className="mstat"><span className="l">Sparsity</span><span className="v num">{(sae.sparsity * 100).toFixed(0)}%</span></div>
            <div className="mstat"><span className="l">Samples</span><span className="v num">{sae.samples}</span></div>
          </div>
          {sae.active_now?.length > 0 && (
            <div className="sae-active">
              {sae.active_now.slice(0, 5).map((a) => (
                <span className="sae-chip" key={a.name} title={`${a.name} = ${a.value}`}>
                  {a.name.replace("sae_", "z")}·{a.value.toFixed(1)}
                </span>
              ))}
            </div>
          )}
        </>
      )}

      {/* readiness gate */}
      <div className="model-sub" style={{ marginTop: 8 }}>
        GO-LIVE READINESS
        <span className={`pill ${readiness.live_allowed ? "live" : "mock"}`} style={{ marginLeft: 8 }}>
          {readiness.passed}/{readiness.total} {readiness.live_allowed ? "· READY" : "· LOCKED"}
        </span>
      </div>
      <div className="gate-list">
        {readiness.bars.map((b) => (
          <div className="gate" key={b.key}>
            <span className={`gk ${b.pass ? "ok" : "no"}`}>{b.pass ? "✓" : "○"}</span>
            <span className="gl">{b.label}</span>
            <span className={`gv num ${b.pass ? "green" : "dim"}`}>
              {b.value == null ? "—" : b.value}
            </span>
          </div>
        ))}
      </div>
      <div className="gate-note">
        Paper-only · real orders are never placed by Argona
      </div>
    </div>
  );
}
