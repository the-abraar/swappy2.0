"""Whole-file copy: the normal, fast path for files that fit on the
destination drive. Runs on the coordinator's worker threads.

Lifecycle of one copy (entry.state stays 'pending' until the very end):
    copy src -> dst        (Win32 CopyFileExW on Windows, buffered Python elsewhere)
    fsync dst              (data durable before we destroy the source)
    verify size
    delete src
    mark entry 'done'      (single SQLite write — done by the COORDINATOR
                            when the worker's CopyResult comes back; worker
                            threads never touch the database)

Crash recovery is coarse and idempotent: if we crash anywhere before the
final mark, the entry is still 'pending' and the whole copy re-runs.
Re-copying over a partial destination is harmless. The only special case
is a crash between `delete src` and `mark done` — detected on the next
run by "source gone but destination complete", which just marks it done.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import progress, winfs
from .db import Entry, ProgressFileHistory, Session

_COPY_BUFFER = 8 * 1024 * 1024
_PROGRESS_GRANULARITY = 8 * 1024 * 1024  # report cadence; matches the python backend


@dataclass(frozen=True)
class CopyResult:
    history: ProgressFileHistory


def copy_file(session: Session, entry: Entry, *, worker_id: int) -> CopyResult:
    src = session.src_path(entry)
    dst = session.dst_path(entry)

    progress.worker_started(worker_id, entry, mode="whole-file", backend=session.backend)

    if not src.exists():
        return CopyResult(
            history=_recover_completed_copy(entry, dst, worker_id, session.backend),
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    if session.backend in ("winapi", "robocopy"):  # robocopy: legacy sessions resume here
        result = _winapi_copy(src, dst, entry, worker_id)
    elif session.backend == "python":
        result = _python_copy(src, dst, entry, worker_id)
    else:
        raise ValueError(f"unknown copy backend: {session.backend}")

    src.unlink()
    return result


def _recover_completed_copy(
    entry: Entry,
    dst: Path,
    worker_id: int,
    backend: str,
) -> ProgressFileHistory:
    """Source already deleted: only legal if the destination is complete."""
    if dst.exists() and dst.stat().st_size == entry.size:
        now = time.time()
        progress.worker_progress(worker_id, bytes_copied=entry.size or 0, note="recovered")
        return ProgressFileHistory(
            entry_id=entry.id,
            rel_path=entry.rel_path,
            size=entry.size or 0,
            worker_id=worker_id,
            mode="whole-file",
            backend=backend,
            started_at=now,
            finished_at=now,
            duration_seconds=0.0,
            throughput_bps=None,
            read_seconds=0.0,
            write_seconds=0.0,
            verify_seconds=0.0,
            chunk_seconds=None,
            notes="recovered after restart",
        )
    raise RuntimeError(
        f"source missing and destination incomplete for {entry.rel_path};"
        " refusing to continue"
    )


def _python_copy(src: Path, dst: Path, entry: Entry, worker_id: int) -> CopyResult:
    started_at = time.time()
    read_seconds = 0.0
    write_seconds = 0.0
    bytes_copied = 0

    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            read_started = time.perf_counter()
            block = fsrc.read(_COPY_BUFFER)
            read_seconds += time.perf_counter() - read_started
            if not block:
                break

            write_started = time.perf_counter()
            fdst.write(block)
            write_seconds += time.perf_counter() - write_started
            bytes_copied += len(block)

            elapsed = max(time.time() - started_at, 1e-9)
            progress.worker_progress(
                worker_id,
                bytes_copied=bytes_copied,
                throughput_bps=bytes_copied / elapsed,
            )

        flush_started = time.perf_counter()
        fdst.flush()
        write_seconds += time.perf_counter() - flush_started

    winfs.fsync_file(dst)
    copy_stat_started = time.perf_counter()
    shutil.copystat(src, dst)
    write_seconds += time.perf_counter() - copy_stat_started

    verify_started = time.perf_counter()
    dst_size = dst.stat().st_size
    verify_seconds = time.perf_counter() - verify_started
    if dst_size != entry.size:
        raise RuntimeError(
            f"copy verification failed: {dst} is {dst_size} bytes, expected {entry.size}"
        )

    finished_at = time.time()
    duration = max(finished_at - started_at, 0.0)
    throughput = (entry.size or 0) / duration if duration > 0 else None
    return CopyResult(
        history=ProgressFileHistory(
            entry_id=entry.id,
            rel_path=entry.rel_path,
            size=entry.size or 0,
            worker_id=worker_id,
            mode="whole-file",
            backend="python",
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            throughput_bps=throughput,
            read_seconds=read_seconds,
            write_seconds=write_seconds,
            verify_seconds=verify_seconds,
            chunk_seconds=None,
        )
    )


def _winapi_copy(src: Path, dst: Path, entry: Entry, worker_id: int) -> CopyResult:
    started_at = time.time()
    last_reported = 0

    def on_progress(total: int, transferred: int) -> None:
        # The kernel reports much more often than the python backend's 8 MB
        # blocks; throttle to the same cadence to stay off the hot path.
        nonlocal last_reported
        if transferred - last_reported < _PROGRESS_GRANULARITY and transferred != total:
            return
        last_reported = transferred
        elapsed = max(time.time() - started_at, 1e-9)
        progress.worker_progress(
            worker_id,
            bytes_copied=transferred,
            throughput_bps=transferred / elapsed,
        )

    winfs.copy_file_with_progress(src, dst, on_progress)

    winfs.fsync_file(dst)
    write_started = time.perf_counter()
    shutil.copystat(src, dst)  # CopyFileExW keeps mtime but not all stat fields
    write_seconds = time.perf_counter() - write_started

    verify_started = time.perf_counter()
    dst_size = dst.stat().st_size
    verify_seconds = time.perf_counter() - verify_started
    if dst_size != entry.size:
        raise RuntimeError(
            f"copy verification failed: {dst} is {dst_size} bytes, expected {entry.size}"
        )

    finished_at = time.time()
    duration = max(finished_at - started_at, 0.0)
    throughput = (entry.size or 0) / duration if duration > 0 else None
    progress.worker_progress(
        worker_id,
        bytes_copied=entry.size or 0,
        throughput_bps=throughput,
    )
    return CopyResult(
        history=ProgressFileHistory(
            entry_id=entry.id,
            rel_path=entry.rel_path,
            size=entry.size or 0,
            worker_id=worker_id,
            mode="whole-file",
            backend="winapi",
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            throughput_bps=throughput,
            read_seconds=None,   # kernel copy: no read/write split available
            write_seconds=write_seconds,
            verify_seconds=verify_seconds,
            chunk_seconds=None,
        )
    )
