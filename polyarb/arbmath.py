"""Depth-aware, fee-aware arbitrage math.

Core primitive: walk a *basket* of order-book ladders level by level,
taking shares as long as each marginal share of the basket is profitable
against a fixed guaranteed payout.

Polymarket taker fee model (2026 schedule, fee-enabled markets only):

    fee_per_share = rate * (price * (1 - price)) ** exponent

verified live against Gamma ``feeSchedule`` objects, e.g. sports markets
carry ``{"rate": 0.03, "exponent": 1, "takerOnly": true}``; 5/15-minute
crypto markets use exponent 2 with a higher rate. The formula is
symmetric in price (p <-> 1-p), so YES and NO takers at complementary
prices pay the same fee and fee math survives book mirroring. Markets
with ``feesEnabled: false`` (e.g. geopolitics) pay nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import BookLevel

EPS = 1e-9


@dataclass(frozen=True)
class FeeParams:
    """Per-market taker fee schedule. ``NO_FEES`` for fee-free markets."""

    rate: float = 0.0
    exponent: float = 1.0


NO_FEES = FeeParams()


def taker_fee_per_share(price: float, fee: FeeParams) -> float:
    """Taker fee in USDC for one share filled at ``price``."""
    if fee.rate <= 0:
        return 0.0
    return fee.rate * (price * (1.0 - price)) ** fee.exponent


def mirror_ladder(levels: list[BookLevel]) -> list[BookLevel]:
    """Convert one side of a YES book into the complementary NO side.

    On Polymarket's CLOB the two outcome tokens of a binary market share
    one matching engine: a resting bid for YES at price p IS liquidity to
    buy NO at 1-p (the exchange mints/merges complete sets to cross
    complementary orders). So the NO ask ladder is the mirrored YES bid
    ladder, exactly — no approximation.
    """
    return [BookLevel(price=round(1.0 - lv.price, 6), size=lv.size) for lv in levels]


@dataclass
class LegFill:
    """What the walk consumed from one leg's ladder."""

    shares: float = 0.0
    cost: float = 0.0  # USDC (sum of price*size over consumed levels)
    fees: float = 0.0
    worst_price: float = 0.0  # deepest level touched -> use as limit price

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares > 0 else 0.0


@dataclass
class BasketFill:
    shares: float  # uniform shares per leg
    legs: list[LegFill] = field(default_factory=list)
    payout_per_share: float = 0.0

    @property
    def gross_cost(self) -> float:
        return sum(l.cost for l in self.legs)

    @property
    def fees(self) -> float:
        return sum(l.fees for l in self.legs)

    @property
    def payout(self) -> float:
        return self.payout_per_share * self.shares

    @property
    def profit(self) -> float:
        return self.payout - self.gross_cost - self.fees

    @property
    def edge_per_share(self) -> float:
        return self.profit / self.shares if self.shares > 0 else 0.0


def walk_buy_basket(
    ladders: list[list[BookLevel]],
    payout_per_share: float,
    fees: list[FeeParams] | None = None,
    min_edge_per_share: float = 0.0,
    max_shares: float = float("inf"),
    max_notional: float = float("inf"),
) -> BasketFill | None:
    """Size a buy-the-basket arb against ask ladders (best price first).

    Buying one share of *every* leg guarantees ``payout_per_share`` USDC.
    Takes shares while the marginal basket cost (incl. taker fees) stays
    below ``payout_per_share - min_edge_per_share``. Returns None if not
    even the first marginal share clears the hurdle, or a ladder is empty.
    """
    k = len(ladders)
    if k == 0 or any(not lad for lad in ladders):
        return None
    fees = fees or [NO_FEES] * k
    idx = [0] * k  # current level per leg
    used = [0.0] * k  # shares consumed at current level
    fills = [LegFill() for _ in range(k)]
    total_shares = 0.0
    notional = 0.0

    while total_shares < max_shares - EPS:
        # marginal cost of one basket share at current levels
        marginal = 0.0
        room = max_shares - total_shares
        for i in range(k):
            if idx[i] >= len(ladders[i]):
                return _finalize(fills, total_shares, payout_per_share)
            lv = ladders[i][idx[i]]
            marginal += lv.price + taker_fee_per_share(lv.price, fees[i])
            room = min(room, lv.size - used[i])
        if marginal > payout_per_share - min_edge_per_share - EPS:
            break  # zero-edge baskets are not arbs
        if notional + marginal * room > max_notional:
            room = min(room, (max_notional - notional) / marginal)
        if room <= EPS:
            break
        # consume `room` shares from every leg at its current level
        for i in range(k):
            lv = ladders[i][idx[i]]
            fills[i].shares += room
            fills[i].cost += lv.price * room
            fills[i].fees += taker_fee_per_share(lv.price, fees[i]) * room
            fills[i].worst_price = lv.price
            used[i] += room
            if used[i] >= lv.size - EPS:
                idx[i] += 1
                used[i] = 0.0
        total_shares += room
        notional += marginal * room

    return _finalize(fills, total_shares, payout_per_share)


@dataclass
class SellBasketFill:
    """Result of a mint-and-sell walk (binary_short arbs)."""

    shares: float  # complete sets minted and sold
    legs: list[LegFill] = field(default_factory=list)  # LegFill.cost = proceeds
    mint_cost_per_share: float = 1.0

    @property
    def proceeds(self) -> float:
        return sum(l.cost for l in self.legs)

    @property
    def fees(self) -> float:
        return sum(l.fees for l in self.legs)

    @property
    def mint_cost(self) -> float:
        return self.mint_cost_per_share * self.shares

    @property
    def profit(self) -> float:
        return self.proceeds - self.mint_cost - self.fees

    @property
    def edge_per_share(self) -> float:
        return self.profit / self.shares if self.shares > 0 else 0.0


def walk_sell_basket(
    bid_ladders: list[list[BookLevel]],
    cost_per_share: float = 1.0,
    fees: list[FeeParams] | None = None,
    min_edge_per_share: float = 0.0,
    max_shares: float = float("inf"),
    max_notional: float = float("inf"),
) -> SellBasketFill | None:
    """Size a mint-and-sell arb against bid ladders (best price first).

    Minting one complete set costs ``cost_per_share`` USDC (e.g. $1
    splits into YES+NO); selling one share of every leg into the bids
    yields the marginal proceeds. Takes shares while marginal proceeds
    net of taker fees exceed ``cost_per_share + min_edge_per_share``.
    ``max_notional`` caps the mint-side capital committed.
    """
    k = len(bid_ladders)
    if k == 0 or any(not lad for lad in bid_ladders):
        return None
    fees = fees or [NO_FEES] * k
    idx = [0] * k
    used = [0.0] * k
    fills = [LegFill() for _ in range(k)]
    total_shares = 0.0

    while total_shares < max_shares - EPS:
        marginal_net = 0.0
        room = max_shares - total_shares
        exhausted = False
        for i in range(k):
            if idx[i] >= len(bid_ladders[i]):
                exhausted = True
                break
            lv = bid_ladders[i][idx[i]]
            marginal_net += lv.price - taker_fee_per_share(lv.price, fees[i])
            room = min(room, lv.size - used[i])
        if exhausted:
            break
        if marginal_net < cost_per_share + min_edge_per_share + EPS:
            break  # zero-edge baskets are not arbs
        if (total_shares + room) * cost_per_share > max_notional:
            room = max_notional / cost_per_share - total_shares
        if room <= EPS:
            break
        for i in range(k):
            lv = bid_ladders[i][idx[i]]
            fills[i].shares += room
            fills[i].cost += lv.price * room  # proceeds
            fills[i].fees += taker_fee_per_share(lv.price, fees[i]) * room
            fills[i].worst_price = lv.price
            used[i] += room
            if used[i] >= lv.size - EPS:
                idx[i] += 1
                used[i] = 0.0
        total_shares += room

    if total_shares <= EPS:
        return None
    return SellBasketFill(
        shares=total_shares, legs=fills, mint_cost_per_share=cost_per_share
    )


def _finalize(
    fills: list[LegFill], shares: float, payout_per_share: float
) -> BasketFill | None:
    if shares <= EPS:
        return None
    return BasketFill(shares=shares, legs=fills, payout_per_share=payout_per_share)
