"""Gamma API client — market & event discovery.

Read-only, no auth. Base: https://gamma-api.polymarket.com

We discover via /events (not /markets) because negRisk arbitrage needs
the event-level grouping: one event holds the N mutually exclusive
binary markets whose YES prices should sum to ~1.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

import requests

from .models import MarketInfo, NegRiskEvent

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _parse_json_field(raw: Any) -> list:
    """Gamma encodes list fields (clobTokenIds, outcomes) as JSON strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def _f(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def parse_market(m: dict, event: dict | None = None) -> MarketInfo | None:
    """Parse one Gamma market dict; None if it isn't CLOB-tradable."""
    token_ids = _parse_json_field(m.get("clobTokenIds"))
    outcomes = [str(o).lower() for o in _parse_json_field(m.get("outcomes"))]
    outcome_prices = _parse_json_field(m.get("outcomePrices"))
    if len(token_ids) != 2:
        return None
    # Binary markets are ["Yes","No"]; align index 0 == YES across ALL
    # index-aligned fields (tokens AND prices), so yes_price_hint and the
    # complete_for_long_yes safety check read the YES price, not the NO price.
    if outcomes and outcomes[0] != "yes":
        token_ids = list(reversed(token_ids))
        outcome_prices = list(reversed(outcome_prices))
    ev = event or {}
    fee_schedule = m.get("feeSchedule") or {}
    if isinstance(fee_schedule, str):
        try:
            fee_schedule = json.loads(fee_schedule)
        except ValueError:
            fee_schedule = {}
    return MarketInfo(
        condition_id=m.get("conditionId", ""),
        question=m.get("question", ""),
        yes_token_id=str(token_ids[0]),
        no_token_id=str(token_ids[1]),
        slug=m.get("slug", ""),
        event_id=str(ev.get("id", "")),
        event_title=ev.get("title", ""),
        neg_risk=bool(m.get("negRisk", False)),
        neg_risk_market_id=m.get("negRiskMarketID", "") or "",
        neg_risk_other=bool(m.get("negRiskOther", False)),
        group_item_title=m.get("groupItemTitle", "") or "",
        best_bid=_f(m.get("bestBid"), None) if m.get("bestBid") is not None else None,
        best_ask=_f(m.get("bestAsk"), None) if m.get("bestAsk") is not None else None,
        spread=_f(m.get("spread"), None) if m.get("spread") is not None else None,
        tick_size=_f(m.get("orderPriceMinTickSize"), 0.001),
        min_order_size=_f(m.get("orderMinSize"), 5.0),
        fees_enabled=bool(m.get("feesEnabled", False)),
        fee_rate=_f(fee_schedule.get("rate")),
        fee_exponent=_f(fee_schedule.get("exponent"), 1.0) or 1.0,
        fee_taker_only=bool(fee_schedule.get("takerOnly", True)),
        liquidity=_f(m.get("liquidityNum") or m.get("liquidity")),
        volume_24h=_f(m.get("volume24hr")),
        end_date_iso=m.get("endDateIso", "") or "",
        accepting_orders=bool(m.get("acceptingOrders", False)),
        active=bool(m.get("active", False)),
        closed=bool(m.get("closed", True)),
        yes_price_hint=(
            _f(outcome_prices[0], None) if outcome_prices else None
        ),
    )


class GammaClient:
    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self.http = session or requests.Session()
        self.http.headers.setdefault("User-Agent", "polyarb/0.1")
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> Any:
        for attempt in range(4):
            try:
                r = self.http.get(
                    f"{GAMMA_BASE}{path}", params=params, timeout=self.timeout
                )
                if r.status_code == 429:
                    wait = 2.0 * (attempt + 1)
                    log.warning("gamma 429, backing off %.1fs", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                if attempt == 3:
                    raise
                log.warning("gamma %s failed (%s), retry %d", path, e, attempt + 1)
                time.sleep(1.5 * (attempt + 1))
        return []

    def iter_events(
        self,
        max_events: int = 500,
        min_liquidity: float = 0.0,
        page_size: int = 100,
        order: str = "volume24hr",
    ) -> Iterator[dict]:
        """Yield open events, highest-activity first."""
        seen = 0
        offset = 0
        while seen < max_events:
            batch = self._get(
                "/events",
                {
                    "closed": "false",
                    "active": "true",
                    "archived": "false",
                    "order": order,
                    "ascending": "false",
                    "limit": min(page_size, max_events - seen),
                    "offset": offset,
                    **(
                        {"liquidity_num_min": min_liquidity}
                        if min_liquidity > 0
                        else {}
                    ),
                },
            )
            if not batch:
                return
            for ev in batch:
                yield ev
                seen += 1
                if seen >= max_events:
                    return
            offset += len(batch)

    def fetch_universe(
        self, max_events: int = 500, min_liquidity: float = 0.0
    ) -> tuple[list[MarketInfo], list[NegRiskEvent]]:
        """Return (all tradable binary markets, negRisk event groupings)."""
        markets: list[MarketInfo] = []
        neg_events: list[NegRiskEvent] = []
        for ev in self.iter_events(max_events=max_events, min_liquidity=min_liquidity):
            ev_markets: list[MarketInfo] = []
            excluded_safe = True  # every excluded market resolved NO?
            for m in ev.get("markets", []) or []:
                mi = parse_market(m, ev) if m.get("enableOrderBook") else None
                if mi is not None and mi.tradable:
                    ev_markets.append(mi)
                    continue
                # market exists in the event but is not in the basket:
                # only safe for long-YES if it is closed AND resolved NO.
                hint = (mi.yes_price_hint if mi else None)
                if hint is None:
                    prices = _parse_json_field(m.get("outcomePrices"))
                    hint = _f(prices[0], None) if prices else None
                resolved_no = bool(m.get("closed")) and hint is not None and hint < 0.01
                if not resolved_no:
                    excluded_safe = False
            markets.extend(ev_markets)
            if bool(ev.get("negRisk")) and len(ev_markets) >= 2:
                neg_events.append(
                    NegRiskEvent(
                        event_id=str(ev.get("id", "")),
                        title=ev.get("title", ""),
                        neg_risk_market_id=ev.get("negRiskMarketID", "") or "",
                        markets=ev_markets,
                        augmented=bool(ev.get("negRiskAugmented", False)),
                        complete_for_long_yes=excluded_safe,
                    )
                )
        log.info(
            "universe: %d tradable markets, %d negRisk events", len(markets), len(neg_events)
        )
        return markets, neg_events
