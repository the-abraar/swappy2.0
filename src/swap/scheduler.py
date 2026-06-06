"""The copy-ordering heuristic. Pure functions — no I/O, no database.

Given the pending whole-file copies and the current free space per side,
pick the next file to copy, or return None to signal a deadlock (no
whole-file copy fits anywhere; the transfer loop then falls back to the
sparse chunk engine).

Heuristic, in priority order:
  1. Only consider copies whose destination keeps `required_headroom`
     bytes free after the copy lands.
  2. Prefer copies whose SOURCE sits on the constrained side (the side
     with the least free space) — completing them frees that side.
  3. Among those, prefer the smallest file: quick wins free space sooner
     and keep options open.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CopyCandidate:
    entry_id: int
    size: int
    src_side: str   # 'A' or 'B' — where the bytes currently live
    dst_side: str   # the other side — where they are going


def choose_next_copy(
    candidates: list[CopyCandidate],
    free: dict[str, int],          # side -> free bytes on that side's volume
    *,
    required_headroom: int,        # safety margin + chunk reserve
) -> CopyCandidate | None:
    """The next whole-file copy to run, or None if nothing fits (deadlock)."""
    fitting = [
        c for c in candidates
        if free[c.dst_side] - c.size >= required_headroom
    ]
    if not fitting:
        return None

    constrained_side = min(free, key=lambda side: free[side])

    def priority(c: CopyCandidate) -> tuple[int, int, int]:
        frees_constrained = 0 if c.src_side == constrained_side else 1
        return (frees_constrained, c.size, c.entry_id)

    return min(fitting, key=priority)
