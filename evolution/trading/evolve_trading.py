"""Nightly trading-config evolution: replay-search-gate-propose.

    python -m evolution.trading.evolve_trading \
        --baseline polyarb.json --data-dir /var/lib/polyarb/data \
        --iterations 400 --mode propose

Zero LLM tokens: candidates come from bounded random search + local
mutation of the elite, scored by offline journal replay with walk-forward
validation. The winner (if it beats baseline on BOTH train and holdout,
past the AutoMergeGate) is written as a proposal through the same
ProposalWriter humans already review for skill evolution. `--mode auto`
still refuses to raise risk ceilings (bounds make it impossible) but is
NOT recommended for trading configs; the default is propose.

Apply an approved proposal (writes config atomically, with .bak):

    python -m evolution.trading.evolve_trading \
        --apply proposals/polyarb-config/20260707_120000 --baseline polyarb.json
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import sys
import time
from pathlib import Path

from evolution.core.proposals import ProposalWriter, build_proposal_record
from evolution.core.regression_guard import AutoMergeGate

from polyarb.tuning import TradingConfig, load_config, save_config

from .genome import mutate, random_genome, validate
from .replay import evaluate, load_episodes, walk_forward

log = logging.getLogger("evolve_trading")


class _Constraint:
    def __init__(self, name: str, passed: bool, message: str):
        self.constraint_name = name
        self.passed = passed
        self.message = message


def search(
    baseline: TradingConfig,
    episodes,
    iterations: int,
    seed: int = 0,
    elite: int = 5,
    registry_path: str | None = None,
) -> tuple[TradingConfig, float]:
    """Random search + elite mutation, scored on the TRAIN split only.

    Every trial is appended to the registry (append-only, across nights)
    so the cumulative number of candidates ever evaluated on this data
    is known — the input any honest multiple-testing correction needs.
    """
    rng = random.Random(seed)
    days = sorted({e.day for e in episodes})
    train = (
        [e for e in episodes if e.day != days[-1]] if len(days) > 1 else episodes
    )
    pool: list[tuple[float, TradingConfig]] = [
        (evaluate(baseline, train).fitness, baseline)
    ]
    reg = open(registry_path, "a", encoding="utf-8") if registry_path else None
    try:
        for i in range(iterations):
            if i < iterations // 2 or len(pool) < 2:
                cand = random_genome(baseline, rng)
            else:
                _, parent = pool[rng.randrange(min(elite, len(pool)))]
                cand = mutate(parent, baseline, rng)
            if validate(cand, baseline):
                continue
            fit = evaluate(cand, train).fitness
            if reg:
                reg.write(json.dumps(
                    {"ts": time.time(), "fitness": round(fit, 4),
                     "genes": {k: getattr(cand, k) for k in
                               ("min_edge_per_share", "safety_margin_per_share",
                                "min_profit_usd", "max_legs",
                                "max_notional_per_arb", "max_notional_per_trade",
                                "max_daily_notional")}},
                    separators=(",", ":")) + "\n")
            pool.append((fit, cand))
            pool.sort(key=lambda t: -t[0])
            del pool[max(elite, 1):]
    finally:
        if reg:
            reg.close()
    return pool[0][1], pool[0][0]


def _anti_lottery_ok(
    baseline: TradingConfig, cand: TradingConfig, episodes, top_n: int = 3
) -> tuple[bool, str]:
    """Candidate must still beat baseline with its biggest wins removed.

    Guards against selecting configs whose fitness is a few fat, possibly
    unfillable episodes (lottery selection / phantom-opportunity bias).
    """
    ranked = sorted(episodes, key=lambda e: -e.max_profit)
    trimmed = ranked[top_n:]
    b = evaluate(baseline, trimmed).fitness
    c = evaluate(cand, trimmed).fitness
    return c >= b - 1e-9, (
        f"top-{top_n}-removed: candidate ${c:.2f} vs baseline ${b:.2f}"
    )


def run_evolution(args) -> int:
    baseline = load_config(args.baseline)
    episodes = load_episodes(args.data_dir)
    if not episodes:
        log.error("no episodes in journals %s — nothing to evolve against", args.data_dir)
        return 1
    log.info("loaded %d episodes across %d days",
             len(episodes), len({e.day for e in episodes}))

    registry = str(Path(args.proposals_dir) / "polyarb-config" / "trials.jsonl")
    Path(registry).parent.mkdir(parents=True, exist_ok=True)
    best, _ = search(
        baseline, episodes, args.iterations, seed=args.seed,
        registry_path=registry,
    )
    base_train, base_hold, wf_warnings = walk_forward(baseline, episodes)
    cand_train, cand_hold, _ = walk_forward(best, episodes)
    lottery_ok, lottery_msg = _anti_lottery_ok(baseline, best, episodes)

    constraints = [
        _Constraint("anti_lottery", lottery_ok, lottery_msg),
        _Constraint("bounds", not validate(best, baseline),
                    "; ".join(validate(best, baseline)) or "all genes in bounds"),
        _Constraint(
            "walk_forward_data",
            not wf_warnings,
            wf_warnings[0] if wf_warnings else
            f"{len({e.day for e in episodes})} days of journal data",
        ),
        _Constraint(
            "holdout_non_regression",
            bool(wf_warnings) or cand_hold.fitness >= base_hold.fitness - 1e-9,
            f"holdout: candidate ${cand_hold.fitness:.2f} vs baseline ${base_hold.fitness:.2f}",
        ),
    ]
    constraints_ok = all(c.passed for c in constraints)
    gate = AutoMergeGate(min_improvement=args.min_improvement)
    # Gate on OUT-OF-SAMPLE improvement (holdout), not the train fitness the
    # search maximized — in-sample deltas are inflated by selection. With
    # insufficient data (wf_warnings) there is no holdout, so we fall back
    # to train but the failing walk_forward_data constraint forces
    # constraints_ok=False, keeping auto_merge off (propose-only).
    if wf_warnings:
        decision = gate.evaluate(base_train.fitness, cand_train.fitness, constraints_ok)
    else:
        decision = gate.evaluate(base_hold.fitness, cand_hold.fitness, constraints_ok)

    record = build_proposal_record(
        skill_name="polyarb-config",
        baseline_text=baseline.to_json(),
        evolved_text=best.to_json(),
        baseline_score=base_train.fitness,
        evolved_score=cand_train.fitness,
        decision=decision,
        constraint_results=constraints,
        mode=args.mode,
        metadata={
            "engine": "trading-replay-search",
            "iterations": args.iterations,
            "episodes": len(episodes),
            "days": sorted({e.day for e in episodes}),
            "baseline": {"train": base_train.fitness,
                         "holdout": base_hold.fitness,
                         "captured": base_train.n_captured},
            "candidate": {"train": cand_train.fitness,
                          "holdout": cand_hold.fitness,
                          "captured": cand_train.n_captured,
                          "capital_used": round(cand_train.capital_used, 2)},
            "genes_changed": {
                k: [getattr(baseline, k), getattr(best, k)]
                for k in TradingConfig.__dataclass_fields__
                if getattr(baseline, k) != getattr(best, k)
                and k not in ("version", "note")
            },
        },
    )
    path = ProposalWriter(args.proposals_dir).write(record)
    print(f"proposal written: {path}")
    print(f"train:   baseline ${base_train.fitness:.2f} -> candidate ${cand_train.fitness:.2f}")
    print(f"holdout: baseline ${base_hold.fitness:.2f} -> candidate ${cand_hold.fitness:.2f}")
    print(f"gate: auto_merge={decision.auto_merge} ({decision.reason})")

    if args.mode == "auto" and decision.auto_merge:
        _apply_config_text(best.to_json(), args.baseline)
        print(f"AUTO-APPLIED to {args.baseline} (backup written)")
    return 0


def _apply_config_text(text: str, target: str) -> None:
    cfg = TradingConfig.from_dict(json.loads(text))  # validates keys
    # Re-check gene bounds at APPLY time against the CURRENT live config as
    # the ceiling — a proposal generated against an older baseline must not
    # be able to raise a risk cap that was since lowered, and any
    # hand-edited/corrupt proposal file is rejected here rather than trusted.
    if Path(target).exists():
        current = load_config(target)
        problems = validate(cfg, current)
        if problems:
            raise ValueError(
                f"refusing to apply: gene bounds violated vs live config: "
                f"{problems}"
            )
    cfg.version += 1
    ts = time.strftime("%Y%m%d_%H%M%S")
    target_p = Path(target)
    if target_p.exists():
        shutil.copy2(target_p, f"{target}.{ts}.bak")
    save_config(cfg, target)


def run_apply(args) -> int:
    pdir = Path(args.apply)
    status = (pdir / "STATUS").read_text().strip()
    if status != "APPROVED":
        log.error("proposal STATUS is %s, not APPROVED — refusing to apply", status)
        return 1
    evolved = (pdir / "evolved_skill.md").read_text()
    try:
        _apply_config_text(evolved, args.baseline)
    except ValueError as e:
        log.error("%s", e)
        return 1
    print(f"applied {pdir} -> {args.baseline} (running daemon hot-reloads "
          f"at next universe refresh)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="evolve_trading")
    p.add_argument("--baseline", required=True, help="live TradingConfig JSON path")
    p.add_argument("--data-dir", action="append", default=None,
                   help="journal dir(s) with opportunities.jsonl (repeatable)")
    p.add_argument("--iterations", type=int, default=400)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--mode", choices=("propose", "auto"), default="propose")
    p.add_argument("--min-improvement", type=float, default=0.5,
                   help="AutoMergeGate: required train-fitness gain ($)")
    p.add_argument("--proposals-dir", default="proposals")
    p.add_argument("--apply", default=None,
                   help="proposal dir to apply (requires STATUS=APPROVED)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.apply:
        return run_apply(args)
    if not args.data_dir:
        p.error("--data-dir is required (unless --apply)")
    return run_evolution(args)


if __name__ == "__main__":
    sys.exit(main())
