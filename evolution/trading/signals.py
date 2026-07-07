"""Drift/regime signals computed from the daemon's journals.

Pure-numeric detectors (zero tokens) whose output feeds both the ntfy
digest and the Hermes reflective pass. The LLM never sees raw journals —
it sees this compact, pre-digested evidence, which keeps the packet
small, the cost bounded, and the reflection grounded in numbers rather
than vibes.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

#: crude category classifier over event titles (good enough for mix drift)
CATEGORY_KEYWORDS = {
    "weather": ("temperature", "rain", "snow", "hottest", "weather"),
    "sports": ("vs.", "cup", "league", "nba", "nfl", "mlb", "winner",
               "champion", "match", "wimbledon"),
    "geopolitics": ("strike", "war", "ceasefire", "nato", "sanction",
                    "military", "hormuz", "missile"),
    "politics": ("election", "president", "nominee", "minister",
                 "parliament", "senate"),
}


def classify(title: str) -> str:
    t = title.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws):
            return cat
    return "other"


@dataclass
class DaySlice:
    day: str
    detections: int = 0
    episodes: int = 0
    mean_edge: float = 0.0
    total_profit: float = 0.0
    executions: int = 0
    exec_success: int = 0
    category_mix: dict = field(default_factory=dict)


@dataclass
class DriftReport:
    days: list[DaySlice] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "days": [vars(d) for d in self.days],
            "flags": self.flags,
        }


def _read_jsonl(path: str) -> list[dict]:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def compute_drift(data_dirs: list[str], lookback_days: int = 14) -> DriftReport:
    from polyarb.ledger import Ledger

    cutoff = time.time() - lookback_days * 86400
    opps: list[dict] = []
    execs: list[dict] = []
    for d in data_dirs:
        opps += [r for r in _read_jsonl(os.path.join(d, "opportunities.jsonl"))
                 if r.get("detected_at", 0) >= cutoff]
        execs += [r for r in _read_jsonl(os.path.join(d, "executions.jsonl"))
                  if r.get("ts", 0) >= cutoff]

    slices: dict[str, DaySlice] = {}

    def day_of(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    edges = defaultdict(list)
    for r in opps:
        s = slices.setdefault(day_of(r["detected_at"]),
                              DaySlice(day=day_of(r["detected_at"])))
        s.detections += 1
        edges[s.day].append(r.get("edge_per_share", 0.0))
        cat = classify(r.get("event_title", ""))
        s.category_mix[cat] = s.category_mix.get(cat, 0) + 1
    for ep in Ledger.episodes(opps):
        s = slices.setdefault(day_of(ep["start"]), DaySlice(day=day_of(ep["start"])))
        s.episodes += 1
        s.total_profit += ep["max_profit"]
    for r in execs:
        s = slices.setdefault(day_of(r["ts"]), DaySlice(day=day_of(r["ts"])))
        s.executions += 1
        if r.get("result", {}).get("success"):
            s.exec_success += 1
    for day, s in slices.items():
        if edges[day]:
            s.mean_edge = sum(edges[day]) / len(edges[day])

    report = DriftReport(days=sorted(slices.values(), key=lambda s: s.day))

    # --- change detection: last day vs trailing mean of the rest ---
    if len(report.days) >= 3:
        *prior, last = report.days
        for metric, floor in (("detections", 5), ("episodes", 1),
                              ("total_profit", 0.5)):
            base = sum(getattr(d, metric) for d in prior) / len(prior)
            cur = getattr(last, metric)
            if base >= floor and cur < 0.4 * base:
                report.flags.append(
                    f"{metric} collapsed: {cur:.1f} vs trailing avg {base:.1f} "
                    "(possible new competitor, API change, or feed problem)"
                )
            if base >= floor and cur > 3.0 * base:
                report.flags.append(
                    f"{metric} spiked: {cur:.1f} vs trailing avg {base:.1f} "
                    "(news regime or detector anomaly — verify before sizing up)"
                )
        succ_rates = [
            d.exec_success / d.executions for d in report.days if d.executions
        ]
        if len(succ_rates) >= 2 and succ_rates[-1] < 0.5 <= succ_rates[-2]:
            report.flags.append(
                "execution success rate dropped below 50% — "
                "fills degrading (competition or stale books)"
            )
    return report
