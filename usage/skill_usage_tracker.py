#!/usr/bin/env python3
"""Skill usage tracker.

Extracts skill_view calls from Hermes state.db and appends to a durable
JSONL log so we can rank skills by real usage over a week+ window.
Safe to run repeatedly: dedupes on (session_id, timestamp, skill_name).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERMES_DB = Path(os.path.expanduser("~/.hermes/state.db"))
SNAPSHOT_DB = Path("/tmp/hermes_state_snapshot.db")
LOG_PATH = Path(__file__).resolve().parent / "skill_usage_log.jsonl"


def snapshot_db():
    """Backup state.db safely — uses SQLite Online Backup API so WAL is merged
    and we don't race with active writers. Falls back to file copy only if
    the source DB doesn't exist."""
    if not HERMES_DB.exists():
        print(f"[tracker] FATAL: {HERMES_DB} missing", file=sys.stderr)
        sys.exit(2)
    # Remove stale snapshot (and any -wal/-shm siblings)
    for p in (SNAPSHOT_DB, Path(str(SNAPSHOT_DB) + "-wal"), Path(str(SNAPSHOT_DB) + "-shm")):
        if p.exists():
            p.unlink()
    try:
        src = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True, timeout=30.0)
        dst = sqlite3.connect(SNAPSHOT_DB)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except sqlite3.Error as e:
        print(f"[tracker] sqlite backup failed ({e}); falling back to file copy", file=sys.stderr)
        shutil.copy2(HERMES_DB, SNAPSHOT_DB)
    return SNAPSHOT_DB


def load_existing_keys(log_path):
    keys = set()
    if not log_path.exists():
        return keys
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            keys.add((row.get("session_id"), row.get("timestamp"), row.get("skill_name")))
    return keys


def extract_skill_views(db_path, since_ts):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    if since_ts is not None:
        cur.execute(
            "SELECT session_id, timestamp, tool_calls FROM messages "
            "WHERE role='assistant' AND tool_calls IS NOT NULL AND timestamp >= ?",
            (since_ts,),
        )
    else:
        cur.execute(
            "SELECT session_id, timestamp, tool_calls FROM messages "
            "WHERE role='assistant' AND tool_calls IS NOT NULL"
        )
    rows = cur.fetchall()
    conn.close()

    out = []
    for session_id, ts, tc_json in rows:
        try:
            calls = json.loads(tc_json)
        except Exception:
            continue
        for c in calls:
            fn = c.get("function", {})
            if fn.get("name") != "skill_view":
                continue
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                continue
            skill = args.get("name")
            if not skill:
                continue
            out.append({
                "timestamp": ts,
                "session_id": session_id,
                "skill_name": skill,
                "file_path": args.get("file_path"),
            })
    return out


def append_log(log_path, records):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_existing_keys(log_path)
    new_rows = []
    for r in records:
        key = (r["session_id"], r["timestamp"], r["skill_name"])
        if key in existing:
            continue
        existing.add(key)
        new_rows.append(r)
    if new_rows:
        with log_path.open("a") as f:
            for r in new_rows:
                f.write(json.dumps(r) + "\n")
    return len(new_rows)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="ingest full DB")
    g.add_argument("--since", type=float, help="unix epoch seconds cutoff")
    g.add_argument("--hours", type=float, default=26.0, help="lookback hours")
    args = ap.parse_args()

    if args.all:
        since_ts = None
    elif args.since is not None:
        since_ts = args.since
    else:
        since_ts = time.time() - (args.hours * 3600)

    snap = snapshot_db()
    records = extract_skill_views(snap, since_ts)
    added = append_log(LOG_PATH, records)

    total = 0
    if LOG_PATH.exists():
        with LOG_PATH.open() as f:
            for _ in f:
                total += 1
    print(f"[tracker] scanned={len(records)} appended={added} total_in_log={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
