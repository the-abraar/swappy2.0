"""Regression tests for CLI size parsing and run-argument validation."""

from __future__ import annotations

import sys

import pytest

from swap import cli, uikit

SQLITE_MAX = 2**63 - 1


def _run(tmp_path, work_dir, *extra):
    cli.main([
        "run", "--folder-a", str(tmp_path / "a"),
        "--folder-b", str(tmp_path / "b"),
        "--work-dir", str(work_dir), *extra,
    ])


@pytest.fixture
def cfg(tmp_path):
    """A UI config whose folders exist, so only size fields can fail."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    return uikit.SwapConfig(
        folder_a=str(tmp_path / "a"), folder_b=str(tmp_path / "b"),
        work_dir=str(tmp_path / "work"),
    )


@pytest.mark.parametrize(("text", "expected"), [
    ("0", 0), ("12", 12), ("12B", 12),
    ("2KB", 2 * 1024), ("3MB", 3 * 1024**2), ("1GB", 1024**3),
    (" 2 kb ", 2 * 1024), ("4mb", 4 * 1024**2),
    (str(SQLITE_MAX), SQLITE_MAX),
    ("9007199254740991KB", 9007199254740991 * 1024),
])
def test_parse_size_valid(text, expected):
    assert cli.parse_size(text) == expected


@pytest.mark.parametrize("text", [
    "", "-1", "1.5GB", "abc", "1TB", "12XB",
    str(SQLITE_MAX + 1), "9007199254740992KB", "8589934592GB",
])
def test_parse_size_invalid(text):
    with pytest.raises(SystemExit, match="invalid size"):
        cli.parse_size(text)


def test_parse_size_rejects_digits_over_int_conversion_limit():
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if limit == 0:
        pytest.skip("no integer string-conversion limit on this Python")
    with pytest.raises(SystemExit, match="invalid size"):
        cli.parse_size("9" * (limit + 1))


@pytest.mark.parametrize("value", ["0", "0KB", "0MB", "0GB"])
def test_run_rejects_zero_chunk_size_before_creating_work_dir(tmp_path, value):
    work_dir = tmp_path / "work"
    with pytest.raises(SystemExit, match="--chunk-size must be greater than 0"):
        _run(tmp_path, work_dir, "--chunk-size", value)
    assert not work_dir.exists()


@pytest.mark.parametrize("option", ["--chunk-size", "--safety-margin"])
@pytest.mark.parametrize("value", ["invalid", str(SQLITE_MAX + 1), "8589934592GB"])
def test_run_rejects_bad_size_before_creating_work_dir(tmp_path, option, value):
    work_dir = tmp_path / "work"
    with pytest.raises(SystemExit, match="invalid size"):
        _run(tmp_path, work_dir, option, value)
    assert not work_dir.exists()


@pytest.mark.parametrize("value", ["0", "0KB", "0MB"])
def test_ui_rejects_zero_chunk_size(cfg, value):
    cfg.chunk_size = value
    assert "Chunk size must be greater than 0." in cfg.validate()


@pytest.mark.parametrize(("field", "message"), [
    ("chunk_size", "Chunk size must be a valid size."),
    ("safety_margin", "Safety margin must be a valid size."),
])
@pytest.mark.parametrize("value", ["not-a-size", str(SQLITE_MAX + 1)])
def test_ui_rejects_invalid_size(cfg, field, message, value):
    setattr(cfg, field, value)
    assert message in cfg.validate()


def test_ui_allows_zero_safety_margin(cfg):
    cfg.safety_margin = "0"
    assert cfg.validate() == []
