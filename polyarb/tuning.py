"""TradingConfig: the evolvable configuration file for the polyarb daemon.

This is the ONLY thing the self-evolution loop is allowed to change.
Execution code, wallet handling, and hard risk ceilings are not
evolvable. The daemon loads the config at startup and hot-reloads it at
each universe refresh when the file's mtime changes, so an approved
proposal takes effect without restarting (and without touching the
running process's code).

Ceilings vs genes: ``max_notional_per_arb``, ``max_notional_per_trade``
and ``max_daily_notional`` may be *lowered* by evolution but never
raised above the values in the baseline file — the human sets the
ceiling, the optimizer works inside it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields

from .detector import DetectorConfig
from .risk import RiskConfig

log = logging.getLogger(__name__)


@dataclass
class TradingConfig:
    # --- detector genes ---
    min_edge_per_share: float = 0.01
    safety_margin_per_share: float = 0.002
    min_profit_usd: float = 0.25
    prefilter_slack: float = 0.03
    max_legs: int = 30
    max_notional_per_arb: float = 250.0
    # --- risk genes (ceiling-bounded) ---
    max_notional_per_trade: float = 100.0
    max_daily_notional: float = 500.0
    event_cooldown_s: float = 900.0
    # --- scanner knobs ---
    max_events: int = 800
    min_liquidity: float = 0.0
    # --- non-gene metadata ---
    version: int = 1
    note: str = ""

    def to_detector_cfg(self) -> DetectorConfig:
        return DetectorConfig(
            min_edge_per_share=self.min_edge_per_share,
            safety_margin_per_share=self.safety_margin_per_share,
            min_profit_usd=self.min_profit_usd,
            prefilter_slack=self.prefilter_slack,
            max_legs=self.max_legs,
            max_notional_per_arb=self.max_notional_per_arb,
        )

    def to_risk_cfg(self) -> RiskConfig:
        return RiskConfig(
            max_notional_per_trade=self.max_notional_per_trade,
            max_daily_notional=self.max_daily_notional,
            event_cooldown_s=self.event_cooldown_s,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, d: dict) -> "TradingConfig":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        return cls(**d)


def load_config(path: str) -> TradingConfig:
    with open(path, encoding="utf-8") as f:
        return TradingConfig.from_dict(json.load(f))


def save_config(cfg: TradingConfig, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(cfg.to_json())
    os.replace(tmp, path)


class ConfigWatcher:
    """Hot-reload helper: reports a new TradingConfig when the file changes."""

    def __init__(self, path: str):
        self.path = path
        self._mtime = 0.0

    def poll(self) -> TradingConfig | None:
        """Return the new config if the file changed since last poll."""
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            return None
        if mtime <= self._mtime:
            return None
        try:
            cfg = load_config(self.path)
        except (OSError, ValueError, TypeError) as e:
            log.error("config reload failed (%s); keeping current config", e)
            self._mtime = mtime  # don't retry a broken file every poll
            return None
        self._mtime = mtime
        return cfg
