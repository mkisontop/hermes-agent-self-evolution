#!/usr/bin/env bash
# polyarb nightly: evaluate-and-accumulate, propose-only.
#
# Phases:
#   1. report   — episode-deduped P&L summary from the day's journal
#   2. evolve   — journal-replay config search -> proposal (NEVER auto-applies)
#   3. notify   — daily digest to ntfy + healthchecks dead-man ping
#
# A proposal is only worth reading when the gate passes; expect one every
# few weeks, not nightly. Approving = human runs:
#   python -m evolution.trading.evolve_trading --apply <proposal_dir> \
#       --baseline /var/lib/polyarb/polyarb.json
# The running daemon hot-reloads the config at its next universe refresh.
set -euo pipefail

REPO="${POLYARB_REPO:-/opt/polyarb/repo}"
VENVPY="${POLYARB_PY:-/opt/polyarb/venv/bin/python}"
DATA="${POLYARB_DATA:-/var/lib/polyarb/data}"
CONFIG="${POLYARB_CONFIG:-/var/lib/polyarb/polyarb.json}"
PROPOSALS="${POLYARB_PROPOSALS:-/var/lib/polyarb/proposals}"
LOGDIR="${POLYARB_LOGS:-/var/log/polyarb}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
HEALTHCHECK_URL="${POLYARB_NIGHTLY_HC:-}"

ts="$(date -u +%Y%m%d_%H%M%S)"
log="$LOGDIR/nightly-$ts.log"
mkdir -p "$LOGDIR" "$PROPOSALS"
exec > >(tee -a "$log") 2>&1
cd "$REPO"
export PYTHONPATH="$REPO"

echo "== polyarb nightly $ts =="

echo "-- phase 1: report --"
report="$("$VENVPY" -m polyarb --data-dir "$DATA" report)"
echo "$report"

echo "-- phase 2: evolve (propose-only) --"
"$VENVPY" -m evolution.trading.evolve_trading \
    --baseline "$CONFIG" \
    --data-dir "$DATA" \
    --iterations "${ITERATIONS:-400}" \
    --mode propose \
    --proposals-dir "$PROPOSALS" || echo "evolve: no episodes yet (ok on day 1)"

pending=$(find "$PROPOSALS" -name STATUS -exec grep -l PENDING {} \; 2>/dev/null | wc -l)

echo "-- phase 3: notify --"
if [ -n "$NTFY_TOPIC" ]; then
    curl -fsS -m 10 -H "Title: polyarb nightly" \
        -d "$(echo "$report" | tail -8)
pending proposals: $pending" \
        "https://ntfy.sh/$NTFY_TOPIC" || true
fi
if [ -n "$HEALTHCHECK_URL" ]; then
    curl -fsS -m 10 "$HEALTHCHECK_URL" -d "pending=$pending" || true
fi
echo "== nightly done =="
