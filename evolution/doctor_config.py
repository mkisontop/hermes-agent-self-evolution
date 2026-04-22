"""Hermes Self-Evolution Doctor.

Prints the effective config with secrets redacted, verifies environment
plumbing, and optionally pings the task + judge models.

Usage:
    python -m evolution.doctor_config          # offline checks only
    python -m evolution.doctor_config --live   # adds LLM pong roundtrip

Exit codes:
    0  all checks passed
    1  one or more WARN/FAIL findings
    2  --live roundtrip failed
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import sys
from pathlib import Path

REDACT_VARS = {"OPENAI_API_KEY"}

TRACKED_VARS = [
    # endpoint + creds
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    # model split
    "MODEL",
    "TASK_MODEL",
    "EVOLUTION_TASK_MODEL",
    "EVOLUTION_OPTIMIZER_MODEL",
    "EVOLUTION_PROMPT_MODEL",
    "EVOLUTION_REFLECTION_MODEL",
    "EVOLUTION_EVAL_MODEL",
    "EVOLUTION_JUDGE_MODEL",
    # behavior
    "OPTIMIZER",
    "EVOLUTION_MIPRO_AUTO",
    "EVOLUTION_AUTO_OPTIMIZER",
    "EVOLUTION_FITNESS_MODE",
    "EVOLUTION_HOLDOUT_METRIC",
    "EVOLUTION_LM_NUM_RETRIES",
    "EVOLUTION_GEPA_MAX_METRIC_CALLS",
    "OPTIMIZER_TRACEBACK_MARGIN",
    # auto-merge gates
    "EVOLUTION_MIN_AUTO_DELTA",
    "EVOLUTION_JUDGE_SIGMA_OVERRIDE",
    "EVOLUTION_EXCLUDE_SKILLS",
    "EVOLUTION_ALLOW_SELF_TARGET",
]

EXPECTED_PKG_VERSIONS = {
    # Expected floor/ceiling. Drift is a warning, not a failure.
    # dspy 3.2.x pins litellm to 1.82.x; 1.82.6 is pre-compromise (safe).
    # Compromised litellm releases were 1.82.7 and 1.82.8 ONLY.
    "dspy": ("3.2.0", None),
    "litellm": ("1.82.6", None),
    "openai": ("1.0.0", None),
}

# Known-compromised package versions — FAIL the doctor if installed.
COMPROMISED_PKG_VERSIONS = {
    "litellm": {"1.82.7", "1.82.8"},
}

ROOT = Path(__file__).resolve().parent.parent


def _c(txt: str, color: str) -> str:
    if not sys.stdout.isatty():
        return txt
    codes = {"green": "32", "yellow": "33", "red": "31", "cyan": "36", "dim": "2"}
    return f"\033[{codes.get(color, '0')}m{txt}\033[0m"


def _ok(msg: str) -> None:
    print(f"  {_c('✓', 'green')} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_c('⚠', 'yellow')} {msg}")


def _fail(msg: str) -> None:
    print(f"  {_c('✗', 'red')} {msg}")


def _redact(name: str, val: str) -> str:
    if not val:
        return "<unset>"
    if name in REDACT_VARS:
        if len(val) <= 10:
            return "***"
        return f"{val[:6]}…{val[-4:]}"
    return val


def section(title: str) -> None:
    print(f"\n{_c('──', 'dim')} {_c(title, 'cyan')} {_c('─' * max(2, 60 - len(title)), 'dim')}")


def check_env(warnings: list) -> None:
    section("environment")
    for name in TRACKED_VARS:
        val = os.environ.get(name, "")
        rendered = _redact(name, val)
        critical = name in {"OPENAI_API_KEY", "OPENAI_API_BASE"}
        if critical and not val:
            _fail(f"{name:<36s} = <UNSET>")
            warnings.append(f"missing critical env var {name}")
        elif not val and name not in {"EVOLUTION_JUDGE_SIGMA_OVERRIDE", "EVOLUTION_AUTO_OPTIMIZER",
                                       "EVOLUTION_GEPA_MAX_METRIC_CALLS", "EVOLUTION_EXCLUDE_SKILLS",
                                       "EVOLUTION_ALLOW_SELF_TARGET"}:
            _warn(f"{name:<36s} = <unset>")
        else:
            _ok(f"{name:<36s} = {rendered}")


def check_routing(warnings: list) -> None:
    section("model routing (from EvolutionConfig)")
    try:
        from evolution.core.config import EvolutionConfig
    except ImportError as e:
        _fail(f"cannot import EvolutionConfig: {e}")
        warnings.append("EvolutionConfig import failed")
        return
    cfg = EvolutionConfig()
    _ok(f"optimizer_model = {cfg.optimizer_model}")
    _ok(f"eval_model      = {cfg.eval_model}")
    _ok(f"judge_model     = {cfg.judge_model}")

    if "gpt-5.4" in cfg.optimizer_model.lower():
        _fail("optimizer_model contains gpt-5.4 — must be codex-spark until cx-router proposer issue is isolated")
        warnings.append("optimizer routed to gpt-5.4")
    else:
        _ok("optimizer routed to codex-spark (judge-only policy enforced)")


def check_packages(warnings: list) -> None:
    section("packages")
    for pkg, (floor, ceiling) in EXPECTED_PKG_VERSIONS.items():
        try:
            v = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            _fail(f"{pkg}: not installed")
            warnings.append(f"package {pkg} missing")
            continue
        msg = f"{pkg}: {v}"
        if pkg in COMPROMISED_PKG_VERSIONS and v in COMPROMISED_PKG_VERSIONS[pkg]:
            _fail(f"{msg} — COMPROMISED RELEASE, upgrade immediately")
            warnings.append(f"compromised {pkg}={v}")
        elif floor and _ver(v) < _ver(floor):
            _warn(f"{msg} (below expected floor {floor})")
        elif ceiling and _ver(v) > _ver(ceiling):
            _warn(f"{msg} (above tested ceiling {ceiling})")
        else:
            _ok(msg)


def _ver(s: str) -> tuple:
    parts = []
    for x in s.split("."):
        try:
            parts.append(int(x.split("+")[0].split("-")[0]))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_lockfile(warnings: list) -> None:
    section("lockfile")
    lock = ROOT / "requirements.lock"
    if not lock.exists():
        _fail("requirements.lock missing — run `pip-compile --generate-hashes`")
        warnings.append("requirements.lock missing")
        return
    text = lock.read_text()
    n_lines = text.count("\n")
    has_hashes = "--hash=sha256:" in text
    _ok(f"requirements.lock exists ({n_lines} lines)")
    if has_hashes:
        _ok("hash-checking mode ready (--require-hashes)")
    else:
        _fail("requirements.lock has no --hash entries")
        warnings.append("lockfile missing hashes")


def check_self_block(warnings: list) -> None:
    section("autopilot self-target block")
    try:
        from usage.skill_usage_picker import DEFAULT_EXCLUDED_SKILLS, resolve_excluded_skills
    except ImportError:
        sys.path.insert(0, str(ROOT))
        try:
            from usage.skill_usage_picker import DEFAULT_EXCLUDED_SKILLS, resolve_excluded_skills
        except ImportError as e:
            _fail(f"cannot import picker: {e}")
            warnings.append("picker import failed")
            return
    excluded = resolve_excluded_skills()
    for name in ("hermes-self-evolution", "self-evolution", "evolution-engine"):
        if name in excluded:
            _ok(f"picker denylist includes {name!r}")
        else:
            _fail(f"picker denylist MISSING {name!r}")
            warnings.append(f"denylist missing {name}")

    # engine invariant
    evolve_skill = (ROOT / "evolution" / "skills" / "evolve_skill.py").read_text()
    if "EVOLUTION_ALLOW_SELF_TARGET" in evolve_skill and "_SELF_EVOLUTION_SKILLS" in evolve_skill:
        _ok("engine invariant: evolve_skill.py refuses self-targets without override")
    else:
        _fail("engine invariant missing in evolve_skill.py")
        warnings.append("engine invariant missing")

    if os.getenv("EVOLUTION_ALLOW_SELF_TARGET") == "1":
        _warn("EVOLUTION_ALLOW_SELF_TARGET=1 is set — self-evolution unlocked (manual only)")


def check_scheduling(warnings: list) -> None:
    section("scheduling / lock")
    nightly = ROOT / "nightly.sh"
    text = nightly.read_text()
    if "mkdir \"$LOCK_DIR\"" in text:
        _ok("nightly.sh uses mkdir-based single-instance lock (macOS-portable)")
    else:
        _fail("nightly.sh lock pattern missing")
        warnings.append("nightly lock missing")
    if "start_new_session=True" in text:
        _ok("nightly.sh uses process-group containment (start_new_session + killpg)")
    else:
        _warn("nightly.sh process-group containment not found")


def check_faulthandler(warnings: list) -> None:
    section("faulthandler wiring")
    text = (ROOT / "evolution" / "skills" / "evolve_skill.py").read_text()
    checks = [
        ("import faulthandler", "import"),
        ("faulthandler.dump_traceback_later(", "dump_traceback_later armed"),
        ("faulthandler.cancel_dump_traceback_later()", "cancel in finally"),
        ("OPTIMIZER_TRACEBACK_MARGIN", "margin env knob"),
    ]
    for needle, label in checks:
        if needle in text:
            _ok(label)
        else:
            _fail(f"missing: {label}")
            warnings.append(f"faulthandler: {label}")


def live_ping(warnings: list) -> None:
    section("live LLM roundtrip (--live)")
    try:
        import dspy
    except ImportError as e:
        _fail(f"dspy import failed: {e}")
        warnings.append("dspy import failed")
        return

    for role, env_key, default in [
        ("task", "EVOLUTION_TASK_MODEL", "openai/cx/gpt-5.3-codex-spark"),
        ("judge", "EVOLUTION_JUDGE_MODEL", "openai/cx/gpt-5.4"),
    ]:
        model = os.getenv(env_key, default)
        try:
            lm = dspy.LM(model, timeout=30, num_retries=0, max_tokens=20)
            out = lm("Reply with exactly: pong")
            reply = out[0] if isinstance(out, list) else str(out)
            short = reply.strip().replace("\n", " ")[:40]
            _ok(f"{role:<5} {model}  →  {short!r}")
        except Exception as e:
            _fail(f"{role:<5} {model}  →  {type(e).__name__}: {e}")
            warnings.append(f"live {role} roundtrip failed")


def judge_canary(skill_name: str) -> int:
    """Single-example judge canary — isolates judge-phase health.

    Runs exactly one real judge call on one holdout example using the
    actual judge prompt shape (skill + task_input + expected_behavior +
    fabricated agent_output), with EVOLUTION_JUDGE_* env overrides. Reports
    prompt size, elapsed wall-clock, and pass/fail. No proposals are written.

    This is the A-prime gate before re-running full MIPRO propose-1 under
    a judge holdout. If this hangs/times out, do not rerun MIPRO — fix the
    judge layer first (gateway, prompt shape, or fallback model).
    """
    import time
    from pathlib import Path

    section("judge-canary")

    try:
        from evolution.skills.skill_module import load_skill, find_skill
        from evolution.core.config import get_hermes_agent_path
        from evolution.core.fitness import LLMJudge
        from evolution.core.lm_factory import make_lm
    except Exception as e:
        _fail(f"import failure: {type(e).__name__}: {e}")
        return 1

    hermes_agent_path = get_hermes_agent_path()
    skill_path = find_skill(skill_name, hermes_agent_path)
    if skill_path is None:
        _fail(f"skill not found: {skill_name}")
        return 1
    skill = load_skill(skill_path)
    skill_body = skill["body"]

    task_input = (
        "We need a plan to add email/password authentication to our FastAPI "
        "app. Current repo has `src/api/`, `src/models/`, `src/services/`, "
        "and `tests/`. Requirements: users can sign up, log in, and access a "
        "protected `/me` endpoint. Use JWTs, bcrypt password hashing, and "
        "SQLite for local dev. Please create an implementation plan."
    )
    expected_behavior = (
        "Produces a full implementation plan document with header format, "
        "goal, architecture, tech stack; bite-sized tasks; TDD cycles; "
        "commit commands; exact file paths."
    )
    agent_output = (
        "## Goal\nAdd email/password auth.\n\n## Task 1 — Model\n"
        "Create `src/models/user.py` with email, hashed_password, created_at.\n"
        "Run pytest tests/test_user.py -q. Commit.\n\n## Task 2 — Hashing\n"
        "Add bcrypt utility. Test round-trip. Commit."
    )

    judge_model = os.getenv("EVOLUTION_JUDGE_MODEL", "openai/cx/gpt-5.4")
    judge_timeout = int(os.getenv("EVOLUTION_JUDGE_TIMEOUT", "360"))
    judge_retries = int(os.getenv("EVOLUTION_JUDGE_RETRIES", "0"))
    judge_max_tokens = int(os.getenv("EVOLUTION_JUDGE_MAX_TOKENS", "1024"))

    prompt_chars = (
        len(skill_body) + len(task_input)
        + len(expected_behavior) + len(agent_output)
    )
    print(f"  skill:          {skill_name}")
    print(f"  model:          {judge_model}")
    print(f"  timeout:        {judge_timeout}")
    print(f"  retries:        {judge_retries}")
    print(f"  max_tokens:     {judge_max_tokens}")
    print(f"  prompt_chars:   {prompt_chars}  (skill={len(skill_body)} "
          f"input={len(task_input)} expected={len(expected_behavior)} "
          f"output={len(agent_output)})")

    judge = LLMJudge(model=judge_model)

    t0 = time.time()
    try:
        score = judge.score(
            task_input=task_input,
            expected_behavior=expected_behavior,
            agent_output=agent_output,
            skill_text=skill_body,
        )
        elapsed = time.time() - t0
        print(f"  elapsed:        {elapsed:.1f}s")
        # Detect the fitness.py fallback-neutral path (feedback starts with
        # "[judge error, fallback neutral]"). That means the judge call
        # raised — a real failure even though score() returned gracefully.
        if str(score.feedback).startswith("[judge error"):
            _fail(f"judge fallback triggered: {score.feedback[:160]}")
            return 1
        print(f"  correctness:    {score.correctness:.2f}")
        print(f"  procedure:      {score.procedure_following:.2f}")
        print(f"  conciseness:    {score.conciseness:.2f}")
        print(_c("  result:         PASS", "green"))
        return 0
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  elapsed:        {elapsed:.1f}s (failed)")
        _fail(f"judge canary failed: {type(e).__name__}: {str(e)[:200]}")
        return 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="Ping task + judge models")
    ap.add_argument(
        "--judge-canary",
        metavar="SKILL",
        default=None,
        help="Run a single-example judge canary on SKILL (no proposal writes)",
    )
    args = ap.parse_args()

    if args.judge_canary:
        print(_c("Hermes Self-Evolution Doctor — Judge Canary", "cyan"))
        return judge_canary(args.judge_canary)

    print(_c("Hermes Self-Evolution Doctor", "cyan"))
    print(_c(f"  root: {ROOT}", "dim"))

    warnings: list[str] = []
    check_env(warnings)
    check_routing(warnings)
    check_packages(warnings)
    check_lockfile(warnings)
    check_self_block(warnings)
    check_scheduling(warnings)
    check_faulthandler(warnings)

    live_failed = False
    if args.live:
        pre = len(warnings)
        live_ping(warnings)
        live_failed = len(warnings) > pre

    section("summary")
    if not warnings:
        print(_c("  ALL GREEN — engine ready", "green"))
        return 0
    for w in warnings:
        print(f"  - {w}")
    if live_failed:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
