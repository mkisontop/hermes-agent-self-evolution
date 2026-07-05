"""Configuration and hermes-agent repo discovery."""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EvolutionConfig:
    """Configuration for a self-evolution optimization run.

    ``hermes_agent_path`` is discovered lazily: constructing a config never
    fails, only operations that actually need the repo do (via
    ``require_hermes_agent_path``). This keeps offline paths — constraint
    validation, proposal review, digests, tests — usable without a
    hermes-agent checkout.
    """

    # hermes-agent repo path (None until discovered or explicitly set)
    hermes_agent_path: Optional[Path] = field(
        default_factory=lambda: discover_hermes_agent_path()
    )

    # Optimization parameters
    iterations: int = 10
    population_size: int = 5

    # LLM configuration
    optimizer_model: str = "openai/gpt-4.1"  # Model for GEPA reflections
    eval_model: str = "openai/gpt-4.1-mini"  # Model for LLM-as-judge scoring
    judge_model: str = "openai/gpt-4.1"  # Model for dataset generation

    # Constraints
    max_skill_size: int = 15_000  # 15KB default
    max_tool_desc_size: int = 500  # chars
    max_param_desc_size: int = 200  # chars
    max_prompt_growth: float = 0.2  # 20% max growth over baseline

    # Eval dataset
    eval_dataset_size: int = 20  # Total examples to generate
    train_ratio: float = 0.5
    val_ratio: float = 0.25
    holdout_ratio: float = 0.25

    # Benchmark gating
    run_pytest: bool = True
    run_tblite: bool = False  # Expensive — opt-in
    tblite_regression_threshold: float = 0.02  # Max 2% regression allowed

    # Output
    output_dir: Path = field(default_factory=lambda: Path("./output"))
    create_pr: bool = True

    def require_hermes_agent_path(self) -> Path:
        """Return the hermes-agent repo path, raising if it cannot be found."""
        if self.hermes_agent_path is not None:
            return self.hermes_agent_path
        raise FileNotFoundError(
            "Cannot find hermes-agent repo. Set HERMES_AGENT_REPO env var "
            "or ensure it exists at ~/.hermes/hermes-agent"
        )


def discover_hermes_agent_path() -> Optional[Path]:
    """Discover the hermes-agent repo path, or None if not found.

    Priority:
    1. HERMES_AGENT_REPO env var
    2. ~/.hermes/hermes-agent (standard install location)
    3. ../hermes-agent (sibling directory)
    """
    env_path = os.getenv("HERMES_AGENT_REPO")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p

    home_path = Path.home() / ".hermes" / "hermes-agent"
    if home_path.exists():
        return home_path

    sibling_path = Path(__file__).parent.parent.parent / "hermes-agent"
    if sibling_path.exists():
        return sibling_path

    return None


def get_hermes_agent_path() -> Path:
    """Discover the hermes-agent repo path, raising if not found."""
    path = discover_hermes_agent_path()
    if path is None:
        raise FileNotFoundError(
            "Cannot find hermes-agent repo. Set HERMES_AGENT_REPO env var "
            "or ensure it exists at ~/.hermes/hermes-agent"
        )
    return path
