# Contributors Guide

This document is the codebase map for Swappy. Read this before making behavior changes.

## Design Goals

The project is intentionally narrow:

- swap two folder trees across distinct volumes;
- remain safe under interruption;
- work when free space is much smaller than either tree;
- keep durable state small and easy to reason about.

The implementation favors idempotent stages and filesystem-backed recovery over complex scheduling metadata.

## Where To Start Reading

Start with `src/swap/pipeline.py`.

The top-level control flow is:

```python
preflight.check(db)
staging.stage_roots(db)
scanner.scan(db)
transfer.transfer_all(db)
cleanup.finalize(db)
```

Each stage reads what it needs from the database, performs one bounded job, and advances `session.stage` only after its work is complete.

## Core Architecture

### 1. Session-driven pipeline

The whole app is a stage machine stored in SQLite.

Forward stages:

- `created`
- `staged`
- `scanned`
- `transferred`
- `done`

Rollback stages:

- `rolling_back`
- `rolled_back`

Stages are monotonic and idempotent. `resume` simply re-runs the same pipeline and lets completed stages no-op.

### 2. Tiny durable state

`src/swap/db.py` is the center of truth.

Correctness-critical tables:

- `session`: one row containing root paths, options, worker count, and current stage.
- `entries`: one row per scanned directory, file, or symlink.

Display-only tables:

- `progress_summary`
- `progress_workers`
- `progress_file_history`

Those progress tables are rebuildable. They must never become part of the recovery contract.

### 3. Recovery by idempotence, not replay logs

The recovery model is intentionally coarse:

- stage completion is durable;
- entry completion is durable;
- sparse copy progress is inferred from the filesystem itself.

There is no chunk table. A sparse transfer truncates the source after each durable chunk, so the current source size becomes the durable progress marker.

### 4. Low-space transfer strategy

`src/swap/transfer.py` coordinates the main move.

The transfer phase has three passes:

1. create destination directories;
2. recreate symlinks and remove their staged originals;
3. move files with whole-file copies first, then sparse rounds when needed.

Whole-file copies are scheduled by `src/swap/scheduler.py`, which is intentionally pure and side-effect free.

If no pending file fits on either side, `src/swap/sparse.py` advances one sparse round. That module is the deadlock breaker for cases where ordinary copies would stall forever.

## Module Map

### Top-level flow

- `src/swap/cli.py`: argparse entry point. Defines `run`, `resume`, `status`, and `rollback`.
- `src/swap/pipeline.py`: forward stage driver. Best first file to read.
- `src/swap/rollback.py`: reverse path. Reuses the same transfer machinery through a mirrored session.

### Durable state and domain model

- `src/swap/db.py`: SQLite schema, dataclasses, session state transitions, entry access, and persisted progress snapshots.

### Pipeline stages

- `src/swap/preflight.py`: validates roots, distinct-volume requirement, nesting rules, and final capacity feasibility.
- `src/swap/staging.py`: renames both roots aside and creates the empty final roots.
- `src/swap/scanner.py`: inventories the staged trees into `entries`; refuses unsupported reparse points.
- `src/swap/transfer.py`: creates directories, recreates symlinks, runs the whole-file scheduler, and invokes sparse rounds when blocked.
- `src/swap/cleanup.py`: removes emptied staged trees and marks the session complete.

### Transfer internals

- `src/swap/scheduler.py`: pure decision logic for choosing the next whole-file copy candidate.
- `src/swap/copier.py`: copies one file, verifies durability, deletes the source, and reports progress history.
- `src/swap/sparse.py`: sparse-file deadlock engine for low-headroom transfers.
- `src/swap/winfs.py`: Windows filesystem helpers such as sparse-file operations and fsync-related utilities.

### Progress and observability

- `src/swap/progress.py`: thread-safe live progress facade, persisted snapshots, worker state, and recent completion history.

### Tests

- `tests/conftest.py`: fake-drive helpers and session builders used across the suite.
- `tests/test_pipeline.py`: end-to-end pipeline, crash/resume, cleanup safety, and persisted progress coverage.
- `tests/test_preflight.py`: root validation and capacity checks.
- `tests/test_scheduler.py`: scheduler heuristics.
- `tests/test_sparse.py`: sparse engine behavior.
- `tests/test_transfer.py`: transfer coordination behavior.
- `tests/test_rollback.py`: rollback invariants and restoration flow.
- `tests/test_progress.py`: progress accounting and persistence.
- `tests/test_winfs.py`: Windows filesystem helpers.

## Important Invariants

These are the rules you should preserve when changing code:

- Never destroy source data before the destination copy is durable.
- Never mark a stage or entry complete before the underlying filesystem work is durable enough to survive interruption.
- Keep stage functions idempotent.
- Keep `scheduler.py` pure.
- Do not introduce new recovery-critical state unless the current filesystem-derived model is insufficient.
- Progress UI state is secondary; recovery must depend only on durable core state.
- Rollback must remain symmetric with forward transfer wherever possible.

## How Rollback Works

Rollback is not a separate copy engine.

`src/swap/rollback.py` mirrors the session so the forward transfer logic can run in reverse:

- forward move: `staged_a -> final_b`, `staged_b -> final_a`
- rollback move: `final_b -> staged_a`, `final_a -> staged_b`

Before that reverse transfer starts, rollback:

1. drains any in-flight sparse transfers to completion;
2. sweeps partial forward destinations that are safe to discard;
3. flips `entries` into rollback meaning in one transaction;
4. runs the mirrored transfer;
5. restores the original root names.

That symmetry is one of the most important design simplifications in the repo.

## Common Change Areas

### Adding a new safety check

Prefer `preflight.py` if the check can run before filesystem mutation starts. Prefer `scanner.py` if the check depends on walking the staged trees.

### Changing transfer order

Keep policy in `scheduler.py` and orchestration in `transfer.py`. Avoid pushing I/O into the scheduler.

### Changing recovery behavior

Start in `db.py`, `pipeline.py`, `transfer.py`, and `rollback.py`. Be explicit about which state is durable, which operations can repeat, and what happens if the process crashes between any two steps.

### Extending progress output

Prefer deriving display state from durable truth on resume rather than persisting more live state.

## Development Notes

- The package uses a `src/` layout under `src/swap/`.
- The project targets Python 3.11+.
- There is no formatter or linter configured; match surrounding style.
- Use `python pytest.py -q` for the test suite.
- Treat generated caches and egg-info directories as build artifacts, not source files.
