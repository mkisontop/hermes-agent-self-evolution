"""Data models for polyarb.

Prices are floats in [0, 1] (USDC per share). Sizes are share counts.
A share of an outcome token pays $1.00 if that outcome resolves true.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    """One outcome token's order book.

    ``bids``/``asks`` are stored best-first (CLOB API returns best-last;
    the client reverses them on ingestion).
    """

    token_id: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    timestamp_ms: int = 0
    tick_size: float = 0.001
    min_order_size: float = 5.0
    neg_risk: bool = False
    hash: str = ""

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    def age_seconds(self) -> float:
        if not self.timestamp_ms:
            return float("inf")
        return time.time() - self.timestamp_ms / 1000.0


@dataclass
class MarketInfo:
    """A single binary market (one condition, two outcome tokens)."""

    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    slug: str = ""
    event_id: str = ""
    event_title: str = ""
    neg_risk: bool = False
    neg_risk_market_id: str = ""
    neg_risk_other: bool = False  # this market is the catch-all "Other" outcome
    group_item_title: str = ""
    best_bid: Optional[float] = None  # YES side, from Gamma (indicative)
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    tick_size: float = 0.001
    min_order_size: float = 5.0
    fees_enabled: bool = False
    fee_rate: float = 0.0  # feeSchedule.rate, e.g. 0.03 for sports
    fee_exponent: float = 1.0  # feeSchedule.exponent (2 for 5/15-min crypto)
    fee_taker_only: bool = True
    liquidity: float = 0.0
    volume_24h: float = 0.0
    end_date_iso: str = ""
    accepting_orders: bool = True
    active: bool = True
    closed: bool = False
    #: indicative YES price from Gamma outcomePrices (resolution hint:
    #: ~0 for resolved-NO, ~1 for resolved-YES)
    yes_price_hint: Optional[float] = None

    @property
    def tradable(self) -> bool:
        return self.active and not self.closed and self.accepting_orders


@dataclass
class NegRiskEvent:
    """A negative-risk event: N mutually exclusive binary markets.

    Exactly one constituent market resolves YES (when the event is
    *complete*). ``augmented`` events can have new outcomes added later,
    which breaks the exactly-one-YES guarantee for long-YES baskets
    unless an "Other" catch-all market is part of the basket.
    """

    event_id: str
    title: str
    neg_risk_market_id: str
    markets: list[MarketInfo] = field(default_factory=list)
    augmented: bool = False
    #: True when every event market that is NOT in ``markets`` (the
    #: tradable basket) is closed and resolved NO — i.e. the winner is
    #: guaranteed to be inside the basket. Long-YES baskets require this;
    #: long-NO baskets are payout-robust to exclusions either way.
    complete_for_long_yes: bool = True

    @property
    def has_other(self) -> bool:
        return any(m.neg_risk_other for m in self.markets)

    @property
    def n_outcomes(self) -> int:
        return len(self.markets)


class ArbKind(str, Enum):
    #: buy YES + NO of one binary market for < $1, merge/hold to redeem $1
    BINARY_LONG = "binary_long"
    #: split $1 -> YES + NO, sell both for > $1 (needs on-chain split)
    BINARY_SHORT = "binary_short"
    #: buy 1 share of every YES in a negRisk event for < $1 total
    NEGRISK_LONG_YES = "negrisk_long_yes"
    #: buy 1 share of every NO in a negRisk event for < $(N-1) total
    NEGRISK_LONG_NO = "negrisk_long_no"


@dataclass
class Leg:
    """One order to place as part of an arb basket."""

    token_id: str
    side: Side
    price: float  # limit price (marginal worst acceptable)
    size: float  # shares
    market_question: str = ""
    outcome: str = ""  # "YES" | "NO"
    condition_id: str = ""

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class Opportunity:
    kind: ArbKind
    legs: list[Leg]
    size: float  # shares per leg (uniform across the basket)
    gross_cost: float  # USDC paid for the basket (at walked book prices)
    payout: float  # guaranteed USDC redeemed at resolution/merge/convert
    fees: float  # estimated taker fees in USDC
    edge_per_share: float  # (payout - cost - fees) / size
    profit: float  # payout - gross_cost - fees
    event_title: str = ""
    event_id: str = ""
    detected_at: float = field(default_factory=time.time)
    #: risk notes, e.g. augmented negRisk event without Other outcome
    warnings: list[str] = field(default_factory=list)

    @property
    def roi(self) -> float:
        return self.profit / self.gross_cost if self.gross_cost > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "size": round(self.size, 4),
            "gross_cost": round(self.gross_cost, 6),
            "payout": round(self.payout, 6),
            "fees": round(self.fees, 6),
            "profit": round(self.profit, 6),
            "roi": round(self.roi, 6),
            "edge_per_share": round(self.edge_per_share, 6),
            "event_title": self.event_title,
            "event_id": self.event_id,
            "detected_at": self.detected_at,
            "warnings": self.warnings,
            "legs": [
                {
                    "token_id": l.token_id,
                    "side": l.side.value,
                    "price": l.price,
                    "size": round(l.size, 4),
                    "question": l.market_question,
                    "outcome": l.outcome,
                    "condition_id": l.condition_id,
                }
                for l in self.legs
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    def describe(self) -> str:
        head = (
            f"[{self.kind.value}] {self.event_title or self.legs[0].market_question} | "
            f"size={self.size:.1f}sh cost=${self.gross_cost:.2f} payout=${self.payout:.2f} "
            f"fees=${self.fees:.4f} profit=${self.profit:.4f} roi={self.roi * 100:.2f}%"
        )
        lines = [head]
        for l in self.legs:
            lines.append(
                f"    {l.side.value} {l.size:.1f} x {l.outcome:<3} @ {l.price:.3f}  {l.market_question[:70]}"
            )
        if self.warnings:
            lines.append("    ⚠ " + "; ".join(self.warnings))
        return "\n".join(lines)
