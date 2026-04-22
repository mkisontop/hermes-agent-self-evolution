#!/bin/bash
# Hermes self-evolution nightly runner
# Targets: github-code-review skill
# LLM: cx/gpt-5.4 via local endpoint http://localhost:20128/v1

set -euo pipefail

cd "$(dirname "$0")"

# Local OpenAI-compatible endpoint
# Load .env (gitignored) for API key + base URL + model defaults
_SELF_EVO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$_SELF_EVO_ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "$_SELF_EVO_ROOT/.env"; set +a
fi
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://localhost:20128/v1}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://localhost:20128/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:?OPENAI_API_KEY not set (put it in $_SELF_EVO_ROOT/.env)}"

# Activate venv
# shellcheck disable=SC1091
source venv/bin/activate

SKILL="${SKILL:-}"
if [ -z "$SKILL" ]; then
    # No explicit skill — fall back to the top usage pick so this script
    # works the same way nightly.sh does. Manual overrides still work
    # via `SKILL=foo bash run_evolution.sh`.
    SKILL="$(python3 "$HOME/.hermes/self-evolution/usage/skill_usage_picker.py" \
        --top 1 --days 7 --json-only 2>/dev/null \
        | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["top"][0]["skill_name"] if d.get("top") else "")')"
    SKILL="${SKILL:-github-code-review}"
fi
ITERATIONS="${ITERATIONS:-10}"
MODEL="${MODEL:-${EVOLUTION_OPTIMIZER_MODEL:-openai/cx/gpt-5.3-codex-spark}}"
TASK_MODEL="${TASK_MODEL:-${EVOLUTION_TASK_MODEL:-openai/cx/gpt-5.3-codex-spark}}"
OPTIMIZER="${OPTIMIZER:-auto}"
OPTIMIZER_TIMEOUT="${OPTIMIZER_TIMEOUT:-900}"
RUN_TIMEOUT="${RUN_TIMEOUT:-1800}"
export EVOLUTION_FITNESS_MODE="${EVOLUTION_FITNESS_MODE:-fast}"
export EVOLUTION_HOLDOUT_METRIC="${EVOLUTION_HOLDOUT_METRIC:-judge}"
export EVOLUTION_MIPRO_AUTO="${EVOLUTION_MIPRO_AUTO:-manual}"

LOG_DIR="$HOME/.hermes/self-evolution/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/evolve-${SKILL}-${STAMP}.log"

echo "[$(date -Iseconds)] Starting evolution: skill=$SKILL iters=$ITERATIONS model=$MODEL task_model=$TASK_MODEL optimizer=$OPTIMIZER timeout=${OPTIMIZER_TIMEOUT}s run_timeout=${RUN_TIMEOUT}s" | tee -a "$LOG"

python3 - "$SKILL" "$ITERATIONS" "$MODEL" "$TASK_MODEL" "$OPTIMIZER" "$OPTIMIZER_TIMEOUT" "$RUN_TIMEOUT" <<'PY' 2>&1 | tee -a "$LOG"
import subprocess
import sys

skill, iterations, model, task_model, optimizer, optimizer_timeout, run_timeout = sys.argv[1:]
cmd = [
    sys.executable,
    "-m",
    "evolution.skills.evolve_skill",
    "--skill", skill,
    "--iterations", iterations,
    "--optimizer-model", model,
    "--eval-model", model,
    "--task-model", task_model,
    "--optimizer", optimizer,
    "--optimizer-timeout", optimizer_timeout,
]
try:
    completed = subprocess.run(cmd, timeout=int(run_timeout), check=False)
    raise SystemExit(completed.returncode)
except subprocess.TimeoutExpired:
    print(f"[run_evolution wrapper] timed out after {run_timeout}s", file=sys.stderr)
    raise SystemExit(124)
PY

echo "[$(date -Iseconds)] Evolution run complete. Log: $LOG" | tee -a "$LOG"
