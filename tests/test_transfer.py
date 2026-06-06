"""Tests for the parallel whole-file transfer coordinator."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import FakeDrives, make_session
from swap import copier, progress, transfer
from swap.db import Database, Entry, ProgressFileHistory


def _open_db(work_dir: Path) -> Database:
    work_dir.mkdir(parents=True, exist_ok=True)
    return Database(work_dir / "swap.db")


def _setup_scanned_session(
    tmp_path: Path,
    *,
    worker_count: int,
    cap_a: int = 100_000,
    cap_b: int = 100_000,
) -> tuple[Database, object, object]:
    drive_a = tmp_path / "driveA"
    drive_b = tmp_path / "driveB"
    drive_a.mkdir()
    drive_b.mkdir()
    drives = FakeDrives(drive_a, drive_b, cap_a, cap_b)

    folder_a = drive_a / "folderA"
    folder_b = drive_b / "folderB"
    folder_a.mkdir()
    folder_b.mkdir()

    db = _open_db(tmp_path / "work")
    drives.bind_db(db)

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
    return db, session, drives


def _insert_file_entry(
    db: Database,
    session,
    *,
    side: str,
    rel_path: str,
    data: bytes,
) -> Entry:
    src = (session.staged_a if side == "A" else session.staged_b) / rel_path
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    db._conn.execute(
        "INSERT INTO entries (side, rel_path, kind, size, link_target, state)"
        " VALUES (?, ?, 'file', ?, NULL, 'pending')",
        (side, rel_path, len(data)),
    )
    row = db._conn.execute(
        "SELECT id, side, rel_path, kind, size, link_target, state FROM entries"
        " WHERE side = ? AND rel_path = ?",
        (side, rel_path),
    ).fetchone()
    return Entry(*row)


def test_copy_files_runs_multiple_workers_in_parallel(tmp_path):
    db, session, drives = _setup_scanned_session(tmp_path, worker_count=3)
    entries = [
        _insert_file_entry(db, session, side="A", rel_path=f"file-{index}.bin", data=b"x" * 4)
        for index in range(3)
    ]
    assert len(entries) == 3

    active = 0
    max_active = 0

    original = copier.copy_file

    def fake_copy_file(current_session, entry, *, worker_id):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.2)
        active -= 1
        now = time.time()
        return copier.CopyResult(
            history=ProgressFileHistory(
                entry_id=entry.id,
                rel_path=entry.rel_path,
                size=entry.size or 0,
                worker_id=worker_id,
                mode="whole-file",
                backend="python",
                started_at=now - 0.1,
                finished_at=now,
                duration_seconds=0.1,
                throughput_bps=float(entry.size or 0) / 0.1,
                read_seconds=0.01,
                write_seconds=0.01,
                verify_seconds=0.0,
                chunk_seconds=None,
            )
        )

    copier.copy_file = fake_copy_file
    progress.start_run(db, session, stage="transfer", status_text="parallel")
    try:
        transfer.copy_files(db, session, drives.free_bytes)
        assert max_active >= 2
        assert all(entry.state == "done" for entry in db.entries(kind="file"))
    finally:
        copier.copy_file = original
        progress.abort_run(status_text="cleanup")
        db.close()


def test_copy_files_reserves_destination_space(tmp_path):
    db, session, drives = _setup_scanned_session(tmp_path, worker_count=2, cap_a=100, cap_b=10)
    _insert_file_entry(db, session, side="A", rel_path="first.bin", data=b"1" * 6)
    _insert_file_entry(db, session, side="A", rel_path="second.bin", data=b"2" * 6)

    active = 0
    max_active = 0

    original = copier.copy_file

    def fake_copy_file(current_session, entry, *, worker_id):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.2)
        active -= 1
        now = time.time()
        return copier.CopyResult(
            history=ProgressFileHistory(
                entry_id=entry.id,
                rel_path=entry.rel_path,
                size=entry.size or 0,
                worker_id=worker_id,
                mode="whole-file",
                backend="python",
                started_at=now - 0.1,
                finished_at=now,
                duration_seconds=0.1,
                throughput_bps=float(entry.size or 0) / 0.1,
                read_seconds=0.01,
                write_seconds=0.01,
                verify_seconds=0.0,
                chunk_seconds=None,
            )
        )

    copier.copy_file = fake_copy_file
    progress.start_run(db, session, stage="transfer", status_text="reserve")
    try:
        transfer.copy_files(db, session, drives.free_bytes)
        assert max_active == 1, "reserved destination bytes should block a second copy"
    finally:
        copier.copy_file = original
        progress.abort_run(status_text="cleanup")
        db.close()


# ---------------------------------------------------------------------------
# winapi backend (Windows-only: CopyFileExW)
# ---------------------------------------------------------------------------

def _make_winapi_session(db, session, backend: str = "winapi"):
    return make_session(
        folder_a=session.folder_a,
        folder_b=session.folder_b,
        backend=backend,
        worker_count=session.worker_count,
    )


def test_winapi_copy_end_to_end(tmp_path):
    """CopyFileExW backend copies bytes, deletes the source, reports exact size."""
    if sys.platform != "win32":
        return  # winapi backend exists only on Windows
    db, session, drives = _setup_scanned_session(tmp_path, worker_count=1)
    data = b"winapi payload " * 1000
    entry = _insert_file_entry(db, session, side="A", rel_path="big.bin", data=data)
    winapi_session = _make_winapi_session(db, session)

    result = copier.copy_file(winapi_session, entry, worker_id=1)

    dst = winapi_session.dst_path(entry)
    assert dst.read_bytes() == data
    assert not winapi_session.src_path(entry).exists()
    assert result.history.backend == "winapi"
    assert result.history.size == len(data)
    db.close()


def test_winapi_recopy_over_partial_destination(tmp_path):
    """Crash recovery: re-copying over a partial destination must overwrite it."""
    if sys.platform != "win32":
        return
    db, session, drives = _setup_scanned_session(tmp_path, worker_count=1)
    data = b"full contents after recovery"
    entry = _insert_file_entry(db, session, side="A", rel_path="partial.bin", data=data)
    winapi_session = _make_winapi_session(db, session)

    dst = winapi_session.dst_path(entry)
    dst.write_bytes(data[:5])  # simulate a copy interrupted mid-write

    copier.copy_file(winapi_session, entry, worker_id=1)

    assert dst.read_bytes() == data
    assert not winapi_session.src_path(entry).exists()
    db.close()


def test_legacy_robocopy_backend_resumes_on_winapi(tmp_path):
    """Sessions stored with backend='robocopy' must still copy after resume."""
    if sys.platform != "win32":
        return
    db, session, drives = _setup_scanned_session(tmp_path, worker_count=1)
    data = b"legacy session data"
    entry = _insert_file_entry(db, session, side="A", rel_path="legacy.bin", data=data)
    legacy_session = _make_winapi_session(db, session, backend="robocopy")

    result = copier.copy_file(legacy_session, entry, worker_id=1)

    assert legacy_session.dst_path(entry).read_bytes() == data
    assert result.history.backend == "winapi"
    db.close()
