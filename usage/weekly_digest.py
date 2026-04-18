#!/usr/bin/env python3
"""Weekly skill usage digest.

Runs the picker against the usage log for the last N days (default 7) across
all three strategies (loads, sessions, hybrid), checks agreement on top-3,
and emits a compact markdown report suitable for a Telegram/cron delivery.

Safe to run anytime — read-only, no network, no LLM.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "skill_usage_log.jsonl"
PICKER = ROOT / "skill_usage_picker.py"


def load_records(log_path: Path, days: float):
    if not log_path.exists():
        return []
    cutoff = time.time() - days * 86400
    out = []
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("timestamp", 0) >= cutoff:
                out.append(r)
    return out


def run_picker(strategy: str, days: float, top: int):
    proc = subprocess.run(
        [sys.executable, str(PICKER),
         "--strategy", strategy, "--days", str(days),
         "--top", str(top), "--json-only"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        return None, proc.stderr
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as e:
        return None, f"JSON decode error: {e}\nstdout: {proc.stdout[:300]}"


def format_report(days: float, top: int) -> str:
    records = load_records(LOG_PATH, days)
    lines = []
    lines.append(f"# 📊 Skill Usage Digest — last {days:g} days")
    lines.append("")
    lines.append(f"**Records considered:** {len(records)}")
    lines.append(f"**Log total:** {sum(1 for _ in LOG_PATH.open()) if LOG_PATH.exists() else 0}")
    lines.append("")

    if not records:
        lines.append("_No usage data in window. Tracker may not be running; check cron._")
        return "\n".join(lines)

    # Sanity stats
    skills_all = Counter()
    sessions_by_skill = defaultdict(set)
    for r in records:
        s = r.get("skill_name")
        sid = r.get("session_id")
        if s:
            skills_all[s] += 1
            if sid:
                sessions_by_skill[s].add(sid)
    lines.append(f"**Distinct skills loaded:** {len(skills_all)}")
    lines.append(f"**Distinct sessions:** {len({r.get('session_id') for r in records if r.get('session_id')})}")
    lines.append("")

    # Run all three strategies
    results = {}
    for strat in ("loads", "sessions", "hybrid"):
        data, err = run_picker(strat, days, top)
        if err:
            lines.append(f"⚠️ picker `{strat}` failed: {err[:200]}")
            continue
        results[strat] = [t["skill_name"] for t in data["top"]]

    # Agreement check
    if len(results) == 3:
        all_tops = [set(v) for v in results.values()]
        intersect = set.intersection(*all_tops)
        union = set.union(*all_tops)
        agreement = len(intersect) / top if top else 0
        lines.append(f"## 🎯 Strategy Agreement")
        lines.append(f"- **All 3 strategies agree on:** {sorted(intersect) if intersect else '_none_'}")
        lines.append(f"- **Union of top-{top} across strategies:** {sorted(union)}")
        lines.append(f"- **Agreement ratio:** {agreement:.0%}")
        lines.append("")

    lines.append(f"## 🏆 Top-{top} by Strategy")
    lines.append("")
    lines.append("| Rank | loads | sessions | hybrid |")
    lines.append("|------|-------|----------|--------|")
    for i in range(top):
        row = [f"{i+1}"]
        for strat in ("loads", "sessions", "hybrid"):
            if strat in results and i < len(results[strat]):
                row.append(f"`{results[strat][i]}`")
            else:
                row.append("—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Top 10 raw leaderboard (from 'hybrid' full output for neutrality)
    lines.append(f"## 📋 Full Leaderboard (hybrid, top 10)")
    lines.append("")
    lines.append("| Rank | Skill | Loads | Sessions |")
    lines.append("|------|-------|-------|----------|")
    ranked = sorted(skills_all, key=lambda s: (len(sessions_by_skill[s]), skills_all[s]), reverse=True)
    for i, s in enumerate(ranked[:10], 1):
        lines.append(f"| {i} | `{s}` | {skills_all[s]} | {len(sessions_by_skill[s])} |")
    lines.append("")

    # Recommendation
    if len(results) == 3 and intersect:
        lines.append("## ✅ Recommendation")
        lines.append(f"Evolution targets (agreed across strategies): **{', '.join(sorted(intersect))}**")
    elif len(results) == 3:
        lines.append("## ⚠️ Recommendation")
        lines.append(f"Strategies disagree. Likely sparse data — keep collecting. "
                     f"Hybrid top-{top}: {', '.join(results.get('hybrid', []))}")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()
    print(format_report(args.days, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
