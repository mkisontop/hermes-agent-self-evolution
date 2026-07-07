"""Bounded parameter genome for TradingConfig evolution.

Design rules (see polyarb/AUTONOMY.md):

* every gene has hard bounds — mutation and crossover clamp into them
* risk genes (notional caps) are additionally ceiling-bounded by the
  BASELINE config: evolution may lower risk, never raise it
* detection genes may not drop below the journal's recording floor —
  a config stricter than the recorder is evaluable on the journal, a
  looser one is not (the journal simply lacks those detections)
"""

from __future__ import annotations

import random
from dataclasses import replace

from polyarb.tuning import TradingConfig

#: journal recording floor: the 24/7 daemon records at these exploration
#: thresholds; candidate configs must be at least this strict to be
#: honestly evaluable by replay.
RECORDING_FLOOR_EDGE = 0.002
RECORDING_FLOOR_PROFIT = 0.05

#: gene -> (lo, hi, is_int). Risk genes get hi = min(hi, baseline value).
GENE_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "min_edge_per_share": (RECORDING_FLOOR_EDGE, 0.05, False),
    "safety_margin_per_share": (0.0, 0.01, False),
    "min_profit_usd": (RECORDING_FLOOR_PROFIT, 2.0, False),
    "prefilter_slack": (0.01, 0.08, False),
    "max_legs": (4, 40, True),
    "max_notional_per_arb": (10.0, 250.0, False),
    "max_notional_per_trade": (10.0, 250.0, False),
    "max_daily_notional": (50.0, 2000.0, False),
    "event_cooldown_s": (300.0, 7200.0, False),
}

#: genes where the baseline value is a hard ceiling (risk never rises)
CEILING_GENES = {
    "max_notional_per_arb",
    "max_notional_per_trade",
    "max_daily_notional",
}


def bounds_for(baseline: TradingConfig) -> dict[str, tuple[float, float, bool]]:
    out = {}
    for gene, (lo, hi, is_int) in GENE_BOUNDS.items():
        if gene in CEILING_GENES:
            hi = min(hi, float(getattr(baseline, gene)))
            lo = min(lo, hi)
        out[gene] = (lo, hi, is_int)
    return out


def _clamp(value: float, lo: float, hi: float, is_int: bool):
    v = max(lo, min(hi, value))
    return int(round(v)) if is_int else v


def validate(cfg: TradingConfig, baseline: TradingConfig) -> list[str]:
    """Return list of bound violations (empty = valid)."""
    problems = []
    for gene, (lo, hi, is_int) in bounds_for(baseline).items():
        v = getattr(cfg, gene)
        if not (lo - 1e-12 <= float(v) <= hi + 1e-12):
            problems.append(f"{gene}={v} outside [{lo}, {hi}]")
    return problems


def random_genome(baseline: TradingConfig, rng: random.Random) -> TradingConfig:
    kwargs = {}
    for gene, (lo, hi, is_int) in bounds_for(baseline).items():
        kwargs[gene] = _clamp(rng.uniform(lo, hi), lo, hi, is_int)
    return replace(baseline, **kwargs)


def mutate(
    cfg: TradingConfig,
    baseline: TradingConfig,
    rng: random.Random,
    sigma: float = 0.15,
) -> TradingConfig:
    """Gaussian-perturb a random subset of genes, clamped into bounds."""
    b = bounds_for(baseline)
    genes = list(b)
    n = max(1, rng.randint(1, 3))
    kwargs = {}
    for gene in rng.sample(genes, n):
        lo, hi, is_int = b[gene]
        span = hi - lo
        if span <= 0:
            continue
        v = float(getattr(cfg, gene)) + rng.gauss(0.0, sigma * span)
        kwargs[gene] = _clamp(v, lo, hi, is_int)
    return replace(cfg, **kwargs) if kwargs else cfg
