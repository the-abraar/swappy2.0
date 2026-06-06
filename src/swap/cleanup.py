"""Pipeline stage 5: remove the now-empty staged trees and finish the session.

By this point every file has been moved (sources were deleted one by one as
each transfer completed) and every symlink dropped, so the staged trees
contain only empty directories. Anything else is a logic error and aborts
loudly rather than deleting data.
"""

from __future__ import annotations

import os
from pathlib import Path

from .db import Database


def finalize(db: Database) -> None:
    session = db.session()
    if session.stage != "transferred":
        return  # already finished (or transfer not done yet)

    _remove_empty_tree(session.staged_a)
    _remove_empty_tree(session.staged_b)

    db.advance_stage("done")


def _remove_empty_tree(root: Path) -> None:
    if not root.exists():
        return  # removed by a previous run
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        if filenames:
            raise RuntimeError(
                f"staged data left behind in {dirpath}: {filenames};"
                " refusing to delete"
            )
        os.rmdir(dirpath)
