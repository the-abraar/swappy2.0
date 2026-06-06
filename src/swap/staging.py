"""Pipeline stage 2: move the original roots aside, create empty final roots.

    D:\\Games\\GameA  ->  D:\\Games\\.swap_staged_a    (rename, instant)
    E:\\Games\\GameB  ->  E:\\Games\\.swap_staged_b
    create empty      D:\\Games\\GameB  and  E:\\Games\\GameA

Every step is guarded by an existence check, so re-running after a crash
simply skips whatever already happened.
"""

from __future__ import annotations

from pathlib import Path

from .db import Database


def stage_roots(db: Database) -> None:
    session = db.session()
    if session.stage != "created":
        return  # already staged

    _rename_once(session.folder_a, session.staged_a)
    _rename_once(session.folder_b, session.staged_b)

    session.final_a.mkdir(parents=True, exist_ok=True)
    session.final_b.mkdir(parents=True, exist_ok=True)

    db.advance_stage("staged")


def _rename_once(original: Path, staged: Path) -> None:
    if staged.exists():
        return  # renamed on a previous run
    if not original.is_dir():
        raise RuntimeError(f"expected directory missing: {original}")
    original.rename(staged)
