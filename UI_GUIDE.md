# Swappy UI Guide

Swappy has two friendly front-ends in addition to the CLI: a **desktop GUI** and
a **terminal UI (TUI)**. Both drive the exact same crash-safe engine, and both
are built on the Python **standard library** — nothing extra to install, nothing
to license.

---

## Install

```bash
python -m pip install -e .
```

That's it. The terminal UI works immediately. The desktop GUI uses Tkinter,
which ships with the official Python installers on Windows and macOS; on Linux
install it once:

```bash
sudo apt install python3-tk       # Debian/Ubuntu
sudo dnf install python3-tkinter  # Fedora
```

> One-shot helpers are also provided: `python launcher.py` (GUI),
> `python launcher.py tui` (TUI), or `swappy_ui.bat` on Windows. They create a
> venv, install Swappy, and open the UI for you.

---

## Desktop GUI

```bash
python -m swap gui
```

1. **Pick folders.** Use *Browse…* for **Folder A** and **Folder B** (the two
   roots to exchange — they must be on different drives) and a **Work dir**
   (any empty folder where Swappy keeps its session database).
2. **Settings (optional).** Sensible defaults are filled in. Adjust chunk size,
   safety margin, workers, backend, or chunk verification only if you need to.
3. **Run.** Click **▶ Run swap**. Watch the live progress bar, per-worker table,
   stats (transferred / total, speed, ETA), and the engine log.
4. **Resume / Rollback / Status.** If a run was interrupted, point the Work dir
   at it and click **Resume**. **Rollback** restores the original layout (only
   before the final cleanup). **Status** shows the current stage and counts.
5. **Cancel any time.** Stopping a run is safe — it just interrupts it. Click
   **Resume** later to continue exactly where you left off.

Closing the window does **not** stop a running swap (it runs in the background);
reopen the GUI and Resume to reattach.

---

## Terminal UI

```bash
python -m swap tui                            # interactive menu
python -m swap tui --work-dir D:\.swap_work   # jump straight to a session
```

- **New swap** walks you through Folder A, Folder B, and the work dir, with an
  optional advanced-settings step, then shows a review before starting.
- A **live dashboard** then takes over: overall progress bar, per-worker states,
  recently completed files, throughput, ETA, and the engine log.
- Press **Ctrl-C** to cancel safely (you'll be asked to confirm); re-run and
  choose **Resume** to continue.
- **Open existing session** gives you Resume / Rollback / Status / Watch for a
  work dir you already have.

The TUI is pure standard library, so it works over SSH and on headless boxes.

---

## What the controls mean

| Setting | Default | Notes |
|---|---|---|
| **Chunk size** | `64MB` | Block size for the low-space sparse fallback. Larger = fewer rounds but more headroom needed. `64MB`, `512MB`, `1GB`. |
| **Safety margin** | `64MB` | Minimum free space each drive must keep after the swap. Bump it if you see "not capacity-feasible". |
| **Workers** | `8` | Parallel whole-file copy threads (≥ 1). Higher helps on fast NVMe; watch I/O contention on spinning disks. |
| **Backend** | `winapi` on Windows, else `python` | `winapi` uses `CopyFileExW` with exact kernel progress; `python` is the portable fallback. |
| **Verify chunks** | on | SHA-256 read-back after each sparse chunk. Safer; turn off only if you fully trust the hardware. |
| **Same-disk test mode** | off | (GUI checkbox / TUI advanced) Treats each root as its own volume so you can try a swap on one disk. For testing only. |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| GUI won't start: "needs Tkinter" | Install Tk (see above), or use `python -m swap tui`. |
| "swap roots must live on distinct volumes" | Pick two folders on different drives. (Same-disk test mode exists for experiments.) |
| "not capacity-feasible" | Increase **Safety margin**, or free up space on the tighter drive. |
| "a session already exists" | Use **Resume** for that work dir, or pick a fresh, empty work dir. |
| Power loss / crash mid-run | Just **Resume** — Swappy rebuilds progress from disk and continues. |

---

## How it works under the hood

Both UIs launch `python -m swap …` as a subprocess and poll the session's
`swap.db` read-only for live progress. That's why a swap survives the UI closing
and why cancelling is always safe. The full engine design is documented in
[CONTRIBUTORS.md](CONTRIBUTORS.md) and `CLAUDE.md`.
