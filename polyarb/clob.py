"""CLOB REST client — order book snapshots (read-only, no auth).

Base: https://clob.polymarket.com
Rate limits (docs.polymarket.com/api-reference/rate-limits): /book /price
/midpoint ~1500 req/10s — batch POST /books keeps us far below that.

The API returns book sides sorted best-LAST; we normalize to best-first.
"""

from __future__ import annotations

import logging
import time

import requests

from .models import BookLevel, OrderBook

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
BOOKS_BATCH_SIZE = 100


def parse_book(raw: dict) -> OrderBook:
    def levels(side: list[dict]) -> list[BookLevel]:
        lv = [BookLevel(price=float(x["price"]), size=float(x["size"])) for x in side]
        return list(reversed(lv))  # API: best last -> we want best first

    return OrderBook(
        token_id=str(raw.get("asset_id", "")),
        bids=levels(raw.get("bids") or []),
        asks=levels(raw.get("asks") or []),
        timestamp_ms=int(raw.get("timestamp") or 0),
        tick_size=float(raw.get("tick_size") or 0.001),
        min_order_size=float(raw.get("min_order_size") or 5),
        neg_risk=bool(raw.get("neg_risk", False)),
        hash=raw.get("hash", ""),
    )


class ClobClient:
    """Unauthenticated market-data client (books, prices)."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self.http = session or requests.Session()
        self.http.headers.setdefault("User-Agent", "polyarb/0.1")
        self.timeout = timeout

    def _post(self, path: str, payload) -> list:
        for attempt in range(4):
            try:
                r = self.http.post(
                    f"{CLOB_BASE}{path}", json=payload, timeout=self.timeout
                )
                if r.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                if attempt == 3:
                    raise
                log.warning("clob %s failed (%s), retry %d", path, e, attempt + 1)
                time.sleep(1.5 * (attempt + 1))
        return []

    def get_book(self, token_id: str) -> OrderBook | None:
        try:
            r = self.http.get(
                f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=self.timeout
            )
            r.raise_for_status()
            return parse_book(r.json())
        except (requests.RequestException, ValueError) as e:
            log.warning("get_book(%s...) failed: %s", token_id[:12], e)
            return None

    def get_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """Batch order-book snapshots, chunked to stay within API limits."""
        out: dict[str, OrderBook] = {}
        for i in range(0, len(token_ids), BOOKS_BATCH_SIZE):
            chunk = token_ids[i : i + BOOKS_BATCH_SIZE]
            try:
                raw = self._post("/books", [{"token_id": t} for t in chunk])
            except (requests.RequestException, ValueError) as e:
                log.error("books batch failed permanently: %s", e)
                continue
            for rb in raw or []:
                book = parse_book(rb)
                if book.token_id:
                    out[book.token_id] = book
        return out
