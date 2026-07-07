"""Regression tests for the code-review revision (24 confirmed findings).

Each test names the finding it locks down so a future refactor can't
silently reintroduce the bug.
"""

import json

import pytest

from polyarb.execution import interpret_leg_response, quantize_leg
from polyarb.models import Side


class TestFillFabrication:
    """CRITICAL: unfilled FAK must never be recorded as filled."""

    def test_unmatched_with_success_true_is_not_filled(self):
        # the exact bug: success=true but the order was killed unfilled
        r = interpret_leg_response(
            {"success": True, "status": "unmatched", "takingAmount": "0"},
            Side.BUY, "t",
        )
        assert not r.ok and r.filled_size == 0.0

    def test_delayed_is_not_assumed_filled(self):
        r = interpret_leg_response(
            {"success": True, "status": "delayed", "orderID": "0xabc"},
            Side.BUY, "t",
        )
        assert not r.ok and r.filled_size == 0.0 and r.order_id == "0xabc"

    def test_matched_with_no_size_is_not_fabricated(self):
        # matched but size omitted -> we do NOT invent a full fill
        r = interpret_leg_response({"status": "matched"}, Side.BUY, "t")
        assert not r.ok and r.filled_size == 0.0

    def test_genuine_full_match_parses_size(self):
        r = interpret_leg_response(
            {"status": "matched", "takingAmount": "100"}, Side.BUY, "t"
        )
        assert r.ok and r.filled_size == pytest.approx(100)

    def test_sell_side_reads_making_amount(self):
        r = interpret_leg_response(
            {"status": "matched", "makingAmount": "50"}, Side.SELL, "t"
        )
        assert r.ok and r.filled_size == pytest.approx(50)

    def test_empty_response(self):
        assert not interpret_leg_response({}, Side.BUY, "t").ok


class TestQuantize:
    """Success test must compare to the quantized (floored) size."""

    def test_four_decimal_price_multiple_of_10000_amounts(self):
        # both makerAmount and takerAmount at 1e6 must be multiples of 10000
        price, size = quantize_leg(0.1234, 57.0, 0.0001)
        maker = round(price * size * 1e6)
        taker = round(size * 1e6)
        assert maker % 10000 == 0 and taker % 10000 == 0

    def test_three_decimal_price(self):
        price, size = quantize_leg(0.129, 57.3, 0.001)
        assert round(price * size * 1e6) % 10000 == 0
        assert round(size * 1e6) % 10000 == 0


class TestExecuteUsesQuantizedSizeAndPerLegTick:
    def test_full_fill_against_quantized_size(self):
        # a 0.001-tick leg: opp.size 57.3 floors to 50 shares; a fill of
        # 50 must count as FULL (comparing to raw 57.3 would false-fail)
        import types

        from polyarb.execution import LiveExecutor
        from polyarb.models import ArbKind, Leg, Opportunity

        ex = object.__new__(LiveExecutor)  # bypass __init__/live gate
        ex.kill_switch_file = "/tmp/does-not-exist-polyarb.KILL"
        leg = Leg("t", Side.BUY, 0.129, 57.3, tick_size=0.001)
        opp = Opportunity(
            kind=ArbKind.NEGRISK_LONG_YES, legs=[leg], size=57.3,
            gross_cost=7.4, payout=57.3, fees=0.0, edge_per_share=0.1,
            profit=1.0,
        )
        _, qsize = quantize_leg(0.129, 57.3, 0.001)
        # stub _post_leg to return a full fill of the quantized size
        ex._post_leg = lambda l, nr, tk=None: types.SimpleNamespace(
            ok=True, filled_size=qsize, token_id=l.token_id,
            order_id="x", error="",
        )
        res = ex.execute(opp)
        assert res.success  # quantized-size comparison, not raw opp.size


class TestClassifyPriority:
    def test_geopolitics_beats_sports_winner(self):
        from evolution.trading.signals import classify

        # "winner" alone would have mislabeled these as sports
        assert classify("Nobel Peace Prize Winner 2026") != "sports"
        assert classify("Presidential Election Winner 2028") == "politics"
        assert classify("Will Israel strike 4 countries in 2026?") == "geopolitics"

    def test_word_boundary(self):
        from evolution.trading.signals import classify

        assert classify("New warehouse opening?") != "geopolitics"  # 'war'

    def test_sports_matchup(self):
        from evolution.trading.signals import classify

        assert classify("Portugal vs. Spain") == "sports"


class TestGammaTokenReversal:
    def test_yes_price_hint_follows_reversed_tokens(self):
        from polyarb.gamma import parse_market

        # outcomes reversed: index 0 is "No"; prices index 0 is the No price
        m = {
            "conditionId": "0xc", "question": "q",
            "clobTokenIds": '["NO_TOKEN", "YES_TOKEN"]',
            "outcomes": '["No", "Yes"]',
            "outcomePrices": '["0.9", "0.1"]',  # No=0.9, Yes=0.1
            "enableOrderBook": True, "acceptingOrders": True,
            "active": True, "closed": False, "negRisk": True,
        }
        mi = parse_market(m)
        assert mi.yes_token_id == "YES_TOKEN"
        assert mi.yes_price_hint == pytest.approx(0.1)  # the YES price


class TestApplyTimeValidation:
    def test_apply_rejects_config_exceeding_current_ceiling(self, tmp_path):
        from evolution.trading.evolve_trading import _apply_config_text
        from polyarb.tuning import TradingConfig, save_config

        # live config already lowered the per-trade cap to 25
        live = tmp_path / "polyarb.json"
        save_config(TradingConfig(max_notional_per_trade=25.0), str(live))
        # a stale proposal tries to set it back to 200 (above the ceiling)
        stale = TradingConfig(max_notional_per_trade=200.0).to_json()
        with pytest.raises(ValueError):
            _apply_config_text(stale, str(live))


class TestJsonExtraction:
    def test_prose_with_braces_does_not_corrupt(self):
        from evolution.trading.reflect import _extract_json

        text = (
            'Here is my analysis (note: use {curly} braces carefully).\n'
            '{"summary": "ok", "escalate_to_human": false}\n'
            'Done {end}.'
        )
        obj = _extract_json(text)
        assert obj["summary"] == "ok"

    def test_fenced_block(self):
        from evolution.trading.reflect import _extract_json

        obj = _extract_json('```json\n{"a": 1}\n```')
        assert obj["a"] == 1

    def test_no_json_raises(self):
        from evolution.trading.reflect import _extract_json

        with pytest.raises(ValueError):
            _extract_json("no object here")


class TestGenomeExcludesUnscorableGenes:
    def test_prefilter_and_cooldown_not_evolvable(self):
        from evolution.trading.genome import GENE_BOUNDS

        assert "prefilter_slack" not in GENE_BOUNDS
        assert "event_cooldown_s" not in GENE_BOUNDS

    def test_random_genome_leaves_them_at_baseline(self):
        import random

        from evolution.trading.genome import random_genome
        from polyarb.tuning import TradingConfig

        base = TradingConfig(prefilter_slack=0.033, event_cooldown_s=777.0)
        g = random_genome(base, random.Random(0))
        assert g.prefilter_slack == 0.033 and g.event_cooldown_s == 777.0
