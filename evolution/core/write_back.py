"""Auto-mode write-back: safely overwrite a live skill with an evolved version.

Write-back is only invoked when:
  - mode == "auto"
  - decision.auto_merge is True
  - constraints all passed
  - improvement >= min_improvement
  - no regression

Batch B (2026-04-22) hardening:
  - root guard: refuse write-back to paths that resolve outside an allowed
    set of roots (prevents accidental or malicious escape)
  - symlink refusal: reject if live_path (or any parent up to the root)
    is a symlink — symlinks are a silent rewrite trap
  - same-device check: tempfile must be on the same filesystem as live_path
    for ``os.replace`` to be truly atomic (APFS same-volume invariant)
  - fsync + parent-dir fsync: ensure data hits disk before ``os.replace``
    swaps the inode, and again so the rename itself is durable
  - rollback hook: if a post-write verifier callback is supplied and
    returns False, restore from the backup before returning

Backup policy: before any overwrite, the current file is copied to
``{live_path.parent}/.backups/{name}.{timestamp}.bak``. The backup is
preserved on both success and rollback so an external reviewer can always
see the previous state.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional


class WriteBackRejected(RuntimeError):
    """Raised when write-back is refused for a safety reason (not a gate reject)."""


@dataclass
class WriteBackResult:
    """Outcome of an auto-merge write-back."""
    merged: bool                 # True if the live skill was overwritten
    live_path: Path              # Path to the skill that was (or would be) overwritten
    backup_path: Optional[Path]  # Path to the .bak file (None if merged=False)
    reason: str                  # Human-readable explanation
    rolled_back: bool = False    # True if we wrote then restored from backup

    def to_dict(self) -> dict:
        d = asdict(self)
        d["live_path"] = str(self.live_path)
        d["backup_path"] = str(self.backup_path) if self.backup_path else None
        return d


def _default_allowed_roots() -> list[Path]:
    """Roots that write-back is allowed to touch. Resolved, absolute paths."""
    candidates = [
        os.getenv("HERMES_AGENT_PATH"),
        os.path.expanduser("~/.hermes/hermes-agent"),
        os.path.expanduser("~/.hermes"),
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for c in candidates:
        if not c:
            continue
        try:
            p = Path(c).expanduser().resolve()
        except Exception:
            continue
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _is_under(path: Path, roots: Iterable[Path]) -> bool:
    """True if ``path`` is equal to or contained within any of ``roots``."""
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        return False
    for r in roots:
        try:
            resolved.relative_to(r)
            return True
        except ValueError:
            continue
    return False


def _reject_if_symlink_chain(path: Path, stop_at: Path) -> Optional[str]:
    """Return a rejection reason if ``path`` or any ancestor up to ``stop_at``
    is a symlink. ``stop_at`` is the allowed-root boundary."""
    p = path
    while True:
        if p.is_symlink():
            return f"symlink refusal: {p} is a symlink"
        if p == stop_at or p.parent == p:
            return None
        p = p.parent


def _fsync_path(path: Path) -> None:
    """fsync a file or directory so its contents/metadata hit disk."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _assert_same_device(a: Path, b: Path) -> None:
    """Refuse if two paths live on different filesystems.

    ``os.replace`` is only atomic within a single filesystem. If the
    tempfile we wrote lives on a different device than the target, the
    rename turns into a copy+delete and loses atomicity. We force the
    tempfile into ``target.parent`` to avoid this, but re-check here as
    a defensive invariant.
    """
    try:
        dev_a = os.stat(a).st_dev
        dev_b = os.stat(b).st_dev
    except FileNotFoundError:
        return  # caller handles missing files
    if dev_a != dev_b:
        raise WriteBackRejected(
            f"cross-device write refused: {a} (dev={dev_a}) vs "
            f"{b} (dev={dev_b}) — atomic rename would degrade to copy+delete"
        )


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically.

    Strategy:
      1. Create temp file in the same directory as target (guarantees same FS)
      2. Write full content
      3. fsync the temp file (data -> disk)
      4. ``os.replace(tmp, target)`` (atomic rename on POSIX, same FS)
      5. fsync the parent directory (rename metadata -> disk)

    If any step fails before step 4, the temp file is removed and the
    original is untouched. After step 4, the original is gone and the new
    content is live; step 5 only hardens durability.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Same-device check (tempfile is in parent → should always match,
        # but verify so a misconfigured TMPDIR can't silently break us).
        if target.exists():
            _assert_same_device(tmp_path, target)
        os.replace(str(tmp_path), str(target))
    except Exception:
        # Clean up temp file only if the rename never happened.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    # Durability fence on the rename metadata.
    try:
        _fsync_path(parent)
    except OSError:
        pass  # fsync on directory isn't supported everywhere; don't fail the write


def write_back_skill(
    live_path: Path,
    evolved_text: str,
    *,
    mode: str,
    auto_merge: bool,
    timestamp: Optional[str] = None,
    backup_dir: Optional[Path] = None,
    allowed_roots: Optional[list[Path]] = None,
    post_write_verifier: Optional[Callable[[Path], bool]] = None,
) -> WriteBackResult:
    """Atomically overwrite a live skill with evolved content.

    Args:
        live_path: Path to the live SKILL.md to overwrite.
        evolved_text: Full evolved skill text (frontmatter + body).
        mode: "auto" or "propose". Only "auto" triggers write-back.
        auto_merge: The gate decision. False => no write.
        timestamp: YYYYMMDD_HHMMSS for backup naming. Auto-generated if None.
        backup_dir: Optional directory to store backups. Defaults to
                    ``live_path.parent / ".backups"``.
        allowed_roots: Override the default allowed-roots set. Must be a
                    list of absolute, resolved Paths.
        post_write_verifier: Optional callback invoked after the atomic
                    write. If it returns False, we roll back from the
                    backup and return merged=False, rolled_back=True.

    Returns:
        WriteBackResult describing what happened.

    Raises:
        FileNotFoundError: if ``live_path`` does not exist (nothing to back up).
        WriteBackRejected: if a safety invariant was violated (root guard,
                    symlink, cross-device).
    """
    if mode != "auto":
        return WriteBackResult(
            merged=False,
            live_path=live_path,
            backup_path=None,
            reason=f"mode={mode!r} is not 'auto' — no write-back",
        )
    if not auto_merge:
        return WriteBackResult(
            merged=False,
            live_path=live_path,
            backup_path=None,
            reason="gate rejected auto-merge — no write-back",
        )
    if not live_path.exists():
        raise FileNotFoundError(
            f"Cannot write back: live_path does not exist: {live_path}"
        )

    # ── Safety invariants (Batch B) ─────────────────────────────────────
    roots = allowed_roots if allowed_roots is not None else _default_allowed_roots()
    if not _is_under(live_path, roots):
        raise WriteBackRejected(
            f"root guard: {live_path} resolves outside allowed roots "
            f"({', '.join(str(r) for r in roots)})"
        )

    # Pick the root this path lives under, so symlink walk stops at the boundary.
    resolved = live_path.resolve(strict=False)
    containing_root: Optional[Path] = None
    for r in roots:
        try:
            resolved.relative_to(r)
            containing_root = r
            break
        except ValueError:
            continue
    if containing_root is None:
        # Belt-and-suspenders: _is_under said yes, this should too.
        raise WriteBackRejected(f"root guard: no containing root for {live_path}")

    symlink_reason = _reject_if_symlink_chain(live_path, containing_root)
    if symlink_reason is not None:
        raise WriteBackRejected(symlink_reason)

    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    if backup_dir is None:
        backup_dir = live_path.parent / ".backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{live_path.name}.{ts}.bak"

    # 1. Back up current content (copy2 preserves mtime/mode — useful for audits).
    shutil.copy2(live_path, backup_path)

    # 2. Atomic overwrite.
    try:
        _atomic_write(live_path, evolved_text)
    except Exception as e:
        # Write never committed. Backup is untouched (source is still the original).
        return WriteBackResult(
            merged=False,
            live_path=live_path,
            backup_path=backup_path,
            reason=f"atomic write failed: {type(e).__name__}: {e}",
        )

    # 3. Post-write verification (e.g. pytest smoke). Rollback on failure.
    if post_write_verifier is not None:
        try:
            ok = bool(post_write_verifier(live_path))
        except Exception as e:
            ok = False
            verifier_err = f"{type(e).__name__}: {e}"
        else:
            verifier_err = None
        if not ok:
            # Restore from backup using the same atomic write path so the
            # rollback itself is durable.
            try:
                _atomic_write(live_path, backup_path.read_text())
            except Exception as re:
                return WriteBackResult(
                    merged=False,  # state is now indeterminate; reviewer must inspect
                    live_path=live_path,
                    backup_path=backup_path,
                    reason=(
                        f"post-write verifier failed AND rollback failed: "
                        f"verifier={verifier_err}; rollback={type(re).__name__}: {re}"
                    ),
                    rolled_back=False,
                )
            return WriteBackResult(
                merged=False,
                live_path=live_path,
                backup_path=backup_path,
                reason=(
                    f"post-write verifier failed — rolled back from {backup_path.name}"
                    + (f" ({verifier_err})" if verifier_err else "")
                ),
                rolled_back=True,
            )

    return WriteBackResult(
        merged=True,
        live_path=live_path,
        backup_path=backup_path,
        reason=(
            f"mode=auto + gate approved — wrote {len(evolved_text)} chars, "
            f"backup at {backup_path.name}"
        ),
    )
