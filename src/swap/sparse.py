"""The deadlock engine: circular-buffer chunk transfer via NTFS sparse files.

Used when no whole-file copy fits on either drive. Each call to
`advance_round` moves ONE chunk in each direction (A->B, then B->A), so a
full round is space-neutral on both drives: every side gains one incoming
chunk and frees one outgoing chunk. Both drives only ever need
`chunk_size` bytes of headroom. The transfer loop calls this repeatedly,
re-consulting the scheduler between rounds — as soon as enough space has
been freed for a normal whole-file copy, the fast path takes over again.

How one file is transferred (1x I/O — every byte read once, written once):

    1. Create the destination as a SPARSE file at full size
       (0 bytes allocated on disk).
    2. Take the chunk at the TAIL of the source, write it into the
       destination at the same byte offset, fsync.
    3. Truncate the source by that chunk, fsync.
    4. Repeat until the source is empty, then delete it.

Crash recovery needs no chunk bookkeeping at all, because truncation makes
the source's current size the durable progress marker:

    source size on disk  ->  what happened              ->  what to do
    unchanged                crashed before/mid dst write    redo the chunk
                             (dst write was idempotent: same data, same offset)
    already shortened        crashed after truncate          next tail chunk
    source file gone         crashed before 'done' mark      verify dst, mark done

The one safety invariant: the source is never truncated until the chunk is
durably (fsync) in the destination — data always exists in >= 1 place.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

from . import winfs
from .db import Database, Entry, Session
from .preflight import FreeBytes

_IO_BLOCK = 8 * 1024 * 1024


class SparseTransferError(RuntimeError):
    pass


def advance_round(db: Database, session: Session, free_bytes: FreeBytes) -> None:
    """Move one chunk in each direction. Called only when whole-file copy
    is deadlocked, so at least one side must have files left to move."""
    progressed = False
    for side in ("A", "B"):
        entry = _current_transfer(db, side)
        if entry is not None:
            _advance_one_chunk(db, session, entry, free_bytes)
            progressed = True
    if not progressed:
        raise SparseTransferError("sparse engine called with nothing to transfer")


def _current_transfer(db: Database, side: str) -> Entry | None:
    """The file this side is currently draining: the in-flight sparse
    transfer if one exists, otherwise the largest pending file (largest
    first — they are the ones that can never go through the fast path)."""
    in_flight = [e for e in db.entries(kind="file", state="sparse") if e.side == side]
    if in_flight:
        return in_flight[0]
    pending = [e for e in db.entries(kind="file", state="pending") if e.side == side]
    if not pending:
        return None
    return max(pending, key=lambda e: (e.size, -e.id))


def _advance_one_chunk(
    db: Database, session: Session, entry: Entry, free_bytes: FreeBytes
) -> None:
    src = session.src_path(entry)
    dst = session.dst_path(entry)

    if entry.state == "pending":
        _require_sparse_support(dst)
        db.set_entry_state(entry.id, "sparse")
        entry = replace(entry, state="sparse")

    if not src.exists():
        _finish(db, entry, dst)  # crashed between source delete and 'done' mark
        return

    remaining = src.stat().st_size
    _ensure_sparse_destination(dst, entry, remaining)

    if remaining > 0:
        offset = (remaining - 1) // session.chunk_size * session.chunk_size
        length = remaining - offset
        _require_headroom(session, free_bytes, length, dst)

        digest = _copy_range(src, dst, offset, length)   # fsyncs dst
        if session.verify_chunks:
            _verify_range(dst, offset, length, digest)
        winfs.truncate_file(src, offset)                 # fsyncs src
        remaining = offset

    if remaining == 0:
        src.unlink()
        _finish(db, entry, dst)


def _ensure_sparse_destination(dst: Path, entry: Entry, remaining: int) -> None:
    """Create the full-size sparse destination, exactly once per file.

    If dst is missing or has the wrong size, we crashed before its creation
    became durable — which can only have happened before any chunk moved,
    i.e. while the source is still complete. Recreating is then free and
    safe. Any other combination means external interference: stop.
    """
    if dst.exists() and dst.stat().st_size == entry.size:
        return
    if remaining != entry.size:
        raise SparseTransferError(
            f"destination {dst} is missing/wrong-sized but the source was"
            " already partially truncated — refusing to continue"
        )
    winfs.create_sparse_file(dst, entry.size)


def _finish(db: Database, entry: Entry, dst: Path) -> None:
    if not dst.exists() or dst.stat().st_size != entry.size:
        raise SparseTransferError(
            f"transfer of {entry.rel_path} cannot complete: destination"
            f" missing or wrong size at {dst}"
        )
    db.set_entry_state(entry.id, "done")


def _copy_range(src: Path, dst: Path, offset: int, length: int) -> bytes:
    """Copy src[offset:offset+length] to the same offset in dst.
    Returns the SHA-256 of the copied bytes. Fsyncs dst before returning."""
    hasher = hashlib.sha256()
    with open(src, "rb") as fsrc, open(dst, "r+b") as fdst:
        fsrc.seek(offset)
        fdst.seek(offset)
        left = length
        while left > 0:
            block = fsrc.read(min(_IO_BLOCK, left))
            if not block:
                raise SparseTransferError(f"unexpected EOF reading {src}")
            hasher.update(block)
            fdst.write(block)
            left -= len(block)
        fdst.flush()
        os.fsync(fdst.fileno())
    return hasher.digest()


def _verify_range(dst: Path, offset: int, length: int, expected_digest: bytes) -> None:
    hasher = hashlib.sha256()
    with open(dst, "rb") as fdst:
        fdst.seek(offset)
        left = length
        while left > 0:
            block = fdst.read(min(_IO_BLOCK, left))
            if not block:
                raise SparseTransferError(f"unexpected EOF verifying {dst}")
            hasher.update(block)
            left -= len(block)
    if hasher.digest() != expected_digest:
        raise SparseTransferError(f"chunk verification failed at {dst} offset {offset}")


def _require_sparse_support(dst: Path) -> None:
    if not winfs.volume_supports_sparse(dst.parent):
        raise SparseTransferError(
            f"volume of {dst} does not support sparse files (non-NTFS?);"
            " the low-space deadlock cannot be resolved on this volume"
        )


def _require_headroom(
    session: Session, free_bytes: FreeBytes, length: int, dst: Path
) -> None:
    if free_bytes(dst.parent) < length:
        raise SparseTransferError(
            f"not enough free space on {dst.parent} for a {length}-byte chunk;"
            " retry with a smaller --chunk-size"
        )
