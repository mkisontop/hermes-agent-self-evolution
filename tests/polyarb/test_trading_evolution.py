"""Tests for evolution/trading: genome bounds, replay fitness, gates."""

import json
import random
import time

import pytest

from evolution.trading.genome import (
    CEILING_GENES,
    bounds_for,
    mutate,
    random_genome,
    validate,
)
from evolution.trading.replay import (
    RECLIP_DECAY,
    EpisodeView,
    evaluate,
    walk_forward,
)
from polyarb.tuning import ConfigWatcher, TradingConfig, load_config, save_config


def ep(profit=2.0, edge=0.03, cost=40.0, legs=5, duration=0.0,
       warned=False, day="2026-07-06", kind="negrisk_long_yes", eid="e1"):
    return EpisodeView(
        event_id=eid, kind=kind, title="t", day=day,
        start=time.mktime(time.strptime(day, "%Y-%m-%d")),
        duration_s=duration, max_profit=profit, max_roi=profit / cost,
        max_size=100, cost_at_max=cost, edge_per_share=edge,
        n_legs=legs, warned=warned,
    )


BASE = TradingConfig()


class TestGenome:
    def test_random_genomes_always_valid(self):
        rng = random.Random(1)
        for _ in range(200):
            assert validate(random_genome(BASE, rng), BASE) == []

    def test_mutation_clamps(self):
        rng = random.Random(2)
        g = random_genome(BASE, rng)
        for _ in range(200):
            g = mutate(g, BASE, rng, sigma=2.0)  # violent mutations
            assert validate(g, BASE) == []

    def test_risk_ceilings_from_baseline(self):
        low_base = TradingConfig(max_notional_per_trade=25.0)
        b = bounds_for(low_base)
        for gene in CEILING_GENES:
            lo, hi, _ = b[gene]
            assert hi <= float(getattr(low_base, gene)) + 1e-9
        rng = random.Random(3)
        for _ in range(100):
            g = random_genome(low_base, rng)
            assert g.max_notional_per_trade <= 25.0 + 1e-9

    def test_validate_catches_violations(self):
        from dataclasses import replace

        bad = replace(BASE, min_edge_per_share=0.5)
        assert validate(bad, BASE)


class TestReplayFitness:
    def test_captures_qualifying_episode(self):
        cfg = TradingConfig(min_edge_per_share=0.01, min_profit_usd=0.5)
        rep = evaluate(cfg, [ep(profit=2.0, edge=0.03)])
        assert rep.n_captured == 1
        assert rep.captured_profit == pytest.approx(2.0)
        assert rep.fitness < 2.0  # lockup penalty applied

    def test_edge_hurdle_excludes(self):
        cfg = TradingConfig(min_edge_per_share=0.04, safety_margin_per_share=0.0)
        assert evaluate(cfg, [ep(edge=0.03)]).n_captured == 0

    def test_warned_and_leggy_excluded(self):
        cfg = TradingConfig(min_edge_per_share=0.002)
        assert evaluate(cfg, [ep(warned=True)]).n_captured == 0
        assert evaluate(cfg, [ep(legs=50)]).n_captured == 0

    def test_notional_cap_scales_profit(self):
        cfg = TradingConfig(max_notional_per_trade=20.0, min_profit_usd=0.05,
                            min_edge_per_share=0.002)
        rep = evaluate(cfg, [ep(profit=2.0, cost=40.0)])
        assert rep.captured_profit == pytest.approx(1.0)  # half the clip

    def test_reclips_decay(self):
        cfg = TradingConfig(min_edge_per_share=0.002, min_profit_usd=0.05,
                            event_cooldown_s=600)
        one = evaluate(cfg, [ep(duration=0)]).captured_profit
        many = evaluate(cfg, [ep(duration=1800)]).captured_profit  # 4 clips
        expected = one * sum(RECLIP_DECAY**k for k in range(4))
        assert many == pytest.approx(expected)

    def test_walk_forward_split_and_insufficient_data(self):
        cfg = TradingConfig(min_edge_per_share=0.002, min_profit_usd=0.05)
        eps = [ep(day="2026-07-06", eid="a"), ep(day="2026-07-07", eid="b")]
        train, hold, warns = walk_forward(cfg, eps)
        assert not warns and train.n_captured == 1 and hold.n_captured == 1
        _, _, warns = walk_forward(cfg, [ep(day="2026-07-06")])
        assert warns  # single day -> insufficient


class TestEndToEnd:
    def test_evolution_writes_gated_proposal(self, tmp_path):
        from evolution.trading.evolve_trading import main

        # synthetic journal: two days of episodes at various edges
        data = tmp_path / "data"
        data.mkdir()
        recs = []
        for day, base_ts in (("06", 1783300000), ("07", 1783390000)):
            for i, edge in enumerate((0.005, 0.02, 0.04)):
                recs.append({
                    "kind": "negrisk_long_yes", "size": 50.0,
                    "gross_cost": 50.0, "payout": 50 + edge * 5000,
                    "fees": 0.0, "profit": edge * 50 * 2,
                    "roi": edge, "edge_per_share": edge,
                    "event_title": f"ev{i}", "event_id": f"ev{i}-{day}",
                    "detected_at": base_ts + i * 1000,
                    "warnings": [],
                    "legs": [{"token_id": "x"}] * 5,
                })
        with open(data / "opportunities.jsonl", "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        baseline_path = tmp_path / "polyarb.json"
        save_config(TradingConfig(), str(baseline_path))
        rc = main([
            "--baseline", str(baseline_path),
            "--data-dir", str(data),
            "--iterations", "60",
            "--proposals-dir", str(tmp_path / "proposals"),
        ])
        assert rc == 0
        pdirs = list((tmp_path / "proposals" / "polyarb-config").iterdir())
        pdir = [p for p in pdirs if p.is_dir()][0]
        assert (pdir / "STATUS").read_text().strip() == "PENDING"
        decision = json.loads((pdir / "decision.json").read_text())
        names = {c["name"] for c in
                 json.loads((pdir / "constraints.json").read_text())}
        assert {"bounds", "anti_lottery", "walk_forward_data",
                "holdout_non_regression"} <= names
        # trial registry accumulated
        assert (tmp_path / "proposals" / "polyarb-config" /
                "trials.jsonl").exists()
        # evolved config text is valid and within bounds
        evolved = TradingConfig.from_dict(
            json.loads((pdir / "evolved_skill.md").read_text()))
        assert validate(evolved, TradingConfig()) == []

    def test_apply_requires_approval(self, tmp_path):
        from evolution.trading.evolve_trading import main

        pdir = tmp_path / "prop"
        pdir.mkdir()
        (pdir / "STATUS").write_text("PENDING\n")
        (pdir / "evolved_skill.md").write_text(TradingConfig().to_json())
        baseline = tmp_path / "polyarb.json"
        save_config(TradingConfig(), str(baseline))
        rc = main(["--baseline", str(baseline), "--apply", str(pdir)])
        assert rc == 1  # refused
        (pdir / "STATUS").write_text("APPROVED\n")
        rc = main(["--baseline", str(baseline), "--apply", str(pdir)])
        assert rc == 0
        assert load_config(str(baseline)).version == 2  # bumped
        assert list(tmp_path.glob("polyarb.json.*.bak"))  # backup kept


class TestConfigHotReload:
    def test_watcher_detects_change(self, tmp_path):
        path = tmp_path / "cfg.json"
        save_config(TradingConfig(min_edge_per_share=0.01), str(path))
        w = ConfigWatcher(str(path))
        first = w.poll()
        assert first is not None and first.min_edge_per_share == 0.01
        assert w.poll() is None  # unchanged
        time.sleep(0.02)
        save_config(TradingConfig(min_edge_per_share=0.02), str(path))
        import os

        os.utime(path, (time.time() + 2, time.time() + 2))
        second = w.poll()
        assert second is not None and second.min_edge_per_share == 0.02

    def test_broken_file_keeps_running(self, tmp_path):
        path = tmp_path / "cfg.json"
        save_config(TradingConfig(), str(path))
        w = ConfigWatcher(str(path))
        assert w.poll() is not None
        path.write_text("{not json")
        import os

        os.utime(path, (time.time() + 2, time.time() + 2))
        assert w.poll() is None  # error swallowed, old config kept

    def test_unknown_keys_rejected(self):
        with pytest.raises(ValueError):
            TradingConfig.from_dict({"min_edge_per_share": 0.01, "evil": 1})
