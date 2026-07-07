"""Journal replay: score a TradingConfig against recorded episodes.

The 24/7 daemon journals every detection at exploration thresholds
(RECORDING_FLOOR_*). Replay collapses detections into episodes and asks:
"which episodes would THIS config have captured, and what would it have
earned?" — entirely offline, zero tokens, real market data.

Anti-reward-hacking measures baked into the fitness:

* re-clips of a persisting episode decay geometrically (0.5^k): paper
  re-fills don't consume depth, live ones do — unverified replenishment
  must not be rewarded linearly
* capital-lockup penalty: long-YES baskets hold to resolution; each
  captured dollar of cost is charged an opportunity cost so the
  optimizer can't hoard slow capital for free
* warned episodes (augmented events, crossed books) are never captured
* episodes below the recording floor cannot exist in the journal, and
  genome bounds forbid configs looser than the floor, so the evaluable
  set is honest by construction
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from polyarb.ledger import Ledger
from polyarb.tuning import TradingConfig

#: geometric decay applied to the k-th re-clip of one episode
RECLIP_DECAY = 0.5
#: daily opportunity cost charged on capital locked to resolution
LOCKUP_DAILY_RATE = 0.0005  # 0.05%/day ~ 18%/yr hurdle
#: assumed lock days for hold-to-resolution baskets (conservative)
ASSUMED_LOCK_DAYS = {"negrisk_long_yes": 60.0, "negrisk_long_no": 60.0}


@dataclass
class EpisodeView:
    """The slice of a journal episode that replay scoring needs."""

    event_id: str
    kind: str
    title: str
    day: str  # UTC date of first detection
    start: float
    duration_s: float
    max_profit: float
    max_roi: float
    max_size: float
    cost_at_max: float
    edge_per_share: float
    n_legs: int
    warned: bool


def load_episodes(data_dirs: list[str], gap_s: float = 60.0) -> list[EpisodeView]:
    records: list[dict] = []
    for d in data_dirs:
        path = os.path.join(d, "opportunities.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    views: list[EpisodeView] = []
    for ep in Ledger.episodes(records, gap_s=gap_s):
        # recover per-share edge and leg count from the best record
        best = None
        for r in records:
            if (
                r.get("event_id") == ep["event_id"]
                and r.get("kind") == ep["kind"]
                and abs(r.get("profit", -1) - ep["max_profit"]) < 1e-9
            ):
                best = r
                break
        if best is None:
            continue
        views.append(
            EpisodeView(
                event_id=ep["event_id"],
                kind=ep["kind"],
                title=ep["title"],
                day=time.strftime("%Y-%m-%d", time.gmtime(ep["start"])),
                start=ep["start"],
                duration_s=ep["duration_s"],
                max_profit=ep["max_profit"],
                max_roi=ep["max_roi"],
                max_size=ep["max_size"],
                cost_at_max=ep["cost_at_max"],
                edge_per_share=best.get("edge_per_share", 0.0),
                n_legs=len(best.get("legs", [])),
                warned=bool(best.get("warnings")),
            )
        )
    return views


@dataclass
class FitnessReport:
    fitness: float = 0.0
    captured_profit: float = 0.0
    lockup_penalty: float = 0.0
    n_captured: int = 0
    n_episodes: int = 0
    capital_used: float = 0.0
    by_day: dict = field(default_factory=dict)


def evaluate(cfg: TradingConfig, episodes: list[EpisodeView]) -> FitnessReport:
    rep = FitnessReport(n_episodes=len(episodes))
    hurdle = cfg.min_edge_per_share + cfg.safety_margin_per_share
    for ep in episodes:
        if ep.warned or ep.n_legs > cfg.max_legs:
            continue
        if ep.edge_per_share < hurdle:
            continue
        # scale the clip to this config's notional caps
        cap = min(cfg.max_notional_per_arb, cfg.max_notional_per_trade)
        if ep.cost_at_max <= 0:
            continue
        scale = min(1.0, cap / ep.cost_at_max)
        clip_profit = ep.max_profit * scale
        clip_cost = ep.cost_at_max * scale
        if clip_profit < cfg.min_profit_usd:
            continue
        # re-clips within the episode, cooldown-limited, geometrically
        # decayed for unverified depth replenishment
        n_clips = 1 + int(ep.duration_s // max(cfg.event_cooldown_s, 60.0))
        total = sum(
            clip_profit * (RECLIP_DECAY**k) for k in range(min(n_clips, 8))
        )
        lock_days = ASSUMED_LOCK_DAYS.get(ep.kind, 1.0)
        lockup = clip_cost * LOCKUP_DAILY_RATE * lock_days
        rep.captured_profit += total
        rep.lockup_penalty += lockup
        rep.n_captured += 1
        rep.capital_used += clip_cost
        day = rep.by_day.setdefault(ep.day, {"profit": 0.0, "n": 0})
        day["profit"] += total - lockup
        day["n"] += 1
    rep.fitness = rep.captured_profit - rep.lockup_penalty
    return rep


def walk_forward(
    cfg: TradingConfig, episodes: list[EpisodeView]
) -> tuple[FitnessReport, FitnessReport, list[str]]:
    """Split by UTC day: train = all but last day, holdout = last day.

    Returns (train_report, holdout_report, warnings). With <2 distinct
    days, holdout is empty and a warning is emitted — the caller must
    treat the result as insufficient for auto-anything.
    """
    days = sorted({e.day for e in episodes})
    warnings = []
    if len(days) < 2:
        warnings.append(
            f"only {len(days)} distinct day(s) of journal data — "
            "walk-forward holdout impossible; propose-only"
        )
        return evaluate(cfg, episodes), FitnessReport(), warnings
    holdout_day = days[-1]
    train = [e for e in episodes if e.day != holdout_day]
    hold = [e for e in episodes if e.day == holdout_day]
    return evaluate(cfg, train), evaluate(cfg, hold), warnings
