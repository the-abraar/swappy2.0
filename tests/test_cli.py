"""Regression tests for CLI size parsing and preflight validation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swap import cli, uikit


@pytest.mark.parametrize("value, expected", [
    ("0", 0),
    ("12", 12),
    ("12B", 12),
    ("2KB", 2 * 1024),
    ("3MB", 3 * 1024**2),
    ("1GB", 1024**3),
    (" 2 kb ", 2 * 1024),
    ("4mb", 4 * 1024**2),
])
def test_parse_size_valid(value, expected):
    assert cli.parse_size(value) == expected


@pytest.mark.parametrize("value", ["", "-1", "1.5GB", "abc", "1TB", "12XB"])
def test_parse_size_invalid(value):
    with pytest.raises(SystemExit, match="invalid size"):
        cli.parse_size(value)


@pytest.mark.parametrize("value", ["0", "0KB", "0MB", "0GB"])
def test_run_rejects_zero_chunk_size_before_creating_work_dir(tmp_path, value):
    work_dir = tmp_path / "work"
    with pytest.raises(SystemExit, match="--chunk-size must be greater than 0"):
        cli.main([
            "run", "--folder-a", str(tmp_path / "a"),
            "--folder-b", str(tmp_path / "b"),
            "--work-dir", str(work_dir), "--chunk-size", value,
        ])
    assert not work_dir.exists()


@pytest.mark.parametrize("value", ["0", "0KB", "0MB"])
def test_ui_rejects_zero_chunk_size(tmp_path, value):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = uikit.SwapConfig(
        folder_a=str(a), folder_b=str(b), work_dir=str(tmp_path / "work"),
        chunk_size=value,
    )
    assert "--chunk-size must be greater than 0" in cfg.validate()


def test_ui_rejects_invalid_chunk_size(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = uikit.SwapConfig(
        folder_a=str(a), folder_b=str(b), work_dir=str(tmp_path / "work"),
        chunk_size="not-a-size",
    )
    assert "Chunk size must be a valid size." in cfg.validate()
