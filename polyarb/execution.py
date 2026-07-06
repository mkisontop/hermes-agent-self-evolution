"""Execution engines: paper (default) and live (py-clob-client-v2).

Live trading is triple-gated by environment variables — ALL must hold:

    POLYARB_MODE=live
    LIVE_TRADING_ENABLED=true
    DRY_RUN=false

plus wallet config:

    POLYMARKET_PRIVATE_KEY   signer EOA private key
    POLYMARKET_FUNDER        proxy/Safe/deposit wallet holding funds
                             (omit for raw EOA, signature type 0)
    POLYMARKET_SIGNATURE_TYPE  0=EOA 1=email/Magic proxy 2=Safe 3=deposit wallet

Basket legs are posted concurrently as FAK (immediate-or-cancel) limit
orders at the walked worst price. There is NO atomic multi-leg order on
Polymarket, so a basket can partially fill (legging risk). On a partial
basket this engine writes the kill-switch file and halts further trading
rather than compounding: the operator decides whether to complete or
unwind the position.
"""

from __future__ import annotations

import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from .models import Leg, Opportunity, Side

log = logging.getLogger(__name__)


def quantize_leg(price: float, size: float, tick: float) -> tuple[float, float]:
    """Snap (price, size) to values the CLOB accepts.

    The exchange requires both amounts at 1e6 scale to be multiples of
    10_000, i.e. size and price*size must each have <= 2 decimals. With a
    price at d>2 decimals that forces the size to a multiple of
    10^(d-2) shares; sizes are floored so we never exceed walked depth.
    """
    p = Decimal(str(price)).quantize(Decimal(str(tick)))
    decimals = max(0, -p.as_tuple().exponent)
    step = Decimal(10) ** max(decimals - 2, -2)  # 0.01 min step
    s = (Decimal(str(size)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    return float(p), float(s)


@dataclass
class LegResult:
    token_id: str
    ok: bool
    filled_size: float = 0.0
    order_id: str = ""
    error: str = ""


@dataclass
class ExecutionResult:
    mode: str
    success: bool
    legs: list[LegResult] = field(default_factory=list)
    started_at: float = 0.0
    elapsed_ms: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "success": self.success,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "note": self.note,
            "legs": [
                {
                    "token_id": l.token_id,
                    "ok": l.ok,
                    "filled_size": l.filled_size,
                    "order_id": l.order_id,
                    "error": l.error,
                }
                for l in self.legs
            ],
        }


class PaperExecutor:
    """Simulated fills at the walked book prices (optimistic baseline).

    Paper results are an upper bound: they assume every leg fills at the
    detected depth with zero latency. Live slippage/legging will only be
    worse — which is exactly why the ledger separates the two.
    """

    mode = "paper"

    def execute(self, opp: Opportunity) -> ExecutionResult:
        t0 = time.time()
        legs = [
            LegResult(token_id=l.token_id, ok=True, filled_size=l.size)
            for l in opp.legs
        ]
        return ExecutionResult(
            mode=self.mode,
            success=True,
            legs=legs,
            started_at=t0,
            elapsed_ms=(time.time() - t0) * 1000,
            note="simulated fill at detection prices",
        )


class DelayedPaperExecutor:
    """Latency-honest paper fills: re-check the LIVE books after a delay.

    Simulates the real order path: detection -> (signing + network +
    matching latency) -> fill against whatever is resting *then*. Each
    BUY leg fills only up to the size still available at or below its
    limit price in the books ``delay_ms`` after detection. This measures
    edge decay / adverse selection directly: if fills at delay 500ms
    match fills at 0ms, latency is not the binding constraint.

    ``book_provider()`` must return the live BookStore (WS mode).
    """

    mode = "paper-delayed"

    def __init__(self, book_provider, delay_ms: float = 500.0):
        self._provider = book_provider
        self.delay_ms = delay_ms

    def _available_at_limit(self, store, leg: Leg) -> float:
        yes, mirrored = store.resolve(leg.token_id)
        books = store.get_books([yes])
        if books is None:
            return 0.0
        book = books[yes]
        from .arbmath import mirror_ladder  # local: avoid cycle at import

        ladder = mirror_ladder(book.bids) if mirrored else book.asks
        avail = 0.0
        for lv in ladder:
            if lv.price > leg.price + 1e-9:
                break
            avail += lv.size
        return avail

    def execute(self, opp: Opportunity) -> ExecutionResult:
        t0 = time.time()
        if self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000.0)
        store = self._provider()
        legs = []
        fillable = []
        for leg in opp.legs:
            avail = self._available_at_limit(store, leg)
            frac = min(1.0, avail / leg.size) if leg.size > 0 else 0.0
            fillable.append(frac)
            legs.append(
                LegResult(
                    token_id=leg.token_id,
                    ok=avail >= leg.size * 0.999,
                    filled_size=min(avail, leg.size),
                )
            )
        basket_frac = min(fillable) if fillable else 0.0
        success = basket_frac >= 0.999
        return ExecutionResult(
            mode=self.mode,
            success=success,
            legs=legs,
            started_at=t0,
            elapsed_ms=(time.time() - t0) * 1000,
            note=(
                f"delay={self.delay_ms:.0f}ms basket_fillable={basket_frac:.3f}"
                + ("" if success else " (FOK basket would NOT fill fully)")
            ),
        )


def live_gate_open() -> bool:
    return (
        os.environ.get("POLYARB_MODE") == "live"
        and os.environ.get("LIVE_TRADING_ENABLED", "").lower() == "true"
        and os.environ.get("DRY_RUN", "").lower() == "false"
    )


class LiveExecutor:
    """Posts real FAK orders through py-clob-client-v2 (or legacy v1).

    Requires funded wallet, USDC/pUSD + CTF allowances already granted,
    and a non-geoblocked network location. See polyarb/README.md.
    """

    mode = "live"

    def __init__(self, kill_switch_file: str = "polyarb.KILL"):
        if not live_gate_open():
            raise RuntimeError(
                "live trading blocked: set POLYARB_MODE=live, "
                "LIVE_TRADING_ENABLED=true, DRY_RUN=false"
            )
        key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")
        self.kill_switch_file = kill_switch_file
        funder = os.environ.get("POLYMARKET_FUNDER") or None
        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        self._client, self._api = self._build_client(key, sig_type, funder)

    @staticmethod
    def _build_client(key: str, sig_type: int, funder: str | None):
        """Try v2 client first (v1 was archived May 2026), fall back to v1."""
        host, chain_id = "https://clob.polymarket.com", 137
        try:
            from py_clob_client_v2 import ClobClient  # type: ignore

            client = ClobClient(
                host, key=key, chain_id=chain_id,
                signature_type=sig_type, funder=funder,
            )
            creds = client.create_or_derive_api_key()
            client = ClobClient(
                host, key=key, chain_id=chain_id, creds=creds,
                signature_type=sig_type, funder=funder,
            )
            return client, "v2"
        except ImportError:
            pass
        from py_clob_client.client import ClobClient  # type: ignore

        client = ClobClient(
            host, key=key, chain_id=chain_id,
            signature_type=sig_type, funder=funder,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        return client, "v1"

    def _post_leg(self, leg: Leg, neg_risk: bool, tick: float) -> LegResult:
        price, size = quantize_leg(leg.price, leg.size, tick)
        if size <= 0:
            return LegResult(leg.token_id, ok=False, error="size quantized to 0")
        try:
            if self._api == "v2":
                from py_clob_client_v2 import (  # type: ignore
                    OrderArgs,
                    OrderType,
                    PartialCreateOrderOptions,
                )
                from py_clob_client_v2.order_builder.constants import (  # type: ignore
                    BUY,
                    SELL,
                )

                resp = self._client.create_and_post_order(
                    OrderArgs(
                        token_id=leg.token_id,
                        price=price,
                        size=size,
                        side=BUY if leg.side == Side.BUY else SELL,
                    ),
                    options=PartialCreateOrderOptions(
                        tick_size=str(tick), neg_risk=neg_risk
                    ),
                    order_type=OrderType.FAK,
                )
            else:
                from py_clob_client.clob_types import (  # type: ignore
                    OrderArgs,
                    OrderType,
                    PartialCreateOrderOptions,
                )
                from py_clob_client.order_builder.constants import (  # type: ignore
                    BUY,
                    SELL,
                )

                signed = self._client.create_order(
                    OrderArgs(
                        token_id=leg.token_id,
                        price=price,
                        size=size,
                        side=BUY if leg.side == Side.BUY else SELL,
                    ),
                    PartialCreateOrderOptions(neg_risk=neg_risk),
                )
                resp = self._client.post_order(signed, OrderType.FAK)
        except Exception as e:  # noqa: BLE001 — every leg error must be captured
            return LegResult(leg.token_id, ok=False, error=str(e)[:300])
        resp = resp or {}
        status = str(resp.get("status", "")).lower()
        matched = status in ("matched", "success") or bool(resp.get("success"))
        filled = float(resp.get("takingAmount") or resp.get("size_matched") or 0.0)
        if matched and filled <= 0:
            filled = size  # some responses omit fill size on full match
        return LegResult(
            leg.token_id,
            ok=matched and filled > 0,
            filled_size=filled,
            order_id=str(resp.get("orderID") or resp.get("orderId") or ""),
            error="" if matched else f"status={status or 'unknown'}",
        )

    def execute(self, opp: Opportunity, tick: float = 0.001) -> ExecutionResult:
        t0 = time.time()
        neg_risk = opp.kind.value.startswith("negrisk")
        with ThreadPoolExecutor(max_workers=min(16, len(opp.legs))) as pool:
            results = list(
                pool.map(lambda l: self._post_leg(l, neg_risk, tick), opp.legs)
            )
        full = all(r.ok and math.isclose(r.filled_size, opp.size, rel_tol=0.05)
                   for r in results)
        any_fill = any(r.ok and r.filled_size > 0 for r in results)
        note = ""
        if any_fill and not full:
            note = (
                "PARTIAL BASKET — kill switch engaged; complete or unwind "
                "the missing legs manually"
            )
            log.critical("%s: %s", opp.event_title, note)
            with open(self.kill_switch_file, "w", encoding="utf-8") as f:
                f.write(f"partial basket at {time.time()}: {opp.to_json()}\n")
        return ExecutionResult(
            mode=self.mode,
            success=full,
            legs=results,
            started_at=t0,
            elapsed_ms=(time.time() - t0) * 1000,
            note=note,
        )
