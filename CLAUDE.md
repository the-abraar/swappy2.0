# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Windows-first CLI that swaps two large folder trees across drives, even when free space on each drive is far smaller than either folder. Crash-safe and resumable. Python 3.11+, src layout (`src/swap/`), only dependency is `rich`. Works on POSIX too (python copy backend, truncate-created sparse holes) — Windows specifics are isolated in `winfs.py` (ctypes Win32: sparse files, fsync, `CopyFileExW`).

## Commands

```text
python pytest.py -q                          # full test suite via the repo's pytest shim
python pytest.py tests/test_sparse.py -q     # single test file
python -m pytest tests -q                    # real pytest also works
python -m pip install -e .                   # editable install

python -m swap run      --folder-a D:\A --folder-b E:\B --work-dir D:\.swap_work
python -m swap resume   --work-dir D:\.swap_work
python -m swap rollback --work-dir D:\.swap_work
python -m swap status   --work-dir D:\.swap_work
```

CLI flags for `run`: `--chunk-size` (default 64MB), `--safety-margin` (default 64MB), `--backend winapi|python` (default winapi on Windows, python elsewhere), `--workers N` (default 8, must be ≥1), `--no-verify-chunks`, `--volume-mode real|simulate-distinct` (the latter treats each root as its own volume, for tests on one disk). Sizes parse as `64MB`-style strings (`cli.parse_size`). `resume` and `rollback` take only `--work-dir` and reuse all stored options including `worker_count`.

`rollback` (`rollback.py`) undoes a partially-completed swap and is itself crash-safe/resumable (re-run `swap rollback`; `resume` refuses while stage is `rolling_back`/`rolled_back`). It refuses once stage=='done'. Stages branch `staged|scanned|transferred → rolling_back → rolled_back` (`db.begin_rollback` flips entry states + stage in one transaction; under rollback, `pending` = move back, `done` = nothing to undo). The reverse transfer is the forward machinery run on a MIRRORED session (staged/final roots swapped pairwise), so scheduler + sparse engine crash-safety applies symmetrically. In-flight `sparse` files are first drained forward (paired `sparse.advance_round` loops, NOT single-file drains — one-directional chunks aren't space-neutral) because a half-moved sparse pair can't be reversed without chunk bookkeeping; the partial-destination sweep hard-refuses while one exists.

Note: `pytest.py` at the repo root is a minimal pytest-shim runner (real pytest is not required). It shadows the `pytest` module for anything imported from the repo root. The shim only supports: function-level conftest fixtures, `tmp_path`, `-q`, `-m markexpr`, path args, and `pytest.mark.*`. Don't use parametrize, classes, or plugins in tests.

There is no linter or formatter configured — match surrounding file style. Style: 4-space indent, snake_case, frozen dataclasses, explicit type hints, short orientation docstrings.

## Architecture

The whole program is five idempotent stage calls in `src/swap/pipeline.py`; each reads what it needs from SQLite, does its work, and advances `session.stage` (`created → staged → scanned → transferred → done`). `run` and `resume` are the same call — resume just re-runs the pipeline; completed stages no-op by checking `session.stage` at entry.

| stage | module | what it does |
|---|---|---|
| 1 | `preflight.py` | refuse impossible swaps: roots exist, not nested, distinct volumes, capacity-feasible (`free + outgoing - incoming ≥ safety_margin` on each side). Only runs while stage=='created'. Raises `PreflightError`. |
| 2 | `staging.py` | rename `folder_a → .swap_staged_a` (sibling dir, instant same-drive rename), same for B; create empty final roots. Final roots swap names: `final_a = folder_a.parent / folder_b.name`. |
| 3 | `scanner.py` | inventory both staged trees into `entries` (atomic full replace). Refuses junctions/reparse points (`UnsupportedEntry`) before anything is modified. Symlinks recorded with `link_target`, never followed. |
| 4 | `transfer.py` | the interesting part — see below |
| 5 | `cleanup.py` | remove the now-empty staged trees (raises rather than delete if any file remains), mark `done`. |

Supporting modules: `scheduler.py` (pure functions, no I/O/DB), `copier.py` (whole-file workers), `sparse.py` (deadlock engine), `progress.py` (live UI + persistence facade), `winfs.py` (ctypes Win32 sparse/fsync), `db.py` (ALL durable state), `cli.py` (argparse → pipeline).

Dependency injection for tests: `pipeline.run(db, volume_id=..., free_bytes=...)` — both are simple callables (`preflight.VolumeId`, `preflight.FreeBytes`).

### Durable state (db.py)

One SQLite database `<work-dir>/swap.db` (WAL, `synchronous=FULL`). Correctness-critical tables:

- `session` — one row: six root paths (`folder_a/b`, `staged_a/b`, `final_a/b`), `chunk_size`, `backend`, `verify_chunks`, `safety_margin`, `worker_count`, `stage`. Dataclass `Session` has path helpers: `src_path(entry)` = staged root + rel_path, `dst_path(entry)` = OTHER side's final root + rel_path.
- `entries` — one row per dir/file/symlink: `(id, side 'A'|'B', rel_path, kind, size, link_target, state)`; `state: pending → done`, or `pending → sparse → done` for deadlock-engine files.

There is deliberately **no chunk table**: every transferred sparse chunk truncates the source file, so the source's current size IS the durable progress marker.

Display-only tables (live progress screen; safe to lose, rebuilt on resume): `progress_summary` (single row of aggregate counters/timings), `progress_workers` (one row per worker slot, fully replaced each flush), `progress_file_history` (append-only per-file records, `INSERT OR IGNORE` keyed on `entry_id`). Key dataclasses: `ProgressSummarySnapshot`, `ProgressWorkerSnapshot`, `ProgressFileHistory`, `ProgressBootstrap` (from `db.progress_bootstrap(session)` — recomputes done/sparse byte counts from `entries` + source file sizes on resume).

### Transfer (transfer.py — coordinator/worker split)

`copy_files` is a single-threaded **coordinator** loop that owns all scheduling, `entries` state transitions, progress flushing (every 0.2 s + forced on completion), and destination free-space reservation: before dispatching, it subtracts `reserved_bytes[side]` from the scheduler's free-space view and adds `entry.size`; the reservation is released exactly once when the future is popped. `leased_ids` prevents double-leasing a file.

Whole-file copies run on a `ThreadPoolExecutor(worker_count)`. Workers (`copier.copy_file`) only copy bytes, emit progress events, and return a `CopyResult(history=ProgressFileHistory)` — **worker threads never touch SQLite**; the coordinator does `db.set_entry_state(id, "done")` when the future completes. Coordinator waits with `wait(futures, timeout=0.1, FIRST_COMPLETED)`.

Fallback: when no whole-file copy fits (`scheduler.choose_next_copy` returns None) AND no futures are in flight, run ONE `sparse.advance_round`, then re-consult the scheduler — sparse rounds are always serialized and never overlap whole-file workers.

`scheduler.choose_next_copy(candidates, free, required_headroom)` heuristic: only copies leaving `required_headroom` (= `safety_margin + chunk_size`) free after landing; prefer sources on the most-constrained side; then smallest file first.

### Whole-file copy lifecycle (copier.py)

`copy src → dst` (Win32 `CopyFileExW` via `winfs.copy_file_with_progress`, or buffered 8 MB python loop) → `fsync dst` → verify size → `delete src` → coordinator marks 'done'. Crash anywhere before the mark: entry still 'pending', whole copy re-runs harmlessly. Special case: source gone + destination complete (crash between delete and mark) → `_recover_completed_copy` just marks done; source gone + dst incomplete → hard error. Winapi backend gets exact byte progress from the kernel's `LPPROGRESS_ROUTINE` callback, throttled to 8 MB granularity (no read/write timing split available); python backend measures read/write/verify timings per block. Legacy sessions stored with `backend='robocopy'` resume on the winapi path.

### The deadlock engine (sparse.py)

When neither drive can hold a whole-file copy: `advance_round` moves ONE chunk in each direction (A→B then B→A) so each full round is space-neutral; only `chunk_size` headroom per drive is ever needed. Per file: create destination as full-size sparse file (`winfs.create_sparse_file`, ~0 bytes allocated; on Windows MUST use SetEndOfFile — CRT truncate zero-fills); copy the TAIL chunk to the same offset + fsync (+ optional SHA-256 read-back verify); truncate source + fsync; repeat; delete source; mark done. `_current_transfer` per side: the in-flight 'sparse' entry, else the LARGEST pending file. Crash recovery from source size alone (see module docstring's recovery table). A finished file with zero bytes moved (zero-byte file, or recovery) still counts as progress in `advance_round` — don't reintroduce the spurious `SparseTransferError`. Module-level `_SPARSE_STATS` carries per-file display timings; `reset_progress_state()` is called at transfer start.

### Progress (progress.py)

Thread-safe facade over a module-level `_RUNTIME` singleton. Emitters: `start_run`, `set_stage`, `worker_started/progress/finished`, `worker_idle`, `chunk_started/progress/finished`, `flush`, `finish_run`, `abort_run`. All are no-ops when no runtime is active (so unit tests can call transfer code without a screen). Workers know nothing about SQLite or Rich. Rich `Live` screen (stderr, only when a real terminal; `rich` import failure degrades gracefully) is **rate-limited to 4 Hz** (`_RENDER_INTERVAL_SECONDS`) because workers report per 8 MB block — do not force per-event renders. DB writes happen only in `flush()`/`worker_finished`, which are called from the coordinator thread. On `start_run`, worker rows are reset to idle and the summary is rebuilt from `db.progress_bootstrap` (in-flight whole-file bytes deliberately discarded; cumulative timings/`started_at` carried over from a prior summary row). `swap status` does NOT use this subsystem; it reads `entries` counts directly.

## Invariants (do not break)

1. Source bytes are never destroyed before destination bytes are fsynced — data always exists in ≥ 1 place.
2. Completion is recorded in SQLite only after the step is fully durable; recovery is coarse and idempotent (worst case redoes one chunk or one file copy).
3. Only the coordinator mutates `entries` state and writes progress to SQLite; worker threads report results back.
4. Free-space reservation is released exactly once per finished/failed worker; a file is never leased to two workers.
5. Sparse rounds never run concurrently with whole-file workers or each other.
6. Resume must not trust in-memory or progress-table state: rebuild from `entries.state` and source-file sizes.

## Tests

`tests/conftest.py` provides:
- `FakeDrives(drive_a, drive_b, cap_a, cap_b)` — two directories acting as capacity-limited volumes. `free_bytes(path)` = cap − sum of file sizes, with in-flight sparse destinations' unwritten holes subtracted (requires `drives.bind_db(db)`). `volume_id(path)` returns the drive root.
- `make_session(...)` — builds a `Session` the same way `cli._cmd_run` does (defaults: chunk_size=4, backend="python", safety_margin=0).

Typical end-to-end setup (see `test_pipeline._setup_and_run`): create FakeDrives → folders with files → `db.create_session` → `pipeline.run(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)`. Crash scenarios are simulated by mutating the filesystem/DB into mid-step states and re-running (see `test_sparse.py` recovery tests). Cover crash-safety, resumability, and low-space edge cases whenever behavior changes. Tiny chunk sizes (4 bytes) make sparse rounds testable.

## Conventions

- Performance of the copy paths matters; keep progress reporting off the hot path (rate-limited renders, coordinator-side DB writes).
- Keep stage logic idempotent; prefer small single-purpose functions and loose coupling through SQLite over cross-module coupling.
- Module docstrings in `db.py`, `sparse.py`, `copier.py`, `transfer.py` document the crash-recovery model — keep them current; reviewers rely on them.
- Commit subjects: short imperative, scoped to one change, e.g. `scanner: reject unsupported reparse points`.

## Known limitations (by design)

Junctions/unknown reparse points refused at scan; symlinks recreated, not followed; deadlock engine requires sparse-file support on both volumes (NTFS yes, exFAT no — clear error); metadata is contents + timestamps/attributes only (no ACLs/ADS); no undo after a completed swap.
