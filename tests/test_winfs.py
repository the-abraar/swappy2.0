"""Tests for winfs filesystem utilities."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # the shim
from swap import winfs


def test_create_sparse_file_logical_size(tmp_path):
    """Logical size of a sparse file equals the requested size."""
    p = tmp_path / "sparse.bin"
    size = 1024 * 1024  # 1 MB
    winfs.create_sparse_file(p, size)
    assert p.exists()
    assert p.stat().st_size == size


def test_create_sparse_file_small_allocated_on_ntfs(tmp_path):
    """A 10 MB sparse file must have ~0 bytes allocated on disk.

    This is the core property the deadlock engine depends on: the
    destination consumes no space until chunks are actually written.
    """
    p = tmp_path / "big_sparse.bin"
    size = 10 * 1024 * 1024  # 10 MB
    winfs.create_sparse_file(p, size)
    assert p.stat().st_size == size

    if not winfs.volume_supports_sparse(tmp_path):
        return  # nothing more to assert on volumes without sparse support

    assert winfs.allocated_size(p) < size // 100


def test_chunk_write_allocates_only_written_clusters(tmp_path):
    """Writing one chunk into a sparse file allocates ~that chunk, no more."""
    if not winfs.volume_supports_sparse(tmp_path):
        return
    p = tmp_path / "chunked.bin"
    # APFS zero-fills holes under ~16 MB in front of a write, so keep the hole large.
    size = 64 * 1024 * 1024
    chunk = 1024 * 1024
    winfs.create_sparse_file(p, size)
    with open(p, "r+b") as f:
        f.seek(32 * chunk)
        f.write(b"x" * chunk)
        f.flush()
        os.fsync(f.fileno())
    alloc = winfs.allocated_size(p)
    assert chunk <= alloc < 2 * chunk


def test_create_sparse_file_overwrite(tmp_path):
    """create_sparse_file overwrites an existing file."""
    p = tmp_path / "sparse.bin"
    p.write_bytes(b"hello world")
    winfs.create_sparse_file(p, 500)
    assert p.stat().st_size == 500


def test_truncate_file_shrinks(tmp_path):
    """truncate_file shrinks the file to the given size."""
    p = tmp_path / "shrink.bin"
    p.write_bytes(b"A" * 100)
    winfs.truncate_file(p, 40)
    assert p.stat().st_size == 40
    assert p.read_bytes() == b"A" * 40


def test_truncate_file_to_zero(tmp_path):
    """truncate_file to zero bytes works."""
    p = tmp_path / "zero.bin"
    p.write_bytes(b"some content")
    winfs.truncate_file(p, 0)
    assert p.stat().st_size == 0


def test_fsync_file_runs_without_error(tmp_path):
    """fsync_file completes without raising."""
    p = tmp_path / "fsync.bin"
    p.write_bytes(b"data to sync")
    # Should not raise
    winfs.fsync_file(p)


def test_write_at_offset_reads_back(tmp_path):
    """Writing at an offset and reading back: unwritten regions read as zeros."""
    p = tmp_path / "partial.bin"
    size = 100
    winfs.create_sparse_file(p, size)

    # Write 10 bytes at offset 50
    data = b"0123456789"
    with open(p, "r+b") as f:
        f.seek(50)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    content = p.read_bytes()
    assert len(content) == size
    # Bytes before the write should be zero
    assert content[:50] == b"\x00" * 50
    # Written bytes should match
    assert content[50:60] == data
    # Bytes after the write should be zero
    assert content[60:] == b"\x00" * 40


def test_unwritten_sparse_regions_are_zero(tmp_path):
    """A freshly-created sparse file reads as all zeros."""
    p = tmp_path / "zeros.bin"
    winfs.create_sparse_file(p, 256)
    content = p.read_bytes()
    assert content == b"\x00" * 256


def test_allocated_size_non_sparse_file(tmp_path):
    """allocated_size for a regular (non-sparse) file is >= its logical size."""
    p = tmp_path / "regular.bin"
    data = b"X" * 4096
    p.write_bytes(data)
    alloc = winfs.allocated_size(p)
    # allocated size should be at least as large as the data
    assert alloc >= len(data)
