"""Tests for Batch B write-back hardening: root guard, symlink refusal,
atomic-write, post-write rollback."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from evolution.core.write_back import (
    WriteBackRejected,
    WriteBackResult,
    write_back_skill,
)


BASELINE = "---\nname: foo\n---\n\nbody v1\n"
EVOLVED = "---\nname: foo\n---\n\nbody v2\n"


# ────────────────────────── root guard ──────────────────────────
def test_root_guard_rejects_out_of_root_path(tmp_path: Path, monkeypatch):
    """Even with the conftest autouse fixture allowing tmp_path, an explicit
    allowed_roots=[unrelated] must refuse writes outside it."""
    live = tmp_path / "SKILL.md"
    live.write_text(BASELINE)
    unrelated_root = tmp_path / "not-our-tree"
    unrelated_root.mkdir()
    with pytest.raises(WriteBackRejected, match="root guard"):
        write_back_skill(
            live, EVOLVED, mode="auto", auto_merge=True,
            allowed_roots=[unrelated_root.resolve()],
        )


def test_root_guard_accepts_in_root_path(tmp_path: Path):
    allowed = tmp_path / "skills"
    allowed.mkdir()
    live = allowed / "SKILL.md"
    live.write_text(BASELINE)
    result = write_back_skill(
        live, EVOLVED, mode="auto", auto_merge=True,
        allowed_roots=[allowed.resolve()],
    )
    assert result.merged
    assert live.read_text() == EVOLVED


# ────────────────────────── symlink refusal ──────────────────────────
def test_symlink_refusal_for_live_path(tmp_path: Path):
    """If live_path itself is a symlink, refuse the write outright."""
    real = tmp_path / "real_SKILL.md"
    real.write_text(BASELINE)
    link = tmp_path / "SKILL.md"
    link.symlink_to(real)
    with pytest.raises(WriteBackRejected, match="symlink"):
        write_back_skill(
            link, EVOLVED, mode="auto", auto_merge=True,
            allowed_roots=[tmp_path.resolve()],
        )


def test_symlink_refusal_for_parent_dir(tmp_path: Path):
    """If any ancestor directory up to the root is a symlink, refuse."""
    real_dir = tmp_path / "real_skills"
    real_dir.mkdir()
    link_dir = tmp_path / "skills"
    link_dir.symlink_to(real_dir)
    live = link_dir / "SKILL.md"
    (real_dir / "SKILL.md").write_text(BASELINE)
    with pytest.raises(WriteBackRejected, match="symlink"):
        write_back_skill(
            live, EVOLVED, mode="auto", auto_merge=True,
            allowed_roots=[tmp_path.resolve()],
        )


# ────────────────────────── atomic-write semantics ──────────────────────────
def test_atomic_write_leaves_no_temp_file_on_success(tmp_path: Path):
    live = tmp_path / "SKILL.md"
    live.write_text(BASELINE)
    result = write_back_skill(
        live, EVOLVED, mode="auto", auto_merge=True,
        allowed_roots=[tmp_path.resolve()],
    )
    assert result.merged
    # No leftover .tmp files in the parent dir
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".SKILL.md.") and p.suffix == ".tmp"]
    assert leftover == []


def test_atomic_write_content_exact(tmp_path: Path):
    live = tmp_path / "SKILL.md"
    live.write_text(BASELINE)
    weird = "---\nname: x\n---\n\nutf-8: café · 日本語 · ✅\n"
    result = write_back_skill(
        live, weird, mode="auto", auto_merge=True,
        allowed_roots=[tmp_path.resolve()],
    )
    assert result.merged
    assert live.read_text(encoding="utf-8") == weird


# ────────────────────────── post-write verifier + rollback ──────────────────────────
def test_post_write_verifier_failure_rolls_back(tmp_path: Path):
    live = tmp_path / "SKILL.md"
    live.write_text(BASELINE)

    def verifier(p: Path) -> bool:
        # Simulate a smoke-test failure after write.
        return False

    result = write_back_skill(
        live, EVOLVED, mode="auto", auto_merge=True,
        allowed_roots=[tmp_path.resolve()],
        post_write_verifier=verifier,
    )
    assert not result.merged
    assert result.rolled_back
    # Original content restored from backup
    assert live.read_text() == BASELINE
    # Backup file exists
    assert result.backup_path is not None
    assert result.backup_path.exists()


def test_post_write_verifier_success_keeps_new_content(tmp_path: Path):
    live = tmp_path / "SKILL.md"
    live.write_text(BASELINE)

    def verifier(p: Path) -> bool:
        # Post-write sanity check passes.
        return p.read_text() == EVOLVED

    result = write_back_skill(
        live, EVOLVED, mode="auto", auto_merge=True,
        allowed_roots=[tmp_path.resolve()],
        post_write_verifier=verifier,
    )
    assert result.merged
    assert not result.rolled_back
    assert live.read_text() == EVOLVED


def test_post_write_verifier_exception_rolls_back(tmp_path: Path):
    live = tmp_path / "SKILL.md"
    live.write_text(BASELINE)

    def angry(p: Path) -> bool:
        raise RuntimeError("verifier exploded")

    result = write_back_skill(
        live, EVOLVED, mode="auto", auto_merge=True,
        allowed_roots=[tmp_path.resolve()],
        post_write_verifier=angry,
    )
    assert not result.merged
    assert result.rolled_back
    assert live.read_text() == BASELINE
    assert "verifier exploded" in result.reason
