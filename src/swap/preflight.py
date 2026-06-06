"""Pipeline stage 1: refuse impossible or unsafe swaps before touching anything.

Checks (only while stage == 'created'; later stages mean the roots are
already renamed and these checks no longer apply):
  1. Both roots exist, are real directories, are not nested in each other.
  2. The roots live on distinct volumes (injectable for tests).
  3. The swap is capacity-feasible: each drive's free space AFTER the swap
     (free + outgoing - incoming) stays above the safety margin.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from .db import Database, Session

VolumeId = Callable[[Path], object]      # maps a path to a volume identity
FreeBytes = Callable[[Path], int]        # free bytes on the volume holding path


class PreflightError(ValueError):
    pass


def check(
    db: Database,
    *,
    volume_id: VolumeId | None = None,
    free_bytes: FreeBytes | None = None,
) -> None:
    session = db.session()
    if session.stage != "created":
        return  # roots already staged; validation happened on a previous run

    volume_id = volume_id or default_volume_id
    free_bytes = free_bytes or default_free_bytes

    _validate_roots(session, volume_id)
    _check_capacity(session, free_bytes)


def default_volume_id(path: Path) -> object:
    return path.resolve(strict=False).anchor.casefold()


def default_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _validate_roots(session: Session, volume_id: VolumeId) -> None:
    a = session.folder_a.resolve(strict=False)
    b = session.folder_b.resolve(strict=False)

    for root in (a, b):
        if not root.is_dir():
            raise PreflightError(f"swap root is not an existing directory: {root}")

    if _nested(a, b) or _nested(b, a) or a == b:
        raise PreflightError("swap roots must not be nested or identical")

    if volume_id(a) == volume_id(b):
        raise PreflightError("swap roots must live on distinct volumes")


def _check_capacity(session: Session, free_bytes: FreeBytes) -> None:
    size_a = tree_size(session.folder_a)
    size_b = tree_size(session.folder_b)
    final_free_a = free_bytes(session.folder_a) + size_a - size_b
    final_free_b = free_bytes(session.folder_b) + size_b - size_a

    for drive, final_free in (("A", final_free_a), ("B", final_free_b)):
        if final_free < session.safety_margin:
            raise PreflightError(
                f"swap is not capacity-feasible: drive {drive} would end with"
                f" {final_free} bytes free (< {session.safety_margin} margin)"
            )


def tree_size(root: Path) -> int:
    """Total bytes of all regular files under root (links not followed)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                total += path.stat().st_size
    return total


def _nested(child: Path, parent: Path) -> bool:
    return child != parent and child.is_relative_to(parent)
