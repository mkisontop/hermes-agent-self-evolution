# Skill Usage Tracker — Runbook

Persistent skill usage log for picking self-evolution targets.

## Why

`state.db` retention is short and sparse. If we want to evolve the *most used*
skills, we need a durable, deduped log of `skill_view` calls collected over
days-to-weeks.

## Files

| File | Purpose |
|------|---------|
| `skill_usage_tracker.py` | Snapshots `~/.hermes/state.db` (WAL-safe backup API), extracts `skill_view` tool calls, appends deduped records to the log. |
| `skill_usage_picker.py` | Ranks skills from the log. Strategies: `loads` (raw count), `sessions` (distinct sessions), `hybrid` (0.6·sessions + 0.4·loads normalized). Emits a table + JSON blob (or `--json-only` for scripting). |
| `weekly_digest.py` | Human-readable weekly report — runs all 3 strategies, checks agreement, lists top-10 leaderboard + recommendation. |
| `skill_usage_log.jsonl` | The durable log. One JSON record per line. Gitignored. |

## Record Schema

```json
{"timestamp": 1776528000.12, "session_id": "abc123", "skill_name": "writing-plans", "file_path": null}
```

Dedup key: `(session_id, timestamp, skill_name)`. Safe to re-run with overlap windows.

## Crons

| Job | Schedule | Delivery | Purpose |
|-----|----------|----------|---------|
| `hermes-skill-usage-tracker` | `0 */4 * * *` (every 4h) | `local` | Incremental collection with `--hours 5` (1h overlap buffer). |
| `hermes-skill-usage-weekly-digest` | `0 9 * * 6` (Sat 09:00 local) | `origin` (Telegram) | Weekly top-3 report with strategy-agreement check. |
| `hermes-self-evolution-nightly` | `0 7 * * *` | **PAUSED** | Will be retargeted once 7 days of data collected. |

## Normal Operation

Nothing to do — crons handle it. Check progress anytime:

```bash
# line count
wc -l ~/.hermes/self-evolution/usage/skill_usage_log.jsonl

# current top-3 (whatever window has data)
python3 ~/.hermes/self-evolution/usage/skill_usage_picker.py --days 7 --top 3

# full human report
python3 ~/.hermes/self-evolution/usage/weekly_digest.py
```

## Pick top-N manually

```bash
python3 skill_usage_picker.py --days 7 --top 3 --strategy hybrid --json-only > top3.json
```

JSON is the contract for the evolution runner (see `nightly.sh`).

## After 7 days: retarget evolution

1. Run `weekly_digest.py` → pick top-3 skills where strategies agree.
2. Edit `nightly.sh` / evolution config to iterate over chosen skills.
3. Resume the paused nightly cron:
   ```
   cronjob list  # find hermes-self-evolution-nightly job_id
   cronjob resume --job_id <id>
   ```

## Troubleshooting

### "DB missing" error from tracker
Expected if `~/.hermes/state.db` doesn't exist. Tracker exits 2 cleanly. Usually means Hermes never ran on this machine.

### Log growth concerns
~1 record per skill-load per session. Assuming 20 sessions × 5 skill-loads/day = 100 records/day ≈ 250 bytes each ≈ 25 KB/day ≈ 175 KB/week. Log will not exceed a few MB in a year of heavy use. No rotation needed.

### Log corruption
The digest + picker skip malformed JSON lines silently (tested). One bad line won't break the pipeline. If corruption is widespread, `jq -c . log.jsonl > log.clean && mv log.clean log.jsonl`.

### WAL not snapshotted
**Fixed.** Tracker uses SQLite's backup API (not `cp`) so WAL is captured correctly.

### Cron fired but nothing in log
Check `~/.hermes/cron/output/fa3e0aa84158/` for the run report. If the tracker output says `appended=0 total_in_log=N` and N is growing, all is well. If N is stuck, check stderr in the report.

### Picker returns `[]`
Not enough data in window. Widen `--days` or wait for tracker to accumulate. Needs at least 1 record in window.

### "Strategies disagree"
Normal for sparse data. Keep collecting. Hybrid is the recommended default.

## Verification Checklist (stress-tested 2026-04-18)

- [x] Tracker idempotent — re-run → 0 appended.
- [x] WAL-safe snapshot via `sqlite3.Connection.backup()`.
- [x] Missing DB → exit 2, clean.
- [x] Corrupt log lines → skipped, no crash.
- [x] Picker handles 500+ records, 20+ skills, all 3 strategies agree.
- [x] Cron fired on-demand, produced correct output, delivered local.
- [x] Evolution cron paused, enabled=false (won't accidentally fire).
- [x] Weekly digest cron scheduled for next Saturday 09:00 Iran.

## Ignoring the log in git

Log + DB snapshots are gitignored — they are machine-local state, not source.
See root `.gitignore`.
