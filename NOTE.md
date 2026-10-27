# What changed

A summary of the changes in this update, plus what to check before pushing to
`main` and running on Windows.

## TL;DR

- Fixed 4 inaccuracies in `CLAUDE.md` so the docs match the code.
- Replaced the paid/closed **PySimpleGUI** UI with two dependency-free front-ends:
  a **Tkinter desktop GUI** and a **terminal UI (TUI)** — both standard library.
- Added a shared, unit-tested UI kit; wired `swap gui` / `swap tui` subcommands.
- Polished packaging (MIT license, metadata), docs, and added CI.
- Test suite grew from **64 → 76 passing** tests.

## Documentation fixes (`CLAUDE.md`)

The architecture notes claimed things the code doesn't do. Corrected:

1. SQLite is opened `synchronous=NORMAL` (raised to `FULL` only inside the
   durable transactions) — not `synchronous=FULL` globally.
2. The coordinator waits with `timeout=0.01`, not `0.1`.
3. Whole-file completions are batched via `db.set_entries_done(ids)`; the
   per-id `set_entry_state(..., "done")` path is the sparse engine's.
4. There is no `leased_ids`; double-dispatch is prevented by `popleft()` off
   the per-side pending deque.

## New user interfaces

The engine and CLI are unchanged in behavior. Added on top:

- `src/swap/uikit.py` — shared, toolkit-agnostic layer (config + validation,
  CLI command builders, a subprocess driver with safe cancel, and a read-only
  progress reader that polls `swap.db`). This is the tested part
  (`tests/test_uikit.py`).
- `src/swap/tui.py` — interactive terminal dashboard (ANSI, standard library).
  Launch: `python -m swap tui`.
- `src/swap/gui.py` — Tkinter desktop app (standard library; no paid deps).
  Launch: `python -m swap gui`.

Both run the engine as a subprocess and read progress from the database, so a
swap keeps running if the UI closes and **Cancel is always safe** (resume later).

Removed/repurposed: `swap_ui.py` is now a thin shim to `swap.gui`;
`ui_requirements.txt` no longer lists PySimpleGUI (no extra pip deps now);
`launcher.py` / `swappy_ui.bat` install Swappy and open a UI.

## Packaging, docs, CI

- Added `LICENSE` (MIT) and richer `pyproject.toml` metadata, plus console
  scripts `swap-gui` and `swap-tui`. Version bumped to `0.1.0`.
- Refreshed `README.md`, `UI_GUIDE.md`, `CONTRIBUTORS.md`.
- Added `.github/workflows/ci.yml` (Ubuntu + Windows × Python 3.11–3.13:
  byte-compile, run tests with both the shim and real pytest, smoke-test the CLI).
- Expanded `.gitignore` (venv, build artifacts, `*.db`, work/staged dirs).

## Will it work on Windows?

Yes. The engine is the original Windows-first code (the `winapi` backend using
`CopyFileExW` and NTFS sparse files is unchanged). The new code is standard
library only and Windows-aware:

- The GUI/TUI launch the engine via `sys.executable -m swap …`; on Windows the
  child console window is suppressed (`CREATE_NO_WINDOW`).
- The TUI enables ANSI/VT processing on Windows 10+ consoles; use **Windows
  Terminal** or a modern console for best results.
- The GUI needs **Tkinter**, which ships with the official python.org installer
  (just keep the "tcl/tk and IDLE" option checked during install).

### Verified on Linux (this environment)
End-to-end swaps via the subprocess path with live progress polling, resume,
and rollback — all with byte-for-byte integrity checks — plus the full test
suite (76 passing). The TUI was exercised directly.

### Not verifiable here (please smoke-test on Windows)
This sandbox has no display and no Tkinter, so the **GUI could not be run**
(its logic lives in the tested `uikit`, and it byte-compiles). After pushing,
do a quick check on your Windows box:

```powershell
python -m pip install -e .
python -m swap gui                # GUI opens
python -m swap tui                # TUI dashboard
# real swap across two drives:
python -m swap run --folder-a D:\A --folder-b E:\B --work-dir D:\.swap_work
```

## Cleanups (done 2026-10-28)

- URLs in `pyproject.toml` point at `the-abraar/swappy2.0`; the PyPI
  distribution is `swappy2` (`swap` and `swappy` are taken). The import
  package and CLI module are still `swap`.
- `agent_files/` and `.vscode/` are untracked and ignored (they remain in git
  history).
- `.github/workflows/windows-smoke.yml` does a real cross-drive swap, a TUI
  swap back, and a GUI launch + screenshot on `windows-latest`.
