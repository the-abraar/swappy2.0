# Final Plan: Transfer/DB Performance Improvements

## Header: Key Changes Proposed

This plan is now based on the updated `proposed_changes.md`, reconciled with `agent_files/perf_in_db.md` and the current codebase.

- Switch the SQLite connection default to `PRAGMA synchronous=NORMAL` for hot-path writes.
- Preserve correctness by forcing durable commits only for the rare state transitions that must never be lost:
  - marking an entry `sparse`
  - `advance_stage()`
  - `begin_rollback()`
  - `finish_rollback()`
- Batch whole-file `done` marks and directory/symlink `done` marks.
- Remove hot-loop full-table reads from `transfer.copy_files()`.
- Remove the O(n) candidate rebuild and O(n) removal/lookups from each dispatch by keeping pending files in two sorted per-side queues.
- Reload pending state only after sparse rounds, not on every coordinator tick.
- Reduce the `wait()` timeout from `0.1` to `0.01`.
- Optionally stop forcing a progress DB flush on every single file completion.

## Priority And Scope

The target is maximum performance in the main forward copy loop. Slow resume startup is acceptable. Resume and rollback correctness are not negotiable.

That means:

- optimize `transfer.copy_files()` aggressively,
- accept extra DB work during resume/bootstrap if that keeps the steady-state loop fast,
- keep sparse and rollback durability rules explicit, even if they cost a little more outside the hot path.

## Core Conclusions

`proposed_changes.md` is directionally right, but the plan must incorporate three important constraints:

1. The `pending -> sparse` transition must remain durably ordered with respect to the first source truncation.
2. A cached pending list must be refreshed after each sparse round, or a mid-sparse file can be re-leased to the whole-file path.
3. Batching `done` writes and removing per-tick DB scans is not enough by itself; the hot loop also needs the O(n)-per-dispatch scheduling work removed.

## Final Implementation Plan

### 1. Change the database durability model in `src/swap/db.py`

Set the connection default to:

- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`

Then add a small internal helper for rare durable commits. The cleanest shape is:

- a helper that temporarily forces `synchronous=FULL` around a specific transaction, then restores `NORMAL`
- or a dedicated helper used only by the few correctness-critical methods

Use durable commits for:

- `advance_stage()`
- `begin_rollback()`
- `finish_rollback()`
- the sparse-entry mark used before the first truncation in `src/swap/sparse.py`

Do not use durable commits for:

- whole-file `done` batches
- directory/symlink `done` batches
- progress summary / worker / history writes

Reason:

- the hot path benefits from `NORMAL`
- the sparse mark and stage/rollback transitions are the only DB writes whose loss can invalidate later filesystem reasoning

### 2. Add explicit batch APIs in `src/swap/db.py`

Add:

- `set_entries_done(entry_ids: list[int])`

Optional generalization if it stays simple:

- `set_entries_state(entry_ids: list[int], state: str, *, durable: bool = False)`

Keep single-entry `set_entry_state()` available for existing call sites, but add an explicit durable path for sparse state marking. Two acceptable designs:

- `set_entry_state(entry_id, state, *, durable=False)`
- `mark_entry_sparse(entry_id)` as a dedicated durable helper

Use the same explicit transaction pattern already used in `replace_entries()` and `begin_rollback()`.

### 3. Update `src/swap/sparse.py` to use the durable sparse mark

At the current `pending -> sparse` transition, use the durable DB helper before the first destination/sparse-file work that can lead to source truncation.

This is the most important correctness guard in the whole plan.

Why it matters:

- if the filesystem chunk write and truncate survive a crash but the `sparse` DB state does not, the file can later be misclassified as ordinary `pending`
- that can cause either whole-file overwrite/truncation of the sparse destination or rollback deletion of its tail data

Add a short code comment explaining this invariant at the call site.

### 4. Refactor `src/swap/transfer.py` for hot-loop performance

This is the main optimization pass.

#### 4a. Replace per-tick DB reads with in-memory pending queues

Before the main loop:

- read pending whole-file entries once from the DB
- split them into two per-side lists
- sort each list by `(size, id)`

During whole-file dispatch:

- expose only the current head of each side as scheduler candidates
- pass at most two candidates to `scheduler.choose_next_copy()`
- pop the chosen head on lease

This preserves scheduler behavior because its priority is still based on:

- whether the copy frees the constrained side
- file size
- entry id

With per-side lists already sorted by `(size, id)`, only each side's head can ever win.

This removes:

- per-dispatch full `CopyCandidate` rebuilds
- `_entry_by_id()` scanning
- `pending.remove(entry)` scanning

Result: dispatch cost becomes O(1) instead of O(n).

#### 4b. Batch whole-file `done` marks

Add:

- `done_batch: list[int]`

On whole-file completion:

- append `entry.id` to `done_batch`
- do not immediately call `db.set_entry_state(..., "done")`

Flush `done_batch` using `db.set_entries_done(...)`.

#### 4c. Enforce the right flush ordering

Before any DB read of live `entries` state inside `copy_files()`, flush `done_batch`.

Specifically flush before:

- the termination check that consults whether anything remains
- entering the sparse branch
- reloading pending after a sparse round
- the worker-failure exception path

Reason:

- otherwise already-copied files still look `pending`
- sparse selection can momentarily pick them again
- recovery would usually save correctness, but it creates duplicate work and confusing progress behavior

#### 4d. Refresh caches only after sparse rounds

After each `sparse.advance_round(...)`:

1. flush `done_batch`
2. reload pending whole-file entries from the DB into the two sorted per-side queues
3. continue scheduling from the refreshed view

Do not re-query pending entries on normal wait/schedule ticks. The goal is to keep the fast loop DB-free.

This is the required fix for the stale-cache bug in the original sketch.

#### 4e. Reduce wait timeout

Change:

- `wait(..., timeout=0.1, return_when=FIRST_COMPLETED)`

to:

- `wait(..., timeout=0.01, return_when=FIRST_COMPLETED)`

Once the DB/query overhead is removed, this becomes cheap and improves dispatch latency for small fast copies.

#### 4f. Optional progress flush improvement

Drop the forced progress DB flush on every per-file completion if the implementation stays clean.

Preferred behavior:

- file completion updates progress in memory immediately
- DB persistence follows normal flush cadence unless a correctness boundary forces a flush

This is safe because progress tables are display-only.

### 5. Batch directory and symlink `done` writes

Update:

- `create_directories()`
- `create_symlinks()`

to accumulate completed entry ids and commit them in one batch per pass instead of one SQLite autocommit per entry.

This is not the main win, but it is simple and consistent with the new DB API.

### 6. Deprioritize resume-only and cold-path optimizations

Given the stated priority, do not spend the first pass on resume startup speed.

That means:

- no need to optimize `progress_bootstrap()`
- no need to optimize rollback drain queries unless needed for correctness
- no need to add a general-purpose `entries` index purely for resume/status speed in the first pass

An index can still be added if it materially helps sparse-round reloads or rollback checks, but it is not a required part of the hot-path plan.

## Files The Coding Agent Should Expect To Touch

- `src/swap/db.py`
- `src/swap/sparse.py`
- `src/swap/transfer.py`
- `tests/test_transfer.py`
- `tests/test_sparse.py`
- `tests/test_rollback.py`

Likely additional updates:

- `README.md`
- `CLAUDE.md`

Only if they currently describe the DB durability model and would become inaccurate.

## Test Plan

The test work should focus on correctness under the new durability split and the new cached scheduler structure.

Add or update tests for:

1. Batched whole-file completion:
   - multiple copied files complete
   - all end as `done`
   - transfer still terminates cleanly after a final batch flush

2. Stale-cache regression:
   - low-space scenario that forces sparse mode
   - after sparse rounds free space, a file already marked `sparse` is never re-leased to the whole-file path

3. Deferred whole-file `done` crash recovery:
   - copied destination complete, source already deleted, DB state still `pending`
   - rerun resumes through `_recover_completed_copy()` and marks the file `done`

4. Sparse-mark durability behavior:
   - regression coverage around the `pending -> sparse` transition
   - verify rollback and resume continue to treat mid-sparse files as sparse, not ordinary pending files

5. Rollback correctness with the new durability split:
   - rollback still works after partial forward work
   - rollback still refuses unsafe states as before

6. Directory/symlink batch updates remain idempotent.

Verification command:

- `python pytest.py -q`

## Recommended Execution Order

1. Update `src/swap/db.py` to `synchronous=NORMAL`, add batch APIs, and add the explicit durable-commit path.
2. Switch `src/swap/sparse.py` to the durable sparse mark.
3. Refactor `src/swap/transfer.py` to use two sorted per-side pending queues and O(1) dispatch selection.
4. Add `done_batch` with the required flush-ordering rules.
5. Change the wait timeout to `0.01`.
6. Batch directory and symlink `done` writes.
7. Add regression tests for sparse-cache invalidation, deferred `done` recovery, and rollback correctness.
8. Update docs only if they would otherwise be wrong.

## Expected Outcome

After these changes, the coordinator should stop dominating throughput.

Expected effect:

- hot-loop DB traffic drops from per-file/per-tick writes and reads to occasional batched writes plus sparse-boundary reloads
- dispatch work drops from O(n) per file to O(1) per file
- steady-state throughput becomes limited mainly by real file I/O:
  - copy
  - destination fsync
  - metadata copy
  - source delete

For many small files, destination fsync remains the hard lower bound, so throughput will not be unbounded. But this should still move the system from the current coordinator-bound behavior to a disk-bound regime.
