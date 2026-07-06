"""Event-driven scanner: WebSocket books trigger detection in ~milliseconds.

Flow: Gamma universe -> subscribe every negRisk event's markets ->
maintain live local L2 books -> each book mutation marks its event dirty
-> main loop batch-detects dirty events from in-memory books (no REST in
the hot path). Detection now happens ~0.1s after a book change instead
of the 2-5s REST polling cycle; the remaining latency to *capture* an
opportunity is the order POST round-trip.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from dataclasses import dataclass, field

from .detector import DetectorConfig, detect_negrisk_event
from .gamma import GammaClient
from .ledger import Ledger
from .models import NegRiskEvent, Opportunity
from .risk import RiskManager
from .scanner import ScannerConfig
from .ws import BookStore, WSFeed

log = logging.getLogger(__name__)


@dataclass
class WSScanner:
    detector_cfg: DetectorConfig = field(default_factory=DetectorConfig)
    scanner_cfg: ScannerConfig = field(default_factory=ScannerConfig)
    ledger: Ledger = field(default_factory=Ledger)
    risk: RiskManager = field(default_factory=RiskManager)
    executor: object | None = None
    gamma: GammaClient = field(default_factory=GammaClient)
    #: safety cap on total subscribed tokens (~10 connections)
    max_tokens: int = 4000
    #: suppress duplicate ledger entries per (event, kind) within this window
    log_cooldown_s: float = 5.0

    def __post_init__(self) -> None:
        self._events_by_id: dict[str, NegRiskEvent] = {}
        self._event_of_token: dict[str, str] = {}
        self._dirty: set[str] = set()
        self._dirty_lock = threading.Lock()
        self._wake = threading.Event()
        self._last_logged: dict[tuple[str, str], float] = {}
        self._store = BookStore()
        self._feed: WSFeed | None = None
        # WS books only mutate on change; freshness = connection health,
        # which BookStore enforces. Disable the timestamp check.
        self._det_cfg = dataclasses.replace(
            self.detector_cfg, max_book_age_s=float("inf")
        )

    # ------------------------------------------------------------------
    def _build_universe(self) -> list[tuple[str, str]]:
        _, neg_events = self.gamma.fetch_universe(
            max_events=self.scanner_cfg.max_events,
            min_liquidity=self.scanner_cfg.min_liquidity,
        )
        events_by_id: dict[str, NegRiskEvent] = {}
        event_of_token: dict[str, str] = {}
        subs: list[tuple[str, str]] = []
        for ev in neg_events:
            markets = [m for m in ev.markets if m.tradable]
            if len(markets) < 2 or len(markets) > self.detector_cfg.max_legs:
                continue
            if len(subs) + len(markets) > self.max_tokens:
                log.warning("token cap %d reached; universe truncated", self.max_tokens)
                break
            events_by_id[ev.event_id] = ev
            for m in markets:
                event_of_token[m.yes_token_id] = ev.event_id
                subs.append((m.yes_token_id, m.no_token_id))
        self._events_by_id = events_by_id
        self._event_of_token = event_of_token
        log.info(
            "ws universe: %d negRisk events, %d tokens subscribed",
            len(events_by_id), len(subs),
        )
        return subs

    def _on_update(self, yes_token: str) -> None:
        ev_id = self._event_of_token.get(yes_token)
        if ev_id is None:
            return
        with self._dirty_lock:
            self._dirty.add(ev_id)
        self._wake.set()

    def _restart_feed(self) -> None:
        if self._feed is not None:
            self._feed.stop()
        subs = self._build_universe()
        self._store = BookStore()
        self._feed = WSFeed(
            store=self._store, subscriptions=subs, on_update=self._on_update
        )
        self._feed.start()

    # ------------------------------------------------------------------
    def _detect_event(self, ev_id: str) -> list[Opportunity]:
        ev = self._events_by_id.get(ev_id)
        if ev is None:
            return []
        tokens = [m.yes_token_id for m in ev.markets if m.tradable]
        books = self._store.get_books(tokens)
        if books is None:
            return []  # missing snapshot or unhealthy connection
        return detect_negrisk_event(ev, books, self._det_cfg)

    def _handle_opportunities(self, opps: list[Opportunity]) -> None:
        now = time.time()
        for opp in opps:
            key = (opp.event_id, opp.kind.value)
            if now - self._last_logged.get(key, 0.0) >= self.log_cooldown_s:
                self._last_logged[key] = now
                self.ledger.log_opportunity(opp)
                print(opp.describe())
            if self.executor is None:
                continue
            allowed, reason = self.risk.check(opp)
            if not allowed:
                log.debug("skip %s: %s", opp.event_title, reason)
                continue
            result = self.executor.execute(opp)
            self.ledger.log_execution(opp, result.to_dict())
            self.risk.record_execution(opp)
            log.warning(
                "EXECUTED [%s] %s profit=$%.4f success=%s",
                result.mode, opp.event_title, opp.profit, result.success,
            )

    def run(self, duration_s: float | None = None) -> None:
        self._restart_feed()
        t_end = time.time() + duration_s if duration_s else None
        next_refresh = time.time() + self.scanner_cfg.universe_refresh_s
        n_detections = 0
        n_batches = 0
        last_stats = time.time()
        try:
            while t_end is None or time.time() < t_end:
                if time.time() >= next_refresh:
                    log.info("refreshing universe + feed")
                    try:
                        self._restart_feed()
                    except Exception:  # noqa: BLE001
                        log.exception("universe refresh failed; keeping old feed")
                    next_refresh = time.time() + self.scanner_cfg.universe_refresh_s
                self._wake.wait(timeout=0.25)
                self._wake.clear()
                with self._dirty_lock:
                    dirty, self._dirty = self._dirty, set()
                if not dirty:
                    continue
                t0 = time.time()
                opps: list[Opportunity] = []
                for ev_id in dirty:
                    try:
                        opps.extend(self._detect_event(ev_id))
                    except Exception:  # noqa: BLE001
                        log.exception("detection failed for event %s", ev_id)
                self._handle_opportunities(opps)
                n_detections += len(opps)
                n_batches += 1
                if time.time() - last_stats > 60:
                    feed_stats = self._feed.stats if self._feed else {}
                    self.ledger.log_scan(
                        {
                            "mode": "ws",
                            "batches": n_batches,
                            "detections": n_detections,
                            "ws_events": feed_stats.get("events", 0),
                            "ws_reconnects": feed_stats.get("reconnects", 0),
                            "last_batch_ms": round((time.time() - t0) * 1000, 2),
                        }
                    )
                    log.info(
                        "ws stats: %d ws events, %d dirty batches, %d detections, "
                        "%d reconnects",
                        feed_stats.get("events", 0), n_batches, n_detections,
                        feed_stats.get("reconnects", 0),
                    )
                    last_stats = time.time()
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            if self._feed is not None:
                self._feed.stop()
