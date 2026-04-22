"""Tests for proposal manifest integrity + verification (Batch B)."""
from __future__ import annotations

from pathlib import Path

import pytest

from evolution.core.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    build_manifest,
    load_manifest,
    sha256_text,
    verify_evolved_artifact,
    verify_live_baseline,
    write_manifest,
)


BASELINE = "---\nname: foo\n---\n\noriginal body\n"
EVOLVED = "---\nname: foo\n---\n\nevolved body — better\n"
DIFF = "--- baseline\n+++ evolved\n@@ -1 +1 @@\n-original body\n+evolved body\n"


def _make_manifest(**overrides) -> "build_manifest":
    kwargs = dict(
        skill_name="foo",
        timestamp="20260422_050000",
        risk_tier="medium",
        baseline_text=BASELINE,
        evolved_text=EVOLVED,
        diff_text=DIFF,
    )
    kwargs.update(overrides)
    return build_manifest(**kwargs)


# ────────────────────────── build / write / load ──────────────────────────
def test_sha256_text_is_stable():
    assert sha256_text("hello") == sha256_text("hello")
    assert sha256_text("hello") != sha256_text("hello!")


def test_build_manifest_computes_all_hashes():
    m = _make_manifest()
    assert m.manifest_version == MANIFEST_VERSION
    assert m.baseline_sha256 == sha256_text(BASELINE)
    assert m.evolved_sha256 == sha256_text(EVOLVED)
    assert m.diff_sha256 == sha256_text(DIFF)
    assert m.baseline_size == len(BASELINE)
    assert m.evolved_size == len(EVOLVED)
    assert m.risk_tier == "medium"
    assert m.skill_name == "foo"
    # dspy_version / litellm_version may be None in some CI envs, but shouldn't crash


def test_write_and_load_roundtrip(tmp_path: Path):
    m = _make_manifest()
    write_manifest(tmp_path, m)
    loaded = load_manifest(tmp_path)
    assert loaded is not None
    assert loaded.baseline_sha256 == m.baseline_sha256
    assert loaded.evolved_sha256 == m.evolved_sha256
    assert loaded.diff_sha256 == m.diff_sha256
    assert loaded.risk_tier == "medium"


def test_load_manifest_missing_returns_none(tmp_path: Path):
    assert load_manifest(tmp_path) is None


def test_load_manifest_corrupt_returns_none(tmp_path: Path):
    (tmp_path / MANIFEST_FILENAME).write_text("{not json")
    assert load_manifest(tmp_path) is None


def test_forward_compat_unknown_fields_captured_in_extra(tmp_path: Path):
    """A newer manifest with extra fields must not crash an older reader."""
    payload = {
        "manifest_version": 1,
        "skill_name": "foo",
        "timestamp": "20260422_050000",
        "risk_tier": "medium",
        "baseline_sha256": "a" * 64,
        "evolved_sha256": "b" * 64,
        "diff_sha256": "c" * 64,
        "baseline_size": 10,
        "evolved_size": 12,
        "created_at": "2026-04-22T05:00:00+00:00",
        "future_field_that_didnt_exist_yet": "surprise",
    }
    import json
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps(payload))
    loaded = load_manifest(tmp_path)
    assert loaded is not None
    assert loaded.extra.get("future_field_that_didnt_exist_yet") == "surprise"


# ──────────────────────────── verify_live_baseline ────────────────────────────
def test_verify_live_baseline_matches(tmp_path: Path):
    live = tmp_path / "SKILL.md"
    live.write_text(BASELINE)
    m = _make_manifest()
    result = verify_live_baseline(m, live)
    assert result.ok
    assert result.live_sha256 == m.baseline_sha256


def test_verify_live_baseline_drifted(tmp_path: Path):
    live = tmp_path / "SKILL.md"
    live.write_text(BASELINE + "\n# drift\n")
    m = _make_manifest()
    result = verify_live_baseline(m, live)
    assert not result.ok
    assert "drifted" in result.reason
    assert result.live_sha256 != m.baseline_sha256


def test_verify_live_baseline_missing_file(tmp_path: Path):
    m = _make_manifest()
    result = verify_live_baseline(m, tmp_path / "missing.md")
    assert not result.ok
    assert "missing" in result.reason


# ──────────────────────────── verify_evolved_artifact ────────────────────────────
def test_verify_evolved_artifact_matches(tmp_path: Path):
    m = _make_manifest()
    (tmp_path / "evolved_skill.md").write_text(EVOLVED)
    result = verify_evolved_artifact(m, tmp_path)
    assert result.ok


def test_verify_evolved_artifact_tampered(tmp_path: Path):
    m = _make_manifest()
    (tmp_path / "evolved_skill.md").write_text(EVOLVED + "\n# tamper\n")
    result = verify_evolved_artifact(m, tmp_path)
    assert not result.ok
    assert "tampered" in result.reason


def test_verify_evolved_artifact_missing(tmp_path: Path):
    m = _make_manifest()
    result = verify_evolved_artifact(m, tmp_path)
    assert not result.ok
    assert "missing" in result.reason
