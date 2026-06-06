"""Pipeline stage 3: inventory both staged trees into the database.

Records every directory, file, and symlink with its relative path and size.
Junctions and other reparse points are refused up front (nothing has been
copied or deleted yet, so refusing here is safe).

The scan replaces the whole inventory atomically, so a crash mid-scan just
means the next run scans again.
"""

from __future__ import annotations

import os
from pathlib import Path

from .db import Database, Entry

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class UnsupportedEntry(ValueError):
    pass


def scan(db: Database) -> None:
    session = db.session()
    if session.stage != "staged":
        return  # already scanned (or not yet staged)

    entries = [
        *_scan_tree("A", session.staged_a),
        *_scan_tree("B", session.staged_b),
    ]
    db.replace_entries(entries)
    db.advance_stage("scanned")


def _scan_tree(side: str, root: Path) -> list[Entry]:
    entries: list[Entry] = []
    _scan_dir(side, root, root, entries)
    return entries


def _scan_dir(side: str, root: Path, directory: Path, entries: list[Entry]) -> None:
    for child in sorted(os.scandir(directory), key=lambda c: c.name.casefold()):
        path = Path(child.path)
        rel_path = path.relative_to(root).as_posix()
        kind = _classify(child, path)
        size = child.stat(follow_symlinks=False).st_size if kind == "file" else None
        target = os.readlink(path) if kind == "symlink" else None
        entries.append(Entry(
            id=0,  # assigned by the database
            side=side, rel_path=rel_path, kind=kind,
            size=size, link_target=target, state="pending",
        ))
        if kind == "dir":
            _scan_dir(side, root, path, entries)


def _classify(child: os.DirEntry[str], path: Path) -> str:
    if child.is_symlink():
        return "symlink"
    if _is_reparse_point(child):
        raise UnsupportedEntry(
            f"unsupported reparse point (junction?): {path} — remove or"
            " exclude it, then run again"
        )
    if child.is_dir(follow_symlinks=False):
        return "dir"
    if child.is_file(follow_symlinks=False):
        return "file"
    raise UnsupportedEntry(f"unsupported entry type: {path}")


def _is_reparse_point(child: os.DirEntry[str]) -> bool:
    attributes = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
