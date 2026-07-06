"""Unit tests for polyarb.arbmath — the money math must be exact."""

import math

import pytest

from polyarb.arbmath import (
    NO_FEES,
    FeeParams,
    mirror_ladder,
    taker_fee_per_share,
    walk_buy_basket,
    walk_sell_basket,
)
from polyarb.models import BookLevel

SPORTS = FeeParams(rate=0.03)  # 2026 sports schedule
CRYPTO_15M = FeeParams(rate=0.25, exponent=2)


def L(*pairs):
    return [BookLevel(price=p, size=s) for p, s in pairs]


class TestFee:
    def test_no_fees(self):
        assert taker_fee_per_share(0.30, NO_FEES) == 0.0

    def test_symmetric_in_price(self):
        assert taker_fee_per_share(0.30, SPORTS) == pytest.approx(
            taker_fee_per_share(0.70, SPORTS)
        )

    def test_sports_docs_example(self):
        # docs: 100 sports shares @ $0.50 -> $0.75 fee (peak)
        assert 100 * taker_fee_per_share(0.50, SPORTS) == pytest.approx(0.75)

    def test_crypto_15m_exponent(self):
        # dynamic fee = 0.25 * (p(1-p))^2 per share
        assert taker_fee_per_share(0.50, CRYPTO_15M) == pytest.approx(
            0.25 * 0.0625
        )


class TestMirror:
    def test_mirror(self):
        m = mirror_ladder(L((0.128, 100), (0.127, 50)))
        assert m[0].price == pytest.approx(0.872)
        assert m[0].size == 100
        assert m[1].price == pytest.approx(0.873)


class TestBuyBasketBinary:
    def test_simple_binary_arb(self):
        # YES ask 0.48, NO ask 0.49 -> basket 0.97, payout 1.00
        res = walk_buy_basket([L((0.48, 100)), L((0.49, 100))], 1.0)
        assert res is not None
        assert res.shares == pytest.approx(100)
        assert res.gross_cost == pytest.approx(97.0)
        assert res.profit == pytest.approx(3.0)
        assert res.edge_per_share == pytest.approx(0.03)
        assert res.legs[0].worst_price == 0.48
        assert res.legs[1].worst_price == 0.49

    def test_no_arb_returns_none(self):
        res = walk_buy_basket([L((0.50, 100)), L((0.51, 100))], 1.0)
        assert res is None

    def test_exact_one_dollar_not_taken(self):
        res = walk_buy_basket([L((0.50, 100)), L((0.50, 100))], 1.0)
        assert res is None  # zero edge => not an arb

    def test_stops_at_unprofitable_depth(self):
        # first 50 shares at 0.48/0.49 profitable; next level 0.52 breaks it
        res = walk_buy_basket(
            [L((0.48, 50), (0.52, 500)), L((0.49, 200))], 1.0
        )
        assert res is not None
        assert res.shares == pytest.approx(50)
        assert res.profit == pytest.approx(50 * 0.03)

    def test_walks_into_deeper_levels_while_profitable(self):
        # level 2 of YES still profitable: 0.49+0.49=0.98
        res = walk_buy_basket(
            [L((0.48, 50), (0.49, 50)), L((0.49, 100))], 1.0
        )
        assert res is not None
        assert res.shares == pytest.approx(100)
        assert res.profit == pytest.approx(50 * 0.03 + 50 * 0.02)
        assert res.legs[0].worst_price == 0.49

    def test_min_edge_hurdle(self):
        res = walk_buy_basket(
            [L((0.48, 100)), L((0.49, 100))], 1.0, min_edge_per_share=0.05
        )
        assert res is None
        res = walk_buy_basket(
            [L((0.48, 100)), L((0.49, 100))], 1.0, min_edge_per_share=0.02
        )
        assert res is not None

    def test_max_shares_cap(self):
        res = walk_buy_basket([L((0.48, 100)), L((0.49, 100))], 1.0, max_shares=10)
        assert res.shares == pytest.approx(10)

    def test_max_notional_cap(self):
        res = walk_buy_basket(
            [L((0.48, 100)), L((0.49, 100))], 1.0, max_notional=9.7
        )
        assert res.shares == pytest.approx(10)
        assert res.gross_cost == pytest.approx(9.7)

    def test_fees_kill_marginal_arb(self):
        # 0.99 basket with 2 sports legs near 0.5: fee ~ 2*0.03*0.25 = 1.5c/share
        res = walk_buy_basket(
            [L((0.494, 100)), L((0.496, 100))], 1.0, fees=[SPORTS, SPORTS]
        )
        assert res is None

    def test_fees_reduce_but_keep_arb(self):
        res = walk_buy_basket(
            [L((0.48, 100)), L((0.49, 100))], 1.0, fees=[SPORTS, SPORTS]
        )
        assert res is not None
        expected = 100 * 0.03 * (0.48 * 0.52 + 0.49 * 0.51)
        assert res.fees == pytest.approx(expected)
        assert res.profit == pytest.approx(3.0 - expected)

    def test_empty_ladder(self):
        assert walk_buy_basket([L((0.48, 100)), []], 1.0) is None


class TestBuyBasketNegRisk:
    def test_negrisk_long_yes(self):
        # 4 outcomes, YES asks sum to 0.96 => 4c/share edge on payout $1
        ladders = [L((0.24, 100)), L((0.25, 100)), L((0.23, 100)), L((0.24, 100))]
        res = walk_buy_basket(ladders, 1.0)
        assert res is not None
        assert res.shares == pytest.approx(100)
        assert res.profit == pytest.approx(4.0)

    def test_negrisk_long_no(self):
        # 4 outcomes, NO asks 0.72/0.73/0.74/0.75 sum=2.94 < N-1=3
        ladders = [L((0.72, 50)), L((0.73, 50)), L((0.74, 50)), L((0.75, 50))]
        res = walk_buy_basket(ladders, payout_per_share=3.0)
        assert res is not None
        assert res.shares == pytest.approx(50)
        assert res.profit == pytest.approx(50 * 0.06)

    def test_size_limited_by_thinnest_leg(self):
        ladders = [L((0.24, 100)), L((0.25, 7)), L((0.23, 100)), L((0.24, 100))]
        res = walk_buy_basket(ladders, 1.0)
        assert res.shares == pytest.approx(7)


class TestSellBasket:
    def test_binary_short(self):
        # bids: YES 0.52, NO 0.51 -> mint $1, sell for 1.03
        res = walk_sell_basket([L((0.52, 100)), L((0.51, 100))], 1.0)
        assert res is not None
        assert res.shares == pytest.approx(100)
        assert res.proceeds == pytest.approx(103.0)
        assert res.profit == pytest.approx(3.0)

    def test_no_arb(self):
        assert walk_sell_basket([L((0.50, 100)), L((0.49, 100))], 1.0) is None

    def test_stops_when_bids_thin_out(self):
        res = walk_sell_basket(
            [L((0.52, 30), (0.48, 100)), L((0.51, 100))], 1.0
        )
        assert res.shares == pytest.approx(30)

    def test_fees(self):
        res = walk_sell_basket(
            [L((0.52, 100)), L((0.51, 100))], 1.0, fees=[SPORTS, SPORTS]
        )
        assert res is not None
        expected_fees = 100 * 0.03 * (0.52 * 0.48 + 0.51 * 0.49)
        assert res.fees == pytest.approx(expected_fees)
        assert res.profit == pytest.approx(3.0 - expected_fees)

    def test_max_notional_caps_mint_capital(self):
        res = walk_sell_basket(
            [L((0.52, 100)), L((0.51, 100))], 1.0, max_notional=25.0
        )
        assert res.shares == pytest.approx(25)
        assert res.mint_cost == pytest.approx(25.0)


class TestConsistency:
    def test_buy_profit_identity(self):
        ladders = [L((0.30, 40), (0.32, 60)), L((0.65, 100))]
        res = walk_buy_basket(ladders, 1.0)
        assert res is not None
        assert res.profit == pytest.approx(
            res.payout - res.gross_cost - res.fees
        )
        for leg in res.legs:
            assert leg.shares == pytest.approx(res.shares)
            assert not math.isnan(leg.avg_price)

    def test_mirrored_sell_equals_direct_buy(self):
        # selling YES+NO into bids == buying mirrored complements
        bids = [L((0.52, 100)), L((0.51, 100))]
        sell = walk_sell_basket(bids, 1.0)
        buy = walk_buy_basket([mirror_ladder(b) for b in bids], 1.0)
        assert sell is not None and buy is not None
        assert sell.profit == pytest.approx(buy.profit)
        assert sell.shares == pytest.approx(buy.shares)
