"""Tests for fitness metrics and GEPA feedback wrappers."""

import dspy
import pytest

from evolution.core.fitness import (
    FitnessScore,
    get_skill_fitness_metric,
    set_global_judge,
    skill_fitness_metric_gepa,
)


class _FakeJudge:
    def __init__(self, score: FitnessScore):
        self._score = score

    def score(self, **kwargs) -> FitnessScore:
        return self._score


@pytest.fixture(autouse=True)
def _reset_metric_env(monkeypatch):
    monkeypatch.delenv("EVOLUTION_FITNESS_MODE", raising=False)
    monkeypatch.delenv("EVOLUTION_JUDGE_RATIO", raising=False)
    set_global_judge(None)
    yield
    set_global_judge(None)


def _example() -> dspy.Example:
    return dspy.Example(
        task_input="Write a deployment plan with rollback steps and test coverage.",
        expected_behavior="Include deployment steps, rollback instructions, and test coverage notes.",
        skill_text="# Procedure\n1. Deploy\n2. Verify\n3. Roll back if needed",
    )


def test_gepa_feedback_includes_missing_topics_in_fast_mode():
    example = _example()
    prediction = dspy.Prediction(output="Provide the deployment steps.")

    result = skill_fitness_metric_gepa(example, prediction)

    assert 0.0 <= result.score <= 1.0
    assert "Missing expected topics:" in result.feedback
    assert "rollback" in result.feedback
    assert "coverage" in result.feedback


def test_gepa_feedback_uses_judge_rubric_when_requested(monkeypatch):
    monkeypatch.setenv("EVOLUTION_FITNESS_MODE", "judge")
    set_global_judge(
        _FakeJudge(
            FitnessScore(
                correctness=0.9,
                procedure_following=0.8,
                conciseness=0.7,
                feedback="Add a more explicit rollback checklist.",
            )
        )
    )
    example = _example()
    prediction = dspy.Prediction(output="Deploy, verify, and document rollback.")

    result = skill_fitness_metric_gepa(example, prediction)

    assert result.score == pytest.approx(0.83, abs=1e-9)
    assert "Correctness=0.900" in result.feedback
    assert "Procedure=0.800" in result.feedback
    assert "Add a more explicit rollback checklist." in result.feedback


def test_get_skill_fitness_metric_rejects_unknown_mode():
    with pytest.raises(ValueError):
        get_skill_fitness_metric("unknown")
