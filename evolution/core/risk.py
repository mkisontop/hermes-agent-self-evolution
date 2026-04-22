"""Risk tier assessment — classifies proposals by blast-radius sensitivity.

Batch B (2026-04-22). Every proposal gets a risk tier that gates what the
auto-merge pipeline is allowed to do:

    LOW       — low-blast skills (informational/reference), auto-merge eligible
    MEDIUM    — default tier for most skills, auto-merge eligible under gate
    HIGH      — skills that touch production behavior or security-sensitive
                flows; auto-merge only with elevated delta threshold
    CRITICAL  — skills that are part of the evolution engine itself, or
                explicitly marked; **never** auto-merge, manual review only

The engine's own skills (``hermes-self-evolution``, ``self-evolution``,
``evolution-engine``) are hard-coded CRITICAL. Batch A already hard-blocks
the engine from being evolved at all; this is defense-in-depth so that if
the hard-block is ever bypassed, the approve path still refuses auto-merge.

Environment overrides:

    EVOLUTION_RISK_HIGH    — comma-separated skill names forced to HIGH
    EVOLUTION_RISK_LOW     — comma-separated skill names forced to LOW
    EVOLUTION_RISK_OVERRIDE_{SKILL}  — per-skill override (tier name)

CRITICAL cannot be overridden to a lower tier via env.
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Optional


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def parse(cls, s: str) -> Optional["RiskTier"]:
        s = (s or "").strip().lower()
        for t in cls:
            if t.value == s:
                return t
        return None


# Skills that are part of the evolution engine itself. Hard-coded CRITICAL.
_CRITICAL_SKILLS = frozenset({
    "hermes-self-evolution",
    "self-evolution",
    "evolution-engine",
})


def _csv_env(name: str) -> frozenset[str]:
    raw = os.getenv(name, "")
    return frozenset(
        s.strip() for s in raw.split(",") if s.strip()
    )


def assess_risk(skill_name: str) -> RiskTier:
    """Compute the risk tier for a skill.

    Order of resolution:
      1. Hard-coded CRITICAL list (cannot be downgraded)
      2. Per-skill env override (``EVOLUTION_RISK_OVERRIDE_<SKILL>``)
      3. HIGH/LOW env lists
      4. Default MEDIUM
    """
    # 1. Hard-coded CRITICAL — immutable.
    if skill_name in _CRITICAL_SKILLS:
        return RiskTier.CRITICAL

    # 2. Per-skill override. Slashes/dashes → underscores for env-safe keys.
    env_key = f"EVOLUTION_RISK_OVERRIDE_{skill_name.upper().replace('-', '_').replace('/', '_')}"
    raw_override = os.getenv(env_key)
    if raw_override:
        parsed = RiskTier.parse(raw_override)
        if parsed is not None:
            # Override cannot escalate to CRITICAL via env (only hard-coded list).
            if parsed == RiskTier.CRITICAL:
                return RiskTier.CRITICAL
            return parsed

    # 3. List-based overrides.
    if skill_name in _csv_env("EVOLUTION_RISK_HIGH"):
        return RiskTier.HIGH
    if skill_name in _csv_env("EVOLUTION_RISK_LOW"):
        return RiskTier.LOW

    # 4. Default.
    return RiskTier.MEDIUM


def is_auto_merge_eligible(tier: RiskTier) -> bool:
    """True if tier permits auto-merge at all. CRITICAL is never eligible."""
    return tier != RiskTier.CRITICAL


def required_delta_for_tier(tier: RiskTier, base_delta: float) -> float:
    """Per-tier multiplier on the base auto-merge delta threshold.

    - LOW       — base delta (cheapest to approve)
    - MEDIUM    — base delta (default)
    - HIGH      — 2× base delta (stricter bar)
    - CRITICAL  — infinity (never auto-merge; included for completeness)
    """
    if tier == RiskTier.CRITICAL:
        return float("inf")
    if tier == RiskTier.HIGH:
        return max(base_delta, base_delta * 2.0)
    return base_delta
