"""Local LLM on this laptop. Ollama only, 127.0.0.1 only, no network egress.

WHAT THIS IS NOT
----------------
This module does not generate trading signals and must never be wired into
the execution path. An LLM has no price data the engine lacks, it fabricates
numbers under pressure, and a single call costs seconds -- which is fatal to a
scalper whose whole thesis is a 1.0% move. Across ~38 tested strategies the
desk has found zero validated edge; an LLM reading the same candles does not
change that arithmetic.

WHAT IT IS FOR
--------------
1. `explain()`  -- turn the structured facts behind a decision (agent votes,
   features, costs) into a sentence a human can audit. Read-only narration of
   numbers computed elsewhere; the LLM is never the source of a number.
2. `propose()`  -- emit strategy hypotheses as strict JSON parameter sets.
   The LLM proposes; signal_eval's gauntlet disposes. A proposal is a
   candidate to be tested against a side-matched random baseline, split-half
   replication and buy-and-hold -- never something to arm.

The asymmetry is the point: the model may suggest what to test, and has no
say in what passes.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

# Loopback is hard-coded rather than configurable. A remote "ollama" host would
# mean the desk's positions, balance and research leave this machine, which is
# the one thing this module exists to prevent.
OLLAMA = "http://127.0.0.1:11434"

# 8 GB of unified memory shared with the API, Next.js and the browser. phi3
# (3.8B, Q4, 2.2 GB) leaves headroom; llama3 (8B, 4.7 GB) does not reliably,
# so it stays opt-in via ARGONA_LLM rather than being the default.
DEFAULT_MODEL = os.environ.get("ARGONA_LLM", "phi3:latest")
TIMEOUT = float(os.environ.get("ARGONA_LLM_TIMEOUT", "90"))


@dataclass
class LlmReply:
    ok: bool
    text: str = ""
    model: str = ""
    error: str = ""
    seconds: float = 0.0


def available() -> dict:
    """Is a local model actually loadable right now? Reports rather than raises:
    every caller here is optional enrichment, and the desk must trade normally
    when the LLM is absent."""
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{OLLAMA}/api/tags")
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
        return {
            "ok": True,
            "models": models,
            "default": DEFAULT_MODEL,
            "default_present": DEFAULT_MODEL in models,
        }
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as data
        return {"ok": False, "models": [], "default": DEFAULT_MODEL,
                "default_present": False, "error": str(e)}


def generate(prompt: str, system: str = "", model: str | None = None,
             temperature: float = 0.2, num_predict: int = 400,
             json_mode: bool = False) -> LlmReply:
    """One non-streaming completion against the local daemon.

    `json_mode` uses Ollama's grammar-constrained decoding. Asking a 3.8B
    model for JSON in the prompt is not enough -- phi3 produced a truncated
    object with an unterminated string on the first real attempt. The
    constraint makes malformed output structurally impossible rather than
    merely discouraged.
    """
    import time
    mdl = model or DEFAULT_MODEL
    body: dict[str, Any] = {
        "model": mdl,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
            # Keep the context modest: a long window on 8 GB pushes the model
            # into swap and a 90s timeout starts looking like a hang.
            "num_ctx": 4096,
        },
    }
    if system:
        body["system"] = system
    if json_mode:
        body["format"] = "json"
    t0 = time.time()
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.post(f"{OLLAMA}/api/generate", json=body)
            r.raise_for_status()
            txt = r.json().get("response", "").strip()
        return LlmReply(ok=True, text=txt, model=mdl, seconds=round(time.time() - t0, 1))
    except Exception as e:  # noqa: BLE001
        return LlmReply(ok=False, model=mdl, error=str(e),
                        seconds=round(time.time() - t0, 1))


EXPLAIN_SYSTEM = (
    "You explain paper-trading decisions for an Indian F&O research desk. "
    "You are given facts that were already computed. Restate them in two or "
    "three plain sentences for a trader. "
    "Rules: use only the numbers provided, never invent a price, percentage "
    "or probability, never predict what happens next, and never recommend a "
    "trade. If the facts show a loss or a weak edge, say so plainly."
)


def explain(facts: dict, model: str | None = None) -> LlmReply:
    """Narrate a decision from facts computed by the engine.

    Every number in the output must already appear in `facts`; the prompt says
    so and `check_numbers_grounded` lets the caller verify it rather than
    trusting the instruction held.
    """
    return generate(
        prompt="Facts:\n" + json.dumps(label_units(facts), indent=2, default=str) +
               "\n\nExplain this decision in 2-3 sentences. Copy every number "
               "exactly as written, including its unit.",
        system=EXPLAIN_SYSTEM, model=model, temperature=0.1, num_predict=220)


_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")


def label_units(facts: dict) -> dict:
    """Render ambiguous numerics as unit-bearing strings before prompting.

    On its first live test phi3 read `risk_pct_of_book: 0.08` as "8% of the
    book" -- a 100x misstatement of the single number that decides whether a
    position is sane. A bare float carries no unit, so the model supplies one.
    Giving it "0.08% of book" removes the guess.
    """
    out: dict[str, Any] = {}
    for k, v in facts.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            out[k] = v
        elif k.endswith("_pct") or "pct" in k:
            out[k] = f"{v}% (percent, not a fraction)"
        elif k.endswith("_inr") or k in ("balance", "notional", "entry", "tp", "sl"):
            out[k] = f"INR {v:,.2f}"
        else:
            out[k] = v
    return out


def check_numbers_grounded(text: str, facts: dict,
                           tol: float = 0.01) -> list[str]:
    """Return numbers in `text` that do not appear in `facts`.

    A non-empty result means the model invented figures and the narration
    should be discarded. Small integers (0-100) are ignored: they are usually
    sentence counts, years or percentages restated from context, and flagging
    them buries the real fabrications in noise.
    """
    # Strip thousands separators first. Without this "Rs 486,320.29" is read
    # as the two numbers 486 and 320.29, neither of which is in the facts, so
    # a perfectly grounded sentence gets flagged while real fabrications are
    # lost in the noise.
    blob = _THOUSANDS.sub("", json.dumps(facts, default=str))
    known = {round(float(m), 4) for m in _NUM.findall(blob)}
    bad: list[str] = []
    for m in _NUM.findall(_THOUSANDS.sub("", text)):
        v = float(m)
        if 0 <= v <= 100 and float(v).is_integer():
            continue
        if not any(abs(v - k) <= max(tol, abs(k) * tol) for k in known):
            bad.append(m)
    return bad


PROPOSE_SYSTEM = (
    "You are a quantitative research assistant proposing strategy variants to "
    "be BACKTESTED. You are not choosing trades and nothing you output will be "
    "traded. Output STRICT JSON only, no prose, no markdown fence: "
    '{\"proposals\":[{\"name\":str,\"rationale\":str,\"direction\":'
    '\"momentum\"|\"meanrev\",\"trend_threshold\":float,\"tp_spot_pct\":float,'
    '\"sl_spot_pct\":float,\"cooldown_sec\":int,\"max_concurrent\":int}]}. '
    "Constraints: tp_spot_pct and sl_spot_pct are fractions of spot, both "
    "between 0.001 and 0.05. Every proposal must be testable and must differ "
    "from the others in more than one parameter."
)


def propose(context: dict, n: int = 3, model: str | None = None) -> dict:
    """Ask for strategy hypotheses. Returns parsed proposals plus the raw reply.

    Nothing here is armed. Each proposal is a candidate for the gauntlet:
    event study against a side-matched random baseline, split-half
    replication, Bonferroni correction, and a buy-and-hold comparison. The
    break-even win rate is computed from the proposal's own TP/SL and cost
    so an arithmetically impossible proposal is rejected before it burns a
    backtest.
    """
    rep = generate(
        prompt=("Desk context:\n" + json.dumps(context, indent=2, default=str) +
                f"\n\nPropose {n} testable strategy variants as JSON."),
        system=PROPOSE_SYSTEM, model=model, temperature=0.6, num_predict=700,
        json_mode=True)
    if not rep.ok:
        return {"ok": False, "error": rep.error, "proposals": [], "raw": ""}

    raw = rep.text
    # Small models wrap JSON in prose or a fence despite being told not to.
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"ok": False, "error": "no JSON in reply", "proposals": [],
                "raw": raw[:800], "model": rep.model, "seconds": rep.seconds}
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"bad JSON: {e}", "proposals": [],
                "raw": raw[:800], "model": rep.model, "seconds": rep.seconds}

    good, rejected = [], []
    for p in (parsed.get("proposals") or [])[:n]:
        try:
            tp = float(p["tp_spot_pct"])
            sl = float(p["sl_spot_pct"])
        except (KeyError, TypeError, ValueError):
            rejected.append({"proposal": p, "why": "missing or non-numeric tp/sl"})
            continue
        if not (0.001 <= tp <= 0.05 and 0.001 <= sl <= 0.05):
            rejected.append({"proposal": p, "why": f"tp/sl out of range ({tp}, {sl})"})
            continue
        # Underlying round trip: ~0.05% all-in. The LLM does not get to assume
        # a cost; it is imposed here.
        cost = 0.0005
        be = (sl + cost) / (tp + sl)
        if be >= 1.0:
            rejected.append({"proposal": p, "why": "break-even win rate >= 100%"})
            continue
        p["break_even_win_rate"] = round(be, 4)
        p["status"] = "UNTESTED - must pass the gauntlet before it can be armed"
        good.append(p)

    return {"ok": True, "model": rep.model, "seconds": rep.seconds,
            "proposals": good, "rejected": rejected, "raw": raw[:400]}
