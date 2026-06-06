"""Thread-safe live progress facade for active run and resume commands."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .db import (
    Database,
    Entry,
    ProgressFileHistory,
    ProgressSummarySnapshot,
    ProgressWorkerSnapshot,
    Session,
    make_initial_progress_summary,
)

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
    from rich.table import Table
except Exception:  # pragma: no cover - live UI is optional in tests
    Console = None
    Group = None
    Live = None
    Panel = None
    Progress = None
    BarColumn = None
    TaskProgressColumn = None
    TextColumn = None
    Table = None

_HISTORY_LIMIT = 20
_RENDER_INTERVAL_SECONDS = 0.25
_LOCK = threading.Lock()
_RUNTIME: _ProgressRuntime | None = None


@dataclass(frozen=True)
class _Totals:
    read_seconds: float = 0.0
    write_seconds: float = 0.0
    verify_seconds: float = 0.0
    chunk_seconds: float = 0.0
    chunk_rounds: int = 0
    chunk_bytes_moved: int = 0


class _ProgressRuntime:
    def __init__(self, db: Database, session: Session, *, stage: str, status_text: str) -> None:
        self._db = db
        self._session = session
        self._lock = threading.Lock()
        self._started_at = time.time()

        existing_summary = db.progress_summary()
        if existing_summary is not None:
            self._started_at = existing_summary.started_at

        bootstrap = db.progress_bootstrap(session)
        self._summary = make_initial_progress_summary(
            session=session,
            bootstrap=bootstrap,
            stage=stage,
            status_text=status_text,
            started_at=self._started_at,
        )
        self._totals = _Totals(
            read_seconds=existing_summary.read_seconds if existing_summary else 0.0,
            write_seconds=existing_summary.write_seconds if existing_summary else 0.0,
            verify_seconds=existing_summary.verify_seconds if existing_summary else 0.0,
            chunk_seconds=existing_summary.chunk_seconds if existing_summary else 0.0,
            chunk_rounds=existing_summary.chunk_rounds if existing_summary else 0,
            chunk_bytes_moved=max(
                bootstrap.sparse_bytes_copied,
                existing_summary.chunk_bytes_moved if existing_summary else 0,
            ),
        )
        self._workers = {
            worker_id: ProgressWorkerSnapshot(worker_id=worker_id, state="idle", rel_path=None,
                                              mode=None, backend=None, file_size=None,
                                              bytes_copied=0, started_at=None, updated_at=None,
                                              throughput_bps=None, elapsed_seconds=None)
            for worker_id in range(1, session.worker_count + 1)
        }
        self._history = db.recent_progress_history(_HISTORY_LIMIT)
        self._finished = False
        self._dirty = True
        self._last_render = 0.0
        self._live = self._open_live()
        self._db.reset_progress(self.snapshot())
        self._refresh_live_unlocked()

    def _open_live(self):
        if Live is None or Console is None:
            return None
        console = Console(stderr=True)
        if not console.is_terminal:
            return None
        live = Live(self._render(), console=console, refresh_per_second=4, transient=True)
        live.start()
        return live

    def close(self) -> None:
        with self._lock:
            live = self._live
            self._live = None
        if live is not None:
            live.stop()

    def set_stage(self, stage: str, status_text: str) -> None:
        with self._lock:
            self._summary = replace(self._summary, stage=stage, status_text=status_text)
            self._dirty = True
            self._refresh_live_unlocked()

    def worker_started(
        self,
        worker_id: int,
        entry: Entry,
        *,
        mode: str,
        backend: str | None,
        note: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._workers[worker_id] = ProgressWorkerSnapshot(
                worker_id=worker_id,
                state="copying" if mode == "whole-file" else "chunking",
                rel_path=entry.rel_path,
                mode=mode,
                backend=backend,
                file_size=entry.size,
                bytes_copied=0,
                started_at=now,
                updated_at=now,
                throughput_bps=None,
                elapsed_seconds=0.0,
                note=note,
            )
            self._dirty = True
            self._refresh_live_unlocked()

    def worker_progress(
        self,
        worker_id: int,
        *,
        bytes_copied: int,
        throughput_bps: float | None = None,
        note: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            worker = self._workers[worker_id]
            started_at = worker.started_at or now
            self._workers[worker_id] = replace(
                worker,
                bytes_copied=bytes_copied,
                updated_at=now,
                throughput_bps=throughput_bps,
                elapsed_seconds=max(0.0, now - started_at),
                note=note if note is not None else worker.note,
            )
            self._dirty = True
            self._refresh_live_unlocked()

    def worker_finished(self, history: ProgressFileHistory) -> None:
        now = time.time()
        with self._lock:
            pending_files = max(0, self._summary.pending_files - (history.mode == "whole-file"))
            sparse_files = max(0, self._summary.sparse_files - (history.mode == "sparse"))
            self._summary = replace(
                self._summary,
                pending_files=pending_files,
                sparse_files=sparse_files,
                done_files=self._summary.done_files + 1,
                done_bytes=self._summary.done_bytes + history.size,
                status_text=f"Completed {history.rel_path}",
                updated_at=now,
            )
            self._totals = replace(
                self._totals,
                read_seconds=self._totals.read_seconds + (history.read_seconds or 0.0),
                write_seconds=self._totals.write_seconds + (history.write_seconds or 0.0),
                verify_seconds=self._totals.verify_seconds + (history.verify_seconds or 0.0),
                chunk_seconds=self._totals.chunk_seconds + (history.chunk_seconds or 0.0),
                chunk_bytes_moved=(
                    self._totals.chunk_bytes_moved +
                    (history.size if history.mode == "sparse" else 0)
                ),
            )
            self._workers[history.worker_id] = ProgressWorkerSnapshot(
                worker_id=history.worker_id,
                state="idle",
                rel_path=None,
                mode=None,
                backend=None,
                file_size=None,
                bytes_copied=0,
                started_at=None,
                updated_at=now,
                throughput_bps=None,
                elapsed_seconds=None,
            )
            self._history.append(history)
            self._history = self._history[-_HISTORY_LIMIT:]
            self._db.append_progress_history(history)
            self._dirty = True
            self._refresh_live_unlocked()

    def worker_idle(self, worker_id: int, *, note: str | None = None, waiting: bool = False) -> None:
        now = time.time()
        with self._lock:
            self._workers[worker_id] = ProgressWorkerSnapshot(
                worker_id=worker_id,
                state="waiting" if waiting else "idle",
                rel_path=None,
                mode=None,
                backend=None,
                file_size=None,
                bytes_copied=0,
                started_at=None,
                updated_at=now,
                throughput_bps=None,
                elapsed_seconds=None,
                note=note,
            )
            self._dirty = True
            self._refresh_live_unlocked()

    def chunk_progress(
        self,
        worker_id: int,
        *,
        rel_path: str,
        chunk_bytes: int,
        total_bytes_copied: int,
        chunk_seconds: float,
        chunk_round_completed: bool,
    ) -> None:
        now = time.time()
        with self._lock:
            worker = self._workers[worker_id]
            self._workers[worker_id] = replace(
                worker,
                rel_path=rel_path,
                bytes_copied=total_bytes_copied,
                updated_at=now,
                elapsed_seconds=max(0.0, now - (worker.started_at or now)),
                throughput_bps=(chunk_bytes / chunk_seconds) if chunk_seconds > 0 else None,
                note=f"chunk {chunk_bytes} B",
            )
            self._summary = replace(
                self._summary,
                bytes_copied=min(self._summary.total_bytes, self._summary.bytes_copied + chunk_bytes),
                status_text=f"Chunking {rel_path}",
                updated_at=now,
            )
            self._totals = replace(
                self._totals,
                chunk_seconds=self._totals.chunk_seconds + chunk_seconds,
                chunk_rounds=self._totals.chunk_rounds + (1 if chunk_round_completed else 0),
                chunk_bytes_moved=self._totals.chunk_bytes_moved + chunk_bytes,
            )
            self._dirty = True
            self._refresh_live_unlocked()

    def finish(self, *, stage: str, status_text: str) -> None:
        with self._lock:
            self._summary = replace(
                self._summary,
                stage=stage,
                status_text=status_text,
                finished_at=time.time(),
            )
            self._finished = True
            self._dirty = True
            self._refresh_live_unlocked()
        self.flush(force=True)
        self.close()

    def abort(self, *, status_text: str) -> None:
        with self._lock:
            self._summary = replace(
                self._summary,
                status_text=status_text,
                updated_at=time.time(),
            )
            self._dirty = True
            self._refresh_live_unlocked()
        self.flush(force=True)
        self.close()

    def snapshot(self) -> ProgressSummarySnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def workers(self) -> list[ProgressWorkerSnapshot]:
        with self._lock:
            return [self._workers[worker_id] for worker_id in sorted(self._workers)]

    def history(self) -> list[ProgressFileHistory]:
        with self._lock:
            return list(self._history)

    def flush(self, *, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            snapshot = self._snapshot_unlocked()
            workers = [self._workers[worker_id] for worker_id in sorted(self._workers)]
            self._db.upsert_progress_summary(snapshot)
            self._db.replace_progress_workers(workers)
            self._dirty = False

    def _snapshot_unlocked(self) -> ProgressSummarySnapshot:
        now = time.time()
        active_workers = sum(
            1 for worker in self._workers.values() if worker.state in {"copying", "chunking"}
        )
        live_whole_file_bytes = sum(
            worker.bytes_copied
            for worker in self._workers.values()
            if worker.mode == "whole-file" and worker.state == "copying"
        )
        sparse_live_bytes = max(
            self._summary.bytes_copied - self._summary.done_bytes,
            0,
        )
        bytes_copied = self._summary.done_bytes + sparse_live_bytes + live_whole_file_bytes
        return replace(
            self._summary,
            active_workers=active_workers,
            bytes_copied=min(self._summary.total_bytes, bytes_copied),
            elapsed_seconds=max(0.0, now - self._started_at),
            read_seconds=self._totals.read_seconds,
            write_seconds=self._totals.write_seconds,
            verify_seconds=self._totals.verify_seconds,
            chunk_seconds=self._totals.chunk_seconds,
            chunk_rounds=self._totals.chunk_rounds,
            chunk_bytes_moved=self._totals.chunk_bytes_moved,
            updated_at=now,
        )

    def _refresh_live_unlocked(self) -> None:
        # Workers report per 8MB block, so unthrottled this would rebuild
        # and repaint the full layout hundreds of times per second under
        # the runtime lock. Rebuild at most at the Live refresh rate.
        if self._live is None:
            return
        now = time.monotonic()
        if now - self._last_render < _RENDER_INTERVAL_SECONDS:
            return
        self._last_render = now
        self._live.update(self._render(), refresh=True)

    def _render(self):
        if Group is None or Table is None or Panel is None or Progress is None:
            return ""
        summary = self._snapshot_unlocked()

        overall = Progress(
            TextColumn("[bold]Overall[/bold]"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn(
                f"{_format_bytes(summary.bytes_copied)} / {_format_bytes(summary.total_bytes)}"
            ),
        )
        overall.add_task(
            "overall",
            total=max(summary.total_bytes, 1),
            completed=summary.bytes_copied,
        )

        worker_table = Table(title="Workers", expand=True)
        worker_table.add_column("Slot", justify="right")
        worker_table.add_column("State")
        worker_table.add_column("Path")
        worker_table.add_column("Progress")
        worker_table.add_column("Copied", justify="right")
        worker_table.add_column("Speed", justify="right")
        worker_table.add_column("Elapsed", justify="right")
        for worker in self._workers.values():
            file_size = worker.file_size or 0
            progress_text = "-"
            if file_size > 0 and worker.state in {"copying", "chunking"}:
                percent = 100.0 * worker.bytes_copied / max(file_size, 1)
                progress_text = f"{percent:5.1f}%"
            worker_table.add_row(
                str(worker.worker_id),
                worker.state,
                _short_path(worker.rel_path),
                progress_text,
                _format_bytes(worker.bytes_copied),
                _format_rate(worker.throughput_bps),
                _format_seconds(worker.elapsed_seconds),
            )

        stats = Table.grid(expand=True)
        stats.add_row("Stage", summary.stage)
        stats.add_row("Status", summary.status_text)
        stats.add_row("Files", f"{summary.done_files}/{summary.total_files} done")
        stats.add_row("Elapsed", _format_seconds(summary.elapsed_seconds))
        stats.add_row("Read", _format_seconds(summary.read_seconds))
        stats.add_row("Write", _format_seconds(summary.write_seconds))
        stats.add_row("Verify", _format_seconds(summary.verify_seconds))
        stats.add_row("Chunk", _format_seconds(summary.chunk_seconds))
        stats.add_row("Chunk Rounds", str(summary.chunk_rounds))
        stats.add_row("Chunk Bytes", _format_bytes(summary.chunk_bytes_moved))

        history_table = Table(title="Completed Files", expand=True)
        history_table.add_column("Path")
        history_table.add_column("Mode")
        history_table.add_column("Size", justify="right")
        history_table.add_column("Duration", justify="right")
        history_table.add_column("Speed", justify="right")
        for item in self._history[-_HISTORY_LIMIT:]:
            history_table.add_row(
                _short_path(item.rel_path),
                item.mode,
                _format_bytes(item.size),
                _format_seconds(item.duration_seconds),
                _format_rate(item.throughput_bps),
            )

        return Group(
            Panel(overall, title="Progress"),
            worker_table,
            Panel(stats, title="Stats"),
            history_table,
        )


def start_run(db: Database, session: Session, *, stage: str = "transfer", status_text: str = "Starting") -> None:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is not None:
            _RUNTIME.close()
        _RUNTIME = _ProgressRuntime(db, session, stage=stage, status_text=status_text)


def set_stage(stage: str, status_text: str) -> None:
    runtime = _runtime()
    if runtime is None:
        return
    runtime.set_stage(stage, status_text)


def worker_started(
    worker_id: int,
    entry: Entry,
    *,
    mode: str,
    backend: str | None,
    note: str | None = None,
) -> None:
    runtime = _runtime()
    if runtime is None:
        return
    runtime.worker_started(
        worker_id,
        entry,
        mode=mode,
        backend=backend,
        note=note,
    )


def worker_progress(
    worker_id: int,
    *,
    bytes_copied: int,
    throughput_bps: float | None = None,
    note: str | None = None,
) -> None:
    runtime = _runtime()
    if runtime is None:
        return
    runtime.worker_progress(
        worker_id,
        bytes_copied=bytes_copied,
        throughput_bps=throughput_bps,
        note=note,
    )


def worker_finished(history: ProgressFileHistory) -> None:
    runtime = _runtime()
    if runtime is None:
        return
    runtime.worker_finished(history)


def worker_idle(worker_id: int, *, note: str | None = None, waiting: bool = False) -> None:
    runtime = _runtime()
    if runtime is None:
        return
    runtime.worker_idle(worker_id, note=note, waiting=waiting)


def chunk_started(worker_id: int, entry: Entry, *, note: str | None = None) -> None:
    runtime = _runtime()
    if runtime is None:
        return
    runtime.worker_started(
        worker_id,
        entry,
        mode="sparse",
        backend="sparse",
        note=note,
    )


def chunk_progress(
    worker_id: int,
    *,
    rel_path: str,
    chunk_bytes: int,
    total_bytes_copied: int,
    chunk_seconds: float,
    chunk_round_completed: bool,
) -> None:
    runtime = _runtime()
    if runtime is None:
        return
    runtime.chunk_progress(
        worker_id,
        rel_path=rel_path,
        chunk_bytes=chunk_bytes,
        total_bytes_copied=total_bytes_copied,
        chunk_seconds=chunk_seconds,
        chunk_round_completed=chunk_round_completed,
    )


def chunk_finished(history: ProgressFileHistory) -> None:
    runtime = _runtime()
    if runtime is None:
        return
    runtime.worker_finished(history)


def flush(*, force: bool = False) -> None:
    runtime = _runtime()
    if runtime is not None:
        runtime.flush(force=force)


def finish_run(*, stage: str, status_text: str) -> None:
    global _RUNTIME
    runtime = _runtime()
    if runtime is None:
        return
    runtime.finish(stage=stage, status_text=status_text)
    with _LOCK:
        _RUNTIME = None


def abort_run(*, status_text: str) -> None:
    global _RUNTIME
    runtime = _runtime()
    if runtime is None:
        return
    runtime.abort(status_text=status_text)
    with _LOCK:
        _RUNTIME = None


def snapshot() -> ProgressSummarySnapshot | None:
    runtime = _runtime()
    if runtime is None:
        return None
    return runtime.snapshot()


def workers() -> list[ProgressWorkerSnapshot]:
    runtime = _runtime()
    if runtime is None:
        return []
    return runtime.workers()


def history() -> list[ProgressFileHistory]:
    runtime = _runtime()
    if runtime is None:
        return []
    return runtime.history()


def _runtime() -> _ProgressRuntime | None:
    with _LOCK:
        return _RUNTIME


def _format_bytes(value: int | None) -> str:
    if not value:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


def _format_rate(value: float | None) -> str:
    if value is None or value <= 0:
        return "-"
    return f"{_format_bytes(int(value))}/s"


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 1:
        return f"{value:.2f}s"
    if value < 60:
        return f"{value:.1f}s"
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes}m{seconds:02d}s"


def _short_path(value: str | Path | None) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= 48 else f"...{text[-45:]}"
