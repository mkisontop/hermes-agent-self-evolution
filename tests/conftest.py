"""Test fixtures for write_back hardening.

Batch B added a root-guard to ``write_back_skill`` that refuses writes
outside a small set of allowed roots (defaults: ``~/.hermes`` and
``~/.hermes/hermes-agent``). Existing tests operate on ``tmp_path`` which
lives under ``/private/var/folders/...`` on macOS and would otherwise be
rejected by the guard.

We auto-patch ``_default_allowed_roots`` to include the pytest session's
``basetemp`` directory so the guard still fires for truly-out-of-root
paths but allows tmp_path-based tests to exercise the write path.

Tests that specifically want to validate the guard can still pass an
explicit ``allowed_roots=[...]`` — explicit arg beats the default.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _allow_tmp_roots_in_write_back(monkeypatch, tmp_path: Path):
    """Add the test's tmp_path tree to the allowed-roots set for write_back."""
    from evolution.core import write_back as _wb

    real_defaults = _wb._default_allowed_roots

    def _with_tmp() -> list[Path]:
        roots = list(real_defaults())
        # Include both the specific tmp_path and its parent tree so backup
        # dirs under tmp_path.parent also resolve under an allowed root.
        try:
            resolved = tmp_path.resolve()
            if resolved not in roots:
                roots.append(resolved)
            parent = resolved.parent
            if parent not in roots:
                roots.append(parent)
        except Exception:
            pass
        return roots

    monkeypatch.setattr(_wb, "_default_allowed_roots", _with_tmp)
    yield
