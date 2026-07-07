"""Arbitrage detection over market universe + order-book snapshots.

Where real arbs live on Polymarket
----------------------------------
A single binary market cannot arb against itself: the CLOB matches
complementary orders by minting/merging complete sets, so the NO book is
exactly the mirrored YES book — "buy YES + buy NO < $1" would require a
crossed book, which the matching engine removes. (We still check for it;
a hit means stale/crossed data, and it is reported with a warning.)

Persistent structural arbs live in **negRisk events**: N mutually
exclusive binary markets (election winner, World Cup winner, ...), each
with its own independent order book. Exactly one resolves YES, so:

* LONG_YES:  buy 1 share of every YES  -> pays $1;      arb if Σ ask_i < 1
* LONG_NO:   buy 1 share of every NO   -> pays $(N-1);  arb if Σ noask_i < N-1
             (noask_i = 1 - bid_i, so equivalently Σ bid_i > 1)

LONG_NO is robust to "augmented" events (outcomes added later: a new
winner makes *all* your NOs pay, payout N > N-1). LONG_YES on an
augmented event without an "Other" catch-all market can pay $0 if a
late-added outcome wins — skipped unless explicitly allowed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .arbmath import (
    NO_FEES,
    BasketFill,
    FeeParams,
    mirror_ladder,
    taker_fee_per_share,
    walk_buy_basket,
)
from .models import (
    ArbKind,
    Leg,
    MarketInfo,
    NegRiskEvent,
    Opportunity,
    OrderBook,
    Side,
)

log = logging.getLogger(__name__)


@dataclass
class DetectorConfig:
    #: required profit per basket-share AFTER fees (price units, $)
    min_edge_per_share: float = 0.01
    #: drop opportunities with absolute profit below this ($)
    min_profit_usd: float = 0.50
    #: cap on capital committed per opportunity ($)
    max_notional_per_arb: float = 250.0
    #: cap shares per leg
    max_shares: float = 5000.0
    #: books older than this are untrusted (seconds)
    max_book_age_s: float = 60.0
    #: legging risk cap — baskets with more legs than this are skipped
    max_legs: int = 30
    #: gamma-price prefilter slack before spending book requests
    prefilter_slack: float = 0.03
    #: take LONG_YES on augmented negRisk events lacking an Other market
    allow_augmented_long_yes: bool = False
    #: extra per-share safety haircut applied as added edge requirement
    #: (covers gas for merge/convert/redeem and adverse book drift)
    safety_margin_per_share: float = 0.002

    @property
    def hurdle(self) -> float:
        return self.min_edge_per_share + self.safety_margin_per_share


def taker_fees(m: MarketInfo) -> FeeParams:
    if not m.fees_enabled or m.fee_rate <= 0:
        return NO_FEES
    return FeeParams(rate=m.fee_rate, exponent=m.fee_exponent)


def _round_shares(shares: float, ndigits: int = 2) -> float:
    """Round DOWN so we never ask for more than the book showed.

    A tiny epsilon absorbs float noise (e.g. 4.9999999 that should be
    5.00) so a share count sitting exactly on min_order_size isn't
    truncated a full step below it and dropped.
    """
    factor = 10**ndigits
    return int(shares * factor + 1e-6) / factor


def _basket_to_opportunity(
    kind: ArbKind,
    fill: BasketFill,
    markets: list[MarketInfo],
    token_ids: list[str],
    outcomes: list[str],
    event: NegRiskEvent | None,
    cfg: DetectorConfig,
    warnings: list[str],
) -> Opportunity | None:
    min_size = max(m.min_order_size for m in markets)
    shares = _round_shares(min(fill.shares, cfg.max_shares))
    if shares < min_size:
        return None
    scale = shares / fill.shares
    gross = fill.gross_cost * scale
    fees = fill.fees * scale
    payout = fill.payout_per_share * shares
    profit = payout - gross - fees
    if profit < cfg.min_profit_usd:
        return None
    legs = [
        Leg(
            token_id=token_ids[i],
            side=Side.BUY,
            price=round(fill.legs[i].worst_price, 6),
            size=shares,
            market_question=markets[i].question,
            outcome=outcomes[i],
            condition_id=markets[i].condition_id,
            tick_size=markets[i].tick_size,
        )
        for i in range(len(markets))
    ]
    return Opportunity(
        kind=kind,
        legs=legs,
        size=shares,
        gross_cost=gross,
        payout=payout,
        fees=fees,
        edge_per_share=profit / shares,
        profit=profit,
        event_title=event.title if event else markets[0].question,
        event_id=event.event_id if event else markets[0].event_id,
        warnings=warnings,
    )


def _fresh_books(
    markets: list[MarketInfo],
    books: dict[str, OrderBook],
    cfg: DetectorConfig,
) -> list[OrderBook] | None:
    out = []
    for m in markets:
        b = books.get(m.yes_token_id)
        if b is None or b.age_seconds() > cfg.max_book_age_s:
            return None
        out.append(b)
    return out


def detect_negrisk_event(
    event: NegRiskEvent,
    books: dict[str, OrderBook],
    cfg: DetectorConfig,
) -> list[Opportunity]:
    """Check one negRisk event for LONG_YES / LONG_NO basket arbs."""
    opps: list[Opportunity] = []
    markets = [m for m in event.markets if m.tradable]
    n = len(markets)
    if n < 2 or n > cfg.max_legs:
        return opps
    ebooks = _fresh_books(markets, books, cfg)
    if ebooks is None:
        return opps
    fees = [taker_fees(m) for m in markets]
    yes_tokens = [m.yes_token_id for m in markets]
    no_tokens = [m.no_token_id for m in markets]

    # --- LONG_YES: buy every YES, guaranteed $1 ---
    # Guaranteed only if the eventual winner MUST be inside the basket.
    warnings: list[str] = []
    unsafe_long_yes = False
    if event.augmented and not event.has_other:
        unsafe_long_yes = True
        warnings.append(
            "augmented negRisk event without 'Other' outcome: a late-added "
            "winner pays $0 on a long-YES basket"
        )
    if not event.complete_for_long_yes:
        unsafe_long_yes = True
        warnings.append(
            "event has non-tradable, not-resolved-NO outcomes outside the "
            "basket: the winner may not be in the basket"
        )
    if not unsafe_long_yes or cfg.allow_augmented_long_yes:
        fill = walk_buy_basket(
            [b.asks for b in ebooks],
            payout_per_share=1.0,
            fees=fees,
            min_edge_per_share=cfg.hurdle,
            max_shares=cfg.max_shares,
            max_notional=cfg.max_notional_per_arb,
        )
        if fill:
            opp = _basket_to_opportunity(
                ArbKind.NEGRISK_LONG_YES,
                fill,
                markets,
                yes_tokens,
                ["YES"] * n,
                event,
                cfg,
                list(warnings),
            )
            if opp:
                opps.append(opp)

    # --- LONG_NO: buy every NO, guaranteed $(N-1) ---
    # NO ask ladder is the mirrored YES bid ladder (one matching engine).
    fill = walk_buy_basket(
        [mirror_ladder(b.bids) for b in ebooks],
        payout_per_share=float(n - 1),
        fees=fees,
        min_edge_per_share=cfg.hurdle,
        max_shares=cfg.max_shares,
        max_notional=cfg.max_notional_per_arb,
    )
    if fill:
        opp = _basket_to_opportunity(
            ArbKind.NEGRISK_LONG_NO,
            fill,
            markets,
            no_tokens,
            ["NO"] * n,
            event,
            cfg,
            [],
        )
        if opp:
            opps.append(opp)
    return opps


def detect_binary_crossed(
    market: MarketInfo, book: OrderBook, cfg: DetectorConfig
) -> Opportunity | None:
    """Crossed-book check on a single binary market (should ~never fire).

    ask_yes + ask_no < 1 with ask_no = 1 - bid_yes means ask < bid. A hit
    is stale/anomalous data more often than free money — always warned.
    """
    if book.age_seconds() > cfg.max_book_age_s:
        return None
    fill = walk_buy_basket(
        [book.asks, mirror_ladder(book.bids)],
        payout_per_share=1.0,
        fees=[taker_fees(market)] * 2,
        min_edge_per_share=cfg.hurdle,
        max_shares=cfg.max_shares,
        max_notional=cfg.max_notional_per_arb,
    )
    if not fill:
        return None
    return _basket_to_opportunity(
        ArbKind.BINARY_LONG,
        fill,
        [market, market],
        [market.yes_token_id, market.no_token_id],
        ["YES", "NO"],
        None,
        cfg,
        ["crossed book on a single binary market — likely stale data; "
         "verify before trusting"],
    )


def event_tightness(
    event: NegRiskEvent,
    books: dict[str, OrderBook],
    cfg: DetectorConfig,
) -> dict | None:
    """How close this event is to an arb right now (for the ledger).

    Recorded every cycle for prefiltered events so a paper run yields an
    edge *distribution* (how tight do baskets get, how often, in which
    events) rather than just a count of full triggers.
    """
    markets = [m for m in event.markets if m.tradable]
    n = len(markets)
    if n < 2:
        return None
    stats = {"asks": 0.0, "bids": 0.0, "fee_ask": 0.0, "fee_bid": 0.0,
             "depth_ask": float("inf"), "depth_bid": float("inf")}
    for m in markets:
        b = books.get(m.yes_token_id)
        if b is None or not b.asks or not b.bids:
            return None
        if b.age_seconds() > cfg.max_book_age_s:
            return None  # stale books must not seed the edge ledger with
            # tightness the detector itself would reject
        fee = taker_fees(m)
        a, bd = b.asks[0], b.bids[0]
        stats["asks"] += a.price
        stats["bids"] += bd.price
        stats["fee_ask"] += taker_fee_per_share(a.price, fee)
        stats["fee_bid"] += taker_fee_per_share(1.0 - bd.price, fee)
        stats["depth_ask"] = min(stats["depth_ask"], a.size)
        stats["depth_bid"] = min(stats["depth_bid"], bd.size)
    # net edge per share if we fired at best levels right now
    long_yes_edge = 1.0 - stats["asks"] - stats["fee_ask"]
    long_no_edge = (n - 1.0) - (n - stats["bids"]) - stats["fee_bid"]
    return {
        "event_id": event.event_id,
        "title": event.title[:60],
        "n": n,
        "sum_ask": round(stats["asks"], 4),
        "sum_bid": round(stats["bids"], 4),
        "long_yes_edge": round(long_yes_edge, 5),
        "long_no_edge": round(long_no_edge, 5),
        "top_depth_ask": round(stats["depth_ask"], 1),
        "top_depth_bid": round(stats["depth_bid"], 1),
        "augmented": event.augmented,
        "complete": event.complete_for_long_yes,
    }


# ---------------------------------------------------------------------------
# Prefilter: decide which events deserve book requests using the free
# indicative bestBid/bestAsk that Gamma already returned.
# ---------------------------------------------------------------------------


@dataclass
class PrefilterResult:
    events: list[NegRiskEvent] = field(default_factory=list)
    token_ids: list[str] = field(default_factory=list)


def prefilter_negrisk(
    events: list[NegRiskEvent], cfg: DetectorConfig
) -> PrefilterResult:
    res = PrefilterResult()
    for ev in events:
        markets = [m for m in ev.markets if m.tradable]
        n = len(markets)
        if n < 2 or n > cfg.max_legs:
            continue
        asks = [m.best_ask for m in markets]
        bids = [m.best_bid for m in markets]
        long_yes_possible = all(a is not None for a in asks) and (
            sum(asks) < 1.0 + cfg.prefilter_slack
        )
        long_no_possible = all(b is not None for b in bids) and (
            sum(bids) > 1.0 - cfg.prefilter_slack
        )
        if long_yes_possible or long_no_possible:
            res.events.append(ev)
            res.token_ids.extend(m.yes_token_id for m in markets)
    return res
