"""Proposal manifest — SHA256-pinned correctness guard.

Batch B (2026-04-22). Every proposal written to disk carries a manifest.json
that pins:

    baseline_sha256   — hash of the *live* skill text at proposal time
    evolved_sha256    — hash of the evolved skill text
    diff_sha256       — hash of the unified diff between them
    baseline_size, evolved_size
    skill_name, timestamp, risk_tier
    dspy_version, litellm_version (supply-chain trace)

At approve-time, the reviewer re-reads the live skill and re-computes its
SHA256. If it differs from ``baseline_sha256`` in the manifest, the live
skill has drifted since the proposal was written — e.g., a parallel run,
a hand-edit, or a previous approval already landed. Approval then refuses
unless the reviewer explicitly passes ``--force-stale`` (an audit trail
invariant).

Evolved and diff hashes guard against post-facto tampering of the evolved
artifact: if ``evolved_skill.md`` on disk no longer matches
``evolved_sha256``, the proposal is compromised and must be rejected.

The manifest is intentionally minimal + stable-by-field. Future additions
should append fields, never reshape existing ones — reviewers may parse
old manifests for months.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


MANIFEST_FILENAME = "manifest.json"
MANIFEST_VERSION = 1


def sha256_text(text: str) -> str:
    """Stable SHA256 hex digest of a unicode string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """SHA256 hex digest of a file's raw bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pkg_version(name: str) -> Optional[str]:
    """Best-effort package version lookup; None if unavailable."""
    try:
        from importlib.metadata import version as _v
        return _v(name)
    except Exception:
        return None


@dataclass
class ProposalManifest:
    """Structural record of a proposal's identity + integrity hashes."""
    manifest_version: int
    skill_name: str
    timestamp: str
    risk_tier: str
    baseline_sha256: str
    evolved_sha256: str
    diff_sha256: str
    baseline_size: int
    evolved_size: int
    created_at: str
    dspy_version: Optional[str] = None
    litellm_version: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> "ProposalManifest":
        # Forward-compat: ignore unknown fields by filtering to known ones.
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        # extra catches any fields we don't explicitly map
        extras = {k: v for k, v in data.items() if k not in known}
        if extras:
            filtered.setdefault("extra", {}).update(extras)
        return cls(**filtered)


def build_manifest(
    *,
    skill_name: str,
    timestamp: str,
    risk_tier: str,
    baseline_text: str,
    evolved_text: str,
    diff_text: str,
    extra: Optional[dict] = None,
) -> ProposalManifest:
    """Compute all hashes and assemble a ProposalManifest."""
    return ProposalManifest(
        manifest_version=MANIFEST_VERSION,
        skill_name=skill_name,
        timestamp=timestamp,
        risk_tier=risk_tier,
        baseline_sha256=sha256_text(baseline_text),
        evolved_sha256=sha256_text(evolved_text),
        diff_sha256=sha256_text(diff_text),
        baseline_size=len(baseline_text),
        evolved_size=len(evolved_text),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dspy_version=_pkg_version("dspy"),
        litellm_version=_pkg_version("litellm"),
        extra=extra or {},
    )


def write_manifest(proposal_dir: Path, manifest: ProposalManifest) -> Path:
    """Persist the manifest under ``proposal_dir/manifest.json``."""
    proposal_dir.mkdir(parents=True, exist_ok=True)
    path = proposal_dir / MANIFEST_FILENAME
    path.write_text(manifest.to_json())
    return path


def load_manifest(proposal_dir: Path) -> Optional[ProposalManifest]:
    """Load manifest.json. Returns None if missing or unreadable."""
    path = proposal_dir / MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return ProposalManifest.from_dict(data)


# ─────────────────────────── verification ──────────────────────────────
@dataclass
class VerificationResult:
    ok: bool
    reason: str
    live_sha256: Optional[str] = None
    evolved_sha256_actual: Optional[str] = None


def verify_live_baseline(
    manifest: ProposalManifest,
    live_path: Path,
) -> VerificationResult:
    """Verify the live skill on disk still matches the baseline the proposal
    was computed against.

    Returns ok=True only when the live file exists AND its SHA256 matches
    ``manifest.baseline_sha256``. Any drift is flagged as stale — approval
    should refuse unless the reviewer explicitly overrides.
    """
    if not live_path.exists():
        return VerificationResult(
            ok=False,
            reason=f"live path missing: {live_path}",
        )
    live_text = live_path.read_text()
    live_hash = sha256_text(live_text)
    if live_hash != manifest.baseline_sha256:
        return VerificationResult(
            ok=False,
            reason=(
                f"live baseline drifted: expected "
                f"{manifest.baseline_sha256[:12]} but got {live_hash[:12]}"
            ),
            live_sha256=live_hash,
        )
    return VerificationResult(
        ok=True,
        reason="live baseline matches manifest",
        live_sha256=live_hash,
    )


def verify_evolved_artifact(
    manifest: ProposalManifest,
    proposal_dir: Path,
) -> VerificationResult:
    """Verify the on-disk evolved artifact still matches the manifest hash."""
    evolved_path = proposal_dir / "evolved_skill.md"
    if not evolved_path.exists():
        return VerificationResult(
            ok=False,
            reason=f"evolved_skill.md missing in {proposal_dir}",
        )
    evolved_text = evolved_path.read_text()
    evolved_hash = sha256_text(evolved_text)
    if evolved_hash != manifest.evolved_sha256:
        return VerificationResult(
            ok=False,
            reason=(
                f"evolved artifact tampered: expected "
                f"{manifest.evolved_sha256[:12]} but got {evolved_hash[:12]}"
            ),
            evolved_sha256_actual=evolved_hash,
        )
    return VerificationResult(
        ok=True,
        reason="evolved artifact matches manifest",
        evolved_sha256_actual=evolved_hash,
    )
