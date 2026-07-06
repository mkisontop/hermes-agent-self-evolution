"""Scan orchestration: universe -> prefilter -> books -> detect -> execute."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .clob import ClobClient
from .detector import (
    DetectorConfig,
    detect_negrisk_event,
    event_tightness,
    prefilter_negrisk,
)
from .gamma import GammaClient
from .ledger import Ledger
from .models import NegRiskEvent, Opportunity
from .risk import RiskManager

log = logging.getLogger(__name__)


@dataclass
class ScannerConfig:
    max_events: int = 500
    min_liquidity: float = 0.0
    universe_refresh_s: float = 600.0
    interval_s: float = 5.0


@dataclass
class Scanner:
    detector_cfg: DetectorConfig = field(default_factory=DetectorConfig)
    scanner_cfg: ScannerConfig = field(default_factory=ScannerConfig)
    ledger: Ledger = field(default_factory=Ledger)
    risk: RiskManager = field(default_factory=RiskManager)
    executor: object | None = None  # PaperExecutor | LiveExecutor | None
    gamma: GammaClient = field(default_factory=GammaClient)
    clob: ClobClient = field(default_factory=ClobClient)
    _neg_events: list[NegRiskEvent] = field(default_factory=list)
    _universe_ts: float = 0.0

    def refresh_universe(self) -> None:
        _, self._neg_events = self.gamma.fetch_universe(
            max_events=self.scanner_cfg.max_events,
            min_liquidity=self.scanner_cfg.min_liquidity,
        )
        self._universe_ts = time.time()

    def scan_once(self) -> list[Opportunity]:
        """One detection cycle. Returns opportunities (already logged)."""
        if time.time() - self._universe_ts > self.scanner_cfg.universe_refresh_s:
            self.refresh_universe()
        pre = prefilter_negrisk(self._neg_events, self.detector_cfg)
        t0 = time.time()
        books = self.clob.get_books(pre.token_ids) if pre.token_ids else {}
        opportunities: list[Opportunity] = []
        for ev in pre.events:
            opportunities.extend(
                detect_negrisk_event(ev, books, self.detector_cfg)
            )
            tight = event_tightness(ev, books, self.detector_cfg)
            if tight is not None:
                self.ledger.log_tightness(tight)
        opportunities.sort(key=lambda o: o.profit, reverse=True)
        for opp in opportunities:
            self.ledger.log_opportunity(opp)
        self.ledger.log_scan(
            {
                "negrisk_events": len(self._neg_events),
                "prefiltered_events": len(pre.events),
                "books_fetched": len(books),
                "opportunities": len(opportunities),
                "cycle_ms": round((time.time() - t0) * 1000, 1),
            }
        )
        log.info(
            "scan: %d negRisk events, %d past prefilter, %d books, %d opps (%.0f ms)",
            len(self._neg_events),
            len(pre.events),
            len(books),
            len(opportunities),
            (time.time() - t0) * 1000,
        )
        return opportunities

    def maybe_execute(self, opportunities: list[Opportunity]) -> None:
        if self.executor is None:
            return
        for opp in opportunities:
            allowed, reason = self.risk.check(opp)
            if not allowed:
                log.info("skip %s: %s", opp.event_title, reason)
                continue
            result = self.executor.execute(opp)
            self.ledger.log_execution(opp, result.to_dict())
            self.risk.record_execution(opp)
            log.warning(
                "EXECUTED [%s] %s profit=$%.4f success=%s",
                result.mode,
                opp.event_title,
                opp.profit,
                result.success,
            )

    def run(self, duration_s: float | None = None) -> None:
        """Continuous loop until duration expires or Ctrl-C."""
        t_end = time.time() + duration_s if duration_s else None
        n_cycles = 0
        try:
            while t_end is None or time.time() < t_end:
                cycle_start = time.time()
                try:
                    opps = self.scan_once()
                    for opp in opps:
                        print(opp.describe())
                    self.maybe_execute(opps)
                except Exception:  # noqa: BLE001 — loop must survive API hiccups
                    log.exception("scan cycle failed; continuing")
                n_cycles += 1
                sleep = self.scanner_cfg.interval_s - (time.time() - cycle_start)
                if sleep > 0:
                    time.sleep(sleep)
        except KeyboardInterrupt:
            log.info("interrupted after %d cycles", n_cycles)
