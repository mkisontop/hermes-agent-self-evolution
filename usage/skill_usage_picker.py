#!/usr/bin/env python3
"""Skill usage picker — rank skills from the usage log, emit top-N JSON.

Strategies (CLI --strategy):
  loads      : raw skill_view counts (default)
  sessions   : number of distinct sessions that loaded the skill (dedupes bursts)
  hybrid     : 0.6 * sessions_score + 0.4 * loads_score, both min-max normalized

Filters:
  --min-sessions N   drop skills loaded in fewer than N distinct sessions
  --since EPOCH      only count records at/after this unix timestamp
  --days N           only count records in the last N days (default: 7)

Output:
  Human-readable table on stdout, plus a JSON blob at the end for tooling.
  --json-only: suppress the table.
  --top N     : report top N (default 3)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "skill_usage_log.jsonl"


def load_records(log_path: Path, since_ts):
    if not log_path.exists():
        return []
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
            if since_ts is not None and r.get("timestamp", 0) < since_ts:
                continue
            out.append(r)
    return out


def rank(records, strategy, min_sessions):
    loads = Counter()
    sessions_by_skill = defaultdict(set)
    for r in records:
        s = r.get("skill_name")
        sid = r.get("session_id")
        if not s:
            continue
        loads[s] += 1
        if sid:
            sessions_by_skill[s].add(sid)

    # filter
    skills = [s for s in loads if len(sessions_by_skill[s]) >= min_sessions]

    if strategy == "loads":
        scores = {s: loads[s] for s in skills}
    elif strategy == "sessions":
        scores = {s: len(sessions_by_skill[s]) for s in skills}
    elif strategy == "hybrid":
        if not skills:
            scores = {}
        else:
            def norm(d, keys):
                vmax = max(d[k] for k in keys) or 1
                return {k: d[k] / vmax for k in keys}
            n_loads = norm({s: loads[s] for s in skills}, skills)
            n_sess = norm({s: len(sessions_by_skill[s]) for s in skills}, skills)
            scores = {s: 0.6 * n_sess[s] + 0.4 * n_loads[s] for s in skills}
    else:
        raise ValueError(f"unknown strategy: {strategy}")

    ranked = sorted(skills, key=lambda s: scores[s], reverse=True)
    return ranked, scores, loads, sessions_by_skill


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["loads", "sessions", "hybrid"], default="hybrid")
    ap.add_argument("--min-sessions", type=int, default=1)
    ap.add_argument("--since", type=float)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    if args.since is not None:
        since_ts = args.since
    elif args.days is not None:
        since_ts = time.time() - args.days * 86400
    else:
        since_ts = None

    records = load_records(LOG_PATH, since_ts)
    ranked, scores, loads, sess = rank(records, args.strategy, args.min_sessions)
    top = ranked[: args.top]

    if not args.json_only:
        print(f"# skill usage picker")
        print(f"records_considered={len(records)} strategy={args.strategy} "
              f"min_sessions={args.min_sessions} days={args.days}")
        print()
        print(f"{'rank':<5}{'score':<8}{'loads':<7}{'sessions':<10}skill")
        for i, s in enumerate(ranked, 1):
            marker = "*" if i <= args.top else " "
            print(f"{marker}{i:<4}{scores[s]:<8.3f}{loads[s]:<7}{len(sess[s]):<10}{s}")
        print()

    out = {
        "generated_at": time.time(),
        "strategy": args.strategy,
        "days": args.days,
        "records_considered": len(records),
        "top": [
            {"skill_name": s, "score": scores[s], "loads": loads[s],
             "sessions": len(sess[s])}
            for s in top
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
