"""Shared fixtures and helpers for the swap test suite."""

from __future__ import annotations

import sys
from pathlib import Path

# Make 'swap' importable from src/
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402  (the shim from the repo root)
from swap.db import Database, Session  # noqa: E402


# ---------------------------------------------------------------------------
# built-in fixture: tmp_path is provided by the shim; no need to define it
# ---------------------------------------------------------------------------


class FakeDrives:
    """Two virtual drives: directories with a declared capacity.

    free_bytes(path) returns:
        capacity_of_drive - used_bytes_on_drive

    used_bytes is the sum of st_size of every regular file under the drive
    directory, MINUS the "hole" for each in-flight sparse destination:
      for each db entry kind='file' state='sparse':
        if dst is on this drive and both src and dst exist:
          subtract src.stat().st_size  (the unwritten part of dst)
    """

    def __init__(self, drive_a: Path, drive_b: Path, cap_a: int, cap_b: int) -> None:
        self.drive_a = drive_a
        self.drive_b = drive_b
        self.cap_a = cap_a
        self.cap_b = cap_b
        self._db: Database | None = None

    def bind_db(self, db: Database) -> None:
        self._db = db

    def _drive_for(self, path: Path) -> tuple[Path, int] | None:
        try:
            path.relative_to(self.drive_a)
            return self.drive_a, self.cap_a
        except ValueError:
            pass
        try:
            path.relative_to(self.drive_b)
            return self.drive_b, self.cap_b
        except ValueError:
            pass
        return None

    def _used(self, drive_root: Path) -> int:
        total = 0
        for p in drive_root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        return total

    def _sparse_hole(self, drive_root: Path) -> int:
        """Subtract the unwritten holes from in-flight sparse destinations."""
        if self._db is None:
            return 0
        try:
            session = self._db.session()
        except RuntimeError:
            return 0
        hole = 0
        for entry in self._db.entries(kind="file", state="sparse"):
            dst = session.dst_path(entry)
            src = session.src_path(entry)
            try:
                dst.relative_to(drive_root)
            except ValueError:
                continue
            if dst.exists() and src.exists():
                # dst has size == entry.size (full logical size), but the
                # unwritten part is src.stat().st_size bytes
                hole += src.stat().st_size
        return hole

    def free_bytes(self, path: Path) -> int:
        info = self._drive_for(path)
        if info is None:
            raise ValueError(f"path {path} is not under either fake drive")
        drive_root, cap = info
        used = self._used(drive_root) - self._sparse_hole(drive_root)
        return cap - used

    def volume_id(self, path: Path) -> object:
        info = self._drive_for(path)
        if info is None:
            raise ValueError(f"path {path} is not under either fake drive")
        return info[0]  # the drive directory Path object is the volume identity


def make_session(
    *,
    folder_a: Path,
    folder_b: Path,
    chunk_size: int = 4,
    safety_margin: int = 0,
    backend: str = "python",
    verify_chunks: bool = False,
    stage: str = "created",
) -> Session:
    """Build a Session row the same way cli._cmd_run does."""
    return Session(
        folder_a=folder_a,
        folder_b=folder_b,
        staged_a=folder_a.parent / ".swap_staged_a",
        staged_b=folder_b.parent / ".swap_staged_b",
        final_a=folder_a.parent / folder_b.name,
        final_b=folder_b.parent / folder_a.name,
        chunk_size=chunk_size,
        backend=backend,
        verify_chunks=verify_chunks,
        safety_margin=safety_margin,
        stage=stage,
    )


@pytest.fixture
def tmp_path():
    """Override provided by shim — listed here so conftest is self-documenting."""
    raise RuntimeError("tmp_path should be supplied by the shim")
