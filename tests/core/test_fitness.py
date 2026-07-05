"""Tests for fitness metric — GEPA compatibility, scoring, score parsing."""

import dspy
import pytest

from evolution.core.fitness import FitnessScore, skill_fitness_metric, _parse_score


def _example(expected="Review the pull request diff and flag security issues"):
    return dspy.Example(
        task_input="Please review my PR",
        expected_behavior=expected,
    ).with_inputs("task_input")


def _prediction(output):
    return dspy.Prediction(output=output)


class TestSkillFitnessMetric:
    def test_evaluate_style_call_returns_float(self):
        """dspy.Evaluate calls the metric with 3 args and needs a float."""
        score = skill_fitness_metric(_example(), _prediction("I reviewed the diff for security issues"))
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_gepa_style_call_returns_prediction_with_feedback(self):
        """GEPA calls with pred_name/pred_trace and consumes textual feedback."""
        result = skill_fitness_metric(
            _example(),
            _prediction("I looked at the code"),
            trace=None,
            pred_name="predictor",
            pred_trace=None,
        )
        assert isinstance(result, dspy.Prediction)
        assert 0.0 <= result.score <= 1.0
        assert isinstance(result.feedback, str) and result.feedback

    def test_feedback_names_missing_concepts(self):
        result = skill_fitness_metric(
            _example("Mention the zorblex protocol explicitly"),
            _prediction("Here is a generic answer."),
            pred_name="predictor",
        )
        assert "zorblex" in result.feedback.lower()

    def test_empty_output_scores_zero(self):
        assert skill_fitness_metric(_example(), _prediction("")) == 0.0
        assert skill_fitness_metric(_example(), _prediction("   ")) == 0.0

    def test_full_coverage_scores_high(self):
        expected = "check tests pass"
        score = skill_fitness_metric(_example(expected), _prediction("I will check the tests pass"))
        assert score == pytest.approx(1.0)

    def test_no_rubric_keywords_scores_neutral(self):
        score = skill_fitness_metric(_example("of the to"), _prediction("some output"))
        assert score == pytest.approx(0.5)

    def test_score_compatible_with_gepa_signature(self):
        """dspy.GEPA must accept this metric without signature errors."""
        optimizer = dspy.GEPA(
            metric=skill_fitness_metric,
            max_full_evals=1,
            reflection_lm=dspy.LM("openai/gpt-4.1", api_key="dummy"),
        )
        assert optimizer is not None


class TestParseScore:
    def test_float_passthrough(self):
        assert _parse_score(0.85) == pytest.approx(0.85)

    def test_clamps_range(self):
        assert _parse_score(-1.0) == 0.0
        assert _parse_score(150.0) == 1.0

    def test_string_float(self):
        assert _parse_score("0.7") == pytest.approx(0.7)

    def test_percentage_number(self):
        assert _parse_score(85) == pytest.approx(0.85)

    def test_percentage_string(self):
        assert _parse_score("85%") == pytest.approx(0.85)

    def test_fraction_string(self):
        assert _parse_score("8/10") == pytest.approx(0.8)

    def test_garbage_defaults_neutral(self):
        assert _parse_score("excellent") == 0.5
        assert _parse_score(None) == 0.5


class TestFitnessScoreComposite:
    def test_weighted_composite(self):
        s = FitnessScore(correctness=1.0, procedure_following=1.0, conciseness=1.0)
        assert s.composite == pytest.approx(1.0)

    def test_length_penalty_subtracts(self):
        s = FitnessScore(correctness=1.0, procedure_following=1.0, conciseness=1.0, length_penalty=0.3)
        assert s.composite == pytest.approx(0.7)

    def test_never_negative(self):
        s = FitnessScore(length_penalty=0.5)
        assert s.composite == 0.0
