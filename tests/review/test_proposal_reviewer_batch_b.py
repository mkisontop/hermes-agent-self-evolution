"""Tests for Batch B reviewer integration: manifest guard, stale baseline,
CRITICAL risk refusal in the approve CLI."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

from evolution.core.manifest import build_manifest, write_manifest
from evolution.core.proposals import (
    PROPOSAL_STATUS_APPROVED,
    ProposalWriter,
    build_proposal_record,
)
from evolution.review import proposal_reviewer as pr


# Import test fixture helpers from the main reviewer test file
from tests.review.test_proposal_reviewer import (
    _FakeDecision,
    _FakeConstraint,
    _write_proposal,
)


LIVE_BASELINE = "---\nname: skill-a\ndescription: live\n---\n\nlive body\n"
EVOLVED_CONTENT = "---\nname: skill-a\ndescription: evolved\n---\n\nevolved body\n"


@pytest.fixture
def proposals_dir(tmp_path: Path) -> Path:
    d = tmp_path / "proposals"
    d.mkdir()
    return d


def _make_live_skill(tmp_path: Path, skill_name: str, content: str) -> Path:
    root = tmp_path / "hermes-agent"
    sd = root / "skills" / "test-category" / skill_name
    sd.mkdir(parents=True)
    sk = sd / "SKILL.md"
    sk.write_text(content)
    return sk


# ───────────────────────── manifest missing ─────────────────────────
def test_approve_refuses_proposal_without_manifest(proposals_dir: Path, tmp_path: Path, capsys):
    _write_proposal(
        proposals_dir, "skill-a", "20260422_050000",
        baseline=LIVE_BASELINE, evolved=EVOLVED_CONTENT,
        with_manifest=False,
    )
    _make_live_skill(tmp_path, "skill-a", LIVE_BASELINE)
    rc = pr.main([
        "--proposals-dir", str(proposals_dir),
        "approve", "skill-a", "20260422_050000",
        "--hermes-agent-path", str(tmp_path / "hermes-agent"),
    ])
    assert rc == 6  # missing manifest rejection code
    err = capsys.readouterr().err
    assert "manifest.json missing" in err


def test_allow_no_manifest_proceeds_without_integrity_check(proposals_dir: Path, tmp_path: Path):
    _write_proposal(
        proposals_dir, "skill-a", "20260422_050000",
        baseline=LIVE_BASELINE, evolved=EVOLVED_CONTENT,
        with_manifest=False,
    )
    _make_live_skill(tmp_path, "skill-a", LIVE_BASELINE)
    rc = pr.main([
        "--proposals-dir", str(proposals_dir),
        "approve", "skill-a", "20260422_050000",
        "--allow-no-manifest",
        "--hermes-agent-path", str(tmp_path / "hermes-agent"),
    ])
    assert rc == 0


# ───────────────────────── stale-baseline guard ─────────────────────────
def test_approve_refuses_stale_baseline(proposals_dir: Path, tmp_path: Path, capsys):
    _write_proposal(
        proposals_dir, "skill-a", "20260422_050000",
        baseline=LIVE_BASELINE, evolved=EVOLVED_CONTENT,
    )
    # live diverges from the baseline the manifest was computed over.
    drifted = LIVE_BASELINE + "\n# drifted after proposal was written\n"
    _make_live_skill(tmp_path, "skill-a", drifted)
    rc = pr.main([
        "--proposals-dir", str(proposals_dir),
        "approve", "skill-a", "20260422_050000",
        "--hermes-agent-path", str(tmp_path / "hermes-agent"),
    ])
    assert rc == 9  # stale-baseline rejection code
    assert "drifted" in capsys.readouterr().err


def test_approve_force_stale_bypasses_guard(proposals_dir: Path, tmp_path: Path):
    _write_proposal(
        proposals_dir, "skill-a", "20260422_050000",
        baseline=LIVE_BASELINE, evolved=EVOLVED_CONTENT,
    )
    drifted = LIVE_BASELINE + "\n# drifted\n"
    live = _make_live_skill(tmp_path, "skill-a", drifted)
    rc = pr.main([
        "--proposals-dir", str(proposals_dir),
        "approve", "skill-a", "20260422_050000",
        "--force-stale",
        "--hermes-agent-path", str(tmp_path / "hermes-agent"),
    ])
    assert rc == 0
    assert "evolved body" in live.read_text()


# ───────────────────────── tampered evolved artifact ─────────────────────────
def test_approve_refuses_tampered_evolved(proposals_dir: Path, tmp_path: Path, capsys):
    proposal_dir = _write_proposal(
        proposals_dir, "skill-a", "20260422_050000",
        baseline=LIVE_BASELINE, evolved=EVOLVED_CONTENT,
    )
    # Tamper with evolved_skill.md after the manifest was written.
    (proposal_dir / "evolved_skill.md").write_text(EVOLVED_CONTENT + "\n# injected\n")
    _make_live_skill(tmp_path, "skill-a", LIVE_BASELINE)
    rc = pr.main([
        "--proposals-dir", str(proposals_dir),
        "approve", "skill-a", "20260422_050000",
        "--hermes-agent-path", str(tmp_path / "hermes-agent"),
    ])
    assert rc == 8  # tampering rejection code
    assert "tampered" in capsys.readouterr().err


# ───────────────────────── CRITICAL-tier refusal ─────────────────────────
def test_approve_refuses_critical_tier(proposals_dir: Path, tmp_path: Path, capsys):
    _write_proposal(
        proposals_dir, "hermes-self-evolution", "20260422_050000",
        baseline=LIVE_BASELINE, evolved=EVOLVED_CONTENT,
        risk_tier="critical",
    )
    _make_live_skill(tmp_path, "hermes-self-evolution", LIVE_BASELINE)
    rc = pr.main([
        "--proposals-dir", str(proposals_dir),
        "approve", "hermes-self-evolution", "20260422_050000",
        "--hermes-agent-path", str(tmp_path / "hermes-agent"),
    ])
    assert rc == 7  # risk-tier rejection code
    assert "risk=critical" in capsys.readouterr().err


def test_force_critical_allows_critical_approval(proposals_dir: Path, tmp_path: Path):
    _write_proposal(
        proposals_dir, "hermes-self-evolution", "20260422_050000",
        baseline=LIVE_BASELINE, evolved=EVOLVED_CONTENT,
        risk_tier="critical",
    )
    live = _make_live_skill(tmp_path, "hermes-self-evolution", LIVE_BASELINE)
    rc = pr.main([
        "--proposals-dir", str(proposals_dir),
        "approve", "hermes-self-evolution", "20260422_050000",
        "--force-critical",
        "--hermes-agent-path", str(tmp_path / "hermes-agent"),
    ])
    assert rc == 0
    assert "evolved body" in live.read_text()
