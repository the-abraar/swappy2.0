"""The top-level execution pipeline. Start reading the codebase here.

Each stage reads everything it needs from the database, does its work, and
advances `session.stage` when finished. Every stage is idempotent: running
an already-completed stage is a no-op. That makes `run` and `resume` the
same call — resuming after a crash just re-runs the pipeline and each
stage skips ahead or redoes a small amount of duplicate work.

To disable a stage, comment out its line below.
"""

from __future__ import annotations

from . import cleanup, preflight, scanner, staging, transfer
from .db import ROLLBACK_STAGES, Database
from .preflight import FreeBytes, VolumeId


def run(
    db: Database,
    *,
    volume_id: VolumeId | None = None,    # injectable for tests
    free_bytes: FreeBytes | None = None,  # injectable for tests
) -> None:
    if db.session().stage in ROLLBACK_STAGES:
        raise RuntimeError(
            "this session is being rolled back; use `swap rollback` (forward"
            " resume would silently no-op through every stage)"
        )
    preflight.check(db, volume_id=volume_id, free_bytes=free_bytes)
    staging.stage_roots(db)
    scanner.scan(db)
    transfer.transfer_all(db, free_bytes=free_bytes)
    cleanup.finalize(db)
