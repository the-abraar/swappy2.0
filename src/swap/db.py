"""All durable state, in one SQLite database (WAL, synchronous=FULL).

Two tables only:

  session  one row: the six root paths, options, and `stage` — how far the
           pipeline has progressed. Stages advance monotonically:
           created -> staged -> scanned -> transferred -> done

  entries  one row per item discovered in the staged trees.
           state: pending -> done            (dirs, symlinks, whole-file copies)
                  pending -> sparse -> done  (files moved by the sparse engine)

There is deliberately no chunk table. Sparse-transfer progress is derived
from the filesystem itself: every transferred chunk truncates the source
file, so the source's current size IS the durable progress marker.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

STAGES = ("created", "staged", "scanned", "transferred", "done")

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
    backend: str        # 'robocopy' or 'python'
    verify_chunks: bool
    safety_margin: int
    stage: str

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


class Database:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- session -----------------------------------------------------------

    def create_session(self, session: Session) -> None:
        if self.has_session():
            raise RuntimeError("a session already exists in this work dir; use resume")
        self._conn.execute(
            "INSERT INTO session (id, folder_a, folder_b, staged_a, staged_b,"
            " final_a, final_b, chunk_size, backend, verify_chunks,"
            " safety_margin, stage) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(session.folder_a), str(session.folder_b),
                str(session.staged_a), str(session.staged_b),
                str(session.final_a), str(session.final_b),
                session.chunk_size, session.backend,
                int(session.verify_chunks), session.safety_margin, session.stage,
            ),
        )

    def has_session(self) -> bool:
        return self._conn.execute("SELECT 1 FROM session").fetchone() is not None

    def session(self) -> Session:
        row = self._conn.execute(
            "SELECT folder_a, folder_b, staged_a, staged_b, final_a, final_b,"
            " chunk_size, backend, verify_chunks, safety_margin, stage FROM session"
        ).fetchone()
        if row is None:
            raise RuntimeError("no session in this work dir; run a swap first")
        return Session(
            folder_a=Path(row[0]), folder_b=Path(row[1]),
            staged_a=Path(row[2]), staged_b=Path(row[3]),
            final_a=Path(row[4]), final_b=Path(row[5]),
            chunk_size=row[6], backend=row[7],
            verify_chunks=bool(row[8]), safety_margin=row[9], stage=row[10],
        )

    def advance_stage(self, new_stage: str) -> None:
        current = self.session().stage
        if STAGES.index(new_stage) <= STAGES.index(current):
            raise RuntimeError(f"cannot move stage backwards: {current} -> {new_stage}")
        self._conn.execute("UPDATE session SET stage = ?", (new_stage,))

    # -- entries -----------------------------------------------------------

    def replace_entries(self, entries: list[Entry]) -> None:
        """Atomically replace the whole inventory (used by the scanner)."""
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM entries")
            self._conn.executemany(
                "INSERT INTO entries (side, rel_path, kind, size, link_target, state)"
                " VALUES (?, ?, ?, ?, ?, 'pending')",
                [(e.side, e.rel_path, e.kind, e.size, e.link_target) for e in entries],
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def entries(self, *, kind: str | None = None, state: str | None = None) -> list[Entry]:
        sql = "SELECT id, side, rel_path, kind, size, link_target, state FROM entries"
        clauses, params = [], []
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

    def set_entry_state(self, entry_id: int, state: str) -> None:
        self._conn.execute("UPDATE entries SET state = ? WHERE id = ?", (state, entry_id))

    def counts_by_state(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) FROM entries GROUP BY state"
        ).fetchall()
        return dict(rows)
