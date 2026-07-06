"""Tests for negRisk detection, prefiltering, risk caps, quantization."""

import time

import pytest

from polyarb.detector import (
    DetectorConfig,
    detect_negrisk_event,
    prefilter_negrisk,
)
from polyarb.execution import PaperExecutor, quantize_leg
from polyarb.models import (
    ArbKind,
    BookLevel,
    MarketInfo,
    NegRiskEvent,
    OrderBook,
)
from polyarb.risk import RiskConfig, RiskManager


def mk_market(i: int, best_bid=None, best_ask=None, **kw) -> MarketInfo:
    return MarketInfo(
        condition_id=f"0xc{i}",
        question=f"Will outcome {i} win?",
        yes_token_id=f"yes{i}",
        no_token_id=f"no{i}",
        neg_risk=True,
        best_bid=best_bid,
        best_ask=best_ask,
        accepting_orders=True,
        active=True,
        closed=False,
        **kw,
    )


def mk_book(token_id: str, bids, asks) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[BookLevel(*b) for b in bids],
        asks=[BookLevel(*a) for a in asks],
        timestamp_ms=int(time.time() * 1000),
    )


def cfg(**kw) -> DetectorConfig:
    defaults = dict(
        min_edge_per_share=0.005,
        min_profit_usd=0.01,
        safety_margin_per_share=0.0,
    )
    defaults.update(kw)
    return DetectorConfig(**defaults)


class TestNegRiskDetection:
    def test_long_yes_detected(self):
        # 3 outcomes, asks 0.30/0.30/0.35 = 0.95 -> 5c/share
        ev = NegRiskEvent(
            event_id="e1", title="Who wins?", neg_risk_market_id="m1",
            markets=[mk_market(i) for i in range(3)],
        )
        books = {
            "yes0": mk_book("yes0", [(0.28, 100)], [(0.30, 100)]),
            "yes1": mk_book("yes1", [(0.28, 100)], [(0.30, 100)]),
            "yes2": mk_book("yes2", [(0.33, 100)], [(0.35, 100)]),
        }
        opps = detect_negrisk_event(ev, books, cfg())
        kinds = {o.kind for o in opps}
        assert ArbKind.NEGRISK_LONG_YES in kinds
        opp = next(o for o in opps if o.kind == ArbKind.NEGRISK_LONG_YES)
        assert opp.size == pytest.approx(100)
        assert opp.profit == pytest.approx(5.0)
        assert [l.token_id for l in opp.legs] == ["yes0", "yes1", "yes2"]
        assert all(l.outcome == "YES" for l in opp.legs)

    def test_long_no_detected(self):
        # bids sum to 1.04 -> buying all NOs at (1-bid) costs 1.96 < N-1=2
        ev = NegRiskEvent(
            event_id="e1", title="Who wins?", neg_risk_market_id="m1",
            markets=[mk_market(i) for i in range(3)],
        )
        books = {
            "yes0": mk_book("yes0", [(0.40, 50)], [(0.42, 50)]),
            "yes1": mk_book("yes1", [(0.40, 50)], [(0.42, 50)]),
            "yes2": mk_book("yes2", [(0.24, 50)], [(0.26, 50)]),
        }
        opps = detect_negrisk_event(ev, books, cfg())
        opp = next(o for o in opps if o.kind == ArbKind.NEGRISK_LONG_NO)
        # cost/share = (1-.4)+(1-.4)+(1-.24) = 1.96, payout 2.0
        assert opp.size == pytest.approx(50)
        assert opp.profit == pytest.approx(50 * 0.04)
        assert [l.token_id for l in opp.legs] == ["no0", "no1", "no2"]
        assert all(l.outcome == "NO" for l in opp.legs)
        # NO limit prices are complements of YES bids
        assert opp.legs[0].price == pytest.approx(0.60)

    def test_no_arb_when_sums_are_fair(self):
        ev = NegRiskEvent(
            event_id="e1", title="fair", neg_risk_market_id="m1",
            markets=[mk_market(i) for i in range(2)],
        )
        books = {
            "yes0": mk_book("yes0", [(0.49, 100)], [(0.51, 100)]),
            "yes1": mk_book("yes1", [(0.48, 100)], [(0.50, 100)]),
        }
        assert detect_negrisk_event(ev, books, cfg()) == []

    def test_augmented_event_skips_long_yes(self):
        ev = NegRiskEvent(
            event_id="e1", title="augmented", neg_risk_market_id="m1",
            markets=[mk_market(i) for i in range(2)], augmented=True,
        )
        books = {
            "yes0": mk_book("yes0", [(0.40, 100)], [(0.45, 100)]),
            "yes1": mk_book("yes1", [(0.40, 100)], [(0.45, 100)]),
        }
        opps = detect_negrisk_event(ev, books, cfg())
        assert not any(o.kind == ArbKind.NEGRISK_LONG_YES for o in opps)
        opps = detect_negrisk_event(
            ev, books, cfg(allow_augmented_long_yes=True)
        )
        long_yes = [o for o in opps if o.kind == ArbKind.NEGRISK_LONG_YES]
        assert len(long_yes) == 1 and long_yes[0].warnings

    def test_stale_books_ignored(self):
        ev = NegRiskEvent(
            event_id="e1", title="stale", neg_risk_market_id="m1",
            markets=[mk_market(i) for i in range(2)],
        )
        old = mk_book("yes0", [(0.40, 100)], [(0.45, 100)])
        old.timestamp_ms = int((time.time() - 3600) * 1000)
        books = {
            "yes0": old,
            "yes1": mk_book("yes1", [(0.40, 100)], [(0.45, 100)]),
        }
        assert detect_negrisk_event(ev, books, cfg()) == []

    def test_min_order_size_respected(self):
        ev = NegRiskEvent(
            event_id="e1", title="tiny", neg_risk_market_id="m1",
            markets=[mk_market(i) for i in range(2)],
        )
        books = {  # only 3 shares of depth < min_order_size 5
            "yes0": mk_book("yes0", [(0.40, 3)], [(0.45, 3)]),
            "yes1": mk_book("yes1", [(0.40, 3)], [(0.45, 3)]),
        }
        assert detect_negrisk_event(ev, books, cfg()) == []


class TestPrefilter:
    def test_prefilter_passes_cheap_yes_sum(self):
        ev = NegRiskEvent(
            event_id="e1", title="t", neg_risk_market_id="m",
            markets=[
                mk_market(0, best_bid=0.28, best_ask=0.30),
                mk_market(1, best_bid=0.28, best_ask=0.30),
                mk_market(2, best_bid=0.33, best_ask=0.35),
            ],
        )
        res = prefilter_negrisk([ev], cfg(prefilter_slack=0.05))
        assert res.events == [ev]
        assert res.token_ids == ["yes0", "yes1", "yes2"]

    def test_prefilter_drops_expensive_event(self):
        ev = NegRiskEvent(
            event_id="e1", title="t", neg_risk_market_id="m",
            markets=[
                mk_market(0, best_bid=0.10, best_ask=0.60),
                mk_market(1, best_bid=0.10, best_ask=0.60),
            ],
        )
        res = prefilter_negrisk([ev], cfg(prefilter_slack=0.02))
        assert res.events == []

    def test_prefilter_drops_missing_quotes(self):
        ev = NegRiskEvent(
            event_id="e1", title="t", neg_risk_market_id="m",
            markets=[
                mk_market(0, best_bid=None, best_ask=None),
                mk_market(1, best_bid=0.4, best_ask=0.45),
            ],
        )
        assert prefilter_negrisk([ev], cfg()).events == []


class TestRisk:
    def _opp(self, cost=50.0, event_id="e1", warnings=None):
        from polyarb.models import Leg, Opportunity, Side

        return Opportunity(
            kind=ArbKind.NEGRISK_LONG_YES,
            legs=[Leg("t", Side.BUY, 0.5, 100)],
            size=100,
            gross_cost=cost,
            payout=cost + 5,
            fees=0.0,
            edge_per_share=0.05,
            profit=5.0,
            event_id=event_id,
            warnings=warnings or [],
        )

    def test_per_trade_cap(self):
        rm = RiskManager(RiskConfig(max_notional_per_trade=40))
        ok, reason = rm.check(self._opp(cost=50))
        assert not ok and "per-trade cap" in reason

    def test_daily_cap_and_cooldown(self):
        rm = RiskManager(RiskConfig(
            max_notional_per_trade=100, max_daily_notional=80,
            event_cooldown_s=9999,
        ))
        opp = self._opp(cost=50)
        ok, _ = rm.check(opp)
        assert ok
        rm.record_execution(opp)
        ok, reason = rm.check(self._opp(cost=50, event_id="e2"))
        assert not ok and "daily notional" in reason
        ok, reason = rm.check(self._opp(cost=10, event_id="e1"))
        assert not ok and "cooldown" in reason

    def test_warnings_refused(self):
        rm = RiskManager()
        ok, reason = rm.check(self._opp(warnings=["sketchy"]))
        assert not ok and "warnings" in reason

    def test_kill_switch(self, tmp_path):
        kill = tmp_path / "KILL"
        kill.write_text("stop")
        rm = RiskManager(RiskConfig(kill_switch_file=str(kill)))
        ok, reason = rm.check(self._opp())
        assert not ok and "kill switch" in reason


class TestQuantize:
    def test_two_decimal_price_integerish_size(self):
        price, size = quantize_leg(0.48, 100.0, 0.01)
        assert price == 0.48 and size == 100.0

    def test_three_decimal_price_forces_step_10(self):
        price, size = quantize_leg(0.129, 57.3, 0.001)
        assert price == 0.129
        assert size == 50.0  # floored to multiple of 10

    def test_size_never_rounds_up(self):
        _, size = quantize_leg(0.5, 9.999, 0.01)
        assert size <= 9.999


class TestPaperExecutor:
    def test_paper_execution_fills_all_legs(self):
        from polyarb.models import Leg, Opportunity, Side

        opp = Opportunity(
            kind=ArbKind.NEGRISK_LONG_YES,
            legs=[Leg("a", Side.BUY, 0.3, 10), Leg("b", Side.BUY, 0.6, 10)],
            size=10, gross_cost=9.0, payout=10.0, fees=0.0,
            edge_per_share=0.1, profit=1.0,
        )
        res = PaperExecutor().execute(opp)
        assert res.success and len(res.legs) == 2
        assert all(l.ok and l.filled_size == 10 for l in res.legs)
        assert res.to_dict()["mode"] == "paper"


class TestCompleteness:
    def test_incomplete_event_skips_long_yes_keeps_long_no(self):
        ev = NegRiskEvent(
            event_id="e1", title="partial", neg_risk_market_id="m1",
            markets=[mk_market(i) for i in range(2)],
            complete_for_long_yes=False,
        )
        books = {  # both directions would fire on a complete event
            "yes0": mk_book("yes0", [(0.55, 100)], [(0.40, 100)]),
            "yes1": mk_book("yes1", [(0.55, 100)], [(0.40, 100)]),
        }
        opps = detect_negrisk_event(ev, books, cfg())
        kinds = {o.kind for o in opps}
        assert ArbKind.NEGRISK_LONG_YES not in kinds
        assert ArbKind.NEGRISK_LONG_NO in kinds


class TestGammaParsing:
    def test_fetch_universe_marks_incomplete_events(self):
        from polyarb.gamma import GammaClient

        def raw_market(i, closed=False, accepting=True, yes_price="0.3"):
            return {
                "conditionId": f"0xc{i}",
                "question": f"q{i}",
                "clobTokenIds": f'["y{i}", "n{i}"]',
                "outcomes": '["Yes", "No"]',
                "outcomePrices": f'["{yes_price}", "0.7"]',
                "enableOrderBook": True,
                "acceptingOrders": accepting,
                "active": True,
                "closed": closed,
                "negRisk": True,
            }

        event_complete = {
            "id": "1", "title": "complete", "negRisk": True,
            "markets": [
                raw_market(0), raw_market(1),
                raw_market(2, closed=True, accepting=False, yes_price="0"),
            ],
        }
        event_incomplete = {
            "id": "2", "title": "paused leg", "negRisk": True,
            "markets": [
                raw_market(0), raw_market(1),
                raw_market(2, closed=False, accepting=False),
            ],
        }
        gc = GammaClient()
        gc.iter_events = lambda **kw: iter([event_complete, event_incomplete])
        markets, evs = gc.fetch_universe()
        assert len(evs) == 2
        by_title = {e.title: e for e in evs}
        assert by_title["complete"].complete_for_long_yes is True
        assert len(by_title["complete"].markets) == 2  # resolved leg excluded
        assert by_title["paused leg"].complete_for_long_yes is False


class TestDelayedPaperExecutor:
    def _store_with_book(self, asks):
        from polyarb.ws import BookStore

        st = BookStore()
        st.register("yesA", "noA", conn_id=0)
        st.set_conn_health(0, True)
        st.apply_snapshot({
            "asset_id": "yesA",
            "bids": [{"price": "0.30", "size": "50"}],
            "asks": [{"price": str(p), "size": str(s)} for p, s in reversed(asks)],
            "timestamp": "1783333115465",
        })
        return st

    def _opp(self, size=10.0, price=0.40):
        from polyarb.models import Leg, Opportunity, Side

        return Opportunity(
            kind=ArbKind.NEGRISK_LONG_YES,
            legs=[Leg("yesA", Side.BUY, price, size)],
            size=size, gross_cost=price * size, payout=size, fees=0.0,
            edge_per_share=1 - price, profit=(1 - price) * size,
        )

    def test_full_fill_when_depth_remains(self):
        from polyarb.execution import DelayedPaperExecutor

        st = self._store_with_book(asks=[(0.40, 20)])
        ex = DelayedPaperExecutor(lambda: st, delay_ms=0)
        res = ex.execute(self._opp(size=10, price=0.40))
        assert res.success and res.legs[0].filled_size == 10

    def test_partial_when_depth_gone(self):
        from polyarb.execution import DelayedPaperExecutor

        st = self._store_with_book(asks=[(0.40, 4)])
        ex = DelayedPaperExecutor(lambda: st, delay_ms=0)
        res = ex.execute(self._opp(size=10, price=0.40))
        assert not res.success
        assert res.legs[0].filled_size == pytest.approx(4)
        assert "filled=0.400" in res.note

    def test_price_moved_pays_up_while_profitable(self):
        # detected at 0.40 (payout 1.0/share); books moved to 0.45 —
        # still profitable, so a live executor pays up and fills
        from polyarb.execution import DelayedPaperExecutor

        st = self._store_with_book(asks=[(0.45, 100)])
        ex = DelayedPaperExecutor(lambda: st, delay_ms=0)
        res = ex.execute(self._opp(size=10, price=0.40))
        assert res.success
        assert "realized_profit=5.5000" in res.note  # 10*(1-0.45)

    def test_price_moved_beyond_profitability_no_fill(self):
        from polyarb.execution import DelayedPaperExecutor

        st = self._store_with_book(asks=[(1.01, 100)])
        ex = DelayedPaperExecutor(lambda: st, delay_ms=0)
        res = ex.execute(self._opp(size=10, price=0.40))
        assert not res.success and res.legs[0].filled_size == 0

    def test_no_leg_fills_on_mirrored_token(self):
        from polyarb.execution import DelayedPaperExecutor
        from polyarb.models import Leg, Opportunity, Side

        st = self._store_with_book(asks=[(0.40, 20)])
        # NO ask ladder = mirror of YES bids: 0.70 x 50
        opp = Opportunity(
            kind=ArbKind.NEGRISK_LONG_NO,
            legs=[Leg("noA", Side.BUY, 0.70, size=30)],
            size=30, gross_cost=21.0, payout=30.0, fees=0.0,
            edge_per_share=0.30, profit=9.0,
        )
        ex = DelayedPaperExecutor(lambda: st, delay_ms=0)
        res = ex.execute(opp)
        assert res.success
        assert res.legs[0].filled_size == pytest.approx(30)
