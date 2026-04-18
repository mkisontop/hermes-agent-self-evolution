# Skill Usage Tracker

Persistent skill usage log for picking self-evolution targets.

## Why

`state.db` retention is short and sparse. If we want to evolve the *most used*
skills, we need a durable, deduped log of `skill_view` calls.

## Files

- `skill_usage_tracker.py` — snapshots `~/.hermes/state.db` and appends new
  `skill_view` records to `skill_usage_log.jsonl`. Dedupes on
  `(session_id, timestamp, skill_name)`.
- `skill_usage_picker.py` — ranks skills from the log. Strategies:
  `loads` (raw count), `sessions` (distinct sessions), `hybrid` (0.6*sessions + 0.4*loads).
- `skill_usage_log.jsonl` — the durable log. One JSON record per line.

## Cron

`hermes-skill-usage-tracker` runs every 4 hours (`0 */4 * * *`), invokes the
tracker with `--hours 5` (1h overlap for safety). Delivery is `local` so it
does not spam Telegram.

## Picking the top-N

```
python3 skill_usage_picker.py --days 7 --top 3 --strategy hybrid
```

Emits a table + JSON blob. Use the JSON blob to feed the evolution runner.

## Ignoring the log in git

The log is gitignored — it is machine-local state, not source.
