#!/bin/bash
# Hermes self-evolution nightly pipeline
#
# Phases:
#   1. Preflight smoke (t1 baseline + t5 propose-mode structural; zero-token, ~10s)
#   2. Evolve skill in propose-mode (safe: no auto-merge to bundled skills)
#   3. Build markdown digest of last 24h of activity
#
# Defaults are safe. Override via env vars:
#   SKILL=github-code-review
#   ITERATIONS=10
#   MODE=propose           (propose | auto)
#   SKIP_SMOKE=0           (1 to bypass preflight)
#   SKIP_EVOLVE=0          (1 to only build digest; default is 1 under cron/no-TTY unless ALLOW_EVOLVE=1)
#   WINDOW_HOURS=24
#   DELIVER=0              (1 to send digest via send_message; requires HERMES_CLI)

set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

# ─── single-instance lock ─────────────────────────────────────────────────────
# Prevent overlapping nightly runs (cron racing with a manual run, e.g.).
# macOS ships without flock(1), so we use mkdir: it's atomic on any POSIX
# filesystem. If another nightly holds the lock we exit 0 so cron doesn't
# record a flap. Stale lock detection: PID in lockdir; if PID is gone, take over.
LOCK_DIR="$ROOT/.nightly.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    LOCK_PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo)"
    if [[ -n "$LOCK_PID" ]] && kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[$(date -Iseconds)] Another Hermes self-evolution nightly (pid $LOCK_PID) is active; exiting."
        exit 0
    fi
    echo "[$(date -Iseconds)] Stale nightly lock (pid $LOCK_PID dead); taking over."
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" || { echo "[$(date -Iseconds)] Failed to acquire lock after stale cleanup"; exit 0; }
fi
echo $$ >"$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

# ─── environment ──────────────────────────────────────────────────────────────
export HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-$HOME/.hermes}"

# Load local credentials + model defaults if present. Gitignored.
if [[ -f "$ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "$ROOT/.env"; set +a
fi

export OPENAI_API_BASE="${OPENAI_API_BASE:-http://localhost:20128/v1}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://localhost:20128/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:?OPENAI_API_KEY not set (put it in $ROOT/.env)}"

# shellcheck disable=SC1091
source venv/bin/activate

# SKILL is resolved dynamically below from the usage picker (top 3 over rolling 7d).
# Override by setting SKILL=<name> (single) or SKILLS="a b c" (explicit list).
SKILL="${SKILL:-}"
SKILLS="${SKILLS:-}"
TOP_N="${TOP_N:-3}"
USAGE_DAYS="${USAGE_DAYS:-7}"
ITERATIONS="${ITERATIONS:-10}"
MODEL="${MODEL:-${EVOLUTION_OPTIMIZER_MODEL:-openai/cx/gpt-5.3-codex-spark}}"
TASK_MODEL="${TASK_MODEL:-${EVOLUTION_TASK_MODEL:-openai/cx/gpt-5.3-codex-spark}}"
# Eval / judge model is intentionally split from MODEL so the optimizer
# (codex-spark) and the judge (gpt-5.4) can differ per Batch A model policy.
EVAL_MODEL="${EVAL_MODEL:-${EVOLUTION_EVAL_MODEL:-${EVOLUTION_JUDGE_MODEL:-openai/cx/gpt-5.4}}}"
MODE="${MODE:-propose}"
OPTIMIZER="${OPTIMIZER:-auto}"
OPTIMIZER_TIMEOUT="${OPTIMIZER_TIMEOUT:-900}"
RUN_TIMEOUT="${RUN_TIMEOUT:-1800}"
WINDOW_HOURS="${WINDOW_HOURS:-24}"
SKIP_SMOKE="${SKIP_SMOKE:-0}"
# Cron-safe default: if stdout is not a TTY (i.e. running under cron/automation)
# and the operator has not explicitly opted in via ALLOW_EVOLVE=1, skip phase 2.
# This prevents the known GEPA hang (2026-04-21) from leaving a stuck process
# in the 07:00 scheduled run. Interactive manual runs keep full behavior.
# Explicit SKIP_EVOLVE=0 or SKIP_EVOLVE=1 always wins.
if [[ -z "${SKIP_EVOLVE:-}" ]]; then
    if [[ ! -t 1 ]] && [[ "${ALLOW_EVOLVE:-0}" != "1" ]]; then
        SKIP_EVOLVE=1
    else
        SKIP_EVOLVE=0
    fi
fi
DELIVER="${DELIVER:-0}"
export EVOLUTION_FITNESS_MODE="${EVOLUTION_FITNESS_MODE:-fast}"
export EVOLUTION_HOLDOUT_METRIC="${EVOLUTION_HOLDOUT_METRIC:-judge}"
export EVOLUTION_MIPRO_AUTO="${EVOLUTION_MIPRO_AUTO:-manual}"

STAMP="$(date +%Y%m%d-%H%M%S)"
DATE_DIR="$(date +%Y-%m-%d)"

LOG_DIR="${ROOT}/logs"
DIGEST_DIR="${ROOT}/logs/digests"
SMOKE_DIR="${ROOT}/logs/smoke"
mkdir -p "$LOG_DIR" "$DIGEST_DIR" "$SMOKE_DIR"

NIGHTLY_LOG="${LOG_DIR}/nightly-${STAMP}.log"
SMOKE_LOG="${SMOKE_DIR}/smoke-${STAMP}.log"
DIGEST_FILE="${DIGEST_DIR}/${DATE_DIR}.md"

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$NIGHTLY_LOG"
}

fail() {
    log "FAIL: $*"
    log "Nightly aborted. See $NIGHTLY_LOG"
    exit 1
}

log "=== Hermes self-evolution nightly ==="

# ─── resolve skill list ───────────────────────────────────────────────────────
# Priority: explicit SKILL > explicit SKILLS > top-N from usage picker
if [[ -n "$SKILL" ]]; then
    SKILL_LIST=("$SKILL")
    log "skill list: explicit SKILL=$SKILL"
elif [[ -n "$SKILLS" ]]; then
    # shellcheck disable=SC2206
    SKILL_LIST=($SKILLS)
    log "skill list: explicit SKILLS=($SKILLS)"
else
    PICKER="${ROOT}/usage/skill_usage_picker.py"
    if [[ ! -f "$PICKER" ]]; then
        fail "usage picker not found: $PICKER"
    fi
    # Picker prints a human table + a JSON block. Extract top-N skill names from JSON.
    PICKED="$(python3 "$PICKER" --top "$TOP_N" --days "$USAGE_DAYS" --json-only 2>>"$NIGHTLY_LOG" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(" ".join(s["skill_name"] for s in d["top"]))')"
    if [[ -z "$PICKED" ]]; then
        fail "usage picker returned no skills (no usage data in last ${USAGE_DAYS}d?)"
    fi
    # shellcheck disable=SC2206
    SKILL_LIST=($PICKED)
    log "skill list: top-${TOP_N}/${USAGE_DAYS}d from usage picker → ${SKILL_LIST[*]}"
fi

log "iters=$ITERATIONS mode=$MODE model=$MODEL task_model=$TASK_MODEL eval_model=$EVAL_MODEL optimizer=$OPTIMIZER timeout=${OPTIMIZER_TIMEOUT}s run_timeout=${RUN_TIMEOUT}s window=${WINDOW_HOURS}h"
log "fitness_mode=$EVOLUTION_FITNESS_MODE holdout_metric=$EVOLUTION_HOLDOUT_METRIC mipro_auto=$EVOLUTION_MIPRO_AUTO"
log "nightly log: $NIGHTLY_LOG"

# ─── phase 1: smoke preflight ─────────────────────────────────────────────────
# Runs T1 (dry-run on all top-5 skills) + T5 (propose-mode structural dry-run).
# Both are zero-token — total ~10s. Use `bash smoke_test.sh full` weekly for the
# expensive T2/T3/T4 tiers that hit the LLM.
if [[ "$SKIP_SMOKE" == "1" ]]; then
    log "Phase 1 (smoke) skipped via SKIP_SMOKE=1"
else
    log "Phase 1: smoke preflight (t1 + t5, zero-token)"
    if ! bash smoke_test.sh t1 >>"$SMOKE_LOG" 2>&1; then
        fail "smoke t1 failed — see $SMOKE_LOG"
    fi
    log "  t1 OK"
    if ! bash smoke_test.sh t5 >>"$SMOKE_LOG" 2>&1; then
        fail "smoke t5 failed — see $SMOKE_LOG"
    fi
    log "  t5 OK"
fi

# ─── phase 2: evolve ──────────────────────────────────────────────────────────
EVOLVE_EXIT=0
EVOLVE_RESULTS=()
if [[ "$SKIP_EVOLVE" == "1" ]]; then
    log "Phase 2 (evolve) skipped via SKIP_EVOLVE=1"
else
    for CURRENT_SKILL in "${SKILL_LIST[@]}"; do
        # Sanitize skill name for filename (slashes → dashes)
        SAFE_NAME="${CURRENT_SKILL//\//-}"
        EVOLVE_LOG="${LOG_DIR}/evolve-${SAFE_NAME}-${STAMP}.log"
        log "Phase 2: evolve skill=$CURRENT_SKILL mode=$MODE → $EVOLVE_LOG"
        set +e
        python3 - "$CURRENT_SKILL" "$ITERATIONS" "$MODEL" "$TASK_MODEL" "$EVAL_MODEL" "$MODE" "$OPTIMIZER" "$OPTIMIZER_TIMEOUT" "$RUN_TIMEOUT" >>"$EVOLVE_LOG" 2>&1 <<'PY'
import os
import signal
import subprocess
import sys

skill, iterations, model, task_model, eval_model, mode, optimizer, optimizer_timeout, run_timeout = sys.argv[1:]
run_timeout_s = int(run_timeout)
cmd = [
    sys.executable,
    "-m",
    "evolution.skills.evolve_skill",
    "--skill", skill,
    "--iterations", iterations,
    "--optimizer-model", model,
    "--eval-model", eval_model,
    "--task-model", task_model,
    "--mode", mode,
    "--optimizer", optimizer,
    "--optimizer-timeout", optimizer_timeout,
]

# start_new_session=True puts the child in its own process group so we can
# kill *all* its descendants (litellm → httpx → anyio workers, etc.) on
# timeout. Without this, grandchildren can survive SIGTERM to the parent
# and leave the nightly with orphan optimizer processes holding sockets.
proc = subprocess.Popen(cmd, start_new_session=True)
pgid = os.getpgid(proc.pid)
try:
    rc = proc.wait(timeout=run_timeout_s)
    raise SystemExit(rc)
except subprocess.TimeoutExpired:
    print(
        f"[nightly wrapper] timed out after {run_timeout_s}s; SIGTERM process group {pgid}",
        file=sys.stderr,
        flush=True,
    )
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        raise SystemExit(124)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        print(
            f"[nightly wrapper] SIGTERM ignored; SIGKILL process group {pgid}",
            file=sys.stderr,
            flush=True,
        )
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    raise SystemExit(124)
PY
        RC=$?
        set -e
        if [[ "$RC" -ne 0 ]]; then
            log "  evolve[$CURRENT_SKILL] exited non-zero ($RC) — continuing"
            EVOLVE_EXIT=$RC
            EVOLVE_RESULTS+=("${CURRENT_SKILL}=fail($RC)")
        else
            log "  evolve[$CURRENT_SKILL] OK"
            EVOLVE_RESULTS+=("${CURRENT_SKILL}=ok")
        fi
    done
fi

# ─── phase 3: digest ──────────────────────────────────────────────────────────
log "Phase 3: build digest (window=${WINDOW_HOURS}h → $DIGEST_FILE)"
set +e
python -m evolution.review.digest \
    --hours "$WINDOW_HOURS" \
    --output "$DIGEST_FILE" \
    --format markdown \
    >>"$NIGHTLY_LOG" 2>&1
DIGEST_EXIT=$?
set -e
if [[ "$DIGEST_EXIT" -ne 0 ]]; then
    log "  digest failed (exit $DIGEST_EXIT) — see $NIGHTLY_LOG"
else
    log "  digest OK — $DIGEST_FILE"
fi

# ─── phase 4: delivery (optional) ─────────────────────────────────────────────
# Delivery is handled externally — cron can chain `send-message` via hermes-agent
# or a separate skill. We just produce a file; delivery is not our concern.
# Set DELIVER_CMD to a shell command that reads $DIGEST_FILE if you want inline delivery.
if [[ "$DELIVER" == "1" ]] && [[ -f "$DIGEST_FILE" ]] && [[ -n "${DELIVER_CMD:-}" ]]; then
    log "Phase 4: running DELIVER_CMD"
    set +e
    DIGEST_FILE="$DIGEST_FILE" bash -c "$DELIVER_CMD" >>"$NIGHTLY_LOG" 2>&1 || \
        log "  delivery failed (non-fatal)"
    set -e
fi

log "=== nightly complete ==="
log "evolve results: ${EVOLVE_RESULTS[*]:-none}"
log "evolve exit: $EVOLVE_EXIT | digest exit: $DIGEST_EXIT"
log "digest:  $DIGEST_FILE"
log "smoke:   $SMOKE_LOG"

# Overall exit: only fail if digest failed (evolve non-zero is not a nightly failure —
# it means the run rejected changes, which is expected in propose-mode).
exit "$DIGEST_EXIT"
