"""Tests for the sparse chunk-transfer engine (sparse.advance_round)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import FakeDrives, make_session
from swap.db import Database, Entry, Session
from swap import winfs
from swap.sparse import SparseTransferError, advance_round, _current_transfer


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_drives(tmp_path: Path, cap_a: int = 100_000, cap_b: int = 100_000):
    drive_a = tmp_path / "driveA"
    drive_b = tmp_path / "driveB"
    drive_a.mkdir()
    drive_b.mkdir()
    return FakeDrives(drive_a, drive_b, cap_a, cap_b)


def _open_db(work_dir: Path) -> Database:
    work_dir.mkdir(parents=True, exist_ok=True)
    return Database(work_dir / "swap.db")


def _insert_file_entry(
    db: Database, side: str, rel_path: str, data: bytes, staged_root: Path
) -> Entry:
    """Write a file into the staged tree and insert a 'pending' entry in the db."""
    full_path = staged_root / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(data)

    # Insert entry directly
    conn = db._conn
    conn.execute(
        "INSERT INTO entries (side, rel_path, kind, size, link_target, state)"
        " VALUES (?, ?, 'file', ?, NULL, 'pending')",
        (side, rel_path, len(data)),
    )
    row = conn.execute(
        "SELECT id, side, rel_path, kind, size, link_target, state FROM entries"
        " WHERE side=? AND rel_path=?", (side, rel_path)
    ).fetchone()
    return Entry(*row)


def _setup_scanned_session(
    tmp_path: Path,
    drives: FakeDrives,
    file_side: str,
    rel_path: str,
    data: bytes,
    chunk_size: int = 4,
):
    """
    Build a DB at stage='scanned' with one file entry on the given side.
    Returns (db, session, entry).
    """
    work = tmp_path / "work"
    folder_a = drives.drive_a / "folderA"
    folder_b = drives.drive_b / "folderB"
    folder_a.mkdir(exist_ok=True)
    folder_b.mkdir(exist_ok=True)

    session = make_session(
        folder_a=folder_a,
        folder_b=folder_b,
        chunk_size=chunk_size,
        safety_margin=0,
        backend="python",
    )

    db = _open_db(work)
    drives.bind_db(db)

    # Create session with stage='scanned' by inserting it at 'created' then advancing
    db.create_session(session)
    db.advance_stage("staged")
    db.advance_stage("scanned")

    session = db.session()

    # Create staged dirs
    session.staged_a.mkdir(parents=True, exist_ok=True)
    session.staged_b.mkdir(parents=True, exist_ok=True)
    # Create final dirs
    session.final_a.mkdir(parents=True, exist_ok=True)
    session.final_b.mkdir(parents=True, exist_ok=True)

    staged_root = session.staged_a if file_side == "A" else session.staged_b
    entry = _insert_file_entry(db, file_side, rel_path, data, staged_root)

    return db, session, entry


# ---------------------------------------------------------------------------
# test: single 10-byte file, chunk_size=4, tail-first (chunks 2,4,4)
# ---------------------------------------------------------------------------

def test_single_file_10_bytes_chunk4(tmp_path):
    """10-byte file with chunk_size=4: runs to completion with correct content."""
    original = b"0123456789"  # 10 bytes
    drives = _make_drives(tmp_path)
    db, session, entry = _setup_scanned_session(
        tmp_path, drives, "A", "file.bin", original, chunk_size=4
    )

    src = session.src_path(entry)
    dst = session.dst_path(entry)

    # Loop advance_round until entry is done
    max_rounds = 20
    for _ in range(max_rounds):
        entries_left = (
            db.entries(kind="file", state="pending") +
            db.entries(kind="file", state="sparse")
        )
        if not entries_left:
            break
        advance_round(db, session, drives.free_bytes)

    # Entry should now be done
    done = db.entries(kind="file", state="done")
    assert len(done) == 1, f"Expected 1 done entry, got {done}"

    # dst content must match original
    assert dst.exists(), "Destination file should exist"
    assert dst.read_bytes() == original, "Destination content mismatch"

    # src should be deleted
    assert not src.exists(), "Source file should have been deleted"

    db.close()


# ---------------------------------------------------------------------------
# test: file smaller than chunk_size (1 chunk)
# ---------------------------------------------------------------------------

def test_file_smaller_than_chunk_size(tmp_path):
    """A file with fewer bytes than chunk_size is handled in one advance_round call."""
    original = b"abc"  # 3 bytes, chunk_size=4
    drives = _make_drives(tmp_path)
    db, session, entry = _setup_scanned_session(
        tmp_path, drives, "B", "tiny.bin", original, chunk_size=4
    )

    src = session.src_path(entry)
    dst = session.dst_path(entry)

    max_rounds = 10
    for _ in range(max_rounds):
        entries_left = (
            db.entries(kind="file", state="pending") +
            db.entries(kind="file", state="sparse")
        )
        if not entries_left:
            break
        advance_round(db, session, drives.free_bytes)

    done = db.entries(kind="file", state="done")
    assert len(done) == 1

    assert dst.exists()
    assert dst.read_bytes() == original
    assert not src.exists()

    db.close()


# ---------------------------------------------------------------------------
# test: recovery after "crash after dst write, before src truncation"
# ---------------------------------------------------------------------------

def test_recovery_crash_after_dst_write_before_truncate(tmp_path):
    """
    Simulate crash after the first chunk was written to dst but before src truncation.
    Re-running advance_round should complete correctly (idempotent chunk write).
    """
    original = b"0123456789"  # 10 bytes, chunk_size=4
    drives = _make_drives(tmp_path)
    db, session, entry = _setup_scanned_session(
        tmp_path, drives, "A", "recover.bin", original, chunk_size=4
    )

    src = session.src_path(entry)
    dst = session.dst_path(entry)

    # Manually simulate: advance_round once to mark entry sparse and move one chunk
    advance_round(db, session, drives.free_bytes)

    # At this point, entry should be 'sparse' (first round transitions it)
    # Now simulate crash: the tail chunk (offset=8, len=2) was written to dst
    # but src was NOT truncated. We achieve this by manually:
    #   - Re-creating the sparse dst at full size with the tail chunk written
    #   - Restoring src to full original data (as if truncate didn't happen)
    # Actually, after one advance_round, the tail chunk has been moved:
    #   - remaining was 10, offset=8, length=2
    #   - dst[8:10] was written, src was truncated to 8
    # "Crash before truncate" means src is still at 10 bytes.
    # We restore src to the original content:
    src.write_bytes(original)
    # dst should have the tail chunk at offset 8 already (from the round)
    # Make sure dst is a proper sparse file with the tail written
    # (it was already written by advance_round, so we just verify)
    assert dst.exists()

    # Now run to completion - advance_round should handle the idempotent
    # re-write of the tail chunk (same data, same offset) gracefully
    max_rounds = 20
    for _ in range(max_rounds):
        entries_left = (
            db.entries(kind="file", state="pending") +
            db.entries(kind="file", state="sparse")
        )
        if not entries_left:
            break
        advance_round(db, session, drives.free_bytes)

    done = db.entries(kind="file", state="done")
    assert len(done) == 1

    assert dst.exists()
    assert dst.read_bytes() == original
    assert not src.exists()

    db.close()


# ---------------------------------------------------------------------------
# test: src deleted but entry still 'sparse' and dst complete
# ---------------------------------------------------------------------------

def test_recovery_src_deleted_dst_complete(tmp_path):
    """
    Crash after src deletion but before 'done' mark:
    advance_round should detect complete dst and mark entry done.
    """
    original = b"hello"
    drives = _make_drives(tmp_path)
    db, session, entry = _setup_scanned_session(
        tmp_path, drives, "A", "srcgone.bin", original, chunk_size=4
    )

    src = session.src_path(entry)
    dst = session.dst_path(entry)

    # Manually set up the "crashed after src deletion" state:
    # Mark entry as sparse
    db.set_entry_state(entry.id, "sparse")

    # Create complete dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(original)

    # Delete src (simulating crash after src unlink, before 'done' mark)
    src.unlink()
    assert not src.exists()

    # Reload entry
    entries = db.entries(kind="file", state="sparse")
    assert len(entries) == 1
    entry = entries[0]

    # advance_round should detect src is gone, dst is complete, and mark done
    advance_round(db, session, drives.free_bytes)

    done = db.entries(kind="file", state="done")
    assert len(done) == 1
    assert dst.read_bytes() == original

    db.close()


# ---------------------------------------------------------------------------
# test: entry 'sparse', dst missing, src complete -> recreated and completes
# ---------------------------------------------------------------------------

def test_recovery_dst_missing_src_complete(tmp_path):
    """
    Entry is 'sparse' but dst is missing while src is fully intact.
    advance_round should recreate the sparse dst and complete the transfer.
    """
    original = b"ABCD"  # exactly one chunk
    drives = _make_drives(tmp_path)
    db, session, entry = _setup_scanned_session(
        tmp_path, drives, "A", "no_dst.bin", original, chunk_size=4
    )

    dst = session.dst_path(entry)

    # Mark entry as sparse (simulating partial first round that was interrupted
    # before create_sparse_file durably returned)
    db.set_entry_state(entry.id, "sparse")
    # dst does NOT exist (crash before create_sparse_file completed)
    assert not dst.exists()
    # src IS complete (size == entry.size)

    # Run to completion
    max_rounds = 10
    for _ in range(max_rounds):
        entries_left = (
            db.entries(kind="file", state="pending") +
            db.entries(kind="file", state="sparse")
        )
        if not entries_left:
            break
        advance_round(db, session, drives.free_bytes)

    done = db.entries(kind="file", state="done")
    assert len(done) == 1
    assert dst.read_bytes() == original

    db.close()


# ---------------------------------------------------------------------------
# test: zero-byte pending file completes in one round without raising
# ---------------------------------------------------------------------------

def test_zero_byte_file_completes_without_error(tmp_path):
    """A zero-byte file moves no chunk but still finishes the entry —
    advance_round must count that as progress, not raise."""
    drives = _make_drives(tmp_path)
    db, session, entry = _setup_scanned_session(
        tmp_path, drives, "A", "empty.bin", b"", chunk_size=4
    )

    src = session.src_path(entry)
    dst = session.dst_path(entry)

    advance_round(db, session, drives.free_bytes)

    done = db.entries(kind="file", state="done")
    assert len(done) == 1
    assert dst.exists() and dst.stat().st_size == 0
    assert not src.exists()

    db.close()


# ---------------------------------------------------------------------------
# test: corruption guard — dst missing, src already truncated -> SparseTransferError
# ---------------------------------------------------------------------------

def test_corruption_guard_dst_missing_src_truncated(tmp_path):
    """
    Entry 'sparse', dst missing, src already truncated (< entry.size):
    This is an irrecoverable corruption — advance_round must raise SparseTransferError.
    """
    original = b"0123456789"  # 10 bytes
    drives = _make_drives(tmp_path)
    db, session, entry = _setup_scanned_session(
        tmp_path, drives, "A", "corrupt.bin", original, chunk_size=4
    )

    src = session.src_path(entry)
    dst = session.dst_path(entry)

    # Mark entry sparse
    db.set_entry_state(entry.id, "sparse")

    # Simulate truncated src (as if one chunk moved) but dst is missing
    winfs.truncate_file(src, 8)  # src is now 8 bytes, not 10
    assert not dst.exists()

    # advance_round must raise SparseTransferError
    raised = False
    try:
        advance_round(db, session, drives.free_bytes)
    except SparseTransferError:
        raised = True

    assert raised, "Expected SparseTransferError for irrecoverable corruption"

    db.close()
