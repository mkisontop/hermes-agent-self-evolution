"""LM factory with role-specific env-driven configuration.

Batch A-prime (2026-04-22): split LM construction into role buckets so the
judge phase can have a longer timeout and zero retries without affecting the
optimizer/task loops. Same factory is used by evolve_skill.py and fitness.py
to keep judge configuration authoritative in one place.

Env var convention:
    EVOLUTION_{ROLE}_TIMEOUT        # per-request timeout (seconds)
    EVOLUTION_{ROLE}_RETRIES        # num_retries
    EVOLUTION_{ROLE}_MAX_TOKENS     # max_tokens (where applicable)
    EVOLUTION_{ROLE}_NUM_THREADS    # eval concurrency (consumed by caller)

Fallbacks cascade:
    role-specific → EVOLUTION_LM_* → hard default

Roles:
    task      — the skill being optimized (baseline / evolved) under eval
    optimizer — prompt_model / reflection_model for MIPRO / GEPA
    judge     — LLM-as-judge holdout scoring
"""

from __future__ import annotations

import os
from typing import Optional

import dspy


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    """Read an int env var with a safe fallback. Empty → default."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_DEFAULTS = {
    "task": {"timeout": 120, "retries": 0, "max_tokens": 2048},
    "optimizer": {"timeout": 120, "retries": 0, "max_tokens": 2048},
    "judge": {"timeout": 360, "retries": 0, "max_tokens": 1024},
    "eval": {"timeout": 360, "retries": 0, "max_tokens": 1024},  # alias for judge
}


def _resolve_int(role: str, knob: str, hard_default: int) -> int:
    """Resolve ``EVOLUTION_{ROLE}_{KNOB}`` with fallback to ``EVOLUTION_LM_{KNOB}``."""
    ru = role.upper()
    ku = knob.upper()
    role_val = _env_int(f"EVOLUTION_{ru}_{ku}")
    if role_val is not None:
        return role_val
    global_val = _env_int(f"EVOLUTION_LM_{ku}")
    if global_val is not None:
        return global_val
    return hard_default


def make_lm(model: str, *, role: str) -> dspy.LM:
    """Build a dspy.LM configured for a specific role.

    Args:
        model: model id (e.g. ``openai/cx/gpt-5.4``).
        role: one of ``task``, ``optimizer``, ``judge`` (``eval`` aliases ``judge``).

    Returns:
        dspy.LM with per-request timeout / retries / max_tokens / cache settings
        tuned for that role.
    """
    role_norm = role.lower().strip()
    if role_norm == "eval":
        role_norm = "judge"
    defaults = _DEFAULTS.get(role_norm, _DEFAULTS["task"])

    timeout = _resolve_int(role_norm, "TIMEOUT", defaults["timeout"])
    retries = _resolve_int(role_norm, "RETRIES", defaults["retries"])
    max_tokens = _resolve_int(role_norm, "MAX_TOKENS", defaults["max_tokens"])

    kwargs = dict(
        model=model,
        timeout=max(1, timeout),
        num_retries=max(0, retries),
        max_tokens=max(64, max_tokens),
        cache=False,
    )

    api_base = os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")
    if api_base:
        kwargs["api_base"] = api_base
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        kwargs["api_key"] = api_key

    return dspy.LM(**kwargs)


def judge_num_threads() -> int:
    """Concurrency for the holdout judge evaluator. Default 1 (serial).

    Parallel judge calls can wedge the local gateway; 1 is the safe canary
    default. Raise only after a single judge call has been proven healthy.
    """
    return max(1, _resolve_int("judge", "NUM_THREADS", 1))


def judge_phase_timeout() -> int:
    """Wall-clock cap on the whole holdout judge phase (baseline + evolved)."""
    return max(60, _resolve_int("judge", "PHASE_TIMEOUT", 900))
