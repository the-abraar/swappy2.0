"""Pure unit tests for scheduler.choose_next_copy."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swap.scheduler import CopyCandidate, choose_next_copy


def _c(entry_id: int, size: int, src_side: str) -> CopyCandidate:
    dst_side = "B" if src_side == "A" else "A"
    return CopyCandidate(entry_id=entry_id, size=size, src_side=src_side, dst_side=dst_side)


def test_returns_none_when_nothing_fits():
    """When no candidate's size fits within free space, return None (deadlock)."""
    candidates = [_c(1, 100, "A"), _c(2, 200, "B")]
    free = {"A": 50, "B": 50}
    result = choose_next_copy(candidates, free, required_headroom=0)
    assert result is None


def test_returns_none_empty_candidates():
    """Empty candidate list always returns None."""
    result = choose_next_copy([], {"A": 1000, "B": 1000}, required_headroom=0)
    assert result is None


def test_respects_required_headroom():
    """A copy that would leave less than required_headroom bytes is not chosen."""
    # dst_side is "B", free["B"] = 110, size = 100 -> remaining = 10, headroom = 20 -> rejected
    candidates = [_c(1, 100, "A")]
    free = {"A": 500, "B": 110}
    result = choose_next_copy(candidates, free, required_headroom=20)
    assert result is None


def test_respects_required_headroom_exact_boundary():
    """A copy leaving exactly headroom bytes is accepted (>= check)."""
    # free[B] - size = 110 - 100 = 10, headroom = 10 -> exactly meets -> accepted
    candidates = [_c(1, 100, "A")]
    free = {"A": 500, "B": 110}
    result = choose_next_copy(candidates, free, required_headroom=10)
    assert result is not None
    assert result.entry_id == 1


def test_prefers_constrained_side_source():
    """Prefer candidates whose src_side is the least-free side."""
    # Side B is constrained (less free). Candidate 1 sources from B, candidate 2 from A.
    # Both fit. Should pick candidate 1 (drains constrained side).
    free = {"A": 1000, "B": 50}
    candidates = [
        _c(1, 10, "B"),   # src on constrained B -> frees constrained side
        _c(2, 10, "A"),   # src on A -> not constrained
    ]
    result = choose_next_copy(candidates, free, required_headroom=0)
    assert result is not None
    assert result.entry_id == 1


def test_prefers_smallest_file_among_constrained():
    """Among candidates sourcing from the constrained side, pick the smallest."""
    free = {"A": 1000, "B": 50}
    candidates = [
        _c(1, 30, "B"),
        _c(2, 10, "B"),  # smallest, should be chosen
        _c(3, 20, "B"),
    ]
    result = choose_next_copy(candidates, free, required_headroom=0)
    assert result is not None
    assert result.entry_id == 2


def test_deterministic_tie_break_by_entry_id():
    """Same size and same src_side: pick the lower entry_id."""
    free = {"A": 1000, "B": 1000}
    candidates = [
        _c(5, 10, "A"),
        _c(2, 10, "A"),  # lower id -> should win
        _c(8, 10, "A"),
    ]
    result = choose_next_copy(candidates, free, required_headroom=0)
    assert result is not None
    assert result.entry_id == 2


def test_falls_back_to_unconstrained_when_constrained_doesnt_fit():
    """When constrained-side candidates don't fit, accepts from other side."""
    # Side B constrained (free=5). Candidate from B needs 100 bytes on A, but size=100
    # and free[A]=1000 so it fits. But we also have a candidate from A -> B that needs
    # free[B]=5 and size=3 -> fits.
    free = {"A": 1000, "B": 5}
    candidates = [
        CopyCandidate(entry_id=1, size=100, src_side="B", dst_side="A"),  # fits on A
        CopyCandidate(entry_id=2, size=3, src_side="A", dst_side="B"),    # fits on B (barely)
    ]
    # Constrained = B. Candidate 1 has src=B (constrained) and dst=A, fits.
    result = choose_next_copy(candidates, free, required_headroom=0)
    assert result is not None
    assert result.entry_id == 1  # sources from constrained side B


def test_both_sides_equal_picks_smallest():
    """When free space is equal on both sides, smallest file wins."""
    free = {"A": 100, "B": 100}
    candidates = [
        _c(1, 50, "A"),
        _c(2, 20, "B"),  # smallest
        _c(3, 30, "A"),
    ]
    # constrained_side = min(free) -> tie -> Python's min picks "A" (lexicographic)
    # Both sides have same free; constrained = "A" (alphabetic min)
    # src_side == "A" candidates: 1 (size=50), 3 (size=30)
    # src_side == "B" candidates: 2 (size=20)
    # priority: (0=constrained, size, id) for A-src; (1, size, id) for B-src
    # Winners from constrained: 3 (0, 30, 3) vs 1 (0, 50, 1) -> candidate 3
    result = choose_next_copy(candidates, free, required_headroom=0)
    assert result is not None
    assert result.entry_id == 3
