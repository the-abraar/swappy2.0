"""Tests for preflight.check via a Database with a 'created' session."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import FakeDrives, make_session
from swap.db import Database
from swap.preflight import PreflightError, check


def _open_db(work_dir: Path) -> Database:
    return Database(work_dir / "swap.db")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_drives(tmp_path: Path, cap_a: int = 10_000, cap_b: int = 10_000):
    drive_a = tmp_path / "driveA"
    drive_b = tmp_path / "driveB"
    drive_a.mkdir()
    drive_b.mkdir()
    return FakeDrives(drive_a, drive_b, cap_a, cap_b)


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ---------------------------------------------------------------------------
# structural / root-validation tests
# ---------------------------------------------------------------------------

def test_rejects_missing_root(tmp_path):
    drives = _make_drives(tmp_path)
    folder_a = drives.drive_a / "folderA"
    folder_b = drives.drive_b / "folderB"
    folder_a.mkdir()
    # folder_b intentionally NOT created

    work = tmp_path / "work"
    db = _open_db(work)
    db.create_session(make_session(
        folder_a=folder_a,
        folder_b=folder_b,
    ))

    try:
        check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
        assert False, "expected PreflightError"
    except PreflightError:
        pass
    finally:
        db.close()


def test_rejects_nested_roots(tmp_path):
    """folder_b nested inside folder_a should raise PreflightError."""
    drives = _make_drives(tmp_path)
    folder_a = drives.drive_a / "outer"
    folder_b = drives.drive_a / "outer" / "inner"
    folder_a.mkdir(parents=True)
    folder_b.mkdir(parents=True)

    work = tmp_path / "work"
    db = _open_db(work)
    db.create_session(make_session(
        folder_a=folder_a,
        folder_b=folder_b,
    ))

    try:
        check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
        assert False, "expected PreflightError"
    except PreflightError:
        pass
    finally:
        db.close()


def test_rejects_identical_roots(tmp_path):
    """The same directory for both sides should raise PreflightError."""
    drives = _make_drives(tmp_path)
    folder = drives.drive_a / "folder"
    folder.mkdir()

    work = tmp_path / "work"
    db = _open_db(work)
    db.create_session(make_session(
        folder_a=folder,
        folder_b=folder,
    ))

    try:
        check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
        assert False, "expected PreflightError"
    except PreflightError:
        pass
    finally:
        db.close()


def test_rejects_same_volume_roots(tmp_path):
    """Both roots on the same volume (same volume_id) should raise PreflightError."""
    drives = _make_drives(tmp_path)
    folder_a = drives.drive_a / "folderA"
    folder_b = drives.drive_a / "folderB"  # also on drive_a!
    folder_a.mkdir()
    folder_b.mkdir()

    work = tmp_path / "work"
    db = _open_db(work)
    db.create_session(make_session(
        folder_a=folder_a,
        folder_b=folder_b,
    ))

    try:
        check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
        assert False, "expected PreflightError"
    except PreflightError:
        pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# capacity feasibility tests
# ---------------------------------------------------------------------------

def test_rejects_capacity_infeasible(tmp_path):
    """Swap that would leave less than safety_margin bytes should fail."""
    drives = _make_drives(tmp_path, cap_a=100, cap_b=100)
    folder_a = drives.drive_a / "folderA"
    folder_b = drives.drive_b / "folderB"
    folder_a.mkdir()
    folder_b.mkdir()

    # A has 80 bytes of data, B has 10 bytes. After swap:
    # drive_a free = 100 - 80 (pre) + 80 - 10 = ... let's compute using preflight formula:
    # final_free_a = free(a) + size_a - size_b = (100-80) + 80 - 10 = 90
    # final_free_b = free(b) + size_b - size_a = (100-10) + 10 - 80 = 20
    # With safety_margin=25: drive B would have 20 < 25 -> infeasible
    _write_file(folder_a / "big.bin", b"A" * 80)
    _write_file(folder_b / "small.bin", b"B" * 10)

    work = tmp_path / "work"
    db = _open_db(work)
    db.create_session(make_session(
        folder_a=folder_a,
        folder_b=folder_b,
        safety_margin=25,
    ))

    try:
        check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
        assert False, "expected PreflightError"
    except PreflightError:
        pass
    finally:
        db.close()


def test_accepts_feasible_swap(tmp_path):
    """A well-sized swap with safety_margin=0 should pass preflight."""
    drives = _make_drives(tmp_path, cap_a=200, cap_b=200)
    folder_a = drives.drive_a / "folderA"
    folder_b = drives.drive_b / "folderB"
    folder_a.mkdir()
    folder_b.mkdir()

    _write_file(folder_a / "file.bin", b"A" * 50)
    _write_file(folder_b / "file.bin", b"B" * 50)

    work = tmp_path / "work"
    db = _open_db(work)
    db.create_session(make_session(
        folder_a=folder_a,
        folder_b=folder_b,
        safety_margin=0,
    ))

    # Should not raise
    check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    db.close()


# ---------------------------------------------------------------------------
# idempotence: non-'created' stage is a no-op
# ---------------------------------------------------------------------------

def test_is_noop_when_stage_is_not_created(tmp_path):
    """preflight.check should do nothing (not raise) when stage != 'created'."""
    drives = _make_drives(tmp_path)
    # Use non-existent folders: if check runs validation it would raise
    folder_a = drives.drive_a / "ghost_a"
    folder_b = drives.drive_b / "ghost_b"

    work = tmp_path / "work"
    db = _open_db(work)
    # Insert directly with stage='staged' to bypass create_session's guard
    from swap.db import Session
    session = Session(
        folder_a=folder_a,
        folder_b=folder_b,
        staged_a=folder_a.parent / ".swap_staged_a",
        staged_b=folder_b.parent / ".swap_staged_b",
        final_a=folder_a.parent / folder_b.name,
        final_b=folder_b.parent / folder_a.name,
        chunk_size=4,
        backend="python",
        verify_chunks=False,
        safety_margin=0,
        stage="staged",
    )
    db.create_session(session)

    # Should not raise even though folders don't exist
    check(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    db.close()
