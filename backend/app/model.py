"""EdgeModel — the real, trainable fair-value estimator.

It replaces the hash heuristic. The model predicts a market's *forward mid* —
where the YES-side price will be by the time we'd close — from the feature
vector in `features.py`. That prediction is our fair value; the difference
from the current mid is our edge.

Design:
  * Output = sigmoid(w·z + b), naturally bounded to (0, 1) like a probability.
  * z = standardized features (running mean/var, Welford) so raw scales don't
    dominate and so it keeps working as the data distribution shifts.
  * Trained online with SGD + L2 on squared error against the realized close
    mid. Each closed paper position is one real, look-ahead-free example.
  * Weights, normalizer state, sample count, and a rolling metrics window all
    persist to disk, so training survives restarts and genuinely accumulates.

Honesty: this is a real model that really learns. It is NOT a claim of edge.
On near-efficient prediction markets its predicted mid will usually track the
current mid closely (small edges), which is the correct, humble behavior.
`metrics()` reports out-of-sample-ish performance (each example is scored by
the model *before* it trains on it), so the numbers aren't self-congratulatory.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from .features import FEATURE_NAMES, N_FEATURES
from .sae import LATENT_NAMES, N_LATENTS

# The EdgeModel sees raw features PLUS the SAE's sparse latent code appended.
INPUT_NAMES = FEATURE_NAMES + LATENT_NAMES
N_INPUT = N_FEATURES + N_LATENTS

MODEL_PATH = Path(os.environ.get("ARGONA_MODEL", Path(__file__).parent.parent / "data" / "model.json"))
MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

WARMUP_SAMPLES = 40          # below this, fair_value falls back to a prior
LR = 0.05                    # SGD learning rate
L2 = 1e-4                    # ridge penalty
METRIC_WINDOW = 200          # rolling window for reported metrics
MAX_EDGE = 0.25              # clamp: never claim more than 25% edge over mid
Z_CLIP = 8.0                 # clip standardized inputs (numerical stability)
W_CLIP = 50.0                # hard bound on weights


def _finite(a) -> bool:
    return bool(np.all(np.isfinite(a)))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class EdgeModel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.w = np.zeros(N_INPUT, dtype=np.float64)
        self.b = 0.0
        # Welford running mean/var for standardization
        self.n = 0
        self.mean = np.zeros(N_INPUT, dtype=np.float64)
        self.m2 = np.ones(N_INPUT, dtype=np.float64)  # start at 1 so early std!=0
        # Rolling pre-update errors (honest, out-of-sample-ish)
        self.abs_err: deque[float] = deque(maxlen=METRIC_WINDOW)
        self.dir_hits: deque[int] = deque(maxlen=METRIC_WINDOW)  # got direction right?
        # The outcome itself, so accuracy can be compared against always
        # guessing the majority class. A model that is 76% accurate when 76%
        # of outcomes go one way has learned nothing — the same side-matched
        # baseline discipline applied everywhere else in this project.
        self.dir_truth: deque[int] = deque(maxlen=METRIC_WINDOW)
        self.brier: deque[float] = deque(maxlen=METRIC_WINDOW)
        self.updated_at = 0.0
        self._load()

    # -- normalization -------------------------------------------------------

    def _std(self) -> np.ndarray:
        if self.n < 2:
            return np.ones(N_INPUT)
        return np.sqrt(np.maximum(self.m2 / (self.n - 1), 1e-8))

    def _standardize(self, x: np.ndarray) -> np.ndarray:
        return np.clip((x - self.mean) / self._std(), -Z_CLIP, Z_CLIP)

    def _observe(self, x: np.ndarray) -> None:
        # Welford update of feature mean/var (drives standardization).
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)

    # -- inference -----------------------------------------------------------

    def is_warm(self) -> bool:
        return self.n >= WARMUP_SAMPLES

    def predict_fair(self, x_list: list[float], current_mid: float) -> float:
        """Predicted forward mid (our fair value). Falls back to the current
        mid during warmup so the desk isn't betting on an untrained model."""
        with self._lock:
            if self.n < WARMUP_SAMPLES:
                return current_mid
            x = np.asarray(x_list, dtype=np.float64)
            z = self._standardize(x)
            raw = float(self.w @ z + self.b)
            pred = _sigmoid(raw)
        # Clamp the implied edge so a wild extrapolation can't oversize a bet.
        edge = pred - current_mid
        edge = max(-MAX_EDGE, min(MAX_EDGE, edge))
        return max(0.02, min(0.98, current_mid + edge))

    # -- training ------------------------------------------------------------

    def learn(self, x_list: list[float], target_mid: float, entry_mid: float) -> dict:
        """One online SGD step. `target_mid` is the realized close mid (the
        label). Scores the example BEFORE updating so metrics stay honest."""
        x = np.asarray(x_list, dtype=np.float64)
        if not _finite(x):
            return {"pred": None, "target": target_mid, "skipped": "non-finite input"}
        target = float(max(0.0, min(1.0, target_mid)))
        with self._lock:
            z = self._standardize(x)
            pred = _sigmoid(float(self.w @ z + self.b))

            # Honest pre-update scoring
            self.abs_err.append(abs(pred - target))
            self.brier.append((pred - target) ** 2)
            pred_up = pred > entry_mid
            real_up = target > entry_mid
            # only count as a directional call if the model saw an edge
            if abs(pred - entry_mid) > 1e-3:
                self.dir_hits.append(1 if pred_up == real_up else 0)
                self.dir_truth.append(1 if real_up else 0)

            # SGD on squared error through the sigmoid: dL/draw = (pred-target)*pred*(1-pred)
            grad_raw = (pred - target) * pred * (1.0 - pred)
            new_w = np.clip(self.w - LR * (grad_raw * z + L2 * self.w), -W_CLIP, W_CLIP)
            new_b = self.b - LR * grad_raw
            if _finite(new_w) and math.isfinite(new_b):
                self.w, self.b = new_w, float(new_b)
                # Update the normalizer only when the step was valid
                self._observe(x)
            self.updated_at = time.time()
            self._save_locked()
            return {"pred": pred, "target": target}

    # -- metrics -------------------------------------------------------------

    def metrics(self) -> dict:
        with self._lock:
            def _safe(x):
                return float(x) if (x is not None and math.isfinite(float(x))) else None
            n_scored = len(self.abs_err)
            mae = _safe(np.mean(self.abs_err)) if n_scored else None
            brier = _safe(np.mean(self.brier)) if n_scored else None
            dir_acc = _safe(np.mean(self.dir_hits)) if self.dir_hits else None
            # Majority-class baseline: always guess whichever way outcomes
            # mostly went. Beating THIS is the only meaningful claim.
            base = None
            edge = None
            if self.dir_truth:
                up_rate = float(np.mean(self.dir_truth))
                base = max(up_rate, 1.0 - up_rate)
                if dir_acc is not None:
                    edge = dir_acc - base
            # Feature importance = |weight| on standardized features. Coerce any
            # non-finite weight to 0 so the API never emits null/NaN.
            safe_w = [(0.0 if not math.isfinite(w) else w) for w in self.w.tolist()]
            weights = sorted(zip(INPUT_NAMES, safe_w), key=lambda kv: -abs(kv[1]))
            return {
                "samples": int(self.n),
                "trained": self.n >= WARMUP_SAMPLES,
                "warmup_remaining": max(0, WARMUP_SAMPLES - int(self.n)),
                "scored": n_scored,
                "mae": mae,                       # mean abs error of predicted mid
                "brier": brier,                   # calibration (lower better)
                "directional_accuracy": dir_acc,  # % of edge-calls with right sign
                "majority_baseline": _safe(base),
                "edge_over_baseline": _safe(edge),
                "learned_anything": bool(edge is not None and edge > 0.05),
                "bias": _safe(self.b) or 0.0,
                "top_features": [{"name": k, "weight": round(v, 4)} for k, v in weights[:8]],
                "updated_at": self.updated_at,
            }

    # -- persistence ---------------------------------------------------------

    def _save_locked(self) -> None:
        blob = {
            "w": self.w.tolist(),
            "b": self.b,
            "n": self.n,
            "mean": self.mean.tolist(),
            "m2": self.m2.tolist(),
            "abs_err": list(self.abs_err),
            "dir_hits": list(self.dir_hits),
            "brier": list(self.brier),
            "updated_at": self.updated_at,
            "feature_names": INPUT_NAMES,
        }
        tmp = MODEL_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob))
        tmp.replace(MODEL_PATH)

    def _load(self) -> None:
        if not MODEL_PATH.exists():
            return
        try:
            blob = json.loads(MODEL_PATH.read_text())
            if blob.get("feature_names") != INPUT_NAMES:
                # Feature schema changed — start fresh rather than misalign.
                return
            w = np.asarray(blob["w"], dtype=np.float64)
            b = float(blob["b"])
            mean = np.asarray(blob["mean"], dtype=np.float64)
            m2 = np.asarray(blob["m2"], dtype=np.float64)
            # Reject a corrupt (NaN/inf) checkpoint instead of loading poison.
            if not (_finite(w) and math.isfinite(b) and _finite(mean) and _finite(m2)):
                return
            self.w = w
            self.b = b
            self.n = int(blob["n"])
            self.mean = mean
            self.m2 = m2
            self.abs_err = deque(blob.get("abs_err", []), maxlen=METRIC_WINDOW)
            self.dir_hits = deque(blob.get("dir_hits", []), maxlen=METRIC_WINDOW)
            self.brier = deque(blob.get("brier", []), maxlen=METRIC_WINDOW)
            self.updated_at = float(blob.get("updated_at", 0.0))
        except Exception:
            # Corrupt file — ignore and start fresh.
            pass

    def reset(self) -> None:
        with self._lock:
            self.__init__()  # type: ignore[misc]


# Process-wide singleton.
_model: EdgeModel | None = None


def get_model() -> EdgeModel:
    global _model
    if _model is None:
        _model = EdgeModel()
    return _model
