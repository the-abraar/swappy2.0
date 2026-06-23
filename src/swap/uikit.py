"""Shared plumbing for the Swappy front-ends (GUI and TUI).

Both UIs drive the engine identically and stay completely decoupled from it:

  * They launch ``swap`` as a **subprocess** (``python -m swap run ...``).
    That means the swap keeps running even if the UI is closed, and it can
    be cancelled at any time — the engine is crash-safe and resumable, so a
    cancelled run is just an interrupted run you can ``resume`` later.
  * They read live progress by polling the session's ``swap.db`` **read-only**
    through the same durable tables the engine writes. WAL mode lets the UI
    read while the engine writes.

Nothing in this module imports a UI toolkit, so it is import-light and unit
testable on its own.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .db import (
    Database,
    ProgressSummarySnapshot,
    ProgressWorkerSnapshot,
    ProgressFileHistory,
    Session,
)

# ---------------------------------------------------------------------------
# Configuration & command building
# ---------------------------------------------------------------------------

STAGE_LABELS = {
    "created": "Created",
    "staged": "Staged",
    "scanned": "Scanned",
    "transferred": "Transferred",
    "done": "Done",
    "rolling_back": "Rolling back",
    "rolled_back": "Rolled back",
}

BACKENDS = ("python", "winapi")


@dataclass
class SwapConfig:
    """Everything the ``run`` subcommand needs, as plain strings/ints."""

    folder_a: str = ""
    folder_b: str = ""
    work_dir: str = ""
    chunk_size: str = "64MB"
    safety_margin: str = "64MB"
    workers: int = 8
    backend: str = "winapi" if sys.platform == "win32" else "python"
    verify_chunks: bool = True
    volume_mode: str = "real"  # or "simulate-distinct" for same-disk testing

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty == OK)."""
        problems: list[str] = []
        if not self.folder_a:
            problems.append("Folder A is required.")
        elif not Path(self.folder_a).is_dir():
            problems.append(f"Folder A is not a directory: {self.folder_a}")
        if not self.folder_b:
            problems.append("Folder B is required.")
        elif not Path(self.folder_b).is_dir():
            problems.append(f"Folder B is not a directory: {self.folder_b}")
        if not self.work_dir:
            problems.append("Work directory is required.")
        if self.folder_a and self.folder_b:
            try:
                if Path(self.folder_a).resolve() == Path(self.folder_b).resolve():
                    problems.append("Folder A and Folder B must be different.")
            except OSError:
                pass
        if self.workers < 1:
            problems.append("Workers must be at least 1.")
        if self.backend not in BACKENDS:
            problems.append(f"Backend must be one of {BACKENDS}.")
        return problems


def python_exe() -> str:
    return sys.executable or "python"


def _child_env() -> dict[str, str]:
    """Environment for engine subprocesses. Ensures the ``swap`` package is
    importable even when running straight from a source checkout (not just a
    ``pip install``) by putting this package's ``src`` dir on PYTHONPATH."""
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parents[1])  # .../src
    existing = env.get("PYTHONPATH", "")
    parts = [src_dir] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def run_argv(cfg: SwapConfig) -> list[str]:
    argv = [
        python_exe(), "-m", "swap", "run",
        "--folder-a", cfg.folder_a,
        "--folder-b", cfg.folder_b,
        "--work-dir", cfg.work_dir,
        "--chunk-size", cfg.chunk_size,
        "--safety-margin", cfg.safety_margin,
        "--workers", str(cfg.workers),
        "--backend", cfg.backend,
    ]
    if not cfg.verify_chunks:
        argv.append("--no-verify-chunks")
    if cfg.volume_mode != "real":
        argv += ["--volume-mode", cfg.volume_mode]
    return argv


def resume_argv(work_dir: str) -> list[str]:
    return [python_exe(), "-m", "swap", "resume", "--work-dir", work_dir]


def rollback_argv(work_dir: str) -> list[str]:
    return [python_exe(), "-m", "swap", "rollback", "--work-dir", work_dir]


def status_argv(work_dir: str) -> list[str]:
    return [python_exe(), "-m", "swap", "status", "--work-dir", work_dir]


# ---------------------------------------------------------------------------
# Subprocess driver with non-blocking log capture
# ---------------------------------------------------------------------------


class SwapProcess:
    """Runs one engine subprocess at a time and captures its output.

    Output is read on a background thread into a thread-safe queue; the UI
    pumps it with :meth:`drain_logs` from its own event loop. Designed so a
    UI never blocks on the child.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._command: str = ""

    # -- lifecycle ----------------------------------------------------------

    def start(self, argv: list[str], *, cwd: str | None = None) -> None:
        if self.is_running():
            raise RuntimeError("a swap operation is already running")
        self._command = " ".join(argv)
        # On Windows, suppress the extra console window the child would
        # otherwise pop up — important when the GUI is launched without a
        # console (pythonw.exe / a desktop shortcut). Pipes still work.
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=_child_env(),
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            self._log_q.put(line.rstrip("\n"))

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    @property
    def command(self) -> str:
        return self._command

    def cancel(self, *, timeout: float = 5.0) -> None:
        """Stop the child gracefully, then forcibly. Safe: the engine is
        resumable, so a cancelled run can be continued later."""
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def drain_logs(self) -> list[str]:
        lines: list[str] = []
        while True:
            try:
                lines.append(self._log_q.get_nowait())
            except queue.Empty:
                break
        return lines


# ---------------------------------------------------------------------------
# Read-only progress polling
# ---------------------------------------------------------------------------


@dataclass
class UiSnapshot:
    """A flattened, UI-friendly view of the session at one instant."""

    exists: bool = False
    stage: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    summary: ProgressSummarySnapshot | None = None
    workers: list[ProgressWorkerSnapshot] = field(default_factory=list)
    history: list[ProgressFileHistory] = field(default_factory=list)
    session: Session | None = None

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage or "", self.stage or "—")

    @property
    def fraction(self) -> float:
        s = self.summary
        if not s or s.total_bytes <= 0:
            return 0.0
        return max(0.0, min(1.0, s.bytes_copied / s.total_bytes))

    @property
    def rate_bps(self) -> float | None:
        s = self.summary
        if not s or s.elapsed_seconds <= 0 or s.bytes_copied <= 0:
            return None
        return s.bytes_copied / s.elapsed_seconds

    @property
    def eta_seconds(self) -> float | None:
        s = self.summary
        rate = self.rate_bps
        if not s or rate is None or rate <= 0:
            return None
        remaining = s.total_bytes - s.bytes_copied
        if remaining <= 0:
            return 0.0
        return remaining / rate

    @property
    def is_finished(self) -> bool:
        return self.stage in ("done", "rolled_back")


class ProgressReader:
    """Polls a session's ``swap.db`` read-only. Opens lazily, tolerates a
    missing/half-written database, and reuses one WAL reader connection."""

    def __init__(self, work_dir: str | Path) -> None:
        self._db_path = Path(work_dir) / "swap.db"
        self._db: Database | None = None

    def _ensure_db(self) -> Database | None:
        if self._db is not None:
            return self._db
        if not self._db_path.exists():
            return None
        try:
            self._db = Database(self._db_path)
        except Exception:
            self._db = None
        return self._db

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    def read(self) -> UiSnapshot:
        db = self._ensure_db()
        if db is None:
            return UiSnapshot(exists=False)
        try:
            if not db.has_session():
                return UiSnapshot(exists=False)
            session = db.session()
            return UiSnapshot(
                exists=True,
                stage=session.stage,
                counts=db.counts_by_state(),
                summary=db.progress_summary(),
                workers=db.progress_workers(),
                history=db.recent_progress_history(12),
                session=session,
            )
        except Exception:
            # The engine may be mid-write on first creation; treat as "not
            # ready yet" rather than crashing the UI.
            return UiSnapshot(exists=False)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_rate(bps: float | None) -> str:
    if bps is None or bps <= 0:
        return "—"
    return f"{format_bytes(bps)}/s"


def format_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    total = int(value)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m {total % 60:02d}s"


def short_path(value: str | None, width: int = 48) -> str:
    if not value:
        return "—"
    if len(value) <= width:
        return value
    return "…" + value[-(width - 1):]


def wait_for_exit(proc: SwapProcess, *, poll: float = 0.1) -> int | None:
    """Block until the subprocess finishes (used by tests/headless runs)."""
    while proc.is_running():
        time.sleep(poll)
    return proc.returncode
