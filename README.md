# swap

Windows-first CLI that swaps two large folder trees across drives, even when
free space on each drive is far smaller than either folder. Crash-safe and
resumable.

```text
python -m swap run    --folder-a D:\Games\GameA --folder-b E:\Games\GameB --work-dir D:\.swap_work
python -m swap resume --work-dir D:\.swap_work
python -m swap status --work-dir D:\.swap_work
```

## How to read this codebase

Start at `src/swap/pipeline.py`. The whole program is five stage calls:

```python
preflight.check(db)        # refuse impossible/unsafe swaps before touching anything
staging.stage_roots(db)    # rename originals aside, create empty final roots
scanner.scan(db)           # inventory the staged trees into SQLite
transfer.transfer_all(db)  # move everything (the interesting part)
cleanup.finalize(db)       # remove empty staged trees, mark the session done
```

Each stage reads what it needs from the database, does its work, and advances
`session.stage`. Stages are idempotent — re-running a completed stage is a
no-op. **Comment out a stage call to disable it.**

The transfer stage is a single loop:

```text
loop:
    scheduler.choose_next_copy(...)   # pure heuristic, scheduler.py
    -> a file fits:   copier.copy_file(...)        # fast path (robocopy)
    -> nothing fits:  sparse.advance_round(...)    # deadlock engine, one chunk each way
```

Module map:

| module | role | talks to |
|---|---|---|
| `pipeline.py` | top-level driver | every stage |
| `db.py` | ALL durable state: 2 SQLite tables (`session`, `entries`) | — |
| `preflight.py` | validation + capacity feasibility | db |
| `staging.py` | root renames (instant, same-drive) | db |
| `scanner.py` | inventory staged trees | db |
| `transfer.py` | structure pass + the copy loop | scheduler, copier, sparse |
| `scheduler.py` | copy-ordering heuristic — pure functions, no I/O, no DB | — |
| `copier.py` | whole-file copy (robocopy / python), verify, delete source | db, winfs |
| `sparse.py` | low-space deadlock engine (see below) | db, winfs |
| `cleanup.py` | remove empty staged trees | db |
| `winfs.py` | ctypes Win32 sparse-file utilities + fsync helpers | — |
| `cli.py` | argparse front end | pipeline, db |

## State model (deliberately tiny)

All durable state is in `swap.db` (SQLite, WAL, `synchronous=FULL`):

- `session` — one row: the six root paths, options, and `stage`
  (`created → staged → scanned → transferred → done`).
- `entries` — one row per dir/file/symlink:
  `pending → done`, or `pending → sparse → done` for files moved by the
  deadlock engine.

There is **no chunk table**. Sparse-transfer progress is derived from the
filesystem itself: every transferred chunk truncates the source file, so the
source's current size *is* the durable progress marker.

## The deadlock engine (`sparse.py`)

When no whole-file copy fits on either drive (e.g. `A(10GB)+B(10GB)` vs
`C(20GB)` with 250MB free per side), the sparse engine alternates
single-chunk moves between the drives — a circular buffer:

1. Create the destination as an **NTFS sparse file** at full size
   (0 bytes allocated on disk).
2. Copy the chunk at the **tail** of the source into the destination at the
   same byte offset; fsync.
3. Truncate the source by one chunk; fsync.
4. Alternate directions: each full round is space-neutral on both drives, so
   `chunk_size` of headroom per drive is all that's ever needed.

Every byte is read once and written once (1x I/O — no merge step, no
temporary chunk files). After every round the scheduler is consulted again,
so the moment enough space frees up, transfers return to the fast
whole-file path.

## Crash safety / resume

The recovery model is **coarse and idempotent**: resume just re-runs the
pipeline (`python -m swap resume`); the worst case redoes one chunk or one
file copy. Two invariants make this safe:

- A source file (or chunk) is never destroyed until its destination data has
  been fsynced to disk.
- Completion is recorded in SQLite only after the step is fully durable, so
  an interrupted step simply re-runs.

## Limitations

- Junctions and unknown reparse points are refused at scan time (before
  anything is modified). Symlinks are recreated, not followed.
- The deadlock engine requires both volumes to support sparse files (NTFS
  does; exFAT does not — the run stops with a clear error).
- Metadata preservation is basic: contents + timestamps/attributes via the
  backend; no ACLs/ADS guarantees.
- No undo after a successfully completed swap.

## Tests

Real pytest is not required; the repo ships a minimal runner shim:

```text
python pytest.py -q
```
