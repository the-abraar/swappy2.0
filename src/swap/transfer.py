"""Pipeline stage 4: move everything from the staged trees to the final roots.

Three passes, each resumable because completion is recorded per entry:

    create_directories  zero-space structure pass
    create_symlinks     recreate links, then drop the staged originals
    copy_files          the main loop:
                            ask the scheduler for the next whole-file copy
                            -> run it (fast path)
                            no copy fits (deadlock)
                            -> advance the sparse engine by one round

The loop re-consults the scheduler after every sparse round, so the moment
enough space is freed, transfers return to the fast whole-file path.
"""

from __future__ import annotations

import os

from . import copier, scheduler, sparse
from .db import Database, Entry, Session
from .preflight import FreeBytes, default_free_bytes


def transfer_all(db: Database, *, free_bytes: FreeBytes | None = None) -> None:
    session = db.session()
    if session.stage != "scanned":
        return  # already transferred (or not yet scanned)
    free_bytes = free_bytes or default_free_bytes

    create_directories(db, session)
    create_symlinks(db, session)
    copy_files(db, session, free_bytes)

    db.advance_stage("transferred")


def create_directories(db: Database, session: Session) -> None:
    for entry in db.entries(kind="dir", state="pending"):
        session.dst_path(entry).mkdir(parents=True, exist_ok=True)
        db.set_entry_state(entry.id, "done")


def create_symlinks(db: Database, session: Session) -> None:
    for entry in db.entries(kind="symlink", state="pending"):
        src = session.src_path(entry)
        dst = session.dst_path(entry)
        if dst.is_symlink():
            dst.unlink()  # leftover from an interrupted previous run
        os.symlink(entry.link_target, dst, target_is_directory=src.is_dir())
        if src.is_symlink():
            src.unlink()  # the link itself; its target is untouched
        db.set_entry_state(entry.id, "done")


def copy_files(db: Database, session: Session, free_bytes: FreeBytes) -> None:
    required_headroom = session.safety_margin + session.chunk_size

    while True:
        pending = db.entries(kind="file", state="pending")
        in_flight = db.entries(kind="file", state="sparse")
        if not pending and not in_flight:
            return  # all files moved

        free = {
            "A": free_bytes(session.final_a),
            "B": free_bytes(session.final_b),
        }
        candidates = [
            scheduler.CopyCandidate(
                entry_id=entry.id,
                size=entry.size,
                src_side=entry.side,
                dst_side=_other(entry.side),
            )
            for entry in pending
        ]
        pick = scheduler.choose_next_copy(
            candidates, free, required_headroom=required_headroom
        )

        if pick is not None:
            entry = _entry_by_id(pending, pick.entry_id)
            copier.copy_file(db, session, entry)
        else:
            sparse.advance_round(db, session, free_bytes)


def _entry_by_id(entries: list[Entry], entry_id: int) -> Entry:
    return next(entry for entry in entries if entry.id == entry_id)


def _other(side: str) -> str:
    return "B" if side == "A" else "A"
