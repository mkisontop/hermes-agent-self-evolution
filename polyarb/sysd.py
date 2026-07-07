"""Minimal systemd integration (sd_notify) — zero dependencies.

The daemon runs under ``Type=notify`` with ``WatchdogSec``: systemd
restarts it not only when it dies but when it *hangs*. Crucially, the
scanner pets the watchdog ONLY while the market feed is healthy, so a
silently-frozen WebSocket (the documented Polymarket failure mode)
converts into a clean automatic restart instead of a blind zombie.

No-ops outside systemd (NOTIFY_SOCKET unset), so dev usage is unchanged.
"""

from __future__ import annotations

import os
import socket


def sd_notify(message: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
    except OSError:
        pass


def sd_ready() -> None:
    sd_notify("READY=1")


def sd_watchdog_interval_s() -> float | None:
    """Half the WatchdogSec window, or None when not under a watchdog."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    try:
        return int(usec) / 2_000_000.0
    except ValueError:
        return None


def sd_pet_watchdog() -> None:
    sd_notify("WATCHDOG=1")


def sd_status(text: str) -> None:
    sd_notify(f"STATUS={text[:120]}")
