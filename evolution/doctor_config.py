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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="Ping task + judge models")
    args = ap.parse_args()

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
