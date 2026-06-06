"""Tests for the live progress subsystem and durable progress tables."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import make_session
from swap import progress
from swap.db import Database, Entry, ProgressFileHistory


def _open_db(work_dir: Path) -> Database:
    work_dir.mkdir(parents=True, exist_ok=True)
    return Database(work_dir / "swap.db")


def _setup_scanned_session(tmp_path: Path, *, worker_count: int = 4):
    drive_a = tmp_path / "driveA"
    drive_b = tmp_path / "driveB"
    drive_a.mkdir()
    drive_b.mkdir()
    folder_a = drive_a / "folderA"
    folder_b = drive_b / "folderB"
    folder_a.mkdir()
    folder_b.mkdir()

    db = _open_db(tmp_path / "work")
    session = make_session(
        folder_a=folder_a,
        folder_b=folder_b,
        backend="python",
        worker_count=worker_count,
    )
    db.create_session(session)
    db.advance_stage("staged")
    db.advance_stage("scanned")
    session = db.session()
    session.staged_a.mkdir(parents=True, exist_ok=True)
    session.staged_b.mkdir(parents=True, exist_ok=True)
    session.final_a.mkdir(parents=True, exist_ok=True)
    session.final_b.mkdir(parents=True, exist_ok=True)
    return db, session


def _insert_file_entry(
    db: Database,
    *,
    side: str,
    rel_path: str,
    size: int,
    state: str = "pending",
) -> Entry:
    db._conn.execute(
        "INSERT INTO entries (side, rel_path, kind, size, link_target, state)"
        " VALUES (?, ?, 'file', ?, NULL, ?)",
        (side, rel_path, size, state),
    )
    return db.entries(kind="file")[-1]


def test_progress_bootstrap_reconstructs_done_and_sparse_bytes(tmp_path):
    db, session = _setup_scanned_session(tmp_path, worker_count=3)
    done_entry = _insert_file_entry(db, side="A", rel_path="done.bin", size=10, state="done")
    sparse_entry = _insert_file_entry(db, side="B", rel_path="sparse.bin", size=8, state="sparse")

    sparse_src = session.src_path(sparse_entry)
    sparse_src.parent.mkdir(parents=True, exist_ok=True)
    sparse_src.write_bytes(b"abc")

    bootstrap = db.progress_bootstrap(session)
    assert bootstrap.total_files == 2
    assert bootstrap.done_files == 1
    assert bootstrap.done_bytes == 10
    assert bootstrap.sparse_files == 1
    assert bootstrap.sparse_bytes_copied == 5
    assert bootstrap.bytes_copied == 15

    db.close()


def test_progress_runtime_persists_worker_rows_and_history(tmp_path):
    db, session = _setup_scanned_session(tmp_path, worker_count=3)
    entry = _insert_file_entry(db, side="A", rel_path="copy.bin", size=12)

    progress.start_run(db, session, stage="transfer", status_text="test")
    try:
        workers = db.progress_workers()
        assert len(workers) == 3
        assert all(worker.state == "idle" for worker in workers)

        progress.worker_started(2, entry, mode="whole-file", backend="python")
        progress.worker_progress(2, bytes_copied=4, throughput_bps=8.0)
        progress.flush(force=True)

        workers = db.progress_workers()
        assert workers[1].worker_id == 2
        assert workers[1].state == "copying"
        assert workers[1].bytes_copied == 4

        started_at = time.time() - 1
        finished_at = time.time()
        progress.worker_finished(ProgressFileHistory(
            entry_id=entry.id,
            rel_path=entry.rel_path,
            size=12,
            worker_id=2,
            mode="whole-file",
            backend="python",
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=finished_at - started_at,
            throughput_bps=12.0,
            read_seconds=0.2,
            write_seconds=0.3,
            verify_seconds=0.1,
            chunk_seconds=None,
        ))
        progress.flush(force=True)

        history = db.progress_history()
        assert len(history) == 1
        assert history[0].rel_path == "copy.bin"
        assert history[0].worker_id == 2

        progress.finish_run(stage="transferred", status_text="done")
        summary = db.progress_summary()
        assert summary is not None
        assert summary.done_files == 1
        assert summary.finished_at is not None
    finally:
        progress.abort_run(status_text="cleanup")
        db.close()


def test_progress_resume_resets_inflight_whole_file_bytes(tmp_path):
    db, session = _setup_scanned_session(tmp_path, worker_count=2)
    entry = _insert_file_entry(db, side="A", rel_path="resume.bin", size=9)

    progress.start_run(db, session, stage="transfer", status_text="first run")
    progress.worker_started(1, entry, mode="whole-file", backend="python")
    progress.worker_progress(1, bytes_copied=5, throughput_bps=5.0)
    progress.flush(force=True)

    first_summary = db.progress_summary()
    assert first_summary is not None
    assert first_summary.bytes_copied == 5

    progress.abort_run(status_text="interrupted")

    progress.start_run(db, session, stage="transfer", status_text="resume")
    try:
        resumed = progress.snapshot()
        assert resumed is not None
        assert resumed.bytes_copied == 0
        workers = db.progress_workers()
        assert len(workers) == 2
        assert all(worker.state == "idle" for worker in workers)
    finally:
        progress.abort_run(status_text="cleanup")
        db.close()
