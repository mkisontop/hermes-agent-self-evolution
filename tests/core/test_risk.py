"""Tests for risk-tier assessment (Batch B)."""
from __future__ import annotations

import pytest

from evolution.core.risk import (
    RiskTier,
    assess_risk,
    is_auto_merge_eligible,
    required_delta_for_tier,
)


@pytest.fixture(autouse=True)
def _clear_risk_env(monkeypatch):
    """Ensure no test inherits stale EVOLUTION_RISK_* env."""
    for key in list(monkeypatch.delenv.__globals__.get("os", __import__("os")).environ):
        if key.startswith("EVOLUTION_RISK_"):
            monkeypatch.delenv(key, raising=False)
    yield


def test_engine_skills_are_critical():
    assert assess_risk("hermes-self-evolution") == RiskTier.CRITICAL
    assert assess_risk("self-evolution") == RiskTier.CRITICAL
    assert assess_risk("evolution-engine") == RiskTier.CRITICAL


def test_default_is_medium():
    assert assess_risk("writing-plans") == RiskTier.MEDIUM
    assert assess_risk("github-code-review") == RiskTier.MEDIUM


def test_high_list_override(monkeypatch):
    monkeypatch.setenv("EVOLUTION_RISK_HIGH", "github-code-review,systematic-debugging")
    assert assess_risk("github-code-review") == RiskTier.HIGH
    assert assess_risk("systematic-debugging") == RiskTier.HIGH
    assert assess_risk("writing-plans") == RiskTier.MEDIUM


def test_low_list_override(monkeypatch):
    monkeypatch.setenv("EVOLUTION_RISK_LOW", "ascii-art,gif-search")
    assert assess_risk("ascii-art") == RiskTier.LOW
    assert assess_risk("gif-search") == RiskTier.LOW


def test_per_skill_override(monkeypatch):
    monkeypatch.setenv("EVOLUTION_RISK_OVERRIDE_WRITING_PLANS", "high")
    assert assess_risk("writing-plans") == RiskTier.HIGH


def test_critical_cannot_be_downgraded_via_override(monkeypatch):
    monkeypatch.setenv("EVOLUTION_RISK_OVERRIDE_HERMES_SELF_EVOLUTION", "low")
    # Hard-coded CRITICAL wins over env override.
    assert assess_risk("hermes-self-evolution") == RiskTier.CRITICAL


def test_auto_merge_eligibility():
    assert is_auto_merge_eligible(RiskTier.LOW)
    assert is_auto_merge_eligible(RiskTier.MEDIUM)
    assert is_auto_merge_eligible(RiskTier.HIGH)
    assert not is_auto_merge_eligible(RiskTier.CRITICAL)


def test_required_delta_scales_with_tier():
    base = 0.05
    assert required_delta_for_tier(RiskTier.LOW, base) == base
    assert required_delta_for_tier(RiskTier.MEDIUM, base) == base
    assert required_delta_for_tier(RiskTier.HIGH, base) == base * 2.0
    assert required_delta_for_tier(RiskTier.CRITICAL, base) == float("inf")


def test_risk_tier_parse_is_lenient():
    assert RiskTier.parse("LOW") == RiskTier.LOW
    assert RiskTier.parse(" high ") == RiskTier.HIGH
    assert RiskTier.parse("unknown") is None
    assert RiskTier.parse("") is None
