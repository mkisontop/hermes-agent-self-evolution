"""Append-only JSONL ledger of detected opportunities and executions.

The ledger is the profitability instrument: run `monitor` for days in
paper mode and `report` tells you what edge actually existed, how big,
how often — before risking a cent.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass

from .models import Opportunity


@dataclass
class Ledger:
    directory: str = "polyarb_data"

    def __post_init__(self) -> None:
        os.makedirs(self.directory, exist_ok=True)

    def _append(self, name: str, record: dict) -> None:
        path = os.path.join(self.directory, name)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    def log_opportunity(self, opp: Opportunity) -> None:
        self._append("opportunities.jsonl", opp.to_dict())

    def log_execution(self, opp: Opportunity, result: dict) -> None:
        self._append(
            "executions.jsonl",
            {"ts": time.time(), "opportunity": opp.to_dict(), "result": result},
        )

    def log_scan(self, stats: dict) -> None:
        self._append("scans.jsonl", {"ts": time.time(), **stats})

    # ------------------------------------------------------------------
    def report(self) -> str:
        """Aggregate the opportunity ledger into a human-readable summary."""
        path = os.path.join(self.directory, "opportunities.jsonl")
        if not os.path.exists(path):
            return "No opportunities logged yet. Run `scan` or `monitor` first."
        by_kind: dict[str, list[dict]] = defaultdict(list)
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                by_kind[rec.get("kind", "?")].append(rec)
        lines = ["polyarb opportunity report", "=" * 60]
        total_profit = 0.0
        n_total = 0
        for kind, recs in sorted(by_kind.items()):
            profits = [r["profit"] for r in recs]
            rois = [r["roi"] for r in recs]
            events = {r.get("event_id") for r in recs}
            total_profit += sum(profits)
            n_total += len(recs)
            lines.append(
                f"{kind:<18} n={len(recs):<6} events={len(events):<5} "
                f"profit: sum=${sum(profits):.2f} max=${max(profits):.2f} "
                f"median_roi={sorted(rois)[len(rois) // 2] * 100:.2f}%"
            )
        lines.append("-" * 60)
        lines.append(
            f"TOTAL detections: {n_total}, theoretical profit ${total_profit:.2f}"
        )
        lines.append(
            "note: detections over time re-count persisting opportunities; "
            "dedupe by event before treating this as realizable P&L"
        )
        # executions
        xpath = os.path.join(self.directory, "executions.jsonl")
        if os.path.exists(xpath):
            n = paper = live = 0
            profit = 0.0
            with open(xpath, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    n += 1
                    res = rec.get("result", {})
                    if res.get("mode") == "live":
                        live += 1
                    else:
                        paper += 1
                    if res.get("success"):
                        profit += rec["opportunity"]["profit"]
            lines.append(
                f"EXECUTIONS: {n} ({paper} paper, {live} live), "
                f"locked-in profit (at detection prices): ${profit:.2f}"
            )
        return "\n".join(lines)
