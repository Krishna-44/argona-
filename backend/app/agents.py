"""Agent committee — specialists that each own one question about a trade.

Seven agents, split into two kinds that are deliberately NOT treated alike:

**Gatekeepers** answer questions with arithmetic, so they get a VETO.
  * cost      — does the round trip cost more than the target is worth?
  * risk      — does this fit the risk budget and the capital caps?
  * validation— has this strategy ever shown edge on this instrument?

**Predictors** answer questions by forecasting, so they only get a VOTE, and
their vote is weighted by their own measured hit rate.
  * trend     — direction and strength from market structure
  * pattern   — candlestick evidence
  * momentum  — the UT/Supertrend/VWAP/RSI composite
  * volatility— is vol expanding or contracting, and is premium rich or cheap?

Why the split matters: this project has measured ~38 strategies across three
venues and found no validated edge, while separately finding that costs and
position sizing decide the outcome almost entirely. So the agents that
*measure* are trusted, and the agents that *predict* start with zero influence
and have to earn it from closed trades. An agent with fewer than MIN_SAMPLES
scored trades carries no weight at all — a fresh agent cannot talk the desk
into a position on confidence alone.

"Training" here means each predictor's vote is recorded at entry, scored when
the trade closes, and its weight recomputed from its realised hit rate and
Brier score. That is honest calibration, not a persona.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, asdict

MIN_SAMPLES = 20          # votes needed before an agent carries any weight
SCORE_KEY = "agent_scores"


@dataclass
class AgentView:
    agent: str
    role: str
    kind: str                 # "gatekeeper" | "predictor"
    vote: float               # -1 bearish .. +1 bullish (0 = no opinion)
    confidence: float         # 0..1, the agent's own conviction
    weight: float             # influence actually granted, 0 until calibrated
    reason: str
    veto: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verdict:
    decision: str             # TAKE | AVOID | VETO
    side: str | None          # "call" (long) | "put" (short) | None
    score: float              # weighted committee score, -1..+1
    agreement: float          # share of voting agents on the winning side
    views: list[dict] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# scorecards — the "training" state


def _load_scores(state) -> dict:
    try:
        raw = state.get_meta(SCORE_KEY, None)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_scores(state, scores: dict) -> None:
    try:
        state.set_meta(SCORE_KEY, scores)
    except Exception:
        pass


def agent_weight(sc: dict) -> float:
    """Influence earned from realised accuracy.

    Below MIN_SAMPLES the weight is exactly 0: an uncalibrated agent must not
    move the desk. Above it, weight scales with how far the hit rate beats a
    coin flip, so an agent that is right half the time earns nothing either.
    """
    n = int(sc.get("n", 0))
    if n < MIN_SAMPLES:
        return 0.0
    hits = float(sc.get("hits", 0))
    rate = hits / n
    edge = max(0.0, rate - 0.5) * 2.0       # 0 at 50%, 1.0 at 100%
    confidence_in_estimate = min(1.0, n / 100.0)
    return round(edge * confidence_in_estimate, 4)


def scorecard(state) -> dict:
    """Per-agent record, for the API and the UI."""
    scores = _load_scores(state)
    out = {}
    for name, sc in scores.items():
        n = int(sc.get("n", 0))
        hits = float(sc.get("hits", 0))
        brier = float(sc.get("brier", 0.0))
        out[name] = {
            "votes_scored": n,
            "hit_rate": round(hits / n * 100, 1) if n else None,
            "brier": round(brier / n, 4) if n else None,
            "weight": agent_weight(sc),
            "calibrated": n >= MIN_SAMPLES,
            "needs": max(0, MIN_SAMPLES - n),
        }
    return out


def record_outcome(state, votes: dict, won: bool) -> None:
    """Score every agent's vote on a closed trade.

    `votes` is {agent_name: signed vote} captured at entry. An agent that
    abstained (vote 0) is not scored — it made no claim.
    """
    scores = _load_scores(state)
    for name, vote in (votes or {}).items():
        try:
            v = float(vote)
        except Exception:
            continue
        if abs(v) < 1e-9:
            continue
        sc = scores.setdefault(name, {"n": 0, "hits": 0, "brier": 0.0})
        sc["n"] = int(sc.get("n", 0)) + 1
        # The agent was "right" if it leaned the way the trade resolved. The
        # desk only ever takes the committee's side, so a long-voting agent is
        # right exactly when the trade won.
        agreed_with_desk = v > 0
        correct = (agreed_with_desk == won)
        if correct:
            sc["hits"] = float(sc.get("hits", 0)) + 1
        # Brier: squared error between stated confidence and the outcome.
        p = min(1.0, abs(v))
        sc["brier"] = float(sc.get("brier", 0.0)) + (p - (1.0 if correct else 0.0)) ** 2
    _save_scores(state, scores)


# ---------------------------------------------------------------------------
# the agents


def _trend_agent(ctx) -> AgentView:
    t = (ctx.get("structure") or {}).get("trend") or {}
    d = t.get("direction")
    adx = float(t.get("adx") or 0.0)
    strength = float(t.get("strength") or 0.0)
    vote = (1.0 if d == "up" else -1.0 if d == "down" else 0.0) * min(strength, 1.0)
    return AgentView(
        "trend", "direction & strength from market structure", "predictor",
        round(vote, 3), round(min(adx / 40.0, 1.0), 3), 0.0,
        f"{d or 'sideways'} · ADX {adx:.0f} · {t.get('structure', 'n/a')}")


def _pattern_agent(ctx) -> AgentView:
    s = float(ctx.get("candle_score") or 0.0)
    pats = ctx.get("patterns") or []
    vote = max(-1.0, min(1.0, s / 3.0))
    return AgentView(
        "pattern", "candlestick evidence on the latest bars", "predictor",
        round(vote, 3), round(min(abs(s) / 3.0, 1.0), 3), 0.0,
        (", ".join(p.replace("_", " ") for p in pats[:3]) if pats
         else "no pattern on the last bar"))


def _momentum_agent(ctx) -> AgentView:
    u = ctx.get("ut_score")
    if u is None:
        return AgentView("momentum", "UT/Supertrend/VWAP/RSI composite", "predictor",
                         0.0, 0.0, 0.0, "composite unavailable")
    vote = max(-1.0, min(1.0, float(u) / 6.0))
    return AgentView(
        "momentum", "UT/Supertrend/VWAP/RSI composite", "predictor",
        round(vote, 3), round(min(abs(float(u)) / 6.0, 1.0), 3), 0.0,
        f"composite {float(u):+.0f} of ±6")


def _volatility_agent(ctx) -> AgentView:
    """Is volatility supportive of this trade reaching its target?

    This used to look for a `realised_vol` key the regime analysis never
    produces, so it abstained on every trade and contributed nothing in the
    mode the desk actually runs. The regime does expose an ATR `percentile`,
    which answers the question that matters for a momentum entry: a 1% target
    in a 6th-percentile tape is unlikely to be reached inside the horizon,
    while an expanding tape supports continuation.

    Expressed as a directional vote (the committee's only currency) by leaning
    WITH the prevailing trend when volatility is expanding and against it when
    the tape is too quiet to carry price to the target.
    """
    reg = (ctx.get("structure") or {}).get("regime") or {}
    trend = (ctx.get("structure") or {}).get("trend") or {}
    label = reg.get("label", "n/a")
    pct = reg.get("percentile")
    iv, rv = ctx.get("iv"), ctx.get("rv")

    # Options: the premium rich/cheap question still applies when both are known.
    if iv and rv and rv > 0:
        vrp = iv - rv
        return AgentView(
            "volatility", "vol regime; premium rich vs cheap", "predictor",
            round(max(-0.5, min(0.5, -vrp * 4.0)), 3), 0.5, 0.0,
            f"regime {label} · IV {iv*100:.0f}% vs RV {rv*100:.0f}% ({vrp*100:+.0f})")

    if pct is None:
        return AgentView("volatility", "vol regime support", "predictor",
                         0.0, 0.2, 0.0, f"regime {label} — no percentile")

    # Lean on the STRUCTURAL trend when there is one. When there is not --
    # market_structure reports "side" most of the time -- fall back to the side
    # the strategy is actually proposing. Leaning on the structural trend alone
    # meant lean=0 and therefore vote=0 on nearly every trade, so this agent
    # abstained its way to a permanent weight of zero and never learned.
    direction = trend.get("direction")
    lean = 1.0 if direction == "up" else -1.0 if direction == "down" else 0.0
    if lean == 0.0:
        side = ctx.get("side")
        lean = 1.0 if side == "call" else -1.0 if side == "put" else 0.0
    # -1 at the quietest, +1 at the most active; 0 at the median.
    support = max(-1.0, min(1.0, (float(pct) - 0.5) * 2.0))
    vote = lean * support * 0.6
    return AgentView(
        "volatility", "vol regime support for reaching the target", "predictor",
        round(vote, 3), round(min(abs(support), 1.0), 3), 0.0,
        f"regime {label} · ATR {float(pct)*100:.0f}th pct — "
        + ("supports continuation" if support > 0.2 else
           "too quiet to reach target" if support < -0.2 else "neutral"))


def _cost_agent(ctx) -> AgentView:
    need = ctx.get("required_win_rate")
    bite = ctx.get("cost_bite_pct")
    if need is None:
        # The signal stopped before a quote was fetched (no setup, wrong
        # session, crossed book), so there is nothing to cost yet. Say that
        # rather than "not computed", which reads like a failure.
        return AgentView("cost", "round-trip cost vs the target", "gatekeeper",
                         0.0, 0.0, 1.0,
                         "nothing to price — signal stopped before a quote",
                         veto=False)
    veto = need >= 100.0
    reason = (f"needs {need:.0f}% win rate"
              + (f" · cost is {bite:.0f}% of the target" if bite is not None else ""))
    if veto:
        reason += " — impossible, cost exceeds the target"
    return AgentView("cost", "round-trip cost vs the target", "gatekeeper",
                     0.0, 1.0, 1.0, reason, veto=veto)


def _risk_agent(ctx) -> AgentView:
    bal = float(ctx.get("balance") or 0.0)
    deployed = float(ctx.get("deployed") or 0.0)
    cap = float(ctx.get("deployed_cap") or 0.0)
    open_n = int(ctx.get("open_trades") or 0)
    max_open = int(ctx.get("max_open") or 6)
    veto = (cap > 0 and deployed >= cap) or open_n >= max_open
    pct = (deployed / bal * 100) if bal else 0.0
    reason = f"book {pct:.0f}% deployed · {open_n}/{max_open} positions"
    if veto:
        reason += " — at the limit"
    return AgentView("risk", "capital, exposure and position limits", "gatekeeper",
                     0.0, 1.0, 1.0, reason, veto=veto)


def _validation_agent(ctx) -> AgentView:
    """Has this strategy ever shown edge out of sample?

    Across ~38 strategies on three venues nothing has. This agent exists so
    that fact is stated on every single trade rather than forgotten, and it
    does not veto — the desk is for practice, and refusing everything would
    teach nothing.
    """
    strat = ctx.get("strategy", "?")
    validated = bool(ctx.get("strategy_validated"))
    if validated:
        return AgentView("validation", "out-of-sample evidence for this strategy",
                         "gatekeeper", 0.0, 1.0, 1.0,
                         f"{strat} has out-of-sample support")
    return AgentView(
        "validation", "out-of-sample evidence for this strategy", "gatekeeper",
        0.0, 1.0, 1.0,
        f"{strat} has NO validated edge — paper practice only", veto=False)


AGENTS = [_trend_agent, _pattern_agent, _momentum_agent, _volatility_agent,
          _cost_agent, _risk_agent, _validation_agent]


def deliberate(ctx: dict, state=None, min_score: float = 0.25) -> Verdict:
    """Run the committee and return a single verdict.

    A gatekeeper veto ends it. Otherwise the predictors' weighted votes decide,
    and because an uncalibrated predictor carries weight 0, a brand-new
    committee produces no trades until the agents have earned influence.
    """
    scores = _load_scores(state) if state is not None else {}
    views: list[AgentView] = []
    for fn in AGENTS:
        try:
            v = fn(ctx)
        except Exception as e:
            v = AgentView(getattr(fn, "__name__", "agent"), "errored", "predictor",
                          0.0, 0.0, 0.0, f"error: {e!r}")
        if v.kind == "predictor":
            v.weight = agent_weight(scores.get(v.agent, {}))
        views.append(v)

    vetoes = [v.agent for v in views if v.veto]
    if vetoes:
        blocking = next(v for v in views if v.veto)
        return Verdict("VETO", None, 0.0, 0.0, [v.as_dict() for v in views],
                       vetoes, f"{blocking.agent}: {blocking.reason}")

    preds = [v for v in views if v.kind == "predictor"]
    wsum = sum(v.weight for v in preds)
    if wsum <= 0:
        return Verdict(
            "AVOID", None, 0.0, 0.0, [v.as_dict() for v in views], [],
            "no agent is calibrated yet — each needs "
            f"{MIN_SAMPLES} scored votes before it can move the desk")

    score = sum(v.vote * v.weight for v in preds) / wsum
    side = "call" if score > 0 else "put"
    agreeing = [v for v in preds if v.weight > 0 and (v.vote > 0) == (score > 0)]
    voting = [v for v in preds if v.weight > 0 and abs(v.vote) > 1e-9]
    agreement = (len(agreeing) / len(voting)) if voting else 0.0

    if abs(score) < min_score:
        return Verdict("AVOID", None, round(score, 3), round(agreement, 2),
                       [v.as_dict() for v in views], [],
                       f"committee score {score:+.2f} below the {min_score:.2f} threshold")
    return Verdict("TAKE", side, round(score, 3), round(agreement, 2),
                   [v.as_dict() for v in views], [],
                   f"committee {score:+.2f} for {'LONG' if side == 'call' else 'SHORT'}")
