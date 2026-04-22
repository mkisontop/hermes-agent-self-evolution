"""Tests for optimizer routing helpers in evolve_skill."""

from evolution.skills.evolve_skill import (
    _build_optimizer_attempt_order,
    _get_env_int,
    _resolve_optimizer_name,
)


def test_auto_routes_fast_metric_to_miprov2():
    assert _resolve_optimizer_name("auto", "fast") == "miprov2"


def test_auto_routes_judge_metric_to_miprov2_by_default(monkeypatch):
    # Batch A policy: auto → MIPROv2 while GEPA is under investigation.
    # Old GEPA-on-judge routing was intentionally reverted.
    monkeypatch.delenv("EVOLUTION_AUTO_OPTIMIZER", raising=False)
    assert _resolve_optimizer_name("auto", "judge") == "miprov2"


def test_auto_respects_explicit_gepa_override(monkeypatch):
    monkeypatch.setenv("EVOLUTION_AUTO_OPTIMIZER", "gepa")
    assert _resolve_optimizer_name("auto", "judge") == "gepa"


def test_gepa_attempt_order_falls_back_to_miprov2():
    assert _build_optimizer_attempt_order("gepa") == ["gepa", "miprov2"]


def test_get_env_int_uses_default_for_invalid_value(monkeypatch):
    monkeypatch.setenv("EVOLUTION_OPTIMIZER_TIMEOUT", "not-an-int")
    assert _get_env_int("EVOLUTION_OPTIMIZER_TIMEOUT", 900) == 900
