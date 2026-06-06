"""Whole-file copy: the normal, fast path for files that fit on the
destination drive.

Lifecycle of one copy (entry.state stays 'pending' until the very end):
    copy src -> dst        (robocopy on Windows, buffered Python elsewhere)
    fsync dst              (data durable before we destroy the source)
    verify size
    delete src
    mark entry 'done'      (single SQLite write)

Crash recovery is coarse and idempotent: if we crash anywhere before the
final mark, the entry is still 'pending' and the whole copy re-runs.
Re-copying over a partial destination is harmless. The only special case
is a crash between `delete src` and `mark done` — detected on the next
run by "source gone but destination complete", which just marks it done.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import winfs
from .db import Database, Entry, Session

_COPY_BUFFER = 8 * 1024 * 1024
_ROBOCOPY_OK_EXIT_CODES = frozenset(range(8))


def copy_file(db: Database, session: Session, entry: Entry) -> None:
    src = session.src_path(entry)
    dst = session.dst_path(entry)

    if not src.exists():
        _recover_completed_copy(db, entry, dst)
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    _copy(src, dst, backend=session.backend)
    winfs.fsync_file(dst)

    if dst.stat().st_size != entry.size:
        raise RuntimeError(
            f"copy verification failed: {dst} is {dst.stat().st_size} bytes,"
            f" expected {entry.size}"
        )

    src.unlink()
    db.set_entry_state(entry.id, "done")


def _recover_completed_copy(db: Database, entry: Entry, dst: Path) -> None:
    """Source already deleted: only legal if the destination is complete."""
    if dst.exists() and dst.stat().st_size == entry.size:
        db.set_entry_state(entry.id, "done")
        return
    raise RuntimeError(
        f"source missing and destination incomplete for {entry.rel_path};"
        " refusing to continue"
    )


def _copy(src: Path, dst: Path, *, backend: str) -> None:
    if backend == "robocopy":
        _robocopy(src, dst)
    elif backend == "python":
        _python_copy(src, dst)
    else:
        raise ValueError(f"unknown copy backend: {backend}")


def _robocopy(src: Path, dst: Path) -> None:
    command = [
        "robocopy", str(src.parent), str(dst.parent), src.name,
        "/COPY:DAT", "/R:1", "/W:1", "/NFL", "/NDL", "/NP", "/NJH", "/NJS",
    ]
    if src.name != dst.name:
        raise ValueError("robocopy requires matching source/destination file names")
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode not in _ROBOCOPY_OK_EXIT_CODES:
        raise RuntimeError(
            f"robocopy failed with exit code {completed.returncode}:"
            f" {completed.stderr or completed.stdout}"
        )


def _python_copy(src: Path, dst: Path) -> None:
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, _COPY_BUFFER)
    shutil.copystat(src, dst)
