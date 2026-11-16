"""SparseAutoencoder — unsupervised representation learning over the market
feature vector.

Why it's here: the EdgeModel is a linear predictor. An SAE learns a small set
of *sparse latent factors* that reconstruct the raw features — think "regimes"
or "microstructure motifs" that only a few of which are active at once. Those
latents are appended to the EdgeModel's input, so the predictor can key off
learned structure instead of only the raw columns. The latents are also
interpretable: `top_active()` shows which factors are firing right now.

It trains unsupervised on EVERY market we scan (no labels needed), so it
accumulates data far faster than the supervised EdgeModel (which only learns
on closed positions).

Pure numpy, online SGD, tanh-free: encoder is Linear→ReLU (non-negative, hence
naturally sparse under an L1 penalty), decoder is Linear. Weights + normalizer
persist to disk.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

from .features import N_FEATURES

SAE_PATH = Path(os.environ.get("ARGONA_SAE", Path(__file__).parent.parent / "data" / "sae.json"))
SAE_PATH.parent.mkdir(parents=True, exist_ok=True)

N_LATENTS = 12               # overcomplete-ish sparse code over 17 raw features
SAE_WARMUP = 60              # below this, encode() returns zeros (untrained)
LR = 0.02
L1 = 0.02                    # sparsity penalty on latent activations
Z_CLIP = 8.0                 # clip standardized inputs so a tiny-variance
                             # feature can't produce a huge activation
GRAD_CLIP = 5.0              # clip per-element gradients (stops NaN blowups)
W_CLIP = 50.0                # hard bound on weights/biases
LATENT_NAMES = [f"sae_{i}" for i in range(N_LATENTS)]


def _finite(a: np.ndarray) -> bool:
    return bool(np.all(np.isfinite(a)))


def _clip(a: np.ndarray, lim: float) -> np.ndarray:
    return np.clip(a, -lim, lim)


def _relu(x: np.ndarray) -> np.ndarray:
    return np.maximum(0.0, x)


class SparseAutoencoder:
    def __init__(self) -> None:
        rng = np.random.default_rng(7)
        self.We = rng.normal(0, 0.3, size=(N_LATENTS, N_FEATURES))
        self.be = np.zeros(N_LATENTS)
        self.Wd = rng.normal(0, 0.3, size=(N_FEATURES, N_LATENTS))
        self.bd = np.zeros(N_FEATURES)
        # running standardization (Welford)
        self.n = 0
        self.mean = np.zeros(N_FEATURES)
        self.m2 = np.ones(N_FEATURES)
        # rolling metrics
        self._recon = 0.0
        self._sparsity = 0.0
        self._last_latent = np.zeros(N_LATENTS)
        self._load()

    # -- normalization -------------------------------------------------------

    def _std(self) -> np.ndarray:
        if self.n < 2:
            return np.ones(N_FEATURES)
        return np.sqrt(np.maximum(self.m2 / (self.n - 1), 1e-8))

    def _standardize(self, x: np.ndarray) -> np.ndarray:
        return _clip((x - self.mean) / self._std(), Z_CLIP)

    def _observe(self, x: np.ndarray) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    def is_warm(self) -> bool:
        return self.n >= SAE_WARMUP

    # -- inference -----------------------------------------------------------

    def encode(self, x_list: list[float]) -> list[float]:
        """Sparse latent code for a raw feature vector. Zeros until warm so we
        never feed the EdgeModel random noise."""
        if not self.is_warm():
            return [0.0] * N_LATENTS
        x = np.asarray(x_list, dtype=np.float64)
        if not _finite(x):
            return [0.0] * N_LATENTS
        z = self._standardize(x)
        h = _relu(self.We @ z + self.be)
        if not _finite(h):
            return [0.0] * N_LATENTS
        return _clip(h, W_CLIP).tolist()

    # -- training ------------------------------------------------------------

    def train(self, x_list: list[float]) -> None:
        """One online SGD step of reconstruction + L1 sparsity. Guarded against
        non-finite inputs and gradient blowups so a long unattended run can't
        diverge to NaN."""
        x = np.asarray(x_list, dtype=np.float64)
        if not _finite(x):
            return  # a NaN upstream (e.g. bad feature) must not poison weights
        z = self._standardize(x)
        pre = self.We @ z + self.be
        h = _relu(pre)
        x_hat = self.Wd @ h + self.bd

        err = x_hat - z                          # (D,)
        # decoder grads
        gWd = _clip(np.outer(err, h), GRAD_CLIP)
        gbd = _clip(err, GRAD_CLIP)
        # backprop to hidden
        dh = self.Wd.T @ err + L1 * (h > 0)      # recon + L1 subgradient
        dpre = dh * (pre > 0)                     # through ReLU
        gWe = _clip(np.outer(dpre, z), GRAD_CLIP)
        gbe = _clip(dpre, GRAD_CLIP)

        if not (_finite(gWd) and _finite(gbd) and _finite(gWe) and _finite(gbe)):
            return

        self.Wd = _clip(self.Wd - LR * gWd, W_CLIP)
        self.bd = _clip(self.bd - LR * gbd, W_CLIP)
        self.We = _clip(self.We - LR * gWe, W_CLIP)
        self.be = _clip(self.be - LR * gbe, W_CLIP)

        # metrics (EWMA)
        recon = float(np.mean(err ** 2))
        spars = float(np.mean(h > 1e-6))
        a = 0.02
        self._recon = (1 - a) * self._recon + a * recon if self.n else recon
        self._sparsity = (1 - a) * self._sparsity + a * spars if self.n else spars
        self._last_latent = h

        self._observe(x)
        self._save()

    # -- introspection -------------------------------------------------------

    def metrics(self) -> dict:
        def _safe(x, d=0.0):
            x = float(x)
            return round(x, 4) if math.isfinite(x) else d
        active = [
            {"name": LATENT_NAMES[i], "value": round(float(v), 3)}
            for i, v in sorted(enumerate(self._last_latent), key=lambda kv: -kv[1])
            if math.isfinite(float(v)) and v > 1e-6
        ][:6]
        return {
            "samples": int(self.n),
            "trained": self.is_warm(),
            "warmup_remaining": max(0, SAE_WARMUP - int(self.n)),
            "latents": N_LATENTS,
            "recon_error": _safe(self._recon),
            "sparsity": _safe(self._sparsity),   # avg fraction of latents active
            "active_now": active,
        }

    # -- persistence ---------------------------------------------------------

    def _save(self) -> None:
        blob = {
            "We": self.We.tolist(), "be": self.be.tolist(),
            "Wd": self.Wd.tolist(), "bd": self.bd.tolist(),
            "n": self.n, "mean": self.mean.tolist(), "m2": self.m2.tolist(),
            "recon": self._recon, "sparsity": self._sparsity,
            "d": N_FEATURES, "h": N_LATENTS,
        }
        tmp = SAE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob))
        tmp.replace(SAE_PATH)

    def _load(self) -> None:
        if not SAE_PATH.exists():
            return
        try:
            b = json.loads(SAE_PATH.read_text())
            if b.get("d") != N_FEATURES or b.get("h") != N_LATENTS:
                return  # shape changed — start fresh
            We = np.asarray(b["We"]); be = np.asarray(b["be"])
            Wd = np.asarray(b["Wd"]); bd = np.asarray(b["bd"])
            mean = np.asarray(b["mean"]); m2 = np.asarray(b["m2"])
            # Refuse a corrupt (NaN/inf) checkpoint — start fresh instead of
            # loading poison. This is what bit us on the first long run.
            if not all(_finite(a) for a in (We, be, Wd, bd, mean, m2)):
                return
            self.We, self.be, self.Wd, self.bd = We, be, Wd, bd
            self.n = int(b["n"]); self.mean = mean; self.m2 = m2
            self._recon = float(b.get("recon", 0.0)); self._sparsity = float(b.get("sparsity", 0.0))
        except Exception:
            pass

    def reset(self) -> None:
        with_default = SparseAutoencoder.__new__(SparseAutoencoder)
        self.__dict__.update(with_default.__dict__) if False else None
        self.__init__()  # type: ignore[misc]


_sae: SparseAutoencoder | None = None


def get_sae() -> SparseAutoencoder:
    global _sae
    if _sae is None:
        _sae = SparseAutoencoder()
    return _sae
