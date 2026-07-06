"""Risk management: hard caps that sit between detection and execution."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from .models import Opportunity

log = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    #: max capital committed to a single opportunity ($)
    max_notional_per_trade: float = 100.0
    #: max capital committed across all trades in a UTC day ($)
    max_daily_notional: float = 500.0
    #: max number of executed trades per UTC day
    max_daily_trades: int = 50
    #: seconds to wait before re-trading the same event
    event_cooldown_s: float = 30.0
    #: refuse baskets with warnings (augmented events, crossed books)
    refuse_warned: bool = True
    #: if this file exists, all trading halts (dead-man kill switch)
    kill_switch_file: str = "polyarb.KILL"


@dataclass
class RiskManager:
    cfg: RiskConfig = field(default_factory=RiskConfig)
    _day: str = ""
    _daily_notional: float = 0.0
    _daily_trades: int = 0
    _last_event_trade: dict[str, float] = field(default_factory=dict)

    def _roll_day(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._day:
            self._day = day
            self._daily_notional = 0.0
            self._daily_trades = 0

    def check(self, opp: Opportunity) -> tuple[bool, str]:
        """Return (allowed, reason-if-not)."""
        self._roll_day()
        if os.path.exists(self.cfg.kill_switch_file):
            return False, f"kill switch present: {self.cfg.kill_switch_file}"
        if opp.warnings and self.cfg.refuse_warned:
            return False, f"has warnings: {opp.warnings[0]}"
        if opp.gross_cost > self.cfg.max_notional_per_trade:
            return False, (
                f"notional ${opp.gross_cost:.2f} > per-trade cap "
                f"${self.cfg.max_notional_per_trade:.2f}"
            )
        if self._daily_notional + opp.gross_cost > self.cfg.max_daily_notional:
            return False, "daily notional cap reached"
        if self._daily_trades >= self.cfg.max_daily_trades:
            return False, "daily trade-count cap reached"
        last = self._last_event_trade.get(opp.event_id, 0.0)
        if time.time() - last < self.cfg.event_cooldown_s:
            return False, f"event cooldown ({self.cfg.event_cooldown_s:.0f}s)"
        return True, ""

    def clamp_size(self, opp: Opportunity) -> float:
        """Shares that fit inside the per-trade cap (0 if none)."""
        if opp.gross_cost <= self.cfg.max_notional_per_trade or opp.size <= 0:
            return opp.size
        per_share_cost = opp.gross_cost / opp.size
        return self.cfg.max_notional_per_trade / per_share_cost

    def record_execution(self, opp: Opportunity) -> None:
        self._roll_day()
        self._daily_notional += opp.gross_cost
        self._daily_trades += 1
        self._last_event_trade[opp.event_id] = time.time()
        log.info(
            "risk: day notional $%.2f/%d trades",
            self._daily_notional,
            self._daily_trades,
        )
