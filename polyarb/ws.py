"""Low-latency market-data engine: CLOB WebSocket -> live local L2 books.

Verified live behavior of wss://ws-subscriptions-clob.polymarket.com/ws/market:

* on subscribe ({"assets_ids": [...], "type": "market"}) a full ``book``
  snapshot arrives per asset within ~100ms (bids/asks best-LAST, like REST)
* ``price_change`` events carry {asset_id, price, size, side, best_bid,
  best_ask} where ``size`` is the NEW TOTAL resting size at that price
  level (``"0"`` removes the level) and side BUY=bid / SELL=ask
* updates arrive for BOTH outcome tokens of a market even when only one
  is subscribed — the complement is mirrored into the YES book
  (NO bid at p == YES ask at 1-p, exactly; one matching engine)
* client must send literal "PING" periodically; server answers "PONG"
* known failure mode: silent freeze — connection stays open, no events.
  Mitigation: liveness watchdog + reconnect + fresh snapshot (deltas are
  never replayed), and books from an unhealthy connection are distrusted.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from .models import BookLevel, OrderBook

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
#: undocumented server cap is 500/connection and it fails SILENTLY above
#: that (no more snapshots, deltas keep flowing) — stay well below it
ASSETS_PER_CONNECTION = 300
PING_INTERVAL_S = 8.0
LIVENESS_TIMEOUT_S = 30.0


def _set_level(levels: list[BookLevel], price: float, size: float) -> list[BookLevel]:
    """Replace/insert/remove one price level; keep best-first ordering.

    ``levels`` are best-first: bids descending, asks ascending. The list
    is rebuilt (cheap: books are small and updates per level are rare).
    """
    out = [lv for lv in levels if abs(lv.price - price) > 1e-9]
    if size > 0:
        out.append(BookLevel(price=price, size=size))
    return out


class BookStore:
    """Thread-safe live books keyed by YES token id.

    Updates addressed to a NO twin are mirrored into the YES book.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._books: dict[str, OrderBook] = {}
        self._no_to_yes: dict[str, str] = {}
        #: token -> id of the connection currently serving it
        self._conn_of: dict[str, int] = {}
        #: connection id -> healthy flag
        self._conn_ok: dict[int, bool] = {}

    def register(self, yes_token: str, no_token: str, conn_id: int) -> None:
        with self._lock:
            self._no_to_yes[no_token] = yes_token
            self._conn_of[yes_token] = conn_id
            self._conn_ok.setdefault(conn_id, False)

    def set_conn_health(self, conn_id: int, ok: bool) -> None:
        with self._lock:
            self._conn_ok[conn_id] = ok

    def healthy(self, yes_token: str) -> bool:
        with self._lock:
            conn = self._conn_of.get(yes_token)
            return bool(conn is not None and self._conn_ok.get(conn))

    def resolve(self, token: str) -> tuple[str, bool]:
        """Map any outcome token to (yes_token, is_mirrored)."""
        with self._lock:
            yes = self._no_to_yes.get(token, token)
            return yes, yes != token

    def apply_snapshot(self, msg: dict) -> str | None:
        """Apply a full ``book`` event; returns the YES token updated."""
        asset = str(msg.get("asset_id", ""))
        with self._lock:
            yes = self._no_to_yes.get(asset, asset)
            mirrored = yes != asset

            def levels(side: list[dict]) -> list[BookLevel]:
                lv = [
                    BookLevel(price=float(x["price"]), size=float(x["size"]))
                    for x in side or []
                ]
                lv.reverse()  # API best-last -> best-first
                if mirrored:
                    lv = [BookLevel(round(1 - x.price, 6), x.size) for x in lv]
                return lv

            bids, asks = levels(msg.get("bids")), levels(msg.get("asks"))
            if mirrored:
                bids, asks = asks, bids
            self._books[yes] = OrderBook(
                token_id=yes,
                bids=bids,
                asks=asks,
                timestamp_ms=int(msg.get("timestamp") or time.time() * 1000),
                hash=msg.get("hash", ""),
            )
            return yes

    def apply_price_change(self, pc: dict) -> str | None:
        """Apply one entry of a ``price_change`` event; returns YES token."""
        asset = str(pc.get("asset_id", ""))
        with self._lock:
            yes = self._no_to_yes.get(asset, asset)
            book = self._books.get(yes)
            if book is None:
                return None  # no snapshot yet; ignore delta
            price = float(pc["price"])
            size = float(pc["size"])
            side = str(pc.get("side", "")).upper()
            if yes != asset:  # mirror NO -> YES
                price = round(1.0 - price, 6)
                side = "SELL" if side == "BUY" else "BUY"
            if side == "BUY":
                book.bids = sorted(
                    _set_level(book.bids, price, size),
                    key=lambda l: -l.price,
                )
            else:
                book.asks = sorted(
                    _set_level(book.asks, price, size),
                    key=lambda l: l.price,
                )
            book.timestamp_ms = int(time.time() * 1000)
            return yes

    def get_books(self, yes_tokens: list[str]) -> dict[str, OrderBook] | None:
        """Snapshot copies of the books, or None if any is missing/unhealthy."""
        with self._lock:
            out: dict[str, OrderBook] = {}
            for t in yes_tokens:
                b = self._books.get(t)
                if b is None or not self.healthy(t):
                    return None
                out[t] = OrderBook(
                    token_id=b.token_id,
                    bids=list(b.bids),
                    asks=list(b.asks),
                    timestamp_ms=b.timestamp_ms,
                    tick_size=b.tick_size,
                    min_order_size=b.min_order_size,
                    neg_risk=b.neg_risk,
                )
            return out


@dataclass
class WSFeed:
    """Runs N sharded websocket connections in a daemon thread.

    ``on_update(yes_token)`` is called from the WS thread for every book
    mutation — keep it O(1) (the scanner just marks the event dirty).
    """

    store: BookStore
    subscriptions: list[tuple[str, str]]  # (yes_token, no_token)
    on_update: callable = None
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    stats: dict = field(default_factory=lambda: {"events": 0, "reconnects": 0})

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="polyarb-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import asyncio

        asyncio.run(self._main())

    async def _main(self) -> None:
        import asyncio

        shards = [
            self.subscriptions[i : i + ASSETS_PER_CONNECTION]
            for i in range(0, len(self.subscriptions), ASSETS_PER_CONNECTION)
        ]
        tasks = [
            asyncio.create_task(self._connection(cid, shard))
            for cid, shard in enumerate(shards)
        ]
        while not self._stop.is_set():
            await asyncio.sleep(0.25)
        for t in tasks:
            t.cancel()

    async def _connection(self, conn_id: int, shard: list[tuple[str, str]]) -> None:
        import asyncio

        import websockets

        for yes, no in shard:
            self.store.register(yes, no, conn_id)
        tokens = [yes for yes, _ in shard]
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=None, max_size=2**24
                ) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    last_msg = time.time()
                    last_ping = 0.0
                    self.store.set_conn_health(conn_id, True)
                    backoff = 1.0
                    while not self._stop.is_set():
                        now = time.time()
                        if now - last_ping > PING_INTERVAL_S:
                            await ws.send("PING")
                            last_ping = now
                        if now - last_msg > LIVENESS_TIMEOUT_S:
                            raise ConnectionError("liveness timeout (silent freeze)")
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        last_msg = time.time()
                        if raw == "PONG":
                            continue
                        self._handle(raw)
            except Exception as e:  # noqa: BLE001 — reconnect on anything
                self.store.set_conn_health(conn_id, False)
                self.stats["reconnects"] += 1
                if not self._stop.is_set():
                    log.warning(
                        "ws conn %d dropped (%s); reconnecting in %.1fs",
                        conn_id, str(e)[:120], backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        items = msg if isinstance(msg, list) else [msg]
        touched: set[str] = set()
        for m in items:
            et = m.get("event_type")
            if et == "book":
                t = self.store.apply_snapshot(m)
                if t:
                    touched.add(t)
            elif et == "price_change":
                for pc in m.get("price_changes", []) or []:
                    t = self.store.apply_price_change(pc)
                    if t:
                        touched.add(t)
            elif et == "tick_size_change":
                pass  # tick handled at order time via market metadata
        self.stats["events"] += len(items)
        if self.on_update:
            for t in touched:
                self.on_update(t)
