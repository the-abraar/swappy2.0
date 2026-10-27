"""Tests for the shared UI kit (swap.uikit): config validation, command
building, formatters, and read-only progress polling against a real session."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import FakeDrives, make_session
from swap import pipeline, uikit
from swap.db import Database


# ---------------------------------------------------------------------------
# formatters
# ---------------------------------------------------------------------------

def test_format_bytes_scales_units():
    assert uikit.format_bytes(0) == "0 B"
    assert uikit.format_bytes(512) == "512 B"
    assert uikit.format_bytes(1536) == "1.5 KB"
    assert uikit.format_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert uikit.format_bytes(None) == "—"


def test_format_rate_and_seconds():
    assert uikit.format_rate(None) == "—"
    assert uikit.format_rate(0) == "—"
    assert uikit.format_rate(1024 * 1024).endswith("/s")
    assert uikit.format_seconds(5) == "5s"
    assert uikit.format_seconds(125) == "2m 05s"
    assert uikit.format_seconds(3725) == "1h 02m 05s"
    assert uikit.format_seconds(None) == "—"


def test_short_path_truncates_from_the_left():
    assert uikit.short_path(None) == "—"
    assert uikit.short_path("a/b.txt", 40) == "a/b.txt"
    long = "x" * 80
    out = uikit.short_path(long, 20)
    assert len(out) == 20 and out.startswith("…") and out.endswith("x")


# ---------------------------------------------------------------------------
# config validation
# ---------------------------------------------------------------------------

def test_validate_reports_missing_fields():
    problems = uikit.SwapConfig().validate()
    assert any("Folder A" in p for p in problems)
    assert any("Folder B" in p for p in problems)
    assert any("Work" in p for p in problems)


def test_validate_rejects_same_folder(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    cfg = uikit.SwapConfig(folder_a=str(d), folder_b=str(d), work_dir=str(tmp_path / "w"))
    assert any("must be different" in p for p in cfg.validate())


def test_validate_passes_for_good_config(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = uikit.SwapConfig(folder_a=str(a), folder_b=str(b),
                           work_dir=str(tmp_path / "w"), workers=4, backend="python")
    assert cfg.validate() == []


def test_validate_rejects_bad_worker_count(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = uikit.SwapConfig(folder_a=str(a), folder_b=str(b),
                           work_dir=str(tmp_path / "w"), workers=0)
    assert any("Workers" in p for p in cfg.validate())


# ---------------------------------------------------------------------------
# command building
# ---------------------------------------------------------------------------

def test_run_argv_contains_all_options():
    cfg = uikit.SwapConfig(folder_a="A", folder_b="B", work_dir="W",
                           chunk_size="8KB", safety_margin="0", workers=3,
                           backend="python", verify_chunks=False,
                           volume_mode="simulate-distinct")
    argv = uikit.run_argv(cfg)
    assert argv[1:4] == ["-m", "swap", "run"]
    assert "--folder-a" in argv and "A" in argv
    assert "--no-verify-chunks" in argv
    assert "simulate-distinct" in argv
    assert "--workers" in argv and "3" in argv


def test_verify_flag_omitted_when_enabled():
    cfg = uikit.SwapConfig(folder_a="A", folder_b="B", work_dir="W", verify_chunks=True)
    assert "--no-verify-chunks" not in uikit.run_argv(cfg)


def test_resume_rollback_status_argv():
    assert uikit.resume_argv("W")[1:] == ["-m", "swap", "resume", "--work-dir", "W"]
    assert uikit.rollback_argv("W")[1:] == ["-m", "swap", "rollback", "--work-dir", "W"]
    assert uikit.status_argv("W")[1:] == ["-m", "swap", "status", "--work-dir", "W"]


# ---------------------------------------------------------------------------
# progress reader
# ---------------------------------------------------------------------------

def test_progress_reader_missing_session(tmp_path):
    snap = uikit.ProgressReader(tmp_path / "nope").read()
    assert snap.exists is False
    assert snap.fraction == 0.0
    assert snap.eta_seconds is None


def test_progress_reader_reads_completed_session(tmp_path):
    # Build two fake drives and run a complete swap, then read it back.
    base = tmp_path / "drives"
    drive_a = base / "A"; drive_b = base / "B"
    drive_a.mkdir(parents=True); drive_b.mkdir(parents=True)
    folder_a = drive_a / "fa"; folder_b = drive_b / "fb"
    folder_a.mkdir(); folder_b.mkdir()
    (folder_a / "one.bin").write_bytes(b"x" * 40)
    (folder_b / "two.bin").write_bytes(b"y" * 60)

    drives = FakeDrives(drive_a, drive_b, cap_a=10_000, cap_b=10_000)
    work = tmp_path / "work"; work.mkdir()
    db = Database(work / "swap.db")
    drives.bind_db(db)
    db.create_session(make_session(folder_a=folder_a, folder_b=folder_b))
    pipeline.run(db, volume_id=drives.volume_id, free_bytes=drives.free_bytes)
    db.close()

    snap = uikit.ProgressReader(work).read()
    assert snap.exists is True
    assert snap.stage == "done"
    assert snap.stage_label == "Done"
    assert snap.is_finished is True
    assert snap.counts.get("done", 0) == 2
    assert 0.0 <= snap.fraction <= 1.0


def test_tui_subcommand_without_work_dir_does_not_reparse_sys_argv():
    """`swap tui` must hand tui.main an explicit [] (None re-reads sys.argv, which holds 'tui')."""
    from swap import cli, tui

    seen = []
    original = tui.main
    tui.main = lambda argv=None: seen.append(argv) or 0
    try:
        assert cli.main(["tui"]) == 0
    finally:
        tui.main = original
    assert seen == [[]]
