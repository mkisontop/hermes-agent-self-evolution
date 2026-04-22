"""Fitness functions for evaluating evolved artifacts.

Three modes:
- fast:    keyword-overlap heuristic (zero LLM cost, noisy signal)
- judge:   LLM-as-judge composite (accurate, expensive)
- hybrid:  deterministic sampling — judge on ~20% of examples by hash,
           fast on the rest. Gives GEPA reflective feedback on harder
           cases while keeping the inner loop cheap.

The fast metric is used as the DEFAULT for GEPA/MIPROv2 inner loops to
keep per-iteration cost predictable. The judge metric is used on the
HOLDOUT set for honest before/after comparison (see evolve_skill.py).

Why split: running a full LLM-judge call per (example × iteration × skill)
blows token budgets at N=3 skills × 10 iters × 20 examples × 3 judge calls
each. Keyword metric on the inner loop + judge on 5-10 holdout examples
gives the right trade-off.
"""

import hashlib
import os
import re

import dspy
from dataclasses import dataclass
from typing import Callable, Optional

from evolution.core.config import EvolutionConfig


_STOPWORDS = {
    "about", "after", "again", "against", "also", "and", "are", "because",
    "been", "being", "between", "could", "from", "have", "into", "just",
    "like", "must", "over", "provide", "response", "should", "some",
    "that", "their", "them", "then", "there", "these", "they", "this",
    "those", "through", "using", "with", "would", "your",
}


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

    def __init__(self, config: Optional[EvolutionConfig] = None, model: Optional[str] = None):
        self.config = config
        self.model = model or (config.eval_model if config else os.getenv("EVOLUTION_EVAL_MODEL", "openai/cx/gpt-5.4"))
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

        # A-prime (2026-04-22): route through role-aware factory so judge
        # timeout/retries/max_tokens follow EVOLUTION_JUDGE_* env overrides.
        # Previously hardcoded timeout=60, num_retries=2 — which multiplied
        # a single gateway slowdown into 180s+ per call and wedged the
        # holdout phase at num_threads=4 concurrency.
        from evolution.core.lm_factory import make_lm
        lm = make_lm(self.model, role="judge")

        try:
            with dspy.context(lm=lm):
                result = self.judge(
                    task_input=task_input,
                    expected_behavior=expected_behavior,
                    agent_output=agent_output,
                    skill_text=skill_text,
                )
        except Exception as e:
            # Judge failure — fall back to neutral score + error feedback.
            # Never crash the optimization loop on a transient LLM failure.
            return FitnessScore(
                correctness=0.5,
                procedure_following=0.5,
                conciseness=0.5,
                length_penalty=0.0,
                feedback=f"[judge error, fallback neutral] {type(e).__name__}: {str(e)[:200]}",
            )

        # Parse scores (clamp to 0-1)
        correctness = _parse_score(result.correctness)
        procedure_following = _parse_score(result.procedure_following)
        conciseness = _parse_score(result.conciseness)

        # Length penalty
        length_penalty = 0.0
        if artifact_size is not None and max_size is not None and max_size > 0:
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


# ─────────────────────────────────────────────────────────────────────────────
# Metric implementations
# ─────────────────────────────────────────────────────────────────────────────

def _extract_keywords(text: str) -> list[str]:
    """Extract stable, lower-noise keywords from task/rubric text."""
    seen = set()
    keywords = []
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower()):
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
    return keywords


def _format_term_list(terms: list[str], limit: int = 8) -> str:
    """Format a compact keyword list for feedback."""
    if not terms:
        return "none"
    shown = terms[:limit]
    suffix = "" if len(terms) <= limit else f", +{len(terms) - limit} more"
    return ", ".join(shown) + suffix

def _keyword_overlap_score(agent_output: str, expected: str) -> float:
    """Fast keyword-overlap heuristic. Zero LLM cost.

    Not a great fitness signal on its own, but cheap enough to use in
    every GEPA/MIPROv2 inner iteration. Mostly answers "did the output
    even touch the right topics?"
    """
    if not agent_output.strip():
        return 0.0

    score = 0.5  # Base score for non-empty output

    expected_words = set(_extract_keywords(expected))
    output_words = set(_extract_keywords(agent_output))
    if expected_words:
        overlap = len(expected_words & output_words) / len(expected_words)
        score = 0.3 + (0.7 * overlap)

    return min(1.0, max(0.0, score))


def _build_fast_feedback(
    task_input: str,
    expected_behavior: str,
    agent_output: str,
    trace=None,
    pred_trace=None,
) -> str:
    """Produce cheap natural-language feedback for GEPA."""
    if not agent_output.strip():
        return "Output is empty. Respond directly to the task and cover the expected behavior."

    expected_keywords = _extract_keywords(expected_behavior)
    output_keywords = set(_extract_keywords(agent_output))
    task_keywords = _extract_keywords(task_input)

    missing_expected = [kw for kw in expected_keywords if kw not in output_keywords]
    missing_task = [kw for kw in task_keywords if kw not in output_keywords]
    covered = len(expected_keywords) - len(missing_expected)

    output_word_count = len(agent_output.split())
    rubric_word_count = max(1, len(expected_behavior.split()))

    feedback = [
        f"Topic coverage: {covered}/{max(1, len(expected_keywords))} expected keywords present.",
    ]
    if missing_expected:
        feedback.append(
            f"Missing expected topics: {_format_term_list(missing_expected)}."
        )
    if task_keywords and len(missing_task) >= max(2, len(task_keywords) // 2):
        feedback.append(
            f"Task-focus drift: output barely mentions task terms like {_format_term_list(missing_task)}."
        )
    if output_word_count < max(12, rubric_word_count // 3):
        feedback.append("Output is probably too brief; add the missing steps or details.")
    elif output_word_count > max(80, rubric_word_count * 3):
        feedback.append("Output is likely too verbose; compress repeated explanation.")

    trace_excerpt = pred_trace if pred_trace is not None else trace
    if isinstance(trace_excerpt, str):
        excerpt = " ".join(trace_excerpt.split())
        if excerpt:
            feedback.append(f"Trace clue: {excerpt[:180]}")

    if len(feedback) == 1 and not missing_expected:
        feedback.append(
            f"Good topical alignment. Preserve coverage of {_format_term_list(expected_keywords[:4])} while improving specificity."
        )
    return " ".join(feedback)


def _judge_score_and_feedback(
    example: dspy.Example,
    prediction: dspy.Prediction,
) -> tuple[float, str]:
    """Judge score paired with structured textual feedback."""
    agent_output = getattr(prediction, "output", "") or ""
    if not agent_output.strip():
        return 0.0, "Output is empty. Respond directly to the task and satisfy the rubric."

    expected = getattr(example, "expected_behavior", "") or ""
    task_input = getattr(example, "task_input", "") or ""
    skill_text = getattr(example, "skill_text", "") or ""

    judge = _get_global_judge()
    score = judge.score(
        task_input=task_input,
        expected_behavior=expected,
        agent_output=agent_output,
        skill_text=skill_text,
    )
    feedback = (
        f"Correctness={score.correctness:.3f}; "
        f"Procedure={score.procedure_following:.3f}; "
        f"Conciseness={score.conciseness:.3f}; "
        f"Penalty={score.length_penalty:.3f}. "
        f"{score.feedback}"
    )
    return score.composite, feedback


def _score_and_feedback(
    example: dspy.Example,
    prediction: dspy.Prediction,
    trace=None,
    pred_trace=None,
) -> tuple[float, str]:
    """Return both the scalar metric and textual diagnostic feedback."""
    mode = os.getenv("EVOLUTION_FITNESS_MODE", "fast").strip().lower()
    try:
        judge_ratio = float(os.getenv("EVOLUTION_JUDGE_RATIO", "0.2"))
    except ValueError:
        judge_ratio = 0.2
    judge_ratio = min(1.0, max(0.0, judge_ratio))

    if mode == "judge":
        return _judge_score_and_feedback(example, prediction)

    if mode == "hybrid":
        task = getattr(example, "task_input", "") or ""
        if _hash_fraction(task) < judge_ratio:
            return _judge_score_and_feedback(example, prediction)

    agent_output = getattr(prediction, "output", "") or ""
    expected = getattr(example, "expected_behavior", "") or ""
    task_input = getattr(example, "task_input", "") or ""
    score = _keyword_overlap_score(agent_output, expected)
    feedback = _build_fast_feedback(
        task_input=task_input,
        expected_behavior=expected,
        agent_output=agent_output,
        trace=trace,
        pred_trace=pred_trace,
    )
    return score, feedback


def skill_fitness_metric_fast(example: dspy.Example, prediction: dspy.Prediction, trace=None) -> float:
    """Fast keyword-overlap metric. Zero LLM cost.

    DEFAULT for GEPA/MIPROv2 inner loop — called thousands of times
    during optimization. Use skill_fitness_metric_judge on the holdout
    set for accurate final scoring.
    """
    agent_output = getattr(prediction, "output", "") or ""
    expected = getattr(example, "expected_behavior", "") or ""
    return _keyword_overlap_score(agent_output, expected)


# Global judge instance, lazily initialized. We keep one per-process so we
# don't re-construct the DSPy ChainOfThought wrapper on every call.
_GLOBAL_JUDGE: Optional[LLMJudge] = None


def _get_global_judge() -> LLMJudge:
    global _GLOBAL_JUDGE
    if _GLOBAL_JUDGE is None:
        _GLOBAL_JUDGE = LLMJudge(model=os.getenv("EVOLUTION_JUDGE_MODEL"))
    return _GLOBAL_JUDGE


def set_global_judge(judge: Optional[LLMJudge]) -> None:
    """Override the process-wide judge instance (tests, custom configs)."""
    global _GLOBAL_JUDGE
    _GLOBAL_JUDGE = judge


def skill_fitness_metric_judge(
    example: dspy.Example,
    prediction: dspy.Prediction,
    trace=None,
) -> float:
    """LLM-as-judge composite metric. 1 LLM call per invocation.

    Use for holdout evaluation (accurate) — NOT for GEPA inner loop
    unless you've budgeted for it.
    """
    return _judge_score_and_feedback(example, prediction)[0]


def _hash_fraction(s: str) -> float:
    """Deterministic 0..1 bucket for a string. Used for hybrid sampling
    so the same example always gets the same fast-vs-judge assignment.
    """
    h = hashlib.sha256(s.encode("utf-8")).digest()
    # Top 4 bytes as uint32, normalize
    val = int.from_bytes(h[:4], "big")
    return val / 0xFFFFFFFF


def make_hybrid_metric(judge_ratio: float = 0.2) -> Callable:
    """Build a hybrid metric that uses the judge on ~judge_ratio of examples.

    Example→bucket assignment is deterministic (sha256 of task_input), so
    the same example always routes to the same scorer across iterations.
    Gives GEPA stable reflective signal on hard cases without paying for
    judge calls on every inner iteration.
    """
    if not 0.0 <= judge_ratio <= 1.0:
        raise ValueError("judge_ratio must be in [0, 1]")

    def _metric(example: dspy.Example, prediction: dspy.Prediction, trace=None) -> float:
        task = getattr(example, "task_input", "") or ""
        if _hash_fraction(task) < judge_ratio:
            return skill_fitness_metric_judge(example, prediction, trace)
        return skill_fitness_metric_fast(example, prediction, trace)

    _metric.__name__ = f"skill_fitness_hybrid_{int(judge_ratio * 100)}"
    return _metric


def get_skill_fitness_metric(mode: str = "fast", judge_ratio: float = 0.2) -> Callable:
    """Return a DSPy-compatible metric for the requested mode.

    mode:
      - "fast"   : keyword overlap only (DEFAULT for inner loop)
      - "judge"  : LLM-as-judge composite every call
      - "hybrid" : deterministic sampling (judge on judge_ratio fraction)
    """
    mode = mode.lower().strip()
    if mode == "fast":
        return skill_fitness_metric_fast
    if mode == "judge":
        return skill_fitness_metric_judge
    if mode == "hybrid":
        return make_hybrid_metric(judge_ratio=judge_ratio)
    raise ValueError(f"Unknown fitness mode: {mode!r}. Expected fast|judge|hybrid")


# ─────────────────────────────────────────────────────────────────────────────
# Backward-compatible alias
# ─────────────────────────────────────────────────────────────────────────────

def skill_fitness_metric(example: dspy.Example, prediction: dspy.Prediction, trace=None) -> float:
    """Legacy entry point — kept for existing imports in evolve_skill.py.

    Dispatches based on the EVOLUTION_FITNESS_MODE env var:
      unset or "fast"  → keyword overlap (pre-2026-04-18 behavior)
      "judge"          → LLM-as-judge
      "hybrid"         → hybrid sampling (judge_ratio from EVOLUTION_JUDGE_RATIO, default 0.2)

    Existing call sites keep working unchanged. To opt into higher-quality
    fitness, set EVOLUTION_FITNESS_MODE=hybrid in the nightly environment.
    """
    return _score_and_feedback(example, prediction, trace=trace)[0]


def skill_fitness_metric_gepa(example, prediction, trace=None, pred_name=None, pred_trace=None):
    """GEPA-compatible 5-arg metric wrapper.

    DSPy 3.x GEPA requires metrics with signature
      (gold, pred, trace, pred_name, pred_trace)
    and can consume either a float or a dspy.Prediction with `score` and
    optional `feedback`. We return a Prediction so GEPA reflection has
    textual feedback to mutate instructions on.
    """
    score, feedback = _score_and_feedback(
        example,
        prediction,
        trace=trace,
        pred_trace=pred_trace,
    )
    try:
        return dspy.Prediction(score=float(score), feedback=feedback)
    except Exception:
        return float(score)


def _parse_score(value) -> float:
    """Parse a score value, handling various LLM output formats."""
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    try:
        return min(1.0, max(0.0, float(str(value).strip())))
    except (ValueError, TypeError):
        return 0.5  # Default to neutral on parse failure
