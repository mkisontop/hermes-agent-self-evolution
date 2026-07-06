"""Tests for the WebSocket book store (snapshot + delta + mirroring)."""

import pytest

from polyarb.ws import BookStore, _set_level
from polyarb.models import BookLevel


def snapshot(asset, bids, asks):
    # API shape: best-LAST, string prices/sizes
    return {
        "event_type": "book",
        "asset_id": asset,
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "timestamp": "1783333115465",
        "hash": "abc",
    }


def pc(asset, price, size, side):
    return {"asset_id": asset, "price": str(price), "size": str(size), "side": side}


@pytest.fixture
def store():
    st = BookStore()
    st.register("yesA", "noA", conn_id=0)
    st.set_conn_health(0, True)
    return st


class TestSetLevel:
    def test_insert_replace_remove(self):
        levels = [BookLevel(0.5, 10)]
        levels = _set_level(levels, 0.51, 5)
        assert len(levels) == 2
        levels = _set_level(levels, 0.5, 20)
        assert sorted(l.size for l in levels) == [5, 20]
        levels = _set_level(levels, 0.51, 0)
        assert len(levels) == 1 and levels[0].size == 20


class TestSnapshot:
    def test_yes_snapshot_normalized_best_first(self, store):
        store.apply_snapshot(
            snapshot("yesA", bids=[(0.126, 100), (0.128, 50)],
                     asks=[(0.131, 10), (0.129, 20)])
        )
        books = store.get_books(["yesA"])
        b = books["yesA"]
        assert b.best_bid == 0.128 and b.best_ask == 0.129
        assert b.bids[0].size == 50 and b.asks[0].size == 20

    def test_no_snapshot_mirrors_into_yes(self, store):
        # NO book: bids ~0.87 == YES asks ~0.13
        store.apply_snapshot(
            snapshot("noA", bids=[(0.869, 10), (0.871, 20)],
                     asks=[(0.874, 30), (0.872, 40)])
        )
        b = store.get_books(["yesA"])["yesA"]
        assert b.best_ask == pytest.approx(0.129)  # 1 - 0.871
        assert b.asks[0].size == 20
        assert b.best_bid == pytest.approx(0.128)  # 1 - 0.872
        assert b.bids[0].size == 40


class TestPriceChange:
    def test_delta_updates_level(self, store):
        store.apply_snapshot(
            snapshot("yesA", bids=[(0.128, 50)], asks=[(0.129, 20)])
        )
        store.apply_price_change(pc("yesA", 0.127, 33, "BUY"))
        b = store.get_books(["yesA"])["yesA"]
        assert b.bids[1].price == pytest.approx(0.127)
        assert b.bids[1].size == 33
        store.apply_price_change(pc("yesA", 0.128, 0, "BUY"))  # remove best
        b = store.get_books(["yesA"])["yesA"]
        assert b.best_bid == pytest.approx(0.127)

    def test_no_delta_mirrored(self, store):
        store.apply_snapshot(
            snapshot("yesA", bids=[(0.128, 50)], asks=[(0.129, 20)])
        )
        # NO bid at 0.870 == YES ask at 0.130
        store.apply_price_change(pc("noA", 0.870, 77, "BUY"))
        b = store.get_books(["yesA"])["yesA"]
        assert b.asks[1].price == pytest.approx(0.130)
        assert b.asks[1].size == 77
        # NO ask at 0.873 == YES bid at 0.127
        store.apply_price_change(pc("noA", 0.873, 11, "SELL"))
        b = store.get_books(["yesA"])["yesA"]
        assert b.bids[1].price == pytest.approx(0.127)
        assert b.bids[1].size == 11

    def test_delta_before_snapshot_ignored(self, store):
        assert store.apply_price_change(pc("yesA", 0.5, 10, "BUY")) is None


class TestHealth:
    def test_unhealthy_connection_blocks_books(self, store):
        store.apply_snapshot(snapshot("yesA", bids=[(0.5, 1)], asks=[(0.51, 1)]))
        assert store.get_books(["yesA"]) is not None
        store.set_conn_health(0, False)
        assert store.get_books(["yesA"]) is None

    def test_missing_book_blocks(self, store):
        assert store.get_books(["yesA", "yesB"]) is None
