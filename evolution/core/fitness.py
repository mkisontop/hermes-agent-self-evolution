"""Fitness functions for evaluating evolved artifacts.

Uses LLM-as-judge with rubrics to score agent outputs.
Supports length penalties and multi-dimensional scoring.

The core metric, ``skill_fitness_metric``, is GEPA-compatible: GEPA invokes
metrics with ``(gold, pred, trace, pred_name, pred_trace)`` and uses the
returned *textual feedback* to drive reflective mutation — the feedback
tells the reflection LM exactly which expectations the candidate missed,
so mutations are targeted rather than random. When called by plain
``dspy.Evaluate`` (3 args), it returns a bare float.
"""

import re
from dataclasses import dataclass
from typing import Optional, Union

import dspy

from evolution.core.config import EvolutionConfig


@dataclass
class FitnessScore:
    """Multi-dimensional fitness score."""
    correctness: float = 0.0  # Did the agent produce correct output? (0-1)
    procedure_following: float = 0.0  # Did it follow the skill's procedure? (0-1)
    conciseness: float = 0.0  # Was it appropriately concise? (0-1)
    length_penalty: float = 0.0  # Penalty for being too verbose (0-1, 0 = no penalty)
    feedback: str = ""  # Textual feedback for GEPA's reflective analysis

    @property
    def composite(self) -> float:
        """Weighted composite score."""
        raw = (
            0.5 * self.correctness
            + 0.3 * self.procedure_following
            + 0.2 * self.conciseness
        )
        return max(0.0, raw - self.length_penalty)


class LLMJudge:
    """LLM-as-judge scorer with rubric-based evaluation.

    Scores agent outputs on multiple dimensions and provides
    textual feedback that GEPA can use for reflective mutation.
    """

    class JudgeSignature(dspy.Signature):
        """Evaluate an agent's response against an expected behavior rubric.

        Score the response on three dimensions (0.0 to 1.0 each):
        1. correctness: Did the response correctly address the task?
        2. procedure_following: Did it follow the expected approach/procedure?
        3. conciseness: Was it appropriately concise without omitting important info?

        Also provide specific, actionable feedback on what could be improved.
        """
        task_input: str = dspy.InputField(desc="The task the agent was given")
        expected_behavior: str = dspy.InputField(desc="Rubric describing what a good response looks like")
        agent_output: str = dspy.InputField(desc="The agent's actual response")
        skill_text: str = dspy.InputField(desc="The skill/instructions the agent was following")
        correctness: float = dspy.OutputField(desc="Score 0.0-1.0: Did the response correctly address the task?")
        procedure_following: float = dspy.OutputField(desc="Score 0.0-1.0: Did it follow the expected procedure?")
        conciseness: float = dspy.OutputField(desc="Score 0.0-1.0: Appropriately concise?")
        feedback: str = dspy.OutputField(desc="Specific, actionable feedback on what could be improved")

    def __init__(self, config: EvolutionConfig):
        self.config = config
        self.judge = dspy.ChainOfThought(self.JudgeSignature)

    def score(
        self,
        task_input: str,
        expected_behavior: str,
        agent_output: str,
        skill_text: str,
        artifact_size: Optional[int] = None,
        max_size: Optional[int] = None,
    ) -> FitnessScore:
        """Score an agent output using LLM-as-judge."""

        lm = dspy.LM(self.config.eval_model)

        with dspy.context(lm=lm):
            result = self.judge(
                task_input=task_input,
                expected_behavior=expected_behavior,
                agent_output=agent_output,
                skill_text=skill_text,
            )

        # Parse scores (clamp to 0-1)
        correctness = _parse_score(result.correctness)
        procedure_following = _parse_score(result.procedure_following)
        conciseness = _parse_score(result.conciseness)

        # Length penalty
        length_penalty = 0.0
        if artifact_size is not None and max_size is not None:
            ratio = artifact_size / max_size
            if ratio > 0.9:
                # Penalty ramps from 0 at 90% to 0.3 at 100%+
                length_penalty = min(0.3, (ratio - 0.9) * 3.0)

        return FitnessScore(
            correctness=correctness,
            procedure_following=procedure_following,
            conciseness=conciseness,
            length_penalty=length_penalty,
            feedback=str(result.feedback),
        )


# Words too common to signal that the output actually addressed the rubric.
_STOPWORDS = frozenset("""
a an and are as at be but by for from has have if in into is it its of on or
should that the their then there these this to was what when which will with
would you your
""".split())


def _keywords(text: str) -> set[str]:
    """Meaningful lowercase words from a text (stopwords and short tokens dropped)."""
    words = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def skill_fitness_metric(
    gold: dspy.Example,
    pred: dspy.Prediction,
    trace=None,
    pred_name: Optional[str] = None,
    pred_trace=None,
) -> Union[float, dspy.Prediction]:
    """DSPy metric for skill optimization — GEPA and Evaluate compatible.

    GEPA calls this with all five arguments and uses the returned
    ``feedback`` text for reflective mutation; ``dspy.Evaluate`` calls it
    with the first three and needs a plain float.

    Scoring is a fast keyword-recall proxy: what fraction of the rubric's
    meaningful vocabulary appears in the agent's output. Full LLM-as-judge
    scoring (``LLMJudge``) is reserved for holdout evaluation where the
    extra cost is justified.
    """
    agent_output = getattr(pred, "output", "") or ""
    expected = getattr(gold, "expected_behavior", "") or ""

    if not agent_output.strip():
        score, feedback = 0.0, "The response was empty. Produce a substantive answer."
    else:
        expected_kw = _keywords(expected)
        if not expected_kw:
            # No rubric vocabulary to check against — non-empty output gets
            # a neutral pass.
            score, feedback = 0.5, "No rubric keywords available; scored neutrally."
        else:
            output_kw = _keywords(agent_output)
            covered = expected_kw & output_kw
            missing = expected_kw - output_kw
            recall = len(covered) / len(expected_kw)
            score = 0.3 + 0.7 * recall
            if missing:
                feedback = (
                    f"The response covered {len(covered)}/{len(expected_kw)} rubric concepts. "
                    f"Expected behavior: {expected[:300]} "
                    f"Missing concepts: {', '.join(sorted(missing)[:15])}."
                )
            else:
                feedback = "The response covered all rubric concepts."

    score = min(1.0, max(0.0, score))

    if pred_name is not None:
        # GEPA reflective path: feedback guides the next mutation.
        return dspy.Prediction(score=score, feedback=feedback)
    return score


def _parse_score(value) -> float:
    """Parse a score value, handling various LLM output formats.

    Accepts floats, ints, "0.85", "85%", and "8/10"; values in (1, 100]
    are treated as percentages.
    """
    if not isinstance(value, (int, float)):
        text = str(value).strip()
        m = re.match(r"^([0-9]*\.?[0-9]+)\s*/\s*([0-9]*\.?[0-9]+)$", text)
        try:
            if m and float(m.group(2)) > 0:
                value = float(m.group(1)) / float(m.group(2))
            else:
                value = float(text.rstrip("%"))
                if text.endswith("%"):
                    value /= 100.0
        except (ValueError, TypeError):
            return 0.5  # Default to neutral on parse failure

    value = float(value)
    if 1.0 < value <= 100.0:
        value /= 100.0  # LLM answered on a percentage scale
    return min(1.0, max(0.0, value))
