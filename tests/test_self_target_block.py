"""Batch A — self-target block (picker denylist + engine invariant)."""
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def test_picker_default_excludes_self_evolution():
    sys.path.insert(0, str(ROOT))
    from usage.skill_usage_picker import DEFAULT_EXCLUDED_SKILLS

    assert "hermes-self-evolution" in DEFAULT_EXCLUDED_SKILLS
    assert "self-evolution" in DEFAULT_EXCLUDED_SKILLS
    assert "evolution-engine" in DEFAULT_EXCLUDED_SKILLS


def test_picker_resolve_excluded_merges_env(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from usage.skill_usage_picker import resolve_excluded_skills

    monkeypatch.setenv("EVOLUTION_EXCLUDE_SKILLS", "custom-skill, another-skill")
    excluded = resolve_excluded_skills()
    assert "hermes-self-evolution" in excluded
    assert "custom-skill" in excluded
    assert "another-skill" in excluded


def test_picker_rank_filters_excluded():
    sys.path.insert(0, str(ROOT))
    from usage.skill_usage_picker import rank

    records = [
        {"skill_name": "writing-plans", "session_id": "s1"},
        {"skill_name": "hermes-self-evolution", "session_id": "s1"},
        {"skill_name": "hermes-self-evolution", "session_id": "s2"},
        {"skill_name": "hermes-self-evolution", "session_id": "s3"},
    ]
    ranked, _, _, _ = rank(
        records, "hybrid", 1, excluded={"hermes-self-evolution"}
    )
    assert "hermes-self-evolution" not in ranked
    assert "writing-plans" in ranked


def test_engine_refuses_self_target(monkeypatch, tmp_path):
    """Engine invariant: evolve_skill CLI rejects self-evolution skills."""
    monkeypatch.delenv("EVOLUTION_ALLOW_SELF_TARGET", raising=False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "evolution.skills.evolve_skill",
            "--skill",
            "hermes-self-evolution",
            "--dry-run",
            "--iterations",
            "1",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, f"expected exit 2, got {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "Refusing to evolve" in result.stdout or "Refusing to evolve" in result.stderr


def test_engine_allows_self_target_with_override(monkeypatch, tmp_path):
    """With EVOLUTION_ALLOW_SELF_TARGET=1 the engine proceeds (may fail later)."""
    env = {
        **{k: v for k, v in __import__("os").environ.items()},
        "EVOLUTION_ALLOW_SELF_TARGET": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "evolution.skills.evolve_skill",
            "--skill",
            "hermes-self-evolution",
            "--dry-run",
            "--iterations",
            "1",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Expectation: no longer blocked at layer 2. May still exit 1 because the
    # skill doesn't exist on disk under hermes-agent/skills/, but the refusal
    # message must NOT appear.
    assert "Refusing to evolve" not in result.stdout
    assert "Refusing to evolve" not in result.stderr
