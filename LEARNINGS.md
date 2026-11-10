# LEARNINGS

Swappy swaps two folder trees that sit on two different drives when neither drive has room for the
other folder. Nabil designed and first built it ([NirobNabil/swappy](https://github.com/NirobNabil/swappy),
June 2026). This fork adds a Windows CI smoke test, the GUI/TUI fixes, hash-verified copies and real
pytest. Interactive replay: [`demo/index.html`](demo/index.html).

## The algorithm, using the numbers from one real run

Source: [Windows smoke run 40988697805](https://github.com/the-abraar/swappy2.0/actions/runs/40988697805),
2026-10-30, commit `9297fd7`. It used two 133.1 MB NTFS volumes (VHDX) on `windows-latest`, `--chunk-size 4MB --safety-margin 8MB`,
8 workers and the `winapi` backend. The data is in [`demo/data/swap_run.json`](demo/data/swap_run.json), made by
[`demo/export_run.py`](demo/export_run.py) from the run's `swap.db` artifact (which expires 2027-01-28).

| | X: | Y: |
|---|---|---|
| folder | `A`, 63.4 MB (one 52.4 MB `big.bin` + 7 small files) | `B`, 58.2 MB (one 47.2 MB `big.bin` + 7 small files) |
| free before the swap | 54.9 MB | 60.1 MB |

1. **Preflight.** Each drive must end with `free + outgoing − incoming ≥ margin`. For X: 54.9 + 63.4 − 58.2 = 60.1 MB.
   For Y: 60.1 + 58.2 − 63.4 = 54.9 MB. Both are ≥ 8 MiB, so the swap can run. Neither drive could hold the other
   folder as a temporary copy.
2. **Stage.** Each root is renamed aside on its own drive (`X:\A → X:\.swap_staged_a`), and empty final roots are
   created. The renames are instant, and the stage is recorded in `swap.db`.
3. **Scan, then whole-file copies.** The scan writes 24 entries (8 dirs, 16 files) to SQLite. The pure scheduler
   (`scheduler.py`) picks any copy that leaves `margin + one chunk` (12.6 MB) free on the destination. All 14 small
   files went through 8 workers in about 30 ms (10–39 ms after transfer start).
4. **Deadlock, then sparse rounds.** Neither `big.bin` fits: either one would leave 7.7 MB free, which is below
   12.6 MB. `sparse.py` then creates each destination as a full-size sparse file (0 bytes allocated). It copies the
   source's last 4 MiB into the destination at the same offset, fsyncs, truncates the source, and does the same
   in the other direction. Each round costs each drive at most one chunk of space. It took 13 rounds (12.5 chunks
   for the 50 MiB file, 11.25 for the 45 MiB one) and 0.30 s.
5. **Cleanup.** The empty staged trees are removed and the stage becomes `done`. The whole swap took 0.342 s.

Recovery needs no chunk table. **The length of the source file is the progress marker**, because a chunk is
fsynced at the destination before the source is truncated. If a sparse transfer crashes, resume reads the
length and carries on from the next tail chunk. The other stages are idempotent, so `resume` just runs the
pipeline again.

## Speeds (and how to read them)

| measure | value |
|---|---|
| sparse, per-file average (2 files) | 172.5 MB/s |
| whole-file, per-file average (14 files) | 246.1 MB/s |
| whole-file, all 8 workers together (22.0 MB in 29.2 ms) | 754.1 MB/s |
| whole swap (121.6 MB in 0.342 s) | 355.7 MB/s |

- The 246.1 average includes the two 8-byte `note.txt` files at about 0 MB/s. Without them it is 287.1.
- These are tiny VHDX volumes on a CI VM, and most of the I/O is served from cache. They show that the
  algorithm works. They are not a benchmark, so expect a real disk to be slower.

## What went wrong and what we learned

- **A size-only check before delete.** Whole-file copies deleted the source after checking only the size.
  `c67466d` now compares a BLAKE2b digest of the fsynced destination with the source before it unlinks
  anything. The run above predates that fix.
- **A fake pytest.** A 288-line `pytest.py` shim in the repo root was picked up by `python -m pytest`.
  It hid that 60 of 77 tests errored under real pytest. Fixed in `34c328a`. The lesson: a local file
  that shares a stdlib or tool name silently shadows it.
- **The GUI at 1024×768.** The CI screenshot ([`demo/img/gui.png`](demo/img/gui.png)) shows two bugs: clam's
  readonly combobox paints our light text on light grey, and the window is taller than a 768 px
  screen minus the taskbar. Fixed on `agent/sw-gui` with a ttk style map, a 4-row scrollable worker table,
  and a window capped to the screen height.
- **A double-counted stat.** `progress_summary.chunk_bytes_moved` reads 199,229,440. That is exactly twice
  the 99,614,720 bytes of the two `big.bin` files: `chunk_progress` adds each chunk, and then `worker_finished`
  adds the whole file again (`progress.py`). It only affects the display, and it is not fixed yet.
- **Legacy console code pages.** The TUI transcript ([`demo/data/tui.txt`](demo/data/tui.txt)) shows the box
  drawing as `?` on the runner's console. The TUI now survives that (it used to crash), but it doesn't render it.
