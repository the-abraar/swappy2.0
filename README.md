<div align="center">

# Swappy

<p>
  <strong>Crash-safe folder swapping for Windows when free space is tight.</strong>
</p>

<p>
  <a href="#"><img src="https://img.shields.io/badge/platform-Windows%20%C2%B7%20macOS%20%C2%B7%20Linux-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Windows / macOS / Linux"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="#"><img src="https://img.shields.io/badge/UI-CLI%20%C2%B7%20TUI%20%C2%B7%20GUI-00d4ff?style=for-the-badge" alt="CLI / TUI / GUI"></a>
  <a href="https://github.com/the-abraar/swappy2.0/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-16A34A?style=for-the-badge" alt="MIT License"></a>
  <a href="#"><img src="https://img.shields.io/badge/recovery-resumable-16A34A?style=for-the-badge" alt="Resumable"></a>
</p>

<p>
  Swap two large folder trees across different drives, even when neither drive
  has enough free space to temporarily hold the other.
</p>

</div>

---

## ✨ Overview

Swappy exists for one very specific and very annoying problem:

> you need to exchange two large folders across two drives, but normal copy,
> move, or rename workflows break down because there is not enough free space
> to juggle them safely.

Instead of relying on fragile manual steps, Swappy stages both roots, tracks
durable state in SQLite, resumes after interruption, and falls back to sparse
chunk movement when whole-file copies would deadlock.

## 🎯 Why It’s Useful

Many "simple" folder swaps are only simple when one of these is true:

- one drive has lots of extra free space;
- you have a third staging drive ready;
- you can afford to manually recover from an interrupted transfer.

Swappy is for the cases where none of that is true.

<table>
  <tr>
    <td width="50%">
      <h3>🧨 Without Swappy</h3>
      <ul>
        <li>Manual copy juggling</li>
        <li>Partial copies on interruption</li>
        <li>Low-space deadlocks</li>
        <li>Messy cleanup and uncertainty</li>
      </ul>
    </td>
    <td width="50%">
      <h3>✅ With Swappy</h3>
      <ul>
        <li>Single-purpose CLI workflow</li>
        <li>Durable session state in <code>swap.db</code></li>
        <li>Resume and rollback support</li>
        <li>Sparse low-space transfer fallback</li>
      </ul>
    </td>
  </tr>
</table>

## 🚀 Features

<table>
  <tr>
    <td width="33%">
      <strong>📦 Low-Space Aware</strong><br>
      Uses ordinary whole-file copies when possible, then switches to sparse
      chunk rounds when the transfer would otherwise stall.
    </td>
    <td width="33%">
      <strong>🛡️ Crash-Safe by Design</strong><br>
      Progress is persisted, stages are idempotent, and interrupted runs can be
      resumed without rebuilding the transfer plan from scratch.
    </td>
    <td width="33%">
      <strong>🔁 Purpose-Built for Swaps</strong><br>
      This is not a generic file sync tool. It is designed specifically for
      exchanging two folder trees across distinct volumes.
    </td>
  </tr>
  <tr>
    <td width="33%">
      <strong>↩️ Rollback Support</strong><br>
      Partial swaps can be reversed before final cleanup completes.
    </td>
    <td width="33%">
      <strong>📊 Live Progress</strong><br>
      Shows multi-worker progress with Rich when running in a terminal.
    </td>
    <td width="33%">
      <strong>🔍 Clear Safety Checks</strong><br>
      Rejects impossible layouts early: same-volume roots, nested roots, and
      unsupported reparse-point cases.
    </td>
  </tr>
</table>

## ⚡ Quick Start

Install from PyPI:

```bash
python -m pip install swappy2
```

Or, from a clone, in editable mode: `python -m pip install -e .`

Start a new swap:

```bash
python -m swap run --folder-a D:\FolderA --folder-b E:\FolderB --work-dir D:\.swap_work
```

Resume an interrupted run:

```bash
python -m swap resume --work-dir D:\.swap_work
```

Check status:

```bash
python -m swap status --work-dir D:\.swap_work
```

Roll back a partial swap:

```bash
python -m swap rollback --work-dir D:\.swap_work
```

> The product name is **Swappy** and the PyPI distribution is `swappy2`, but the
> Python package and CLI module are still named `swap`.

## 🖥️ Three ways to use it

Swappy ships with the same engine behind three front-ends. Pick whichever you
like — they all run the identical, crash-safe pipeline and can hand off to each
other (start in the GUI, resume from the CLI, watch from the TUI).

| | Launch | Best for |
|---|---|---|
| **Desktop GUI** | `python -m swap gui` | Point-and-click: folder pickers, a live progress bar, per-worker table, and a log pane. |
| **Terminal UI** | `python -m swap tui` | A friendly, full-screen dashboard right in your terminal — guided prompts plus live stats. |
| **CLI** | `python -m swap run …` | Scripting, automation, and remote/headless machines. |

Both UIs are **standard-library only** — there is nothing extra to `pip install`
and no paid components. The desktop GUI uses Tkinter (bundled with the official
Python installers; `sudo apt install python3-tk` on Debian/Ubuntu). The terminal
UI needs nothing at all.

Key things the UIs do for you:

- **Live progress** — overall bar, transferred/total, throughput, ETA, and a
  per-worker view, polled straight from the durable session database.
- **Safe to close** — the swap runs as a background process, so quitting the
  window or terminal does not stop it; reopen and **Resume** any time.
- **Cancel without fear** — stopping mid-run just interrupts it; the engine is
  resumable, so you continue exactly where you left off.

See [UI_GUIDE.md](https://github.com/the-abraar/swappy2.0/blob/main/UI_GUIDE.md) for a full walkthrough.

## ⚙️ How It Works

Swappy runs a staged pipeline:

```mermaid
flowchart LR
    A[Preflight] --> B[Staging]
    B --> C[Scanner]
    C --> D[Transfer]
    D --> E[Cleanup]
```

### 🧭 Stage flow

1. `preflight`
   Validates both roots, checks they live on distinct volumes, and rejects
   impossible swaps before anything is modified.
2. `staging`
   Renames the original roots aside on their own drives and prepares the final
   destination roots.
3. `scanner`
   Walks both staged trees and records the inventory in SQLite.
4. `transfer`
   Recreates structure, copies files, and engages the sparse engine when free
   space is too tight for ordinary whole-file movement.
5. `cleanup`
   Removes the now-empty staged trees and marks the session complete.

## 🧩 Low-Space Strategy

The interesting part is the transfer engine.

Swappy always prefers the fast path first:

- pick a whole file that fits;
- copy it durably;
- delete the source only after the destination is safe.

If nothing fits on either side, it does not give up. Instead it switches to a
sparse round-robin strategy that moves one chunk at a time in each direction
until enough space is freed to return to whole-file copying.

That makes it useful in the exact scenario where manual workflows usually fail.

## 🛡️ Safety Model

Swappy is built around a few deliberate guarantees:

- source data is never destroyed before destination data is durable;
- session progress is persisted in SQLite;
- sparse progress is derived from the filesystem itself;
- stage functions are idempotent, so `resume` simply re-runs the pipeline;
- rollback remains available until final cleanup completes.

## 📋 Requirements

- Windows, macOS, or Linux (Windows-first, but the engine runs on POSIX too)
- Python 3.11+
- Two existing source roots on distinct volumes
- Sparse-file support on both relevant volumes for the low-space fallback path
- For the desktop GUI only: Tkinter (bundled on Windows/macOS; `python3-tk` on Linux)

NTFS supports sparse files. exFAT does not.

## 🧪 Command Reference

### `run`

Starts a new swap session and creates `swap.db` in the work directory.

```bash
python -m swap run ^
  --folder-a D:\FolderA ^
  --folder-b E:\FolderB ^
  --work-dir D:\.swap_work ^
  --chunk-size 64MB ^
  --safety-margin 64MB ^
  --workers 8
```

Useful options:

- `--chunk-size`: sparse transfer chunk size, default `64MB`
- `--safety-margin`: minimum free space each drive must still have after the full swap, default `64MB`
- `--workers`: concurrent whole-file copy workers, default `8`
- `--backend`: copy backend, `winapi` or `python`
- `--no-verify-chunks`: skip sparse chunk read-back verification

### `resume`

Re-runs the idempotent pipeline for an existing work directory.

### `status`

Prints the session stage and entry counts by state.

### `rollback`

Restores a partially completed swap, as long as the session has not already
reached the final `done` state.

## 🚧 Limitations

- roots must already exist and must not be nested;
- roots must be on distinct volumes;
- junctions and unsupported reparse points are refused during scanning;
- symlinks are recreated, not followed;
- metadata preservation is basic and does not guarantee ACLs or alternate data streams;
- once cleanup finishes and the session reaches `done`, the swap is final.

## 🛠️ Development

Run the test suite with:

```bash
python pytest.py -q
```

Contributor-oriented architecture notes live in [CONTRIBUTORS.md](https://github.com/the-abraar/swappy2.0/blob/main/CONTRIBUTORS.md).

## 🗂️ Project Notes

- Main package: `src/swap/`
- Entry point: `src/swap/cli.py`
- Pipeline driver: `src/swap/pipeline.py`
- Durable state: `src/swap/db.py`

If you are contributing, start with [CONTRIBUTORS.md](https://github.com/the-abraar/swappy2.0/blob/main/CONTRIBUTORS.md).
