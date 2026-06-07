"""All durable state, in one SQLite database (WAL, synchronous=NORMAL).

Correctness-critical tables:

  session  one row: the six root paths, options, and `stage` — how far the
           pipeline has progressed. Stages advance monotonically:
           created -> staged -> scanned -> transferred -> done
           `swap rollback` branches off that line instead:
           staged|scanned|transferred -> rolling_back -> rolled_back

  entries  one row per item discovered in the staged trees.
           state: pending -> done            (dirs, symlinks, whole-file copies)
                  pending -> sparse -> done  (files moved by the sparse engine)
           Once stage is 'rolling_back' the same values carry the REVERSE
           meaning: 'pending' = still needs moving back to its staged tree,
           'done' = nothing left to undo (see rollback.py).

There is deliberately no chunk table. Sparse-transfer progress is derived
from the filesystem itself: every transferred chunk truncates the source
file, so the source's current size IS the durable progress marker.

Display-only tables (the live progress screen; safe to lose or rebuild):

  progress_summary       one row: aggregate byte/file/timing counters
  progress_workers       one row per worker slot, replaced on every flush
  progress_file_history  append-only per-file completion records

On resume these are rebuilt from the durable truth above — stale in-flight
worker rows are discarded, never trusted.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

STAGES = ("created", "staged", "scanned", "transferred", "done")
ROLLBACK_STAGES = ("rolling_back", "rolled_back")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    folder_a TEXT NOT NULL,
    folder_b TEXT NOT NULL,
    staged_a TEXT NOT NULL,
    staged_b TEXT NOT NULL,
    final_a TEXT NOT NULL,
    final_b TEXT NOT NULL,
    chunk_size INTEGER NOT NULL,
    backend TEXT NOT NULL,
    verify_chunks INTEGER NOT NULL,
    safety_margin INTEGER NOT NULL,
    worker_count INTEGER NOT NULL,
    stage TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    side TEXT NOT NULL CHECK (side IN ('A', 'B')),
    rel_path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('dir', 'file', 'symlink')),
    size INTEGER,
    link_target TEXT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'sparse', 'done')),
    UNIQUE (side, rel_path)
);
CREATE TABLE IF NOT EXISTS progress_summary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    stage TEXT NOT NULL,
    worker_count INTEGER NOT NULL,
    total_files INTEGER NOT NULL,
    pending_files INTEGER NOT NULL,
    sparse_files INTEGER NOT NULL,
    done_files INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    done_bytes INTEGER NOT NULL,
    bytes_copied INTEGER NOT NULL,
    active_workers INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL,
    read_seconds REAL NOT NULL,
    write_seconds REAL NOT NULL,
    verify_seconds REAL NOT NULL,
    chunk_seconds REAL NOT NULL,
    chunk_rounds INTEGER NOT NULL,
    chunk_bytes_moved INTEGER NOT NULL,
    status_text TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS progress_workers (
    worker_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    rel_path TEXT,
    mode TEXT,
    backend TEXT,
    file_size INTEGER,
    bytes_copied INTEGER NOT NULL,
    started_at REAL,
    updated_at REAL,
    throughput_bps REAL,
    elapsed_seconds REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS progress_file_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    worker_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    backend TEXT,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    duration_seconds REAL NOT NULL,
    throughput_bps REAL,
    read_seconds REAL,
    write_seconds REAL,
    verify_seconds REAL,
    chunk_seconds REAL,
    notes TEXT
);
"""


@dataclass(frozen=True)
class Session:
    folder_a: Path      # original root on drive A
    folder_b: Path      # original root on drive B
    staged_a: Path      # folder_a renamed aside during the swap
    staged_b: Path
    final_a: Path       # destination root on drive A (receives B's content)
    final_b: Path       # destination root on drive B (receives A's content)
    chunk_size: int
    backend: str        # 'winapi' or 'python' ('robocopy' accepted from legacy sessions)
    verify_chunks: bool
    safety_margin: int
    worker_count: int = 8
    stage: str = "created"

    def staged_root(self, side: str) -> Path:
        return self.staged_a if side == "A" else self.staged_b

    def final_root_for(self, side: str) -> Path:
        """Destination root for content that originated on `side`."""
        return self.final_b if side == "A" else self.final_a

    def src_path(self, entry: Entry) -> Path:
        return self.staged_root(entry.side) / entry.rel_path

    def dst_path(self, entry: Entry) -> Path:
        return self.final_root_for(entry.side) / entry.rel_path


@dataclass(frozen=True)
class Entry:
    id: int
    side: str           # which staged tree it came from: 'A' or 'B'
    rel_path: str
    kind: str           # 'dir' | 'file' | 'symlink'
    size: int | None    # original size at scan time (files only)
    link_target: str | None
    state: str          # 'pending' | 'sparse' | 'done'


@dataclass(frozen=True)
class ProgressBootstrap:
    total_files: int
    total_bytes: int
    pending_files: int
    sparse_files: int
    done_files: int
    done_bytes: int
    sparse_bytes_copied: int

    @property
    def bytes_copied(self) -> int:
        return self.done_bytes + self.sparse_bytes_copied


@dataclass(frozen=True)
class ProgressSummarySnapshot:
    stage: str
    worker_count: int
    total_files: int
    pending_files: int
    sparse_files: int
    done_files: int
    total_bytes: int
    done_bytes: int
    bytes_copied: int
    active_workers: int
    elapsed_seconds: float
    read_seconds: float
    write_seconds: float
    verify_seconds: float
    chunk_seconds: float
    chunk_rounds: int
    chunk_bytes_moved: int
    status_text: str
    started_at: float
    updated_at: float
    finished_at: float | None


@dataclass(frozen=True)
class ProgressWorkerSnapshot:
    worker_id: int
    state: str
    rel_path: str | None
    mode: str | None
    backend: str | None
    file_size: int | None
    bytes_copied: int
    started_at: float | None
    updated_at: float | None
    throughput_bps: float | None
    elapsed_seconds: float | None
    note: str | None = None


@dataclass(frozen=True)
class ProgressFileHistory:
    entry_id: int
    rel_path: str
    size: int
    worker_id: int
    mode: str
    backend: str | None
    started_at: float
    finished_at: float
    duration_seconds: float
    throughput_bps: float | None
    read_seconds: float | None
    write_seconds: float | None
    verify_seconds: float | None
    chunk_seconds: float | None
    notes: str | None = None


class Database:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self, *, durable: bool = False):
        if durable:
            self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("BEGIN")
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        finally:
            if durable:
                self._conn.execute("PRAGMA synchronous=NORMAL")

    # -- session -----------------------------------------------------------

    def create_session(self, session: Session) -> None:
        if self.has_session():
            raise RuntimeError("a session already exists in this work dir; use resume")
        self._conn.execute(
            "INSERT INTO session (id, folder_a, folder_b, staged_a, staged_b,"
            " final_a, final_b, chunk_size, backend, verify_chunks,"
            " safety_margin, worker_count, stage)"
            " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(session.folder_a),
                str(session.folder_b),
                str(session.staged_a),
                str(session.staged_b),
                str(session.final_a),
                str(session.final_b),
                session.chunk_size,
                session.backend,
                int(session.verify_chunks),
                session.safety_margin,
                session.worker_count,
                session.stage,
            ),
        )

    def has_session(self) -> bool:
        return self._conn.execute("SELECT 1 FROM session").fetchone() is not None

    def session(self) -> Session:
        row = self._conn.execute(
            "SELECT folder_a, folder_b, staged_a, staged_b, final_a, final_b,"
            " chunk_size, backend, verify_chunks, safety_margin, worker_count, stage"
            " FROM session"
        ).fetchone()
        if row is None:
            raise RuntimeError("no session in this work dir; run a swap first")
        return Session(
            folder_a=Path(row[0]),
            folder_b=Path(row[1]),
            staged_a=Path(row[2]),
            staged_b=Path(row[3]),
            final_a=Path(row[4]),
            final_b=Path(row[5]),
            chunk_size=row[6],
            backend=row[7],
            verify_chunks=bool(row[8]),
            safety_margin=row[9],
            worker_count=row[10],
            stage=row[11],
        )

    def advance_stage(self, new_stage: str) -> None:
        current = self.session().stage
        if STAGES.index(new_stage) <= STAGES.index(current):
            raise RuntimeError(f"cannot move stage backwards: {current} -> {new_stage}")
        with self._transaction(durable=True):
            self._conn.execute("UPDATE session SET stage = ?", (new_stage,))

    def begin_rollback(self, entry_states: dict[int, str]) -> None:
        """Atomically remap entry states to their rollback meaning and enter
        'rolling_back'. One transaction, so a crash never leaves flipped
        entries under a forward stage (or vice versa)."""
        current = self.session().stage
        if current not in ("staged", "scanned", "transferred"):
            raise RuntimeError(f"cannot start a rollback from stage '{current}'")
        with self._transaction(durable=True):
            self._conn.executemany(
                "UPDATE entries SET state = ? WHERE id = ?",
                [(state, entry_id) for entry_id, state in entry_states.items()],
            )
            self._conn.execute("UPDATE session SET stage = 'rolling_back'")

    def finish_rollback(self) -> None:
        current = self.session().stage
        if current not in ("created", "rolling_back"):
            raise RuntimeError(f"cannot finish a rollback from stage '{current}'")
        with self._transaction(durable=True):
            self._conn.execute("UPDATE session SET stage = 'rolled_back'")

    # -- entries -----------------------------------------------------------

    def replace_entries(self, entries: list[Entry]) -> None:
        """Atomically replace the whole inventory (used by the scanner)."""
        with self._transaction():
            self._conn.execute("DELETE FROM entries")
            self._conn.executemany(
                "INSERT INTO entries (side, rel_path, kind, size, link_target, state)"
                " VALUES (?, ?, ?, ?, ?, 'pending')",
                [(e.side, e.rel_path, e.kind, e.size, e.link_target) for e in entries],
            )

    def entries(self, *, kind: str | None = None, state: str | None = None) -> list[Entry]:
        sql = "SELECT id, side, rel_path, kind, size, link_target, state FROM entries"
        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY side, rel_path"
        rows = self._conn.execute(sql, params).fetchall()
        return [Entry(*row) for row in rows]

    def set_entry_state(self, entry_id: int, state: str, *, durable: bool = False) -> None:
        with self._transaction(durable=durable):
            self._conn.execute("UPDATE entries SET state = ? WHERE id = ?", (state, entry_id))

    def set_entries_state(
        self,
        entry_ids: list[int],
        state: str,
        *,
        durable: bool = False,
    ) -> None:
        if not entry_ids:
            return
        placeholders = ", ".join("?" for _ in entry_ids)
        with self._transaction(durable=durable):
            self._conn.execute(
                f"UPDATE entries SET state = ? WHERE id IN ({placeholders})",
                [state, *entry_ids],
            )

    def set_entries_done(self, entry_ids: list[int]) -> None:
        self.set_entries_state(entry_ids, "done")

    def mark_entry_sparse(self, entry_id: int) -> None:
        self.set_entry_state(entry_id, "sparse", durable=True)

    def counts_by_state(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) FROM entries GROUP BY state"
        ).fetchall()
        return dict(rows)

    # -- progress ----------------------------------------------------------

    def progress_bootstrap(self, session: Session) -> ProgressBootstrap:
        rows = self._conn.execute(
            "SELECT id, side, rel_path, size, state FROM entries"
            " WHERE kind = 'file' ORDER BY id"
        ).fetchall()
        total_files = len(rows)
        total_bytes = sum((row[3] or 0) for row in rows)
        pending_files = 0
        sparse_files = 0
        done_files = 0
        done_bytes = 0
        sparse_bytes_copied = 0

        for entry_id, side, rel_path, size, state in rows:
            size = int(size or 0)
            if state == "done":
                done_files += 1
                done_bytes += size
                continue
            if state == "pending":
                pending_files += 1
                continue
            sparse_files += 1
            entry = Entry(
                id=entry_id,
                side=side,
                rel_path=rel_path,
                kind="file",
                size=size,
                link_target=None,
                state=state,
            )
            src = session.src_path(entry)
            dst = session.dst_path(entry)
            if src.exists():
                sparse_bytes_copied += max(0, size - src.stat().st_size)
            elif dst.exists() and dst.stat().st_size == size:
                sparse_bytes_copied += size

        return ProgressBootstrap(
            total_files=total_files,
            total_bytes=total_bytes,
            pending_files=pending_files,
            sparse_files=sparse_files,
            done_files=done_files,
            done_bytes=done_bytes,
            sparse_bytes_copied=sparse_bytes_copied,
        )

    def reset_progress(self, summary: ProgressSummarySnapshot) -> None:
        self._conn.execute("DELETE FROM progress_workers")
        self._conn.executemany(
            "INSERT INTO progress_workers"
            " (worker_id, state, rel_path, mode, backend, file_size, bytes_copied,"
            " started_at, updated_at, throughput_bps, elapsed_seconds, note)"
            " VALUES (?, 'idle', NULL, NULL, NULL, NULL, 0, NULL, NULL, NULL, NULL, NULL)",
            [(worker_id,) for worker_id in range(1, summary.worker_count + 1)],
        )
        self.upsert_progress_summary(summary)

    def upsert_progress_summary(self, summary: ProgressSummarySnapshot) -> None:
        self._conn.execute(
            "INSERT INTO progress_summary"
            " (id, stage, worker_count, total_files, pending_files, sparse_files,"
            " done_files, total_bytes, done_bytes, bytes_copied, active_workers,"
            " elapsed_seconds, read_seconds, write_seconds, verify_seconds,"
            " chunk_seconds, chunk_rounds, chunk_bytes_moved, status_text,"
            " started_at, updated_at, finished_at)"
            " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " stage=excluded.stage,"
            " worker_count=excluded.worker_count,"
            " total_files=excluded.total_files,"
            " pending_files=excluded.pending_files,"
            " sparse_files=excluded.sparse_files,"
            " done_files=excluded.done_files,"
            " total_bytes=excluded.total_bytes,"
            " done_bytes=excluded.done_bytes,"
            " bytes_copied=excluded.bytes_copied,"
            " active_workers=excluded.active_workers,"
            " elapsed_seconds=excluded.elapsed_seconds,"
            " read_seconds=excluded.read_seconds,"
            " write_seconds=excluded.write_seconds,"
            " verify_seconds=excluded.verify_seconds,"
            " chunk_seconds=excluded.chunk_seconds,"
            " chunk_rounds=excluded.chunk_rounds,"
            " chunk_bytes_moved=excluded.chunk_bytes_moved,"
            " status_text=excluded.status_text,"
            " started_at=excluded.started_at,"
            " updated_at=excluded.updated_at,"
            " finished_at=excluded.finished_at",
            (
                summary.stage,
                summary.worker_count,
                summary.total_files,
                summary.pending_files,
                summary.sparse_files,
                summary.done_files,
                summary.total_bytes,
                summary.done_bytes,
                summary.bytes_copied,
                summary.active_workers,
                summary.elapsed_seconds,
                summary.read_seconds,
                summary.write_seconds,
                summary.verify_seconds,
                summary.chunk_seconds,
                summary.chunk_rounds,
                summary.chunk_bytes_moved,
                summary.status_text,
                summary.started_at,
                summary.updated_at,
                summary.finished_at,
            ),
        )

    def replace_progress_workers(self, workers: list[ProgressWorkerSnapshot]) -> None:
        with self._transaction():
            self._conn.execute("DELETE FROM progress_workers")
            self._conn.executemany(
                "INSERT INTO progress_workers"
                " (worker_id, state, rel_path, mode, backend, file_size, bytes_copied,"
                " started_at, updated_at, throughput_bps, elapsed_seconds, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        worker.worker_id,
                        worker.state,
                        worker.rel_path,
                        worker.mode,
                        worker.backend,
                        worker.file_size,
                        worker.bytes_copied,
                        worker.started_at,
                        worker.updated_at,
                        worker.throughput_bps,
                        worker.elapsed_seconds,
                        worker.note,
                    )
                    for worker in workers
                ],
            )

    def append_progress_history(self, history: ProgressFileHistory) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO progress_file_history"
            " (entry_id, rel_path, size, worker_id, mode, backend, started_at,"
            " finished_at, duration_seconds, throughput_bps, read_seconds,"
            " write_seconds, verify_seconds, chunk_seconds, notes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                history.entry_id,
                history.rel_path,
                history.size,
                history.worker_id,
                history.mode,
                history.backend,
                history.started_at,
                history.finished_at,
                history.duration_seconds,
                history.throughput_bps,
                history.read_seconds,
                history.write_seconds,
                history.verify_seconds,
                history.chunk_seconds,
                history.notes,
            ),
        )

    def progress_summary(self) -> ProgressSummarySnapshot | None:
        row = self._conn.execute(
            "SELECT stage, worker_count, total_files, pending_files, sparse_files,"
            " done_files, total_bytes, done_bytes, bytes_copied, active_workers,"
            " elapsed_seconds, read_seconds, write_seconds, verify_seconds,"
            " chunk_seconds, chunk_rounds, chunk_bytes_moved, status_text,"
            " started_at, updated_at, finished_at"
            " FROM progress_summary WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return ProgressSummarySnapshot(*row)

    def progress_workers(self) -> list[ProgressWorkerSnapshot]:
        rows = self._conn.execute(
            "SELECT worker_id, state, rel_path, mode, backend, file_size, bytes_copied,"
            " started_at, updated_at, throughput_bps, elapsed_seconds, note"
            " FROM progress_workers ORDER BY worker_id"
        ).fetchall()
        return [
            ProgressWorkerSnapshot(
                worker_id=row[0],
                state=row[1],
                rel_path=row[2],
                mode=row[3],
                backend=row[4],
                file_size=row[5],
                bytes_copied=row[6],
                started_at=row[7],
                updated_at=row[8],
                throughput_bps=row[9],
                elapsed_seconds=row[10],
                note=row[11],
            )
            for row in rows
        ]

    def progress_history(self) -> list[ProgressFileHistory]:
        rows = self._conn.execute(
            "SELECT entry_id, rel_path, size, worker_id, mode, backend, started_at,"
            " finished_at, duration_seconds, throughput_bps, read_seconds,"
            " write_seconds, verify_seconds, chunk_seconds, notes"
            " FROM progress_file_history ORDER BY finished_at, id"
        ).fetchall()
        return [
            ProgressFileHistory(
                entry_id=row[0],
                rel_path=row[1],
                size=row[2],
                worker_id=row[3],
                mode=row[4],
                backend=row[5],
                started_at=row[6],
                finished_at=row[7],
                duration_seconds=row[8],
                throughput_bps=row[9],
                read_seconds=row[10],
                write_seconds=row[11],
                verify_seconds=row[12],
                chunk_seconds=row[13],
                notes=row[14],
            )
            for row in rows
        ]

    def recent_progress_history(self, limit: int) -> list[ProgressFileHistory]:
        rows = self._conn.execute(
            "SELECT entry_id, rel_path, size, worker_id, mode, backend, started_at,"
            " finished_at, duration_seconds, throughput_bps, read_seconds,"
            " write_seconds, verify_seconds, chunk_seconds, notes"
            " FROM progress_file_history ORDER BY finished_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        history = [
            ProgressFileHistory(
                entry_id=row[0],
                rel_path=row[1],
                size=row[2],
                worker_id=row[3],
                mode=row[4],
                backend=row[5],
                started_at=row[6],
                finished_at=row[7],
                duration_seconds=row[8],
                throughput_bps=row[9],
                read_seconds=row[10],
                write_seconds=row[11],
                verify_seconds=row[12],
                chunk_seconds=row[13],
                notes=row[14],
            )
            for row in rows
        ]
        history.reverse()
        return history


def make_initial_progress_summary(
    *,
    session: Session,
    bootstrap: ProgressBootstrap,
    stage: str,
    status_text: str,
    started_at: float | None = None,
) -> ProgressSummarySnapshot:
    now = time.time()
    started_at = started_at or now
    return ProgressSummarySnapshot(
        stage=stage,
        worker_count=session.worker_count,
        total_files=bootstrap.total_files,
        pending_files=bootstrap.pending_files,
        sparse_files=bootstrap.sparse_files,
        done_files=bootstrap.done_files,
        total_bytes=bootstrap.total_bytes,
        done_bytes=bootstrap.done_bytes,
        bytes_copied=bootstrap.bytes_copied,
        active_workers=0,
        elapsed_seconds=max(0.0, now - started_at),
        read_seconds=0.0,
        write_seconds=0.0,
        verify_seconds=0.0,
        chunk_seconds=0.0,
        chunk_rounds=0,
        chunk_bytes_moved=bootstrap.sparse_bytes_copied,
        status_text=status_text,
        started_at=started_at,
        updated_at=now,
        finished_at=None,
    )
