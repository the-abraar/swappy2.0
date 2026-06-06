"""Undo a partially-completed swap: `swap rollback --work-dir ...`.

Everything moved so far is moved back and the original roots are restored
byte-for-byte. Refused once the swap is 'done' (the staged trees are gone —
the documented no-undo point). Like the forward pipeline, every phase is
idempotent and keyed on `session.stage`, so re-running `swap rollback`
after a crash resumes where it left off.

The trick is that reversal needs no new copy machinery. Forward moved
`staged_a/rel -> final_b/rel`; rollback moves `final_b/rel -> staged_a/rel`,
which is exactly the forward transfer run against a MIRRORED session
(staged and final roots swapped pairwise). `transfer.copy_files`, the
scheduler, and the sparse deadlock engine run verbatim on the mirror, so
all of their crash-safety guarantees apply symmetrically.

Phases, in order:

  A  drain      run forward sparse rounds until no 'sparse' entry remains.
                A half-moved sparse pair (source truncated, destination
                holding only the tail) cannot be reversed without chunk
                bookkeeping; finished forward it becomes an ordinary
                fully-transferred file.
  B  flip       sweep partial destinations left by interrupted whole-file
                copies, then remap every entry state to its rollback
                meaning ('pending' = move back, 'done' = nothing to undo)
                and set stage='rolling_back' — one SQLite transaction.
  C  reverse    transfer.create_symlinks + transfer.copy_files on the
                mirrored session. Resumable exactly like the forward run.
  D  restore    remove the (now empty) final trees, rename the staged
                roots back to the original names, mark 'rolled_back'.

Phase B's sweep runs only while stage is still a forward stage, and only
deletes a destination while the source is intact — data always exists in
>= 1 place. Phase A MUST complete before the sweep: a mid-sparse source
looks intact but is truncated, and deleting its destination would destroy
the tail bytes. The sweep therefore refuses if any 'sparse' entry remains.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from . import preflight, progress, sparse, transfer
from .db import Database, Session
from .preflight import FreeBytes, default_free_bytes


class RollbackError(RuntimeError):
    pass


def run(db: Database, *, free_bytes: FreeBytes | None = None) -> None:
    session = db.session()
    free_bytes = free_bytes or default_free_bytes

    if session.stage == "done":
        raise RollbackError(
            "the swap already completed and the staged trees were removed;"
            " a finished swap cannot be rolled back"
        )
    if session.stage == "rolled_back":
        return  # already rolled back
    if session.stage == "created":
        db.finish_rollback()  # nothing touched the filesystem yet
        return

    if session.stage != "rolling_back":
        _drain_sparse_transfers(db, session, free_bytes)
        entry_states = _sweep_and_classify(db, session)
        _check_capacity(session, free_bytes)
        db.begin_rollback(entry_states)
        session = db.session()

    _reverse_transfer(db, session, free_bytes)
    _restore_roots(db, session)


def _drain_sparse_transfers(db: Database, session: Session, free_bytes: FreeBytes) -> None:
    """Phase A: run forward sparse rounds until no 'sparse' entry remains.

    Draining one in-flight file by itself is NOT space-neutral (its chunks
    only fill the destination drive) and can wedge on a tight drive, so
    advance_round is reused verbatim: it pairs the two directions, pulling
    in pending ballast when a side has no in-flight file — exactly like the
    forward run, and therefore feasible whenever the forward swap was.
    Anything extra it moves forward is simply rolled back afterwards like
    any other transferred file."""
    while db.entries(kind="file", state="sparse"):
        sparse.advance_round(db, session, free_bytes)


def _sweep_and_classify(db: Database, session: Session) -> dict[int, str]:
    """Phase B (filesystem half): delete partial destinations, and compute
    each entry's rollback state. Idempotent — a re-run after a crash makes
    the same decisions because destinations are only deleted while the
    source is intact (re-classified as 'nothing to undo' either way)."""
    states: dict[int, str] = {}
    for entry in db.entries():
        if entry.state == "sparse":
            raise RollbackError(
                f"entry {entry.rel_path} is still mid-sparse-transfer;"
                " it must be drained before states flip"
            )
        if entry.kind == "dir":
            # forward only created destination dirs; the staged dirs were
            # never removed (cleanup is refused above), so nothing to undo —
            # the final trees are deleted wholesale in phase D
            states[entry.id] = "done"
        elif entry.state == "done":
            states[entry.id] = "pending"  # moved to the final tree: move it back
        elif entry.kind == "file":
            states[entry.id] = _classify_pending_file(session, entry)
        else:
            states[entry.id] = _classify_pending_symlink(session, entry)
    return states


def _classify_pending_file(session: Session, entry) -> str:
    src = session.src_path(entry)
    dst = session.dst_path(entry)
    if src.exists():
        # untouched, or a partial destination from an interrupted copy
        # (also a complete one when the crash hit between fsync and source
        # delete) — the source is intact, so the destination is disposable
        if dst.exists():
            dst.unlink()
        return "done"
    if dst.exists() and dst.stat().st_size == entry.size:
        # the copy actually completed; only the 'done' mark was lost
        # (mirror of copier._recover_completed_copy)
        return "pending"
    raise RollbackError(
        f"source missing and destination incomplete for {entry.rel_path};"
        " refusing to roll back"
    )


def _classify_pending_symlink(session: Session, entry) -> str:
    src = session.src_path(entry)
    dst = session.dst_path(entry)
    if src.is_symlink():  # not exists(): a dangling link is still a link
        if dst.is_symlink():
            dst.unlink()  # leftover from an interrupted forward run
        return "done"
    if dst.is_symlink():
        return "pending"  # recreated forward, crashed before the 'done' mark
    raise RollbackError(
        f"symlink {entry.rel_path} is missing on both sides; refusing to roll back"
    )


def _check_capacity(session: Session, free_bytes: FreeBytes) -> None:
    """Reversal is not automatically feasible: mid-transfer a drive can be
    fuller than at the start. Margin is 0 — rollback restores the original
    layout, so only physical feasibility is required, not the swap margin."""
    outgoing_a = preflight.tree_size(session.final_a)  # B's content going home
    outgoing_b = preflight.tree_size(session.final_b)
    preflight.ensure_capacity(
        free_a=free_bytes(session.staged_a),
        free_b=free_bytes(session.staged_b),
        outgoing_a=outgoing_a,
        incoming_a=outgoing_b,
        outgoing_b=outgoing_b,
        incoming_b=outgoing_a,
        safety_margin=0,
        action="rollback",
    )


def _mirror(session: Session) -> Session:
    """The reverse-direction session: staged and final roots swapped so that
    src_path/dst_path (and the free-space probes inside copy_files) point
    the other way. Note the scheduler's 'A'/'B' labels then no longer match
    physical drives, but they stay internally consistent — free space is
    always probed at the actual destination root."""
    return replace(
        session,
        staged_a=session.final_b,
        staged_b=session.final_a,
        final_a=session.staged_b,
        final_b=session.staged_a,
    )


def _reverse_transfer(db: Database, session: Session, free_bytes: FreeBytes) -> None:
    """Phase C: the forward transfer machinery, run on the mirrored session."""
    mirror = _mirror(session)
    progress.start_run(db, mirror, stage="rollback", status_text="Preparing rollback")
    sparse.reset_progress_state()
    try:
        progress.set_stage("rollback", "Restoring symlinks")
        transfer.create_symlinks(db, mirror)

        progress.set_stage("rollback", "Moving files back")
        transfer.copy_files(db, mirror, free_bytes)

        progress.finish_run(stage="rollback", status_text="Rollback transfer complete")
    except BaseException:
        progress.abort_run(status_text="Rollback interrupted")
        raise


def _restore_roots(db: Database, session: Session) -> None:
    """Phase D: per side, remove the empty final tree, then rename the staged
    root back. Strictly in that order — when both folders share a name the
    final root occupies the original path (final_a == folder_a)."""
    _restore_side(session.staged_a, session.final_a, session.folder_a)
    _restore_side(session.staged_b, session.final_b, session.folder_b)
    db.finish_rollback()


def _restore_side(staged: Path, final: Path, original: Path) -> None:
    if not staged.exists():
        if original.is_dir():
            return  # this side was fully restored on a previous run
        raise RollbackError(f"both staged and original root missing: {staged}")
    _remove_final_tree(final)
    staged.rename(original)


def _remove_final_tree(root: Path) -> None:
    """Like cleanup._remove_empty_tree, but for a final root: by now every
    file and symlink has been moved back, so only directories may remain.
    Anything else (e.g. user-written files) aborts loudly."""
    if not root.exists():
        return  # removed by a previous run
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        if filenames:
            raise RollbackError(
                f"unexpected files left in {dirpath}: {filenames};"
                " rollback refuses to delete data it did not create"
            )
        os.rmdir(dirpath)
