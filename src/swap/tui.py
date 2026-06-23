"""Swappy terminal UI — an interactive, live dashboard.

Run it with::

    python -m swap tui
    python -m swap tui --work-dir D:\\.swap_work     # jump straight to a session

It is a friendly front-end over the same CLI everything else uses: it collects
settings with prompts, launches the engine as a subprocess (so the swap survives
the UI closing and can be cancelled safely), and renders a live dashboard by
polling ``swap.db`` read-only.

Built on the Python standard library only — no third-party UI toolkit — so it
runs anywhere Python does, including Windows terminals, with nothing extra to
install.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import uikit

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

CSI = "\x1b["
RESET = f"{CSI}0m"
BOLD = f"{CSI}1m"
DIM = f"{CSI}2m"
CLEAR = f"{CSI}2J{CSI}H"
HOME = f"{CSI}H"
HIDE_CURSOR = f"{CSI}?25l"
SHOW_CURSOR = f"{CSI}?25h"

CYAN = f"{CSI}38;5;45m"
GREEN = f"{CSI}38;5;42m"
YELLOW = f"{CSI}38;5;220m"
RED = f"{CSI}38;5;203m"
MAGENTA = f"{CSI}38;5;177m"
GREY = f"{CSI}38;5;245m"

_USE_COLOR = True


def _enable_ansi() -> bool:
    """Best-effort enable ANSI on the current terminal."""
    global _USE_COLOR
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        _USE_COLOR = False
        return False
    if sys.platform == "win32":
        # Turn on virtual-terminal processing on modern Windows consoles.
        os.system("")
    _USE_COLOR = True
    return True


def c(text: str, color: str) -> str:
    if not _USE_COLOR:
        return text
    return f"{color}{text}{RESET}"


STATE_COLORS = {
    "copying": CYAN,
    "chunking": MAGENTA,
    "idle": GREY,
    "waiting": YELLOW,
    "done": GREEN,
    "failed": RED,
}

BANNER = r"""
 ___
/ __|_      ____ _ _ __ _ __ _  _
\__ \ \ /\ / / _` | '_ \ '_ \ || |
|___/_\_/\_/\__,_| .__/ .__/\_, |
                 |_|  |_|    |__/
""".strip("\n")


# ---------------------------------------------------------------------------
# Frame rendering (pure: snapshot -> list[str])
# ---------------------------------------------------------------------------


def _term_width(default: int = 92) -> int:
    try:
        return max(60, min(120, os.get_terminal_size().columns))
    except OSError:
        return default


def _bar(fraction: float, width: int) -> str:
    width = max(10, width)
    filled = int(round(fraction * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "·" * (width - filled)


def _rule(label: str, width: int) -> str:
    label = f" {label} "
    side = max(0, (width - len(label)) // 2)
    return c("─" * side + label + "─" * (width - side - len(label)), CYAN)


def _row(cells: list[tuple[str, int, str]], width: int) -> str:
    """Render fixed-width cells: (text, width, align 'l'|'r'|'c')."""
    out = []
    for text, w, align in cells:
        text = text[:w]
        if align == "r":
            out.append(text.rjust(w))
        elif align == "c":
            out.append(text.center(w))
        else:
            out.append(text.ljust(w))
    return " ".join(out)[:width]


def render_frame(snapshot: uikit.UiSnapshot, command: str, logs: list[str], width: int) -> str:
    lines: list[str] = []
    stage = snapshot.stage_label
    stage_col = GREEN if snapshot.is_finished else CYAN
    head = _row(
        [(c("SWAPPY", BOLD + CYAN), 20, "l"), (c(f"stage: {stage}", BOLD + stage_col), width - 21, "r")],
        width,
    )
    lines.append(head)
    if command:
        lines.append(c(command[: width], DIM))
    lines.append("")

    if not snapshot.exists:
        lines.append(c("  Waiting for the engine to initialise the session…", GREY))
        return "\n".join(lines)

    s = snapshot.summary
    frac = snapshot.fraction
    bar_w = width - 8
    lines.append(_rule("Progress", width))
    lines.append(f"  {c(_bar(frac, bar_w), CYAN)} {int(frac * 100):3d}%")
    copied = uikit.format_bytes(s.bytes_copied) if s else "—"
    grand = uikit.format_bytes(s.total_bytes) if s else "—"
    files = f"{s.done_files}/{s.total_files}" if s else "—"
    lines.append(
        "  "
        + c("Transferred ", GREY) + c(f"{copied} / {grand}", BOLD)
        + c("   Files ", GREY) + c(files, BOLD)
        + c("   Speed ", GREY) + c(uikit.format_rate(snapshot.rate_bps), BOLD)
        + c("   ETA ", GREY) + c(uikit.format_seconds(snapshot.eta_seconds), BOLD)
    )
    lines.append("")

    lines.append(_rule("Workers", width))
    lines.append(c("  " + _row([("#", 3, "r"), ("State", 9, "l"), ("File", width - 40, "l"),
                                 ("Done", 9, "r"), ("Speed", 11, "r")], width - 2), GREY))
    workers = snapshot.workers or []
    for w in workers:
        pct = ""
        if w.file_size and w.state in ("copying", "chunking"):
            pct = f" {100.0 * w.bytes_copied / max(w.file_size, 1):.0f}%"
        state_txt = c(w.state, STATE_COLORS.get(w.state, ""))
        # pad state manually (color codes break ljust width)
        state_pad = w.state.ljust(9)
        state_cell = state_pad.replace(w.state, state_txt) if _USE_COLOR else state_pad
        done = (uikit.format_bytes(w.bytes_copied) + pct) if w.bytes_copied else "—"
        lines.append("  " + _row([
            (str(w.worker_id), 3, "r"),
            (state_cell, 9 + (len(state_txt) - len(w.state) if _USE_COLOR else 0), "l"),
            (uikit.short_path(w.rel_path, width - 40), width - 40, "l"),
            (done, 9, "r"),
            (uikit.format_rate(w.throughput_bps), 11, "r"),
        ], width + 40))
    if not workers:
        lines.append(c("  (no active workers yet)", GREY))
    lines.append("")

    lines.append(_rule("Recently completed", width))
    rows = snapshot.history[-6:]
    for h in rows:
        lines.append("  " + _row([
            (uikit.short_path(h.rel_path, width - 34), width - 34, "l"),
            (h.mode, 10, "l"),
            (uikit.format_bytes(h.size), 9, "r"),
            (uikit.format_rate(h.throughput_bps), 11, "r"),
        ], width))
    if not rows:
        lines.append(c("  (nothing yet)", GREY))
    lines.append("")

    if logs:
        lines.append(_rule("Engine log", width))
        for ln in logs[-5:]:
            lines.append(c("  " + ln[: width - 2], DIM))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live monitor loop
# ---------------------------------------------------------------------------


def _monitor(proc: uikit.SwapProcess, work_dir: str, *, title: str) -> int | None:
    reader = uikit.ProgressReader(work_dir)
    logs: list[str] = []
    interactive = sys.stdout.isatty()
    if interactive:
        sys.stdout.write(HIDE_CURSOR)
    print(c(f"{title} — Ctrl-C to cancel safely (resumable).", GREY))
    try:
        while True:
            logs.extend(proc.drain_logs())
            snap = reader.read()
            frame = render_frame(snap, proc.command, logs, _term_width())
            if interactive:
                sys.stdout.write(HOME + CLEAR + frame + "\n")
                sys.stdout.flush()
            if not proc.is_running():
                logs.extend(proc.drain_logs())
                if not interactive:
                    print(render_frame(reader.read(), proc.command, logs, _term_width()))
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        if interactive:
            sys.stdout.write(SHOW_CURSOR)
        if _ask_yesno("\nCancel the running operation?", default=False):
            proc.cancel()
            print(c("Cancelled. Re-run and choose Resume to continue.", YELLOW))
            reader.close()
            return proc.returncode
        return _monitor(proc, work_dir, title=title)
    finally:
        if interactive:
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.flush()
    reader.close()
    rc = proc.returncode
    if rc == 0:
        print(c("✓ Operation complete.", BOLD + GREEN))
    elif rc is not None:
        print(c(f"✗ Engine exited with code {rc}. See the log above.", BOLD + RED))
    return rc


# ---------------------------------------------------------------------------
# Prompt helpers (stdlib input)
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(c(f"{prompt}{suffix}: ", CYAN)).strip().strip('"')
    return raw or default


def _ask_yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(c(f"{prompt} [{d}]: ", CYAN)).strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def _ask_int(prompt: str, default: int) -> int:
    while True:
        raw = input(c(f"{prompt} [{default}]: ", CYAN)).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(c("  Please enter a whole number.", RED))


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    while True:
        raw = input(c(f"{prompt} ({'/'.join(choices)}) [{default}]: ", CYAN)).strip()
        if not raw:
            return default
        if raw in choices:
            return raw
        print(c(f"  Choose one of: {', '.join(choices)}", RED))


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def _summary(cfg: uikit.SwapConfig) -> str:
    return (
        c("  Review", BOLD) + "\n"
        + f"    Folder A    {cfg.folder_a}\n"
        + f"    Folder B    {cfg.folder_b}\n"
        + f"    Work dir    {cfg.work_dir}\n"
        + f"    Chunk/marg  {cfg.chunk_size} / {cfg.safety_margin}\n"
        + f"    Workers/be  {cfg.workers} / {cfg.backend}\n"
        + f"    Verify      {'yes' if cfg.verify_chunks else 'no'}"
    )


def _ask_config() -> uikit.SwapConfig | None:
    print(_rule("New swap", _term_width()))
    cfg = uikit.SwapConfig()
    cfg.folder_a = _ask("Folder A (first root)")
    cfg.folder_b = _ask("Folder B (second root)")
    default_work = str(Path(cfg.folder_a).parent / ".swap_work") if cfg.folder_a else ""
    cfg.work_dir = _ask("Work directory", default_work)

    if _ask_yesno("Adjust advanced settings?", default=False):
        cfg.chunk_size = _ask("Chunk size", cfg.chunk_size)
        cfg.safety_margin = _ask("Safety margin", cfg.safety_margin)
        cfg.workers = _ask_int("Workers", cfg.workers)
        cfg.backend = _ask_choice("Backend", list(uikit.BACKENDS), cfg.backend)
        cfg.verify_chunks = _ask_yesno("Verify chunks (SHA-256)?", default=True)

    problems = cfg.validate()
    if problems:
        print(c("Cannot start:", BOLD + RED))
        for p in problems:
            print(f"  • {p}")
        return None

    print(_summary(cfg))
    if not _ask_yesno("Start the swap?", default=True):
        return None
    return cfg


def _new_swap() -> None:
    cfg = _ask_config()
    if cfg is None:
        return
    proc = uikit.SwapProcess()
    proc.start(uikit.run_argv(cfg))
    _monitor(proc, cfg.work_dir, title="Running swap")


def _print_status(work_dir: str) -> None:
    snap = uikit.ProgressReader(work_dir).read()
    if not snap.exists:
        print(c("No session in that work directory yet.", YELLOW))
        return
    print(c("  Status", BOLD))
    print(f"    Stage   {snap.stage_label}")
    for state in ("pending", "sparse", "done"):
        print(f"    {state:<8}{snap.counts.get(state, 0)}")


def _watch_only(work_dir: str) -> None:
    reader = uikit.ProgressReader(work_dir)
    interactive = sys.stdout.isatty()
    print(c("Watching (read-only). Ctrl-C to stop.", GREY))
    try:
        while True:
            frame = render_frame(reader.read(), "", [], _term_width())
            if interactive:
                sys.stdout.write(HOME + CLEAR + frame + "\n")
                sys.stdout.flush()
            else:
                print(frame)
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    reader.close()


def _existing_session(work_dir: str) -> None:
    while True:
        snap = uikit.ProgressReader(work_dir).read()
        stage = snap.stage_label if snap.exists else "no session found"
        print(_rule(f"Session: {work_dir}  ·  {stage}", _term_width()))
        print("  1 Resume   2 Rollback   3 Status   4 Watch   5 Back")
        choice = _ask_choice("Choose", ["1", "2", "3", "4", "5"], "1")
        if choice == "5":
            return
        if choice == "3":
            _print_status(work_dir)
            continue
        if choice == "4":
            _watch_only(work_dir)
            continue
        if choice == "2" and not _ask_yesno(
            "Roll back the swap to its original layout?", default=False
        ):
            continue
        argv = uikit.resume_argv(work_dir) if choice == "1" else uikit.rollback_argv(work_dir)
        title = "Resuming" if choice == "1" else "Rolling back"
        proc = uikit.SwapProcess()
        proc.start(argv)
        _monitor(proc, work_dir, title=title)


def _main_menu() -> None:
    print(c(BANNER, BOLD + CYAN))
    print(c("  Crash-safe folder swapping for tight drives", GREY))
    while True:
        print()
        print("  1 New swap    2 Open existing session    3 Quit")
        choice = _ask_choice("Choose", ["1", "2", "3"], "1")
        if choice == "1":
            _new_swap()
        elif choice == "2":
            _existing_session(_ask("Work directory"))
        else:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swap tui", description="Swappy terminal UI")
    parser.add_argument("--work-dir", help="jump straight to an existing session")
    args = parser.parse_args(argv)
    _enable_ansi()
    try:
        if args.work_dir:
            _existing_session(args.work_dir)
        else:
            _main_menu()
    except (KeyboardInterrupt, EOFError):
        print(c("\nBye.", GREY))
    finally:
        if sys.stdout.isatty():
            sys.stdout.write(SHOW_CURSOR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
