"""Tests for `swap rollback` (rollback.py): undo a partially-completed swap."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import FakeDrives, make_session
from swap import pipeline, preflight, rollback, scanner, staging, transfer
from swap.db import Database
from swap.rollback import RollbackError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

FILES_A = {
    "readme.txt": b"hello from A",
    "sub/data.bin": b"nested A data!!!",
    "empty.bin": b"",
}
FILES_B = {
    "config.cfg": b"config from B",
    "assets/image.png": b"fake png bytes B",
}


class Crash(Exception):
    pass


def _make_drives(base: Path, cap_a: int, cap_b: int) -> FakeDrives:
    drive_a = base / "driveA"
    drive_b = base / "driveB"
    drive_a.mkdir(parents=True, exist_ok=True)
    drive_b.mkdir(parents=True, exist_ok=True)
    return FakeDrives(drive_a, drive_b, cap_a, cap_b)


def _open_db(work: Path) -> Database:
    work.mkdir(parents=True, exist_ok=True)
    return Database(work / "swap.db")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    result = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            result[p.relative_to(root).as_posix()] = p.read_bytes()
    return result


def _setup(
    base: Path,
    cap_a: int,
    cap_b: int,
    files_a: dict[str, bytes],
    files_b: dict[str, bytes],
    folder_a_name: str = "folderA",
    folder_b_name: str = "folderB",
    chunk_size: int = 4,
) -> tuple[Database, FakeDrives]:
    """Create the drive layout and a session at stage 'created' (no run)."""
    drives = _make_drives(base / "drives", cap_a, cap_b)
    folder_a = drives.drive_a / folder_a_name
    folder_b = drives.drive_b / folder_b_name
    folder_a.mkdir(parents=True, exist_ok=True)
    folder_b.mkdir(parents=True, exist_ok=True)
    for rel, data in files_a.items():
        _write(folder_a / rel, data)
    for rel, data in files_b.items():
        _write(folder_b / rel, data)

    db = _open_db(base / "work")
    drives.bind_db(db)
    db.create_session(make_session(folder_a=folder_a, folder_b=folder_b, chunk_size=chunk_size))
    return db, drives


def _run_through_transfer(db: Database, drives: FakeDrives) -> None:
    """Forward pipeline up to stage 'transferred' (cleanup NOT run)."""
    preflight.check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    staging.stage_roots(db)
    scanner.scan(db)
    transfer.transfer_all(db, free_bytes=drives.free_bytes)
    assert db.session().stage == "transferred"


def _assert_restored(db: Database, files_a: dict[str, bytes], files_b: dict[str, bytes]) -> None:
    session = db.session()
    assert session.stage == "rolled_back", f"expected rolled_back, got {session.stage}"
    assert _tree_bytes(session.folder_a) == files_a, "folder_a content mismatch after rollback"
    assert _tree_bytes(session.folder_b) == files_b, "folder_b content mismatch after rollback"
    assert not session.staged_a.exists(), "staged_a should be gone after rollback"
    assert not session.staged_b.exists(), "staged_b should be gone after rollback"
    if session.final_a != session.folder_a:  # distinct unless the folders share a name
        assert not session.final_a.exists(), "final_a should be gone after rollback"
        assert not session.final_b.exists(), "final_b should be gone after rollback"


# ---------------------------------------------------------------------------
# stage-edge behavior
# ---------------------------------------------------------------------------

def test_rollback_before_anything_happened(tmp_path):
    """stage='created': nothing on disk to undo; just mark rolled_back."""
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B)
    rollback.run(db, free_bytes=drives.free_bytes)
    _assert_restored(db, FILES_A, FILES_B)
    db.close()


def test_rollback_after_staging_only(tmp_path):
    """stage='staged': undo the renames and remove the empty final roots."""
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B)
    staging.stage_roots(db)
    rollback.run(db, free_bytes=drives.free_bytes)
    _assert_restored(db, FILES_A, FILES_B)
    db.close()


def test_rollback_after_transfer_complete(tmp_path):
    """stage='transferred': every file moved; full reverse transfer."""
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B, chunk_size=64)
    _run_through_transfer(db, drives)
    rollback.run(db, free_bytes=drives.free_bytes)
    _assert_restored(db, FILES_A, FILES_B)
    db.close()


def test_rollback_refuses_after_done(tmp_path):
    """A completed swap (staged trees removed) cannot be rolled back."""
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B)
    pipeline.run(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    assert db.session().stage == "done"
    raised = False
    try:
        rollback.run(db, free_bytes=drives.free_bytes)
    except RollbackError:
        raised = True
    assert raised, "rollback after 'done' must refuse"
    db.close()


def test_rollback_idempotent_when_rolled_back(tmp_path):
    """A second `swap rollback` is a no-op."""
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B)
    _run_through_transfer(db, drives)
    rollback.run(db, free_bytes=drives.free_bytes)
    rollback.run(db, free_bytes=drives.free_bytes)  # must not raise
    _assert_restored(db, FILES_A, FILES_B)
    db.close()


# ---------------------------------------------------------------------------
# pending-state handling (the sweep)
# ---------------------------------------------------------------------------

def test_rollback_deletes_partial_destination(tmp_path):
    """pending file + intact src + partial dst (crashed in-flight copy):
    the partial dst is deleted and the src moves back untouched."""
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B)
    preflight.check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    staging.stage_roots(db)
    scanner.scan(db)
    session = db.session()

    entry = next(e for e in db.entries(kind="file") if e.rel_path == "readme.txt")
    dst = session.dst_path(entry)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"hello")  # partial copy, src still intact

    rollback.run(db, free_bytes=drives.free_bytes)
    _assert_restored(db, FILES_A, FILES_B)
    db.close()


def test_rollback_recovers_completed_but_unmarked_copy(tmp_path):
    """pending file + missing src + complete dst (crash between source delete
    and the 'done' mark): the file is actually transferred — reverse it."""
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B)
    preflight.check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    staging.stage_roots(db)
    scanner.scan(db)
    session = db.session()

    entry = next(e for e in db.entries(kind="file") if e.rel_path == "readme.txt")
    src = session.src_path(entry)
    dst = session.dst_path(entry)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    src.unlink()  # entry stays 'pending'

    rollback.run(db, free_bytes=drives.free_bytes)
    _assert_restored(db, FILES_A, FILES_B)
    db.close()


def test_rollback_refuses_when_file_lost_on_both_sides(tmp_path):
    """pending file with missing src and missing/incomplete dst means data
    was externally destroyed: refuse rather than roll back silently."""
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B)
    preflight.check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    staging.stage_roots(db)
    scanner.scan(db)
    session = db.session()

    entry = next(e for e in db.entries(kind="file") if e.rel_path == "readme.txt")
    session.src_path(entry).unlink()  # no dst either

    raised = False
    try:
        rollback.run(db, free_bytes=drives.free_bytes)
    except RollbackError:
        raised = True
    assert raised, "expected RollbackError for a file missing on both sides"
    # nothing flipped: the session is still resumable forward... or by a
    # rollback once the user restores the file
    assert db.session().stage == "scanned"
    db.close()


# ---------------------------------------------------------------------------
# crash-anywhere sweeps
# ---------------------------------------------------------------------------

def _crash_sweep_layout() -> tuple[dict[str, bytes], dict[str, bytes]]:
    # deadlock-prone: 6 bytes free per side, chunk_size=4 -> sparse engine runs
    return {"A.bin": b"A" * 10, "B.bin": b"B" * 10}, {"C.bin": b"C" * 20}


def test_rollback_after_forward_crash_anywhere(tmp_path):
    """Crash the forward run on the Nth free_bytes call (hits whole-file,
    sparse mid-flight, and reservation states), then roll back cleanly."""
    files_a, files_b = _crash_sweep_layout()

    for n in range(1, 40):
        base = tmp_path / f"iter_{n}"
        base.mkdir()
        db, drives = _setup(base, 26, 26, files_a, files_b)

        count = [0]

        def crashing_free_bytes(path, _drives=drives, _n=n, _count=count):
            _count[0] += 1
            if _count[0] == _n:
                raise Crash(f"simulated crash on call {_n}")
            return _drives.free_bytes(path)

        try:
            pipeline.run(db, volume_id=drives.volume_id, free_bytes=crashing_free_bytes)
        except Crash:
            pass
        finally:
            db.close()

        db2 = _open_db(base / "work")
        drives2 = FakeDrives(drives.drive_a, drives.drive_b, 26, 26)
        drives2.bind_db(db2)

        if db2.session().stage == "done":  # n exceeded the forward call count
            db2.close()
            break
        rollback.run(db2, free_bytes=drives2.free_bytes)
        _assert_restored(db2, files_a, files_b)
        db2.close()


def test_rollback_crash_anywhere_then_rerun(tmp_path):
    """Interrupt the forward run mid-transfer, then crash the ROLLBACK on its
    Mth free_bytes call, then re-run rollback: it must resume and finish.
    While stage is 'rolling_back', forward resume must refuse loudly."""
    files_a, files_b = _crash_sweep_layout()
    forward_crash_at = 12  # mid-transfer

    for m in range(1, 30):
        base = tmp_path / f"iter_{m}"
        base.mkdir()
        db, drives = _setup(base, 26, 26, files_a, files_b)

        count = [0]

        def fwd_crash(path, _drives=drives, _count=count):
            _count[0] += 1
            if _count[0] == forward_crash_at:
                raise Crash("forward crash")
            return _drives.free_bytes(path)

        try:
            pipeline.run(db, volume_id=drives.volume_id, free_bytes=fwd_crash)
        except Crash:
            pass
        finally:
            db.close()

        db2 = _open_db(base / "work")
        drives2 = FakeDrives(drives.drive_a, drives.drive_b, 26, 26)
        drives2.bind_db(db2)

        count2 = [0]

        def rb_crash(path, _drives=drives2, _m=m, _count=count2):
            _count[0] += 1
            if _count[0] == _m:
                raise Crash(f"rollback crash on call {_m}")
            return _drives.free_bytes(path)

        try:
            rollback.run(db2, free_bytes=rb_crash)
        except Crash:
            pass
        finally:
            db2.close()

        # simulate a fresh process: reopen and finish the rollback
        db3 = _open_db(base / "work")
        drives3 = FakeDrives(drives.drive_a, drives.drive_b, 26, 26)
        drives3.bind_db(db3)

        if db3.session().stage == "rolling_back":
            # forward resume must refuse while a rollback is underway
            raised = False
            try:
                pipeline.run(db3, volume_id=drives3.volume_id, free_bytes=drives3.free_bytes)
            except RuntimeError:
                raised = True
            assert raised, f"m={m}: resume during rolling_back must raise"

        rollback.run(db3, free_bytes=drives3.free_bytes)
        _assert_restored(db3, files_a, files_b)
        db3.close()


# ---------------------------------------------------------------------------
# resume interplay after a finished rollback
# ---------------------------------------------------------------------------

def test_resume_refuses_after_rollback(tmp_path):
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B)
    _run_through_transfer(db, drives)
    rollback.run(db, free_bytes=drives.free_bytes)

    raised = False
    try:
        pipeline.run(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    except RuntimeError:
        raised = True
    assert raised, "forward resume after a rollback must raise"
    db.close()


# ---------------------------------------------------------------------------
# tricky layouts
# ---------------------------------------------------------------------------

def test_rollback_same_name_folders(tmp_path):
    """When both folders share a name, final_a IS the original folder_a path;
    the final tree must be removed before the staged root renames back."""
    db, drives = _setup(
        tmp_path, 100_000, 100_000, FILES_A, FILES_B,
        folder_a_name="Games", folder_b_name="Games", chunk_size=64,
    )
    session = db.session()
    assert session.final_a == session.folder_a  # the collision under test

    _run_through_transfer(db, drives)
    rollback.run(db, free_bytes=drives.free_bytes)
    _assert_restored(db, FILES_A, FILES_B)
    db.close()


def test_rollback_low_space_uses_reverse_sparse_engine(tmp_path):
    """After a full transfer on nearly-full drives, no whole-file reverse copy
    fits either — the rollback must fall back to the sparse engine."""
    files_a, files_b = _crash_sweep_layout()
    db, drives = _setup(tmp_path, 26, 26, files_a, files_b)
    _run_through_transfer(db, drives)
    rollback.run(db, free_bytes=drives.free_bytes)
    _assert_restored(db, files_a, files_b)
    db.close()


def test_rollback_restores_symlinks(tmp_path):
    db, drives = _setup(tmp_path, 100_000, 100_000, FILES_A, FILES_B, chunk_size=64)
    session = db.session()
    try:
        os.symlink("readme.txt", session.folder_a / "link.txt")
    except OSError:
        db.close()
        return  # symlinks not permitted on this system (Windows non-admin)

    _run_through_transfer(db, drives)
    rollback.run(db, free_bytes=drives.free_bytes)
    _assert_restored(db, FILES_A, FILES_B)
    link = db.session().folder_a / "link.txt"
    assert link.is_symlink()
    assert os.readlink(link) == "readme.txt"
    db.close()
