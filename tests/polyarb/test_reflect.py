"""Tests for the Hermes reflective pass — zero-trust handling of LLM output."""

import json
import time
import types

import pytest

from evolution.trading.reflect import (
    build_packet,
    call_llm,
    run_reflection,
    sanitize_suggestion,
)
from evolution.trading.signals import classify, compute_drift
from polyarb.tuning import TradingConfig, save_config

BASE = TradingConfig()


def fake_client(payload: dict):
    """Minimal OpenAI-shaped stub returning `payload` as message content."""
    msg = types.SimpleNamespace(content=json.dumps(payload))
    choice = types.SimpleNamespace(message=msg)
    resp = types.SimpleNamespace(choices=[choice])
    completions = types.SimpleNamespace(create=lambda **kw: resp)
    chat = types.SimpleNamespace(completions=completions)
    return types.SimpleNamespace(chat=chat)


def write_journal(tmp_path, days=("2026-07-05", "2026-07-06", "2026-07-07")):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    recs = []
    for d, day in enumerate(days):
        ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + 3600
        for i in range(3):
            recs.append({
                "kind": "negrisk_long_yes", "size": 50.0, "gross_cost": 50.0,
                "payout": 52.0, "fees": 0.0, "profit": 2.0, "roi": 0.04,
                "edge_per_share": 0.04, "event_title": f"Will X strike {i}?",
                "event_id": f"e{i}-{d}", "detected_at": ts + i * 500,
                "warnings": [], "legs": [{"token_id": "x"}] * 5,
            })
    with open(data / "opportunities.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    return str(data)


class TestSignals:
    def test_classify(self):
        assert classify("Will Israel strike 4 countries?") == "geopolitics"
        assert classify("Highest temperature in Ankara") == "weather"
        assert classify("Portugal vs. Spain") == "sports"
        assert classify("Something else entirely") == "other"

    def test_drift_computes_days(self, tmp_path):
        data = write_journal(tmp_path)
        rep = compute_drift([data])
        assert len(rep.days) == 3
        assert all(d.detections == 3 for d in rep.days)
        assert rep.days[0].category_mix.get("geopolitics") == 3

    def test_collapse_flag(self, tmp_path):
        data = write_journal(tmp_path)
        # add a fourth day with almost nothing
        ts = time.mktime(time.strptime("2026-07-08", "%Y-%m-%d"))
        with open(tmp_path / "data" / "opportunities.jsonl", "a") as f:
            f.write(json.dumps({
                "kind": "negrisk_long_yes", "size": 5, "gross_cost": 5,
                "payout": 5.05, "fees": 0, "profit": 0.05, "roi": 0.01,
                "edge_per_share": 0.01, "event_title": "x", "event_id": "solo",
                "detected_at": ts, "warnings": [], "legs": [{}],
            }) + "\n")
        rep = compute_drift([data])
        assert any("collapsed" in fl for fl in rep.flags)


class TestSanitizer:
    def test_valid_suggestion_accepted(self):
        cand, notes = sanitize_suggestion(
            {"config_suggestion": {"genes": {"min_edge_per_share": 0.02}}}, BASE
        )
        assert cand is not None and cand.min_edge_per_share == 0.02

    def test_out_of_bounds_clamped(self):
        cand, notes = sanitize_suggestion(
            {"config_suggestion": {"genes": {"min_edge_per_share": 0.9}}}, BASE
        )
        assert cand is not None and cand.min_edge_per_share == 0.05
        assert any("clamped" in n for n in notes)

    def test_risk_ceiling_cannot_rise(self):
        cand, _ = sanitize_suggestion(
            {"config_suggestion": {"genes": {"max_notional_per_trade": 99999}}},
            BASE,
        )
        assert cand is not None
        assert cand.max_notional_per_trade <= BASE.max_notional_per_trade

    def test_unknown_and_junk_genes_dropped(self):
        cand, notes = sanitize_suggestion(
            {"config_suggestion": {"genes": {
                "execute_shell": "rm -rf /", "min_edge_per_share": "high",
            }}}, BASE,
        )
        assert cand is None
        assert any("unknown gene" in n for n in notes)

    def test_gene_count_capped(self):
        genes = {"min_edge_per_share": 0.02, "min_profit_usd": 0.3,
                 "prefilter_slack": 0.04, "max_legs": 10, "event_cooldown_s": 500}
        cand, _ = sanitize_suggestion({"config_suggestion": {"genes": genes}}, BASE)
        changed = sum(
            1 for k in genes if getattr(cand, k) != getattr(BASE, k)
        )
        assert changed <= 3

    def test_null_suggestion(self):
        cand, notes = sanitize_suggestion({"config_suggestion": None}, BASE)
        assert cand is None


class TestReflectionEndToEnd:
    def _args(self, tmp_path, data, **kw):
        import argparse

        baseline = tmp_path / "polyarb.json"
        save_config(BASE, str(baseline))
        d = dict(baseline=str(baseline), data_dir=[data],
                 proposals_dir=str(tmp_path / "proposals"), model="test-model",
                 min_improvement=0.01, min_interval_days=6.0,
                 force=True, dry_run=False)
        d.update(kw)
        return argparse.Namespace(**d)

    def test_dry_run_builds_packet_without_llm(self, tmp_path, capsys):
        data = write_journal(tmp_path)
        rc = run_reflection(self._args(tmp_path, data, dry_run=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "dry run" in out and "gene_bounds" in out

    def test_full_pass_writes_report_and_proposal(self, tmp_path, capsys):
        data = write_journal(tmp_path)
        client = fake_client({
            "summary": "Steady geopolitics flow.",
            "anomalies": [], "hypotheses": ["h1"],
            "config_suggestion": {
                "genes": {"min_edge_per_share": 0.03},
                "rationale": "edge distribution supports a higher hurdle",
            },
            "escalate_to_human": False, "escalation_reason": "",
        })
        rc = run_reflection(self._args(tmp_path, data), client=client)
        assert rc == 0
        assert list((tmp_path / "data").glob("reflection-*.md"))
        pdirs = [p for p in
                 (tmp_path / "proposals" / "polyarb-config").iterdir()
                 if p.is_dir()]
        assert pdirs
        decision = json.loads((pdirs[0] / "decision.json").read_text())
        assert decision["metadata"]["engine"] == "hermes-reflective"
        assert (pdirs[0] / "STATUS").read_text().strip() == "PENDING"
        # spend recorded
        assert (tmp_path / "data" / "llm_spend.jsonl").exists()

    def test_spend_guard_blocks_second_run(self, tmp_path, capsys):
        data = write_journal(tmp_path)
        client = fake_client({"summary": "s", "anomalies": [],
                              "hypotheses": [], "config_suggestion": None,
                              "escalate_to_human": False, "escalation_reason": ""})
        assert run_reflection(self._args(tmp_path, data), client=client) == 0
        # second run without --force is skipped by the spend guard
        rc = run_reflection(self._args(tmp_path, data, force=False), client=client)
        assert rc == 0
        assert len(list((tmp_path / "data").glob("reflection-*.md"))) == 1

    def test_malformed_llm_output_raises_cleanly(self, tmp_path):
        data = write_journal(tmp_path)
        client = fake_client({})  # returns "{}" -> parses, no suggestion
        rc = run_reflection(self._args(tmp_path, data), client=client)
        assert rc == 0  # report written, no proposal

    def test_no_json_from_llm(self):
        msg = types.SimpleNamespace(content="I refuse to answer in JSON.")
        choice = types.SimpleNamespace(message=msg)
        resp = types.SimpleNamespace(choices=[choice])
        completions = types.SimpleNamespace(create=lambda **kw: resp)
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions))
        with pytest.raises(ValueError):
            call_llm({"x": 1}, client=client, model="m")


class TestPacket:
    def test_packet_contains_bounds_and_trials(self, tmp_path):
        data = write_journal(tmp_path)
        packet = build_packet(BASE, [data], str(tmp_path / "proposals"))
        assert "min_edge_per_share" in packet["gene_bounds"]
        assert packet["trials_total"] == 0
        assert len(json.dumps(packet)) < 30000
