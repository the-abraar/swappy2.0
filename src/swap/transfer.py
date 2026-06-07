"""Pipeline stage 4: move everything from the staged trees to the final roots.

Three passes, each resumable because completion is recorded per entry:

    create_directories  zero-space structure pass
    create_symlinks     recreate links, then drop the staged originals
    copy_files          the main loop:
                            ask the scheduler for whole-file copies and
                            dispatch them to worker threads (fast path)
                            no copy fits and nothing in flight (deadlock)
                            -> advance the sparse engine by one round

The coordinator (this module, single thread) owns all scheduling,
destination free-space reservation, `entries` state transitions, and
progress flushing. Worker threads only copy bytes and report results.
Sparse rounds run only while no whole-file workers are in flight, and the
loop re-consults the scheduler after every completion and sparse round, so
the moment enough space is freed, transfers return to the fast path.

`rollback.py` reuses create_symlinks and copy_files verbatim, passing a
MIRRORED session (staged and final roots swapped) to move everything back.
"""

from __future__ import annotations

import os
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from . import copier, progress, scheduler, sparse
from .db import Database, Entry, Session
from .preflight import FreeBytes, default_free_bytes

_FLUSH_INTERVAL_SECONDS = 0.2


def transfer_all(db: Database, *, free_bytes: FreeBytes | None = None) -> None:
    session = db.session()
    if session.stage != "scanned":
        return  # already transferred (or not yet scanned)
    free_bytes = free_bytes or default_free_bytes

    progress.start_run(db, session, stage="transfer", status_text="Preparing transfer")
    sparse.reset_progress_state()
    try:
        progress.set_stage("transfer", "Creating directories")
        create_directories(db, session)

        progress.set_stage("transfer", "Recreating symlinks")
        create_symlinks(db, session)

        progress.set_stage("transfer", "Copying files")
        copy_files(db, session, free_bytes)

        db.advance_stage("transferred")
        progress.finish_run(stage="transferred", status_text="Transfer complete")
    except BaseException:
        progress.abort_run(status_text="Transfer interrupted")
        raise


def create_directories(db: Database, session: Session) -> None:
    done_ids: list[int] = []
    for entry in db.entries(kind="dir", state="pending"):
        session.dst_path(entry).mkdir(parents=True, exist_ok=True)
        done_ids.append(entry.id)
    db.set_entries_done(done_ids)


def create_symlinks(db: Database, session: Session) -> None:
    done_ids: list[int] = []
    for entry in db.entries(kind="symlink", state="pending"):
        src = session.src_path(entry)
        dst = session.dst_path(entry)
        if dst.is_symlink():
            dst.unlink()  # leftover from an interrupted previous run
        os.symlink(entry.link_target, dst, target_is_directory=src.is_dir())
        if src.is_symlink():
            src.unlink()  # the link itself; its target is untouched
        done_ids.append(entry.id)
    db.set_entries_done(done_ids)


def copy_files(db: Database, session: Session, free_bytes: FreeBytes) -> None:
    required_headroom = session.safety_margin + session.chunk_size
    pending_by_side = _load_pending_queues(db)
    reserved_bytes = {"A": 0, "B": 0}
    available_workers = list(range(1, session.worker_count + 1))
    futures: dict[Future[copier.CopyResult], tuple[Entry, int, str]] = {}
    done_batch: list[int] = []
    last_flush = 0.0

    with ThreadPoolExecutor(max_workers=session.worker_count) as executor:
        while True:
            scheduled = False
            while available_workers:
                free = {
                    "A": free_bytes(session.final_a) - reserved_bytes["A"],
                    "B": free_bytes(session.final_b) - reserved_bytes["B"],
                }
                candidates = _candidate_heads(pending_by_side)
                pick = scheduler.choose_next_copy(
                    candidates,
                    free,
                    required_headroom=required_headroom,
                )
                if pick is None:
                    break

                entry = pending_by_side[pick.src_side].popleft()
                worker_id = available_workers.pop(0)
                reserved_bytes[pick.dst_side] += entry.size or 0
                future = executor.submit(copier.copy_file, session, entry, worker_id=worker_id)
                futures[future] = (entry, worker_id, pick.dst_side)
                scheduled = True

            if futures:
                if not scheduled:
                    for worker_id in available_workers:
                        progress.worker_idle(worker_id, note="waiting for free space", waiting=True)
                done, _ = wait(list(futures), timeout=0.01, return_when=FIRST_COMPLETED)
                if not done:
                    last_flush = _maybe_flush(last_flush)
                    continue
                for future in done:
                    entry, worker_id, dst_side = futures.pop(future)
                    reserved_bytes[dst_side] -= entry.size or 0
                    available_workers.append(worker_id)
                    available_workers.sort()
                    try:
                        result = future.result()
                    except BaseException:
                        _flush_done_batch(db, done_batch)
                        progress.worker_idle(worker_id, note="failed")
                        progress.flush(force=True)
                        raise
                    done_batch.append(entry.id)
                    progress.worker_finished(result.history)
                    last_flush = _maybe_flush(last_flush)
                continue

            if _has_pending(pending_by_side):
                _flush_done_batch(db, done_batch)
                sparse_worker_id = available_workers[0] if available_workers else 1
                for worker_id in available_workers[1:]:
                    progress.worker_idle(worker_id, note="waiting for sparse", waiting=True)
                sparse.advance_round(db, session, free_bytes, worker_id=sparse_worker_id)
                progress.worker_idle(sparse_worker_id, note="sparse round complete")
                _flush_done_batch(db, done_batch)
                pending_by_side = _load_pending_queues(db)
                last_flush = _maybe_flush(last_flush)
                continue

            _flush_done_batch(db, done_batch)
            in_flight_sparse = db.entries(kind="file", state="sparse")
            if in_flight_sparse:
                sparse_worker_id = available_workers[0] if available_workers else 1
                for worker_id in available_workers[1:]:
                    progress.worker_idle(worker_id, note="waiting for sparse", waiting=True)
                sparse.advance_round(db, session, free_bytes, worker_id=sparse_worker_id)
                progress.worker_idle(sparse_worker_id, note="sparse round complete")
                _flush_done_batch(db, done_batch)
                pending_by_side = _load_pending_queues(db)
                last_flush = _maybe_flush(last_flush)
                continue

            progress.flush(force=True)
            return


def _maybe_flush(last_flush: float, *, force: bool = False) -> float:
    now = time.monotonic()
    if force or now - last_flush >= _FLUSH_INTERVAL_SECONDS:
        progress.flush(force=force)
        return now
    return last_flush


def _load_pending_queues(db: Database) -> dict[str, deque[Entry]]:
    queues = {"A": deque(), "B": deque()}
    pending = sorted(
        db.entries(kind="file", state="pending"),
        key=lambda entry: ((entry.size or 0), entry.id),
    )
    for entry in pending:
        queues[entry.side].append(entry)
    return queues


def _candidate_heads(pending_by_side: dict[str, deque[Entry]]) -> list[scheduler.CopyCandidate]:
    candidates: list[scheduler.CopyCandidate] = []
    for side in ("A", "B"):
        if not pending_by_side[side]:
            continue
        entry = pending_by_side[side][0]
        candidates.append(
            scheduler.CopyCandidate(
                entry_id=entry.id,
                size=entry.size or 0,
                src_side=entry.side,
                dst_side=_other(entry.side),
            )
        )
    return candidates


def _flush_done_batch(db: Database, done_batch: list[int]) -> None:
    if not done_batch:
        return
    db.set_entries_done(done_batch)
    done_batch.clear()


def _has_pending(pending_by_side: dict[str, deque[Entry]]) -> bool:
    return bool(pending_by_side["A"] or pending_by_side["B"])


def _other(side: str) -> str:
    return "B" if side == "A" else "A"
