"""End-to-end pipeline tests using FakeDrives."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import FakeDrives, make_session
from swap import pipeline
from swap.db import Database, Session
from swap.cleanup import finalize


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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
    """Return {rel_path: content} for all files under root."""
    result = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            result[p.relative_to(root).as_posix()] = p.read_bytes()
    return result


def _setup_and_run(
    tmp_path: Path,
    cap_a: int,
    cap_b: int,
    files_a: dict[str, bytes],
    files_b: dict[str, bytes],
    chunk_size: int = 4,
    safety_margin: int = 0,
    free_bytes_wrapper=None,
) -> tuple[Database, FakeDrives, Session]:
    """
    Build the drive layout, create a session, and run the pipeline.
    Returns (db, drives, session_after_run).
    The work dir is a sibling to the fake drive dirs (outside them).
    """
    drives_base = tmp_path / "drives"
    drives = _make_drives(drives_base, cap_a, cap_b)

    folder_a = drives.drive_a / "folderA"
    folder_b = drives.drive_b / "folderB"
    folder_a.mkdir(parents=True, exist_ok=True)
    folder_b.mkdir(parents=True, exist_ok=True)

    for rel, data in files_a.items():
        _write(folder_a / rel, data)
    for rel, data in files_b.items():
        _write(folder_b / rel, data)

    work = tmp_path / "work"  # sibling to drives_base, outside the drive dirs
    db = _open_db(work)
    drives.bind_db(db)

    session = make_session(
        folder_a=folder_a,
        folder_b=folder_b,
        chunk_size=chunk_size,
        safety_margin=safety_margin,
        backend="python",
    )
    db.create_session(session)

    fb = free_bytes_wrapper if free_bytes_wrapper is not None else drives.free_bytes

    pipeline.run(db, volume_id=drives.volume_id, free_bytes=fb)

    return db, drives, db.session()


# ---------------------------------------------------------------------------
# test: happy path — nested dirs + several files
# ---------------------------------------------------------------------------

def test_happy_path_nested_dirs_and_files(tmp_path):
    """Full pipeline: nested dirs and multiple files; final trees match originals."""
    files_a = {
        "readme.txt": b"hello from A",
        "sub/data.bin": b"nested A data",
        "sub/deep/file.bin": b"deep A",
    }
    files_b = {
        "config.cfg": b"config from B",
        "assets/image.png": b"fake png B",
    }

    # Generous capacity
    db, drives, session = _setup_and_run(
        tmp_path,
        cap_a=100_000,
        cap_b=100_000,
        files_a=files_a,
        files_b=files_b,
        chunk_size=64,
    )

    assert session.stage == "done"

    # final_a receives B's content, final_b receives A's content
    final_a_tree = _tree_bytes(session.final_a)
    final_b_tree = _tree_bytes(session.final_b)

    assert final_a_tree == files_b, (
        f"final_a should contain B's files.\nExpected: {files_b}\nGot: {final_a_tree}"
    )
    assert final_b_tree == files_a, (
        f"final_b should contain A's files.\nExpected: {files_a}\nGot: {final_b_tree}"
    )

    # Staged roots should be gone
    assert not session.staged_a.exists(), "staged_a should have been removed"
    assert not session.staged_b.exists(), "staged_b should have been removed"

    db.close()


# ---------------------------------------------------------------------------
# test: deadlock scenario
# ---------------------------------------------------------------------------

def test_deadlock_scenario_completes(tmp_path):
    """
    drive1: A.bin(10) + B.bin(10), drive2: C.bin(20), 6 bytes free each, chunk=4.
    The sparse engine must resolve the deadlock.
    """
    files_a = {
        "A.bin": b"A" * 10,
        "B.bin": b"B" * 10,
    }
    files_b = {
        "C.bin": b"C" * 20,
    }
    # Total A side data: 20 bytes. Total B side data: 20 bytes.
    # drive_a capacity: 20 (used) + 6 (free) = 26
    # drive_b capacity: 20 (used) + 6 (free) = 26
    # chunk_size=4, safety_margin=0
    # Required headroom = 0 + 4 = 4
    # With only 6 bytes free, no whole-file copy fits (10 bytes > 6, 20 bytes > 6)
    # Must use sparse engine.

    db, drives, session = _setup_and_run(
        tmp_path,
        cap_a=26,
        cap_b=26,
        files_a=files_a,
        files_b=files_b,
        chunk_size=4,
        safety_margin=0,
    )

    assert session.stage == "done"

    final_a_tree = _tree_bytes(session.final_a)
    final_b_tree = _tree_bytes(session.final_b)

    assert final_a_tree == files_b
    assert final_b_tree == files_a

    db.close()


# ---------------------------------------------------------------------------
# test: crash-anywhere / resume sweep
# ---------------------------------------------------------------------------

class Crash(Exception):
    pass


def test_crash_anywhere_resume(tmp_path):
    """
    For N in range(1, 40): crash on the Nth free_bytes call, then reopen
    the DB and resume. Final state must be 'done' with correct bytes.
    """
    files_a = {
        "A.bin": b"A" * 10,
        "B.bin": b"B" * 10,
    }
    files_b = {
        "C.bin": b"C" * 20,
    }

    for N in range(1, 40):
        # Fresh temp dir per iteration (subdirectory of tmp_path)
        iteration_dir = tmp_path / f"iter_{N}"
        iteration_dir.mkdir()

        drives_base = iteration_dir / "drives"
        drives = _make_drives(drives_base, cap_a=26, cap_b=26)

        folder_a = drives.drive_a / "folderA"
        folder_b = drives.drive_b / "folderB"
        folder_a.mkdir(parents=True, exist_ok=True)
        folder_b.mkdir(parents=True, exist_ok=True)

        for rel, data in files_a.items():
            _write(folder_a / rel, data)
        for rel, data in files_b.items():
            _write(folder_b / rel, data)

        work = iteration_dir / "work"
        db = _open_db(work)
        drives.bind_db(db)

        session = make_session(
            folder_a=folder_a,
            folder_b=folder_b,
            chunk_size=4,
            safety_margin=0,
            backend="python",
        )
        db.create_session(session)

        # Wrapping free_bytes to raise Crash on the Nth call
        call_count = [0]

        def crashing_free_bytes(path, _drives=drives, _N=N, _count=call_count):
            _count[0] += 1
            if _count[0] == _N:
                raise Crash(f"simulated crash on call {_N}")
            return _drives.free_bytes(path)

        try:
            pipeline.run(db, volume_id=drives.volume_id, free_bytes=crashing_free_bytes)
        except Crash:
            pass  # expected
        finally:
            db.close()

        # Reopen db and resume with normal free_bytes
        db2 = _open_db(work)
        drives2 = FakeDrives(drives.drive_a, drives.drive_b, 26, 26)
        drives2.bind_db(db2)

        pipeline.run(db2, volume_id=drives2.volume_id, free_bytes=drives2.free_bytes)

        session_final = db2.session()
        assert session_final.stage == "done", (
            f"N={N}: expected stage 'done', got '{session_final.stage}'"
        )

        final_a_tree = _tree_bytes(session_final.final_a)
        final_b_tree = _tree_bytes(session_final.final_b)

        assert final_a_tree == files_b, f"N={N}: final_a mismatch"
        assert final_b_tree == files_a, f"N={N}: final_b mismatch"

        db2.close()


# ---------------------------------------------------------------------------
# test: idempotence — calling pipeline.run again after 'done' is a no-op
# ---------------------------------------------------------------------------

def test_idempotent_after_done(tmp_path):
    """Calling pipeline.run a second time after completion does not raise."""
    files_a = {"file.bin": b"content A"}
    files_b = {"other.bin": b"content B"}

    db, drives, session = _setup_and_run(
        tmp_path,
        cap_a=10_000,
        cap_b=10_000,
        files_a=files_a,
        files_b=files_b,
    )

    assert session.stage == "done"

    # Run again — should be a no-op
    pipeline.run(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)

    session2 = db.session()
    assert session2.stage == "done"

    db.close()


# ---------------------------------------------------------------------------
# test: cleanup safety — stray file in staged root raises RuntimeError
# ---------------------------------------------------------------------------

def test_cleanup_raises_on_stray_file_in_staged_root(tmp_path):
    """
    If a file remains in a staged root when cleanup.finalize runs,
    it should raise RuntimeError (not silently delete data).
    """
    drives_base = tmp_path / "drives"
    drives = _make_drives(drives_base, 10_000, 10_000)

    folder_a = drives.drive_a / "folderA"
    folder_b = drives.drive_b / "folderB"
    folder_a.mkdir()
    folder_b.mkdir()

    work = tmp_path / "work"
    db = _open_db(work)
    drives.bind_db(db)

    session = make_session(
        folder_a=folder_a,
        folder_b=folder_b,
        chunk_size=4,
        safety_margin=0,
        backend="python",
    )
    db.create_session(session)
    session_obj = db.session()

    # Advance to 'transferred' manually (skip actual transfer)
    db.advance_stage("staged")
    db.advance_stage("scanned")
    db.advance_stage("transferred")

    # Create the staged roots (they'd normally exist from staging.stage_roots)
    session_obj.staged_a.mkdir(parents=True, exist_ok=True)
    session_obj.staged_b.mkdir(parents=True, exist_ok=True)

    # Leave a stray file in staged_a
    stray = session_obj.staged_a / "stray.bin"
    stray.write_bytes(b"I should not be here")

    raised = False
    try:
        finalize(db)
    except RuntimeError:
        raised = True

    assert raised, "Expected RuntimeError when stray file present in staged root"

    db.close()
