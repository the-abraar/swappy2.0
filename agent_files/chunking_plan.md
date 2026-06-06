# Plan 2: Generalized Low-Space Deadlock Resolution via Sparse-File Circular Buffer (1x I/O)

This document is self-contained. It describes a new feature for the existing swap tool (see `plan.md` and `README.md` for the original design). Read this whole file before touching code.

---

## 1. Background and Motivation

### 1.1 What exists today

The tool swaps two folder trees across drives. The normal path copies whole files with robocopy, deleting sources after verification — the scheduler orders operations so small files go first, freeing space incrementally. When **no whole-file copy fits on either drive** (a "deadlock"), the only fallback is `execute_equal_size_exchange()` in `src/swap/chunk_exchange.py`, which swaps two files **in-place** chunk-by-chunk. It requires:

- The two files are **exactly equal size** (`_validate_pair` raises `UnsupportedChunkExchange` otherwise).
- They are **symmetric partners**: `left.src_absolute_path == right.dst_absolute_path` and vice versa.

If the deadlocked files don't satisfy this, the session fails (see `_scheduled_operation` in `src/swap/orchestrator.py`, lines ~262-275: deadlock → `select_equal_size_exchange_candidates` → no pairs → `RuntimeError`).

### 1.2 What we want

Handle the general deadlock case: any mix of file sizes on both drives, as long as the swap is capacity-feasible. Example that currently fails:

```
Drive1: A (10GB), B (10GB), 250MB free
Drive2: C (20GB), 250MB free
Goal: A,B → drive2; C → drive1
```

No whole-file copy fits. No equal-size symmetric pair exists. Session fails today.

### 1.3 The algorithm (designed in a prior session)

A "circular buffer" that alternates single-chunk transfers between drives:

1. Sort pending files on each drive descending by size.
2. Alternate: move one chunk of a drive2-file to drive1, then one chunk of a drive1-file to drive2.
3. Each chunk move = copy chunk + truncate source by chunk_size. Net space change per full cycle on each drive: zero.
4. Both drives only ever need `chunk_size` free space.

**The I/O problem solved by this plan:** chunks must be taken from the **tail** of the source (truncation only works from the end), so they arrive in reverse order. The naive design stores each chunk as a separate `.bin` file and merges them at the end — doubling total I/O (every byte read twice, written twice).

**The fix:** create the destination as an **NTFS sparse file** at full target size (zero bytes allocated on disk), and write each chunk directly at its correct byte offset as it arrives. No merge step. Every byte is read once and written once — same I/O as a normal copy.

Key NTFS facts (verified against Microsoft docs):
- `FSCTL_SET_SPARSE` + `SetEndOfFile` creates a file of logical size N with **0 bytes allocated**. Unwritten regions read as zeros and consume no disk space.
- Writing at any offset in a sparse file allocates only the clusters actually written (aligned to 64KB compression units — irrelevant at 64-256MB chunk sizes).
- Sparse files have been stable on NTFS since Windows 2000. No admin privileges needed.

---

## 2. Codebase Orientation

```
src/swap/
├── models.py          # StrEnum states + frozen dataclasses (Operation, ChunkOperation, ...)
├── state.py           # SQLite StateStore; schema in SCHEMA_SQL; transition maps at top
├── chunk_exchange.py  # execute_equal_size_exchange + recover_chunk state machine
├── scheduler.py       # score_operation, choose_next_operation, detect_deadlock,
│                      #   select_equal_size_exchange_candidates
├── orchestrator.py    # run_swap/resume_swap; _scheduled_operation handles deadlock
├── executor.py        # whole-file copy state machine
├── feasibility.py     # capacity check before staging
├── crash.py           # CrashInjector (arm/trip) + NoCrashInjector
└── backends/          # robocopy + local copy backends
tests/
├── conftest.py        # TreeBuilder, CrashInjector fixture, FakeVolume
└── test_chunk_exchange_recovery.py  # THE pattern to copy for crash tests
```

### 2.1 Conventions you must follow

- **Durable state machine pattern:** every risky step is bracketed by SQLite state updates. Look at `recover_chunk()` in `chunk_exchange.py` — a `while` loop over the persisted state, each branch does one durable step then advances the state. Execution and recovery are THE SAME function (resume = re-run).
- **Crash hooks:** `store.crash_hook.trip("point_name")` after every durable milestone. Tests arm these points to simulate crashes (`CrashInjector.arm("point")` raises at `trip`). Every new state transition needs a trip point.
- **State transition maps:** `state.py` has `CHUNK_TRANSITIONS` / `OPERATION_TRANSITIONS` dicts. `store.update_chunk_state` validates transitions against these. New states MUST be added to the maps.
- **fsync discipline:** `_sync_file()` and `_sync_parent()` in `chunk_exchange.py` — reuse them. Sync data before recording a state that claims durability.
- **Frozen dataclasses, StrEnum, `from __future__ import annotations`** — match existing style.
- **Source is never destroyed until destination data is durable + recorded.** This is the core safety invariant of the whole codebase.

### 2.2 Relevant existing schema (state.py SCHEMA_SQL)

```sql
chunk_operations (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id),
    chunk_index INTEGER NOT NULL,
    state TEXT NOT NULL,
    offset INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    staging_path TEXT, file_a_path TEXT, file_b_path TEXT,
    backup_a_path TEXT, backup_b_path TEXT,
    last_error TEXT, created_at TEXT, updated_at TEXT,
    UNIQUE(operation_id, chunk_index)
)
```

This table can be reused for sparse transfer chunks (one row per chunk: `file_a_path` = source, `file_b_path` = destination, backups unused/NULL). Decide during implementation; a separate `phase` or reusing existing columns are both acceptable, but **do not break the existing equal-size exchange rows**.

---

## 3. The Feature

### 3.1 New module: `src/swap/winfs.py` (Win32 sparse-file utilities)

All via `ctypes` against `kernel32.dll`. No external dependencies.

```python
FSCTL_SET_SPARSE = 0x000900C4
FILE_SUPPORTS_SPARSE_FILES = 0x00000040

def volume_supports_sparse(path: Path) -> bool:
    # GetVolumeInformationW(root, ..., &flags, ...) ; check FILE_SUPPORTS_SPARSE_FILES
    # root = path anchor e.g. "C:\\"
    # On non-Windows (tests on CI?), return False.

def create_sparse_file(path: Path, size: int) -> None:
    # 1. CreateFileW (or open(path, "wb") then get msvcrt handle via msvcrt.get_osfhandle)
    # 2. DeviceIoControl(handle, FSCTL_SET_SPARSE, NULL, 0, NULL, 0, &returned, NULL)
    # 3. Seek to size, SetEndOfFile  (python: f.truncate(size) after marking sparse)
    # 4. fsync
    # Python-level approach that works:
    #   with open(path, "wb") as f:
    #       handle = msvcrt.get_osfhandle(f.fileno())
    #       DeviceIoControl(handle, FSCTL_SET_SPARSE, ...)
    #       f.truncate(size)
    #       f.flush(); os.fsync(f.fileno())

def truncate_file(path: Path, new_size: int) -> None:
    # with open(path, "r+b") as f: f.truncate(new_size); os.fsync(f.fileno())
    # (os-level truncate works fine on Windows; no ctypes needed)
```

Notes:
- `truncate()` from Python calls `SetEndOfFile` under the hood — no ctypes needed for truncation.
- Marking sparse DOES need `DeviceIoControl` via ctypes. Signature:
  ```python
  kernel32.DeviceIoControl(
      handle,                 # HANDLE from msvcrt.get_osfhandle
      FSCTL_SET_SPARSE,       # dwIoControlCode
      None, 0,                # lpInBuffer, nInBufferSize (FILE_SET_SPARSE_BUFFER optional; omitted = set sparse)
      None, 0,                # lpOutBuffer, nOutBufferSize
      ctypes.byref(returned), # lpBytesReturned (DWORD)
      None,                   # lpOverlapped
  )
  ```
- Guard everything with `sys.platform == "win32"`; expose `volume_supports_sparse` returning False elsewhere so tests can run anywhere and the fallback path activates.
- **Important verification during development:** after `create_sparse_file(path, 1GB)`, check allocated size ≈ 0 via `GetCompressedFileSizeW` (also expose as `allocated_size(path) -> int`). Use this in tests and optionally in the runtime check.

### 3.2 New states in `models.py`

Add to `ChunkState` (reusing the existing enum keeps the schema/table unchanged):

```python
class ChunkState(StrEnum):
    # ... existing values ...
    # sparse transfer states:
    SPARSE_DEST_WRITTEN = "sparse_dest_written"     # chunk written + synced at offset in sparse dest
    SPARSE_SRC_TRUNCATED = "sparse_src_truncated"   # source truncated to offset + synced
```

A sparse-transfer chunk lifecycle: `PLANNED → SPARSE_DEST_WRITTEN → SPARSE_SRC_TRUNCATED → DONE`.

Add to `CHUNK_TRANSITIONS` in `state.py`:

```python
ChunkState.PLANNED: ... | {ChunkState.SPARSE_DEST_WRITTEN},      # extend existing frozenset
ChunkState.SPARSE_DEST_WRITTEN: frozenset({ChunkState.SPARSE_SRC_TRUNCATED, ChunkState.FAILED}),
ChunkState.SPARSE_SRC_TRUNCATED: frozenset({ChunkState.DONE, ChunkState.FAILED}),
```

Operation-level: reuse existing `OperationState` lifecycle (`PLANNED → COPY_STARTED → ... → COMPLETED`) exactly like `_complete_exchange_operation` does in `chunk_exchange.py`. A new operation-level flag or type is NOT required — sparse transfers run on existing `FILE_COPY_OP` operations chosen at deadlock time. But you need a way for **resume** to know an in-flight FILE_COPY_OP is a sparse transfer, not an equal-size exchange. Simplest discriminator: chunk rows where `backup_a_path IS NULL` → sparse transfer; rows with backups → equal-size exchange. Make this explicit in a helper, e.g. `is_sparse_transfer(chunks) -> bool`.

### 3.3 New function in `chunk_exchange.py`: `execute_sparse_transfer`

```python
def execute_sparse_transfer(
    store: StateStore,
    operation: Operation,        # a FILE_COPY_OP: src_absolute_path -> dst_absolute_path
    *,
    chunk_size: int,
) -> Operation:
```

Behavior (mirror the `recover_chunk` while-loop pattern exactly):

```
SETUP (idempotent):
  total_size = persisted operation.size_bytes (fall back to src stat ONLY on first run;
               after truncations begin, src size is meaningless — size_bytes must be set)
  if no chunk rows exist for this operation:
      create destination sparse file (winfs.create_sparse_file(dst, total_size))
      crash_hook.trip("sparse_dest_created")
      create chunk rows: chunk_index 0..N-1, offset = i*chunk_size,
        size = min(chunk_size, total - offset)
        file_a_path = src, file_b_path = dst, backups = NULL
  else:
      if dst missing → recreate sparse file (we crashed between create and first chunk row?
        actually create chunk rows in the same transaction as a session-level event AFTER
        the sparse file is created+synced, so on resume: dst exists OR no rows exist)
  advance operation PLANNED → COPY_STARTED (reuse _mark_exchange_started)

PER CHUNK — process in DESCENDING chunk_index order (tail-first!):
  while chunk.state is not DONE:
    if PLANNED:
        data = read src[offset : offset+size]        # src still has this range
        write dst at offset; flush; fsync dst
        crash_hook.trip("sparse_chunk_dest_written")
        → SPARSE_DEST_WRITTEN
    if SPARSE_DEST_WRITTEN:
        # verify before destroying source data:
        assert dst[offset:offset+size] == src[offset:offset+size]  (re-read both)
        truncate src to offset (winfs.truncate_file); fsync src
        crash_hook.trip("sparse_chunk_src_truncated")
        → SPARSE_SRC_TRUNCATED
    if SPARSE_SRC_TRUNCATED:
        → DONE
        crash_hook.trip("sparse_chunk_done")

FINALIZE:
  verify dst.stat().st_size == total_size
  delete src (now 0 bytes); fsync parent
  advance operation through COPY_FINISHED → DEST_SYNCED → DEST_VERIFIED → COMPLETED
  (reuse the _complete_exchange_operation walk; add SOURCE_DELETED states if you
   follow executor.py's fuller walk — match whichever pattern is simpler and legal
   per OPERATION_TRANSITIONS)
```

Crash-recovery table (this is what the implementation must guarantee):

| Persisted chunk state | On-disk reality | Recovery action |
|---|---|---|
| `PLANNED` | src has the range; dst region maybe partially written | Re-read src, rewrite dst (idempotent) |
| `SPARSE_DEST_WRITTEN` | dst durable; src may or may not be truncated yet | Verify dst (src range may be gone if truncate happened pre-crash — handle src-shorter-than-offset+size as "already truncated", skip verify-vs-src, go truncate (idempotent: truncate to offset when len(src) > offset)) |
| `SPARSE_SRC_TRUNCATED` | both durable | Mark DONE |

⚠️ Subtlety in `SPARSE_DEST_WRITTEN` recovery: the verify-against-source comparison is only possible while the source still has the range. If we crashed AFTER truncation but BEFORE recording `SPARSE_SRC_TRUNCATED`, the source is already short. So: `if src_size > offset: compare; truncate` else `just truncate(idempotent no-op)`. The dst write was fsynced before the state was recorded, so dst is trustworthy.

⚠️ The dst sparse file is inside the staged/final tree. The existing planner already computes `dst_absolute_path` for every FILE_COPY_OP — reuse it. Ensure parent dirs exist (structure ops run before file ops, so they should).

### 3.4 The circular buffer driver: `execute_sparse_swap_round` (orchestrator or new module)

When deadlock is detected and no equal-size pair exists, instead of failing:

```python
def resolve_deadlock_with_sparse_transfers(store, session, pending_copies, pressures, *, chunk_size) -> None:
    # 1. Both drives must support sparse files, else raise the existing error.
    # 2. Group pending FILE_COPY_OPs by destination drive anchor.
    #    side_to_1 = ops whose dst is drive1, sorted descending by size_bytes
    #    side_to_2 = ops whose dst is drive2, sorted descending by size_bytes
    # 3. Both drives must have >= chunk_size free (use pressures / get_volume_space).
    # 4. Alternate single chunks:
    #    while either list has an unfinished op:
    #        advance ONE chunk of current op going to drive1   (frees chunk_size on drive2)
    #        advance ONE chunk of current op going to drive2   (frees chunk_size on drive1)
    #    when an op's chunks are all DONE, finalize it and move to the next op in its list.
    # 5. If one direction runs out of files first, just keep advancing the other —
    #    by then the other drive is gaining free space every chunk anyway.
```

To "advance one chunk" you need a step-wise variant of `execute_sparse_transfer` — factor the per-chunk while-loop into `advance_sparse_chunk(store, chunk) -> ChunkOperation` and have both the single-file function and the alternating driver call it. **Setup (sparse dest creation + chunk row creation) happens once per operation when it first becomes current.**

Resume semantics: on restart, the orchestrator's existing `_resume_existing_exchange` path triggers when an operation has chunk rows. Extend it: if `is_sparse_transfer(chunks)` → re-enter the alternating driver (it must rediscover both lists from persisted operation/chunk state and continue alternating — deterministic order: sort by (destination anchor, size desc, operation id)).

### 3.5 Orchestrator wiring (`orchestrator.py`)

In `_scheduled_operation`, the deadlock branch currently:

```python
except DeadlockDetected as error:
    pairs = select_equal_size_exchange_candidates(eligible)
    if not pairs:
        ... FAIL ...
    execute_equal_size_exchange(...)
```

New logic:

```python
except DeadlockDetected as error:
    pairs = select_equal_size_exchange_candidates(eligible)
    if pairs:
        execute_equal_size_exchange(store, *pairs[0], work_dir=work_dir, chunk_size=session.chunk_size)
        return _current_operation(store, session.id)
    if sparse_supported_on_both_drives(session) and both_drives_have_chunk_size_free(...):
        resolve_deadlock_with_sparse_transfers(store, session, eligible, pressures, chunk_size=session.chunk_size)
        return _current_operation(store, session.id)
    store.update_session_state(session.id, SessionState.FAILED)
    raise RuntimeError(...)
```

Keep equal-size exchange as first preference (it's in-place, no free space consumed at all; sparse transfer transiently uses chunk_size per drive).

In `_resume_existing_exchange`: branch on `is_sparse_transfer(chunks)` before requiring a symmetric partner (sparse transfers have no partner — current code would fail the session).

`rollback_current` in orchestrator + `rollback_current_exchange` in chunk_exchange: for sparse transfers, "rollback" of the current chunk means: if chunk is `PLANNED` or `SPARSE_DEST_WRITTEN` and src still has the range, nothing was destroyed — mark chunk FAILED, op FAILED. If src was already truncated for this chunk (`SPARSE_SRC_TRUNCATED`), data lives in dst — do NOT delete dst. Partial-file rollback across many completed chunks is NOT supported (same philosophy as today: rollback-current repairs the in-flight step only). Document this in the docstring.

### 3.6 Feasibility / scheduler touches

- `feasibility.py`: no change required — capacity check already validates total fit.
- `scheduler.py`: add `select_sparse_transfer_candidates(operations) -> list[Operation]` (pending FILE_COPY_OPs with size+paths set, grouped/sorted as in 3.4) OR put that grouping in the new driver. Also `detect_deadlock` stays as-is — sparse resolution happens after detection, like equal-size exchange does today.

---

## 4. Edge Cases (must handle / must test)

1. **Last chunk is partial** (`total % chunk_size != 0`): first chunk processed (highest index) is the short one. Offsets/sizes come from chunk rows, not arithmetic at runtime.
2. **File smaller than chunk_size:** degenerates to 1 chunk. Must work.
3. **Crash between sparse-file creation and chunk-row creation:** make chunk-row creation the durable marker; on resume, if no rows → recreate sparse file from scratch (recreating an empty sparse file is free and idempotent — just don't recreate it once rows exist, since chunks may already be written into it).
4. **Unequal totals per direction** (e.g. 20GB → drive1 but only 5GB → drive2): alternation runs out on one side; the loop continues single-sided. Free space grows on the emptier drive every cycle, so it cannot deadlock.
5. **Drive with < chunk_size free at deadlock:** refuse sparse path with a clear error suggesting `--chunk-size` reduction. (Session chunk_size default is 64MB per `RunOptions`.)
6. **Non-NTFS volume (exFAT etc.):** `volume_supports_sparse` false → fall through to the existing failure error (do NOT implement the .bin+merge fallback in this iteration; just keep the current behavior and say so in the error message).
7. **dst file already exists at sparse-create time** (crash case 3, or leftover): if chunk rows exist → reuse; else delete and recreate.
8. **Verify mode:** sparse path does its own per-chunk verify (byte compare before truncation). Final whole-file verify against original hash is impossible (source is destroyed incrementally) — per-chunk verify is the verification story; document it.
9. **The work-dir DB lives on one of the drives:** SQLite writes during the loop consume a little space; the existing `safety_margin + chunk_reserve` accounting in `DrivePressure` covers this — reuse `chunk_reserve_bytes` semantics when checking "has chunk_size free".

---

## 5. Test Plan (`tests/test_sparse_transfer.py` + extensions)

Use small sizes (bytes, not GB) and `chunk_size=4` like `test_chunk_exchange_recovery.py`. Use `open_state_store(path, crash_hook=CrashInjector())` from `swap.crash`.

1. **Happy path single transfer:** 10-byte file, chunk_size=4 (chunks: 4,4,2). Run `execute_sparse_transfer`, assert dst bytes == original, src deleted, all chunks DONE.
2. **Crash injection at every hook** (`sparse_dest_created`, `sparse_chunk_dest_written`, `sparse_chunk_src_truncated`, `sparse_chunk_done`): arm → run → catch crash → reopen store → re-run → assert correct final content. Mirror the loop structure of `test_chunk_exchange_recovery.py::test_chunk_exchange_recovers_after_interruption_at_each_modeled_state`.
3. **Crash mid-truncation simulation:** manually truncate src to offset, set chunk state `SPARSE_DEST_WRITTEN`, run recovery → must complete without verify error (the src-shorter-than-range branch).
4. **Alternating driver:** build the A(10)+B(10) vs C(20) scenario with tiny files and a fake `DrivePressureProvider` reporting tiny free space so deadlock fires; assert full swap completes and final trees match.
5. **Orchestrator integration:** end-to-end `run_swap` with `volume_mode="simulate-distinct"` and a pressure provider forcing deadlock with NO equal-size pair; assert session COMPLETED (this currently fails — it's the headline test for the feature).
6. **Resume of alternating driver:** crash mid-round (arm a chunk hook), `resume_swap`, assert completion.
7. **Sparse utilities unit tests:** `create_sparse_file` then `allocated_size(path)` is ~0 while `stat().st_size` is full; skip with `pytest.mark.skipif(not volume_supports_sparse(tmp_path))`.
8. **Non-sparse volume:** monkeypatch `volume_supports_sparse` → False; assert the deadlock error message still raised (graceful refusal).
9. **Regression:** full existing suite `python -m pytest -q` (47 tests currently pass — must stay green).

---

## 6. Implementation Order

1. `winfs.py` + its unit tests (foundation, no integration risk).
2. `models.py` + `state.py` states/transitions (mechanical).
3. `advance_sparse_chunk` + `execute_sparse_transfer` in `chunk_exchange.py` + crash tests (the core).
4. Alternating driver + scheduler candidate helper + tests.
5. Orchestrator wiring (deadlock branch, resume branch, rollback) + end-to-end tests.
6. Full-suite regression run.

## 7. Out of Scope (do not build)

- `.bin` + merge fallback for non-NTFS volumes (just refuse clearly).
- Hole-punching of source files (`FSCTL_SET_ZERO_DATA`) — truncation-only design.
- Full rollback of a partially sparse-transferred file.
- Cross-file virtual concatenation / many-to-one chunk exchange.
- Changes to the equal-size exchange path (it stays preferred and untouched).
