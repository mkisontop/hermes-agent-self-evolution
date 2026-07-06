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


def _episode(event_id: str, kind: str, recs: list[dict]) -> dict:
    best = max(recs, key=lambda r: r.get("profit", 0.0))
    return {
        "event_id": event_id,
        "kind": kind,
        "title": best.get("event_title", ""),
        "n_detections": len(recs),
        "start": recs[0]["detected_at"],
        "duration_s": recs[-1]["detected_at"] - recs[0]["detected_at"],
        "max_profit": best.get("profit", 0.0),
        "max_roi": best.get("roi", 0.0),
        "max_size": best.get("size", 0.0),
        "cost_at_max": best.get("gross_cost", 0.0),
    }


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

    def log_tightness(self, stats: dict) -> None:
        self._append("tightness.jsonl", {"ts": time.time(), **stats})

    # ------------------------------------------------------------------
    @staticmethod
    def episodes(records: list[dict], gap_s: float = 60.0) -> list[dict]:
        """Collapse repeated detections of a persisting opportunity.

        Records for one (event, kind) are split into *episodes* wherever
        the gap between consecutive detections exceeds ``gap_s``. One
        episode ~= one distinct chance to trade; its value is the max
        depth-true profit seen during it (you could have fired once).
        """
        by_key: dict[tuple, list[dict]] = defaultdict(list)
        for r in records:
            by_key[(r.get("event_id"), r.get("kind"))].append(r)
        out: list[dict] = []
        for (event_id, kind), recs in by_key.items():
            recs.sort(key=lambda r: r.get("detected_at", 0))
            cur: list[dict] = []
            for r in recs:
                if cur and r["detected_at"] - cur[-1]["detected_at"] > gap_s:
                    out.append(_episode(event_id, kind, cur))
                    cur = []
                cur.append(r)
            if cur:
                out.append(_episode(event_id, kind, cur))
        return out

    def report(self) -> str:
        """Aggregate the opportunity ledger into a human-readable summary."""
        path = os.path.join(self.directory, "opportunities.jsonl")
        if not os.path.exists(path):
            return "No opportunities logged yet. Run `scan` or `monitor` first."
        records: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
        eps = self.episodes(records)
        by_kind: dict[str, list[dict]] = defaultdict(list)
        for e in eps:
            by_kind[e["kind"]].append(e)
        span_s = (
            max(r["detected_at"] for r in records)
            - min(r["detected_at"] for r in records)
            if len(records) > 1
            else 0.0
        )
        lines = [
            "polyarb opportunity report (episode-deduped)",
            f"window: {span_s / 3600:.2f}h, raw detections: {len(records)}, "
            f"episodes: {len(eps)}",
            "=" * 72,
        ]
        total = 0.0
        for kind, kes in sorted(by_kind.items()):
            profits = [e["max_profit"] for e in kes]
            durs = [e["duration_s"] for e in kes]
            total += sum(profits)
            lines.append(
                f"{kind:<18} episodes={len(kes):<4} "
                f"profit/episode: med=${sorted(profits)[len(profits) // 2]:.2f} "
                f"max=${max(profits):.2f} sum=${sum(profits):.2f} | "
                f"median duration={sorted(durs)[len(durs) // 2]:.0f}s"
            )
        lines.append("-" * 72)
        lines.append(
            f"TOTAL realizable (1 fill per episode, depth-true): ${total:.2f}"
        )
        top = sorted(eps, key=lambda e: -e["max_profit"])[:8]
        if top:
            lines.append("top episodes:")
            for e in top:
                lines.append(
                    f"  ${e['max_profit']:>7.2f} roi={e['max_roi'] * 100:5.2f}% "
                    f"dur={e['duration_s']:6.0f}s {e['kind']:<17} {e['title'][:44]}"
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
