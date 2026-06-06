# Performance Plan: DB / Coordinator Bottlenecks

## Root cause

The "few KB/s" throughput is coordinator overhead, not copy-backend speed.
Three independent bottlenecks in `src/swap/transfer.py` + `src/swap/db.py`:

| # | Bottleneck | Location |
|---|---|---|
| A | Full `db.entries()` scan on every coordinator loop tick | `transfer.py:90-95` |
| B | One SQLite fsync per completed file (`synchronous=FULL` + autocommit) | `db.py:246`, `transfer.py:152` |
| C | 100 ms `wait()` timeout between dispatch cycles | `transfer.py:136` |

Investigated switching to LMDB or Redis — neither helps. The bottleneck is the
OS fsync cost itself, not SQLite. LMDB safe mode has the same per-commit fsync;
unsafe mode has no crash-safety. Redis AOF-always is slower (2–5k writes/sec).
SQLite fixed correctly is the right call.

---

## Change 1 — `synchronous=NORMAL` (`db.py:248`)

```python
# Before
self._conn.execute("PRAGMA synchronous=FULL")
# After
self._conn.execute("PRAGMA synchronous=NORMAL")
```

**Why safe**: Data safety is enforced by `winfs.fsync_file(dst)` in `copier.py`
and chunk fsyncs in `sparse.py` — not by SQLite. SQLite only records completion
state. With WAL + NORMAL, committed data is never corrupted on power failure;
only in-flight transactions are rolled back. On resume, `_recover_completed_copy`
catches any "pending" entries whose physical copy already finished (src gone +
dst complete → mark done). No data loss.

Delivers ~2–5× reduction in per-transaction fsync overhead alone.

---

## Change 2 — Batch `set_entry_state("done")` (`db.py` + `transfer.py`)

**Problem**: `set_entry_state(entry_id, "done")` at `transfer.py:152` is an
autocommit transaction — one fsync per file. At 1 ms/fsync this caps
at ~1 000 file-marks/sec regardless of copy speed.

**New method** in `db.py` (alongside `set_entry_state`):

```python
def set_entries_done(self, entry_ids: list[int]) -> None:
    """Mark a batch of entries done in one transaction (one fsync)."""
    if not entry_ids:
        return
    self._conn.execute("BEGIN")
    try:
        self._conn.executemany(
            "UPDATE entries SET state = 'done' WHERE id = ?",
            [(eid,) for eid in entry_ids],
        )
        self._conn.execute("COMMIT")
    except BaseException:
        self._conn.execute("ROLLBACK")
        raise
```

**Changes in `copy_files`** (`transfer.py`):
- Add `done_batch: list[int] = []` before the while loop.
- Replace `db.set_entry_state(entry.id, "done")` (line 152) with
  `done_batch.append(entry.id)`.
- In `_maybe_flush` (or inline before `progress.flush()`), call
  `db.set_entries_done(done_batch); done_batch.clear()`.
- At the `return` (termination check), do a final flush of `done_batch` first.

**Crash safety**: Entries not yet batch-committed remain "pending". On resume,
`_recover_completed_copy` handles them (src gone + dst complete → mark done).
Worst case: re-verifies a few already-finished files. No data loss.

---

## Change 3 — Cache pending entries in memory (`transfer.py`)

**Problem**: Lines 90–95 call `db.entries(kind="file", state="pending")` and
`db.entries(kind="file", state="sparse")` on every outer loop tick, including
pure 100 ms timeout iterations. Since the coordinator is the only thread that
transitions entry states, the DB query is always redundant between completions.

**Fix**: Load pending entries once before the while loop. The inner scheduling
loop already does `pending.remove(entry)` on lease — just promote that list
outside the outer while loop. For sparse entries, query only when entering the
sparse fallback branch (not every tick).

```python
# Before the while loop:
pending: list[Entry] = list(db.entries(kind="file", state="pending"))

while True:
    # No db.entries() calls here on the fast path.

    if not pending and not futures:
        in_flight_sparse = db.entries(kind="file", state="sparse")
        if not in_flight_sparse:
            db.set_entries_done(done_batch)   # final flush
            progress.flush(force=True)
            return

    scheduled = False
    while available_workers and pending:
        ...
        pending.remove(entry)   # already present; just stays outside outer loop
        ...

    if futures:
        done, _ = wait(list(futures), timeout=0.01, ...)
        for future in done:
            ...
            done_batch.append(entry.id)        # deferred write
            last_flush = _maybe_flush(last_flush, force=True)
        continue

    in_flight_sparse = db.entries(kind="file", state="sparse")
    if pending or in_flight_sparse:
        sparse.advance_round(...)
        ...
```

---

## Change 4 — Reduce coordinator poll timeout (`transfer.py:136`)

```python
# Before
done, _ = wait(list(futures), timeout=0.1, return_when=FIRST_COMPLETED)
# After
done, _ = wait(list(futures), timeout=0.01, return_when=FIRST_COMPLETED)
```

10 ms is invisible to the 200 ms flush interval and 4 Hz Rich render but allows
10× faster dispatch for fast-completing small files.

---

## Files to edit

| File | Lines | Change |
|---|---|---|
| `src/swap/db.py` | 248 | `synchronous=FULL` → `synchronous=NORMAL` |
| `src/swap/db.py` | after `set_entry_state` | add `set_entries_done(ids)` |
| `src/swap/transfer.py` | 90–95 | replace per-tick `db.entries()` with in-memory list |
| `src/swap/transfer.py` | 136 | `timeout=0.1` → `timeout=0.01` |
| `src/swap/transfer.py` | 152 | accumulate `done_batch` instead of immediate write |
| `src/swap/transfer.py` | `_maybe_flush` | flush `done_batch` here |

---

## Verification

1. `python pytest.py -q` — all tests pass.
2. Create two dirs with 1000 × 1 KB files on distinct volumes; run swap; confirm
   throughput is I/O-bound (should be >> a few KB/s).
3. Kill mid-transfer, `swap resume` — all files complete correctly.
4. `swap rollback` after partial forward transfer — verifies deferred-write
   crash model.
