# Progress Screen and Parallel Transfer Review Goals

## Purpose

This document explains what the recent transfer/progress changes were trying to achieve, so a reviewer can judge the code against the intended behavior rather than only against the previous serial implementation.

## Primary Goals

1. Add a live progress experience for active `swap run` and `swap resume`.
2. Keep progress reporting separate from transfer logic through a dedicated `swap.progress` facade.
3. Add real parallel whole-file transfer with configurable worker count.
4. Preserve the existing crash-safety model and resumability guarantees.
5. Persist enough progress state in SQLite to reconstruct useful progress after interruption.
6. Keep sparse deadlock resolution serialized even when normal copies run in parallel.

## Intended User-Facing Outcome

- `swap run` should accept `--workers`, defaulting to `8`.
- `swap resume` should reuse the stored worker count from the session.
- During active transfer, the program should show a Rich-based live screen with:
  - overall byte progress
  - one row per worker slot
  - aggregate timing and throughput stats
  - recent completed-file history
- `swap status` was intentionally left unchanged.

## Architectural Intent

### Progress subsystem

- The rest of the code should only emit events such as:
  - `start_run`
  - `set_stage`
  - `worker_started`
  - `worker_progress`
  - `worker_finished`
  - `worker_idle`
  - `chunk_started`
  - `chunk_progress`
  - `chunk_finished`
  - `flush`
  - `finish_run`
- Transfer workers should not know about SQLite tables or Rich rendering details.
- The progress subsystem should be thread-safe because whole-file workers run concurrently.

### Transfer coordination

- The coordinator in `transfer.py` should remain the single owner of:
  - scheduling decisions
  - destination free-space reservation
  - `entries` table state transitions
  - sparse-engine invocation
  - periodic progress flushing
- Worker threads should only perform file-copy work and report copy results.
- Sparse transfer should still be single-owner and should not run in parallel across multiple chunk workers.

## Durability and Recovery Goals

### Session and DB

- `Session` should persist `worker_count`.
- Progress persistence should live in `db.py`, consistent with the repo convention that durable state is centralized there.
- The DB should store:
  - one current summary row
  - one row per worker slot
  - append-only completed-file history

### Resume behavior

- On resume, stale in-flight whole-file worker progress should be discarded.
- Progress should be rebuilt from durable truth:
  - completed bytes from `entries.state = 'done'`
  - sparse bytes from `entry.size - current truncated source size`
  - zero active whole-file worker bytes
- A crash between source deletion and final `done` mark should still be recoverable as before.

## Concurrency Goals

- Whole-file copies should run in parallel when free-space constraints allow it.
- The scheduler should not oversubscribe destination space.
- Before dispatching a worker, the coordinator should reserve destination bytes for that copy.
- A file should never be leased to two workers at once.
- If no whole-file copy fits, the system should fall back to one sparse round at a time.

## Instrumentation Goals

- Python copy should use a manual buffered loop instead of `shutil.copyfileobj` so byte progress and timing are measurable.
- Per-file history should include, where available:
  - path
  - size
  - worker id
  - mode
  - backend
  - start/end time
  - duration
  - throughput
  - read/write/verify/chunk timing
- `robocopy` should remain supported, but its metrics can be coarse if exact timing breakdowns are unavailable.

## Important Invariants To Review

1. Source bytes must not be deleted before destination bytes are durable.
2. Only the coordinator should mutate `entries` state for whole-file completion.
3. Worker threads should not write to SQLite directly.
4. Free-space reservation should be released exactly once per finished or failed worker.
5. Sparse progress reconstruction on resume should not depend on in-memory state.
6. Progress tables should reflect the configured worker count and preserve completed-file history.
7. Parallel execution should not break idempotence or resume safety.

## Non-Goals

- No change to the `status` command output.
- No attempt to parallelize sparse chunk transfer itself.
- No cross-process monitoring interface beyond the persisted tables.
- No guarantee of fine-grained metrics from `robocopy`; coarse reporting is acceptable there.

## Good Review Questions

1. Does the implementation actually preserve the original crash-safety semantics?
2. Is the coordinator truly the only owner of scheduling and DB completion state?
3. Can destination-space reservation fail open and allow oversubscription?
4. Are worker lifecycle updates and progress snapshots thread-safe?
5. Does resume discard stale active whole-file state while correctly reconstructing sparse progress?
6. Are the new DB writes and flush cadence reasonable, or do they introduce correctness or performance issues?
7. Are there race conditions around worker completion, error handling, or progress finalization?
8. Does the sparse path remain serialized even with `workers > 1`?

## Files Most Relevant To Review

- [src/swap/db.py](H:/projects/swap/src/swap/db.py)
- [src/swap/progress.py](H:/projects/swap/src/swap/progress.py)
- [src/swap/transfer.py](H:/projects/swap/src/swap/transfer.py)
- [src/swap/copier.py](H:/projects/swap/src/swap/copier.py)
- [src/swap/sparse.py](H:/projects/swap/src/swap/sparse.py)
- [src/swap/cli.py](H:/projects/swap/src/swap/cli.py)
- [tests/test_progress.py](H:/projects/swap/tests/test_progress.py)
- [tests/test_transfer.py](H:/projects/swap/tests/test_transfer.py)
- [tests/test_pipeline.py](H:/projects/swap/tests/test_pipeline.py)

