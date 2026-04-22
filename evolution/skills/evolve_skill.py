"""Evolve a Hermes Agent skill using DSPy + GEPA.

Usage:
    python -m evolution.skills.evolve_skill --skill github-code-review --iterations 10
    python -m evolution.skills.evolve_skill --skill arxiv --eval-source golden --dataset datasets/skills/arxiv/
"""

import faulthandler
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import click
import dspy
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from evolution.core.config import EvolutionConfig, get_hermes_agent_path
from evolution.core.dataset_builder import SyntheticDatasetBuilder, EvalDataset, GoldenDatasetLoader
from evolution.core.external_importers import build_dataset_from_external
from evolution.core.fitness import (
    skill_fitness_metric,
    get_skill_fitness_metric,
)
from evolution.core.constraints import ConstraintValidator
from evolution.core.regression_guard import AutoMergeGate
from evolution.core.proposals import ProposalWriter, build_proposal_record
from evolution.core.write_back import write_back_skill
from evolution.core.manifest import build_manifest, write_manifest
from evolution.core.risk import (
    assess_risk,
    is_auto_merge_eligible,
    required_delta_for_tier,
)
from evolution.core.lm_factory import (
    make_lm,
    judge_num_threads,
    judge_phase_timeout,
)
from evolution.skills.skill_module import (
    SkillModule,
    load_skill,
    find_skill,
    reassemble_skill,
)

console = Console()


class OptimizerTimeoutError(TimeoutError):
    """Raised when an optimizer compile step exceeds its time budget."""


def _get_env_int(name: str, default: int) -> int:
    """Read an integer env var with a safe fallback."""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _lm_num_retries() -> int:
    """LM retry count. Default 0 so a single hang doesn't triple the wall-clock.

    Raise in production via env override (e.g. ``EVOLUTION_LM_NUM_RETRIES=1``).
    """
    return max(0, _get_env_int("EVOLUTION_LM_NUM_RETRIES", 0))


def _lm_request_timeout() -> int:
    """Per-LM-request timeout (seconds). Forwarded to LiteLLM."""
    return max(1, _get_env_int("EVOLUTION_LM_TIMEOUT", 120))


def _gepa_log_dir(skill_name: str) -> str:
    """Per-run GEPA log directory. Always a fresh path to avoid silent resume.

    Relative to the current working directory (self-evolution root) to keep
    logs scoped with the rest of the engine's on-disk artifacts.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_name = skill_name.replace("/", "-")
    path = Path("logs") / "gepa" / safe_name / stamp
    path.mkdir(parents=True, exist_ok=False)
    return str(path)


def _gepa_max_metric_calls(iterations: int, valset_len: int) -> Optional[int]:
    """Cap on total metric calls for a GEPA run.

    Env override ``EVOLUTION_GEPA_MAX_METRIC_CALLS`` wins. Otherwise derive a
    generous default from iterations × valset, with a floor that prevents
    accidental micro-budgets but still trips runaway fan-out.
    Return None to leave GEPA's own default in place if explicitly set to 0.
    """
    raw = os.getenv("EVOLUTION_GEPA_MAX_METRIC_CALLS")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        return None
    return max(20, iterations * max(1, valset_len) * 3)


def _resolve_optimizer_name(requested: str, inner_metric_mode: str) -> str:
    """Resolve auto-routing to a concrete optimizer.

    Historically ``auto`` + ``judge`` → GEPA. GEPA currently hangs on the local
    gateway (2026-04-21), so we pivot ``auto`` to MIPROv2 until GEPA is
    validated. Override with ``EVOLUTION_AUTO_OPTIMIZER=gepa`` (or ``miprov2``)
    to force a specific resolution for a single run.

    ``mipro`` is accepted as an alias for ``miprov2`` at any layer so the
    ``.env`` convention (OPTIMIZER=mipro) flows through nightly.sh cleanly.
    """
    requested = requested.strip().lower()
    inner_metric_mode = inner_metric_mode.strip().lower()
    # Alias: mipro → miprov2 (matches what nightly.sh / .env use)
    if requested == "mipro":
        return "miprov2"
    if requested != "auto":
        return requested
    override = os.getenv("EVOLUTION_AUTO_OPTIMIZER", "").strip().lower()
    if override in {"gepa", "miprov2", "mipro"}:
        return "miprov2" if override == "mipro" else override
    # Default auto → MIPROv2 while GEPA is under investigation.
    return "miprov2"


def _build_optimizer_attempt_order(selected: str) -> list[str]:
    """Build the ordered list of optimizer attempts for a run."""
    if selected == "gepa":
        return ["gepa", "miprov2"]
    return [selected]


def _run_phase_with_timeout(timeout_seconds: int, label: str, phase_fn: Callable[[], object]):
    """Run a phase (optimizer compile, holdout eval, etc.) with a hard wall-clock timeout.

    Generalized from ``_compile_with_timeout`` (A-prime, 2026-04-22) so the
    post-optimizer judge phase can get the same SIGALRM + faulthandler
    treatment. A wedge in the holdout judge path is functionally identical
    to a wedge in the optimizer loop — same hang class, same fix.

    On top of SIGALRM, arms faulthandler.dump_traceback_later() a few seconds
    before the alarm so that if the process is wedged inside a C-level
    socket/poll (the 2026-04-21 ``sock_recv`` case), we get a thread dump
    *before* the kill — turning the next hang into evidence instead of folklore.
    """
    if timeout_seconds <= 0 or not hasattr(signal, "setitimer"):
        return phase_fn()

    def _handle_timeout(signum, frame):
        raise OptimizerTimeoutError(f"{label} exceeded {timeout_seconds}s")

    traceback_margin = max(1, _get_env_int("OPTIMIZER_TRACEBACK_MARGIN", 15))
    dump_after = max(1, timeout_seconds - traceback_margin)

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)

    faulthandler_was_enabled = faulthandler.is_enabled()
    if not faulthandler_was_enabled:
        try:
            faulthandler.enable(file=sys.stderr)
        except Exception:
            pass  # defensive — never let faulthandler setup block the run
    try:
        faulthandler.dump_traceback_later(
            dump_after,
            repeat=False,
            file=sys.stderr,
        )
    except Exception:
        pass

    try:
        return phase_fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass


# Back-compat alias: optimizer path still calls _compile_with_timeout.
_compile_with_timeout = _run_phase_with_timeout


def _run_gepa(
    baseline_module: SkillModule,
    trainset: list,
    valset: list,
    iterations: int,
    optimizer_model: str,
    timeout_seconds: int,
    skill_name: str,
):
    """Run GEPA with reflection enabled and a compile timeout."""
    from evolution.core.fitness import skill_fitness_metric_gepa

    reflection_lm = make_lm(optimizer_model, role="optimizer")
    max_metric_calls = _gepa_max_metric_calls(iterations, len(valset))
    log_dir = _gepa_log_dir(skill_name)
    console.print(f"  GEPA log_dir: {log_dir}")
    # DSPy GEPA docs require exactly one budget param (auto | max_full_evals |
    # max_metric_calls). Passing both is undefined behavior. We prefer
    # max_metric_calls for debugging: it's a hard cap on LM calls, easier to
    # reason about than full evals × valset size.
    if max_metric_calls is None:
        max_metric_calls = max(4, iterations * max(1, len(valset)) * 3)
    console.print(f"  GEPA max_metric_calls: {max_metric_calls}")
    gepa_kwargs = dict(
        metric=skill_fitness_metric_gepa,
        max_metric_calls=max_metric_calls,
        reflection_lm=reflection_lm,
        num_threads=1,
        log_dir=log_dir,
        track_stats=True,
    )
    optimizer = dspy.GEPA(**gepa_kwargs)
    return _compile_with_timeout(
        timeout_seconds,
        "GEPA",
        lambda: optimizer.compile(
            baseline_module,
            trainset=trainset,
            valset=valset,
        ),
    )


def _run_miprov2(
    baseline_module: SkillModule,
    trainset: list,
    valset: list,
    iterations: int,
    optimizer_model: str,
    task_model: str,
    eval_model: str,
    timeout_seconds: int,
):
    """Run MIPROv2 with explicit proposer and task models."""
    auto_level = os.getenv("EVOLUTION_MIPRO_AUTO", "manual").strip().lower() or "manual"
    auto_setting = None if auto_level in {"none", "off", "manual"} else auto_level
    num_candidates = None if auto_setting is not None else max(3, min(6, iterations + 2))
    prompt_lm = make_lm(optimizer_model, role="optimizer")
    task_lm = make_lm(task_model, role="task")
    optimizer = dspy.MIPROv2(
        metric=skill_fitness_metric,
        prompt_model=prompt_lm,
        task_model=task_lm,
        max_bootstrapped_demos=0,
        max_labeled_demos=0,
        auto=auto_setting,
        num_candidates=num_candidates,
        num_threads=1,
        track_stats=True,
    )
    compile_kwargs = {
        "trainset": trainset,
        "valset": valset,
        "minibatch": False,
        "requires_permission_to_run": False,
    }
    if auto_setting is None:
        compile_kwargs["num_trials"] = iterations
    return _compile_with_timeout(
        timeout_seconds,
        "MIPROv2",
        lambda: optimizer.compile(baseline_module, **compile_kwargs),
    )


def evolve(
    skill_name: str,
    iterations: int = 10,
    eval_source: str = "synthetic",
    dataset_path: Optional[str] = None,
    optimizer_model: str = os.getenv("EVOLUTION_OPTIMIZER_MODEL", "openai/cx/gpt-5.3-codex-spark"),
    eval_model: str = os.getenv("EVOLUTION_EVAL_MODEL", "openai/cx/gpt-5.4"),
    task_model: Optional[str] = None,
    hermes_repo: Optional[str] = None,
    run_tests: bool = False,
    dry_run: bool = False,
    mode: str = "propose",
    optimizer: str = "auto",
    optimizer_timeout: Optional[int] = None,
    min_improvement: float = 0.02,
    regression_tolerance: float = 0.01,
    proposals_dir: Optional[str] = None,
):
    """Main evolution function — orchestrates the full optimization loop."""

    config = EvolutionConfig(
        iterations=iterations,
        optimizer_model=optimizer_model,
        eval_model=eval_model,
        judge_model=eval_model,  # Use same model for dataset generation
        run_pytest=run_tests,
    )
    if hermes_repo:
        config.hermes_agent_path = Path(hermes_repo)

    # ── 0. Self-target invariant (Batch A — layer 2) ─────────────────────
    # The engine must not rewrite itself. Picker has its own denylist, but
    # defense-in-depth requires refusing the skill here too — even if the
    # user explicitly passed --skill hermes-self-evolution. Override only
    # via EVOLUTION_ALLOW_SELF_TARGET=1 and only for manual runs.
    _SELF_EVOLUTION_SKILLS = {
        "hermes-self-evolution",
        "self-evolution",
        "evolution-engine",
    }
    if skill_name in _SELF_EVOLUTION_SKILLS and os.getenv("EVOLUTION_ALLOW_SELF_TARGET") != "1":
        console.print(
            f"[red]✗ Refusing to evolve engine/self-evolution skill: {skill_name}[/red]"
        )
        console.print(
            "[yellow]  Set EVOLUTION_ALLOW_SELF_TARGET=1 for a deliberate manual run.[/yellow]"
        )
        sys.exit(2)

    # ── 1. Find and load the skill ──────────────────────────────────────
    console.print(f"\n[bold cyan]🧬 Hermes Agent Self-Evolution[/bold cyan] — Evolving skill: [bold]{skill_name}[/bold]\n")

    skill_path = find_skill(skill_name, config.hermes_agent_path)
    if not skill_path:
        console.print(f"[red]✗ Skill '{skill_name}' not found in {config.hermes_agent_path / 'skills'}[/red]")
        sys.exit(1)

    skill = load_skill(skill_path)
    console.print(f"  Loaded: {skill_path.relative_to(config.hermes_agent_path)}")
    console.print(f"  Name: {skill['name']}")
    console.print(f"  Size: {len(skill['raw']):,} chars")
    console.print(f"  Description: {skill['description'][:80]}...")

    inner_metric_mode = os.getenv("EVOLUTION_FITNESS_MODE", "fast").strip().lower() or "fast"
    selected_optimizer = _resolve_optimizer_name(optimizer, inner_metric_mode)
    timeout_seconds = optimizer_timeout or _get_env_int("EVOLUTION_OPTIMIZER_TIMEOUT", 900)
    task_model = task_model or os.getenv("EVOLUTION_TASK_MODEL") or eval_model

    if dry_run:
        console.print(f"\n[bold green]DRY RUN — setup validated successfully.[/bold green]")
        console.print(f"  Would generate eval dataset (source: {eval_source})")
        console.print(f"  Would run {selected_optimizer.upper()} optimization ({iterations} iterations)")
        console.print(f"  Inner-loop metric: {inner_metric_mode}")
        console.print(f"  Task model: {task_model}")
        console.print(f"  Optimizer timeout: {timeout_seconds}s")
        console.print(f"  Would validate constraints and create PR")
        return

    # ── 2. Build or load evaluation dataset ─────────────────────────────
    console.print(f"\n[bold]Building evaluation dataset[/bold] (source: {eval_source})")

    if eval_source == "golden" and dataset_path:
        dataset = GoldenDatasetLoader.load(Path(dataset_path))
        console.print(f"  Loaded golden dataset: {len(dataset.all_examples)} examples")
    elif eval_source == "sessiondb":
        save_path = Path(dataset_path) if dataset_path else Path("datasets") / "skills" / skill_name
        dataset = build_dataset_from_external(
            skill_name=skill_name,
            skill_text=skill["raw"],
            sources=["claude-code", "copilot", "hermes"],
            output_path=save_path,
            model=eval_model,
        )
        if not dataset.all_examples:
            console.print("[red]✗ No relevant examples found from session history[/red]")
            sys.exit(1)
        console.print(f"  Mined {len(dataset.all_examples)} examples from session history")
    elif eval_source == "synthetic":
        builder = SyntheticDatasetBuilder(config)
        dataset = builder.generate(
            artifact_text=skill["raw"],
            artifact_type="skill",
        )
        # Save for reuse
        save_path = Path("datasets") / "skills" / skill_name
        dataset.save(save_path)
        console.print(f"  Generated {len(dataset.all_examples)} synthetic examples")
        console.print(f"  Saved to {save_path}/")
    elif dataset_path:
        dataset = EvalDataset.load(Path(dataset_path))
        console.print(f"  Loaded dataset: {len(dataset.all_examples)} examples")
    else:
        console.print("[red]✗ Specify --dataset-path or use --eval-source synthetic[/red]")
        sys.exit(1)

    console.print(f"  Split: {len(dataset.train)} train / {len(dataset.val)} val / {len(dataset.holdout)} holdout")

    # ── 3. Validate constraints on baseline ─────────────────────────────
    console.print(f"\n[bold]Validating baseline constraints[/bold]")
    validator = ConstraintValidator(config)
    baseline_constraints = validator.validate_all(skill["raw"], "skill")
    all_pass = True
    for c in baseline_constraints:
        icon = "✓" if c.passed else "✗"
        color = "green" if c.passed else "red"
        console.print(f"  [{color}]{icon} {c.constraint_name}[/{color}]: {c.message}")
        if not c.passed:
            all_pass = False

    if not all_pass:
        console.print("[yellow]⚠ Baseline skill has constraint violations — proceeding anyway[/yellow]")

    # ── 4. Set up DSPy + GEPA optimizer ─────────────────────────────────
    console.print(f"\n[bold]Configuring optimizer[/bold]")
    console.print(f"  Requested optimizer: {optimizer}")
    console.print(f"  Selected optimizer: {selected_optimizer} ({iterations} iterations)")
    console.print(f"  Optimizer model: {optimizer_model}")
    console.print(f"  Eval model: {eval_model}")
    console.print(f"  Task model: {task_model}")
    console.print(f"  Inner-loop metric: {inner_metric_mode}")
    console.print(f"  Optimizer timeout: {timeout_seconds}s")

    # Default session LM = task role (used during baseline.forward() and
    # evolved.forward() to generate agent outputs). The judge LM is built
    # separately below with judge-role config (longer timeout, max_tokens cap).
    lm = make_lm(task_model or eval_model, role="task")
    dspy.configure(lm=lm)

    # Create the baseline skill module
    baseline_module = SkillModule(skill["body"])

    # Prepare DSPy examples
    trainset = dataset.to_dspy_examples("train")
    valset = dataset.to_dspy_examples("val")

    # ── 5. Run optimizer ────────────────────────────────────────────────
    console.print(
        f"\n[bold cyan]Running {selected_optimizer.upper()} optimization ({iterations} iterations)...[/bold cyan]\n"
    )

    start_time = time.time()

    optimized_module = None
    optimizer_attempts = _build_optimizer_attempt_order(selected_optimizer)
    last_error = None
    for attempt_name in optimizer_attempts:
        try:
            if attempt_name == "gepa":
                optimized_module = _run_gepa(
                    baseline_module=baseline_module,
                    trainset=trainset,
                    valset=valset,
                    iterations=iterations,
                    optimizer_model=optimizer_model,
                    timeout_seconds=timeout_seconds,
                    skill_name=skill_name,
                )
            elif attempt_name == "miprov2":
                optimized_module = _run_miprov2(
                    baseline_module=baseline_module,
                    trainset=trainset,
                    valset=valset,
                    iterations=iterations,
                    optimizer_model=optimizer_model,
                    task_model=task_model,
                    eval_model=eval_model,
                    timeout_seconds=timeout_seconds,
                )
            else:
                raise ValueError(f"Unknown optimizer: {attempt_name}")
            selected_optimizer = attempt_name
            break
        except Exception as e:
            last_error = e
            console.print(
                f"[yellow]{attempt_name.upper()} failed ({type(e).__name__}: {e})[/yellow]"
            )
            if attempt_name != optimizer_attempts[-1]:
                console.print("[yellow]Falling back to MIPROv2[/yellow]")

    if optimized_module is None:
        raise last_error or RuntimeError("Optimizer failed without an exception")

    elapsed = time.time() - start_time
    console.print(f"\n  Optimization completed in {elapsed:.1f}s")

    # ── 6. Extract evolved skill text ───────────────────────────────────
    # The Predictor's signature.instructions IS the optimizable parameter
    # that GEPA reflection / MIPROv2 proposals mutate. Read it back.
    evolved_body = optimized_module.predictor.predict.signature.instructions
    evolved_full = reassemble_skill(skill["frontmatter"], evolved_body)

    # ── 7. Validate evolved skill ───────────────────────────────────────
    console.print(f"\n[bold]Validating evolved skill[/bold]")
    # Validate the reassembled full skill (frontmatter + body) so structure
    # checks like YAML frontmatter presence can pass. Growth check still
    # compares body-only against baseline body-only via baseline_text.
    evolved_constraints = validator.validate_all(evolved_full, "skill", baseline_text=skill["body"])
    evolved_pass = True
    for c in evolved_constraints:
        icon = "✓" if c.passed else "✗"
        color = "green" if c.passed else "red"
        console.print(f"  [{color}]{icon} {c.constraint_name}[/{color}]: {c.message}")
        if not c.passed:
            evolved_pass = False

    if not evolved_pass:
        console.print("[red]✗ Evolved skill FAILED constraints — not deploying[/red]")
        # Still save for inspection
        output_path = Path("output") / skill_name / "evolved_FAILED.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(evolved_full)
        console.print(f"  Saved failed variant to {output_path}")
        return

    # ── 8. Evaluate on holdout set ──────────────────────────────────────
    console.print(f"\n[bold]Evaluating on holdout set ({len(dataset.holdout)} examples)[/bold]")

    holdout_examples = dataset.to_dspy_examples("holdout")

    # Holdout uses the LLM-judge metric for honest before/after scoring.
    # Inner loop (GEPA/MIPROv2 above) uses the cheap keyword metric — or
    # whatever EVOLUTION_FITNESS_MODE overrides it to — to stay in budget.
    # Holdout is small (5-10 examples) so the judge cost is bounded.
    holdout_metric_mode = os.getenv("EVOLUTION_HOLDOUT_METRIC", "judge")
    holdout_metric = get_skill_fitness_metric(mode=holdout_metric_mode)
    console.print(f"  Holdout metric: {holdout_metric_mode}")

    # Judge LM: separate role with longer timeout / zero retries / token cap.
    # The judge is the authority layer — it must not share the task LM's
    # short-timeout/high-concurrency config. (A-prime, 2026-04-22)
    judge_threads = judge_num_threads() if holdout_metric_mode == "judge" else 4
    console.print(f"  Holdout concurrency: num_threads={judge_threads}")

    from dspy.evaluate import Evaluate
    evaluator = Evaluate(
        devset=holdout_examples,
        metric=holdout_metric,
        num_threads=judge_threads,
        display_progress=True,
        max_errors=max(1, len(holdout_examples) // 2),
        failure_score=0.0,
    )

    # Judge failures must NOT discard optimizer success. If the holdout
    # phase wedges or errors, we still want to emit a proposal artifact
    # with judge_failed=true, auto_merge=false so the evolved skill can be
    # reviewed manually. Concretely: wrap the holdout eval in a phase
    # timeout + broad except, and surface the failure through metadata.
    judge_failed = False
    judge_error_type: Optional[str] = None
    judge_error_message: Optional[str] = None
    avg_baseline = 0.0
    avg_evolved = 0.0
    phase_cap = judge_phase_timeout() if holdout_metric_mode == "judge" else 0

    def _run_holdout() -> tuple[float, float]:
        with dspy.context(lm=lm):
            b = evaluator(baseline_module)
            e = evaluator(optimized_module)

        def _norm(r):
            s = getattr(r, "score", r)
            return s / 100.0 if s > 1.0 else s

        return _norm(b), _norm(e)

    try:
        if phase_cap > 0:
            avg_baseline, avg_evolved = _run_phase_with_timeout(
                phase_cap, "holdout judge evaluation", _run_holdout
            )
        else:
            avg_baseline, avg_evolved = _run_holdout()
    except Exception as je:
        judge_failed = True
        judge_error_type = type(je).__name__
        judge_error_message = str(je)[:300]
        console.print(
            f"[red]✗ Holdout evaluation failed: {judge_error_type}: {judge_error_message}[/red]"
        )
        console.print(
            "[yellow]  Preserving evolved artifact for manual review (auto_merge=false).[/yellow]"
        )
        avg_baseline = 0.0
        avg_evolved = 0.0

    improvement = avg_evolved - avg_baseline

    # ── 9. Report results ───────────────────────────────────────────────
    table = Table(title="Evolution Results")
    table.add_column("Metric", style="bold")
    table.add_column("Baseline", justify="right")
    table.add_column("Evolved", justify="right")
    table.add_column("Change", justify="right")

    change_color = "green" if improvement > 0 else "red"
    table.add_row(
        "Holdout Score",
        f"{avg_baseline:.3f}",
        f"{avg_evolved:.3f}",
        f"[{change_color}]{improvement:+.3f}[/{change_color}]",
    )
    table.add_row(
        "Skill Size",
        f"{len(skill['body']):,} chars",
        f"{len(evolved_body):,} chars",
        f"{len(evolved_body) - len(skill['body']):+,} chars",
    )
    table.add_row("Time", "", f"{elapsed:.1f}s", "")
    table.add_row("Iterations", "", str(iterations), "")

    console.print()
    console.print(table)

    # ── 10. Save output ─────────────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path("output") / skill_name / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save evolved skill
    (output_dir / "evolved_skill.md").write_text(evolved_full)

    # Save baseline for comparison
    (output_dir / "baseline_skill.md").write_text(skill["raw"])

    # Save metrics
    metrics = {
        "skill_name": skill_name,
        "timestamp": timestamp,
        "iterations": iterations,
        "optimizer_model": optimizer_model,
        "eval_model": eval_model,
        "task_model": task_model,
        "requested_optimizer": optimizer,
        "selected_optimizer": selected_optimizer,
        "inner_metric_mode": inner_metric_mode,
        "optimizer_timeout_seconds": timeout_seconds,
        "baseline_score": avg_baseline,
        "evolved_score": avg_evolved,
        "improvement": improvement,
        "baseline_size": len(skill["body"]),
        "evolved_size": len(evolved_body),
        "train_examples": len(dataset.train),
        "val_examples": len(dataset.val),
        "holdout_examples": len(dataset.holdout),
        "elapsed_seconds": elapsed,
        "constraints_passed": evolved_pass,
    }
    (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    # ── 9b. Auto-merge gate ──────────────────────────────────────────────
    # Risk tier comes first — CRITICAL is never auto-mergeable, HIGH needs
    # 2× the base delta. Engine-self-evolution skills are hard-coded CRITICAL
    # as defense-in-depth over the Batch A picker denylist.
    risk_tier = assess_risk(skill_name)
    tier_min_delta = required_delta_for_tier(risk_tier, min_improvement)
    console.print(f"  Risk tier: [bold]{risk_tier.value}[/bold]  (required Δ ≥ {tier_min_delta})")

    gate = AutoMergeGate(
        min_improvement=tier_min_delta,
        regression_tolerance=regression_tolerance,
    )
    decision = gate.evaluate(avg_baseline, avg_evolved, evolved_pass)

    # Judge failure override: if holdout eval couldn't produce real scores,
    # the gate cannot approve auto-merge regardless of what 0.0 vs 0.0 says.
    # Force auto_merge=false with an explicit reason.
    if judge_failed:
        try:
            decision.auto_merge = False
            decision.reason = (
                f"judge_failed: {judge_error_type} — manual review required"
            )
        except Exception:
            pass  # decision dataclass may be frozen in some builds

    # Risk-tier override: CRITICAL can never auto-merge even if the gate
    # somehow approves it. Belt-and-suspenders over the hard-coded list.
    if not is_auto_merge_eligible(risk_tier):
        try:
            decision.auto_merge = False
            decision.reason = (
                f"risk={risk_tier.value} — auto-merge forbidden, manual review only"
            )
        except Exception:
            pass

    metrics["auto_merge"] = decision.auto_merge
    metrics["gate_reason"] = decision.reason
    metrics["regression"] = decision.regression
    metrics["mode"] = mode
    metrics["risk_tier"] = risk_tier.value
    metrics["required_auto_delta"] = tier_min_delta
    metrics["judge_failed"] = judge_failed
    metrics["judge_error_type"] = judge_error_type
    metrics["judge_error_message"] = judge_error_message
    (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    console.print(f"\n[bold]Gate decision:[/bold] {decision.reason}")

    # ── 9c. Propose-mode output ─────────────────────────────────────────
    # Write a ProposalRecord for human review. This runs in both `propose`
    # mode (always) and `auto` mode when the gate rejects — so a rejected
    # auto run still leaves a reviewable artifact behind.
    proposals_root = Path(proposals_dir) if proposals_dir else (Path("proposals"))
    should_write_proposal = (mode == "propose") or (not decision.auto_merge)
    proposal_path: Optional[Path] = None
    if should_write_proposal:
        writer = ProposalWriter(proposals_root)
        record = build_proposal_record(
            skill_name=skill_name,
            baseline_text=skill["raw"],
            evolved_text=evolved_full,
            baseline_score=avg_baseline,
            evolved_score=avg_evolved,
            decision=decision,
            constraint_results=evolved_constraints,
            mode=mode,
            metadata={
                "iterations": iterations,
                "optimizer_model": optimizer_model,
                "eval_model": eval_model,
                "elapsed_seconds": elapsed,
                "train_examples": len(dataset.train),
                "val_examples": len(dataset.val),
                "holdout_examples": len(dataset.holdout),
                "eval_source": eval_source,
                "output_dir": str(output_dir),
                "judge_failed": judge_failed,
                "judge_error_type": judge_error_type,
                "judge_error_message": judge_error_message,
            },
            timestamp=timestamp,
        )
        proposal_path = writer.write(record)
        metrics["proposal_path"] = str(proposal_path)

        # Batch B: write manifest.json with SHA256 hashes of baseline,
        # evolved, and diff. Enables stale-baseline / tampered-evolved
        # detection at approve-time.
        try:
            diff_text = (proposal_path / "diff.patch").read_text()
        except Exception:
            diff_text = ""
        manifest = build_manifest(
            skill_name=skill_name,
            timestamp=timestamp,
            risk_tier=risk_tier.value,
            baseline_text=skill["raw"],
            evolved_text=evolved_full,
            diff_text=diff_text,
            extra={
                "optimizer": selected_optimizer,
                "optimizer_model": optimizer_model,
                "eval_model": eval_model,
                "inner_metric_mode": inner_metric_mode,
                "holdout_metric_mode": os.getenv("EVOLUTION_HOLDOUT_METRIC", "judge"),
                "required_auto_delta": tier_min_delta,
                "judge_failed": judge_failed,
            },
        )
        manifest_path = write_manifest(proposal_path, manifest)
        metrics["manifest_path"] = str(manifest_path)
        metrics["baseline_sha256"] = manifest.baseline_sha256
        metrics["evolved_sha256"] = manifest.evolved_sha256

        (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
        console.print(f"  Proposal written: [cyan]{proposal_path}[/cyan]")
        console.print(
            f"  Manifest: [dim]baseline={manifest.baseline_sha256[:12]} "
            f"evolved={manifest.evolved_sha256[:12]} risk={risk_tier.value}[/dim]"
        )

    # ── 9d. Auto-mode write-back ────────────────────────────────────────
    # Only when mode=='auto' AND the gate approves do we overwrite the
    # live skill in hermes-agent. All other paths go through human review.
    # Always creates a timestamped backup before overwriting.
    wb_result = write_back_skill(
        live_path=skill_path,
        evolved_text=evolved_full,
        mode=mode,
        auto_merge=decision.auto_merge,
        timestamp=timestamp,
    )
    if wb_result.merged:
        console.print(
            f"[bold green]✓ AUTO-MERGED[/bold green] — wrote evolved skill to {wb_result.live_path}"
        )
        console.print(f"  Backup: [cyan]{wb_result.backup_path}[/cyan]")
        metrics["merged_to"] = str(wb_result.live_path)
        metrics["backup_path"] = str(wb_result.backup_path)
        (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    else:
        metrics["merged_to"] = None
        (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    if decision.regression:
        if mode == "propose":
            # Propose-mode success: regression was caught and a proposal was written
            # for human review. The pipeline worked correctly — do not exit non-zero.
            console.print(f"[yellow]⚠ REGRESSION detected — proposal written for review (propose mode)[/yellow]")
        else:
            console.print(f"[red]✗ REGRESSION — exiting non-zero[/red]")
            sys.exit(2)

    console.print(f"\n  Output saved to {output_dir}/")

    if improvement > 0:
        console.print(f"\n[bold green]✓ Evolution improved skill by {improvement:+.3f} ({improvement/max(0.001, avg_baseline)*100:+.1f}%)[/bold green]")
        console.print(f"  Review the diff: diff {output_dir}/baseline_skill.md {output_dir}/evolved_skill.md")
    else:
        console.print(f"\n[yellow]⚠ Evolution did not improve skill (change: {improvement:+.3f})[/yellow]")
        console.print("  Try: more iterations, better eval dataset, or different optimizer model")


@click.command()
@click.option("--skill", required=True, help="Name of the skill to evolve")
@click.option("--iterations", default=10, help="Number of GEPA iterations")
@click.option("--eval-source", default="synthetic", type=click.Choice(["synthetic", "golden", "sessiondb"]),
              help="Source for evaluation dataset")
@click.option("--dataset-path", default=None, help="Path to existing eval dataset (JSONL)")
@click.option("--optimizer-model", default=lambda: os.getenv("EVOLUTION_OPTIMIZER_MODEL", "openai/cx/gpt-5.3-codex-spark"), help="Model for GEPA reflections / MIPRO prompt_model (default: $EVOLUTION_OPTIMIZER_MODEL or codex-spark)")
@click.option("--eval-model", default=lambda: os.getenv("EVOLUTION_EVAL_MODEL", "openai/cx/gpt-5.4"), help="Model for evaluations / judge (default: $EVOLUTION_EVAL_MODEL or gpt-5.4)")
@click.option("--task-model", default=None, help="Model for inner-loop rollout generation; defaults to EVOLUTION_TASK_MODEL or eval-model")
@click.option("--hermes-repo", default=None, help="Path to hermes-agent repo")
@click.option("--run-tests", is_flag=True, help="Run full pytest suite as constraint gate")
@click.option("--dry-run", is_flag=True, help="Validate setup without running optimization")
@click.option("--mode", type=click.Choice(["propose", "auto"]), default="propose",
              help="propose: write to review queue (Task 3); auto: overwrite live skill if gate passes")
@click.option("--optimizer", type=click.Choice(["auto", "gepa", "miprov2", "mipro"]), default="auto",
              help="Optimizer to use: auto routes to the stable default for the current metric mode. 'mipro' is an alias for 'miprov2'.")
@click.option("--optimizer-timeout", default=None, type=int,
              help="Wall-clock timeout in seconds for optimizer.compile (default: EVOLUTION_OPTIMIZER_TIMEOUT or 900)")
@click.option("--min-improvement", default=0.02, type=float, help="Minimum holdout Δ for auto-merge")
@click.option("--regression-tolerance", default=0.01, type=float, help="Negative Δ tolerance before flagging regression")
@click.option("--proposals-dir", default=None, help="Where propose-only writes land (Task 3)")
def main(skill, iterations, eval_source, dataset_path, optimizer_model, eval_model, task_model,
         hermes_repo, run_tests, dry_run, mode, optimizer, optimizer_timeout,
         min_improvement, regression_tolerance, proposals_dir):
    """Evolve a Hermes Agent skill using DSPy + GEPA optimization."""
    evolve(
        skill_name=skill,
        iterations=iterations,
        eval_source=eval_source,
        dataset_path=dataset_path,
        optimizer_model=optimizer_model,
        eval_model=eval_model,
        task_model=task_model,
        hermes_repo=hermes_repo,
        run_tests=run_tests,
        dry_run=dry_run,
        mode=mode,
        optimizer=optimizer,
        optimizer_timeout=optimizer_timeout,
        min_improvement=min_improvement,
        regression_tolerance=regression_tolerance,
        proposals_dir=proposals_dir,
    )


if __name__ == "__main__":
    main()
