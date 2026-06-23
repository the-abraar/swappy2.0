"""Command-line interface.

    python -m swap run      --folder-a D:\\Games\\GameA --folder-b E:\\Games\\GameB --work-dir D:\\.swap_work
    python -m swap resume   --work-dir D:\\.swap_work
    python -m swap rollback --work-dir D:\\.swap_work
    python -m swap status   --work-dir D:\\.swap_work
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import pipeline, rollback
from .db import Database, Session

_SIZE_PATTERN = re.compile(r"^(\d+)\s*(B|KB|MB|GB)?$", re.IGNORECASE)
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # UI front-ends don't operate on a fixed work dir up front.
    if args.command == "gui":
        from . import gui
        return gui.main()
    if args.command == "tui":
        from . import tui
        tui_args = ["--work-dir", args.work_dir] if getattr(args, "work_dir", None) else None
        return tui.main(tui_args)

    db = Database(Path(args.work_dir) / "swap.db")
    try:
        if args.command == "run":
            _cmd_run(db, args)
        elif args.command == "resume":
            _cmd_resume(db)
        elif args.command == "rollback":
            rollback.run(db)
        elif args.command == "status":
            _cmd_status(db)
    finally:
        db.close()
    return 0


def _cmd_run(db: Database, args: argparse.Namespace) -> None:
    folder_a = Path(args.folder_a).resolve(strict=False)
    folder_b = Path(args.folder_b).resolve(strict=False)
    db.create_session(Session(
        folder_a=folder_a,
        folder_b=folder_b,
        staged_a=folder_a.parent / ".swap_staged_a",
        staged_b=folder_b.parent / ".swap_staged_b",
        # the swap exchanges contents, so each drive's final root takes
        # the OTHER folder's name
        final_a=folder_a.parent / folder_b.name,
        final_b=folder_b.parent / folder_a.name,
        chunk_size=parse_size(args.chunk_size),
        backend=args.backend,
        verify_chunks=not args.no_verify_chunks,
        safety_margin=parse_size(args.safety_margin),
        worker_count=args.workers,
        stage="created",
    ))
    volume_id = _simulated_volume_id if args.volume_mode == "simulate-distinct" else None
    pipeline.run(db, volume_id=volume_id)


def _cmd_resume(db: Database) -> None:
    stage = db.session().stage
    if stage == "rolling_back":
        raise SystemExit("a rollback is in progress; run `swap rollback` to finish it")
    if stage == "rolled_back":
        raise SystemExit("this session was rolled back; start over with `swap run`")
    pipeline.run(db)


def _cmd_status(db: Database) -> None:
    session = db.session()
    counts = db.counts_by_state()
    print(f"stage:   {session.stage}")
    print(f"folders: {session.folder_a} <-> {session.folder_b}")
    for state in ("pending", "sparse", "done"):
        print(f"entries {state}: {counts.get(state, 0)}")


def _simulated_volume_id(path: Path) -> object:
    return path.resolve(strict=False)  # every root counts as its own volume


def parse_size(text: str) -> int:
    match = _SIZE_PATTERN.match(text.strip())
    if match is None:
        raise SystemExit(f"invalid size: {text!r} (expected e.g. 64MB)")
    return int(match.group(1)) * _SIZE_UNITS[(match.group(2) or "B").upper()]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="swap", description="Crash-safe low-space folder swap")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="start a new swap")
    run.add_argument("--folder-a", required=True)
    run.add_argument("--folder-b", required=True)
    run.add_argument("--work-dir", required=True)
    run.add_argument("--chunk-size", default="64MB")
    run.add_argument("--safety-margin", default="64MB")
    run.add_argument("--backend", choices=("winapi", "python"),
                     default="winapi" if sys.platform == "win32" else "python")
    run.add_argument("--workers", type=int, default=8)
    run.add_argument("--no-verify-chunks", action="store_true",
                     help="skip per-chunk read-back verification in the sparse engine")
    run.add_argument("--volume-mode", choices=("real", "simulate-distinct"), default="real",
                     help="simulate-distinct treats each root as its own volume (tests)")

    resume = commands.add_parser("resume", help="continue an interrupted swap")
    resume.add_argument("--work-dir", required=True)

    rollback_cmd = commands.add_parser("rollback", help="undo a partially-completed swap")
    rollback_cmd.add_argument("--work-dir", required=True)

    status = commands.add_parser("status", help="show progress")
    status.add_argument("--work-dir", required=True)

    commands.add_parser("gui", help="launch the desktop GUI (Tkinter)")

    tui_cmd = commands.add_parser("tui", help="launch the interactive terminal UI")
    tui_cmd.add_argument("--work-dir", help="open an existing session directly")

    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "run" and args.workers <= 0:
        raise SystemExit("--workers must be at least 1")
    return args
