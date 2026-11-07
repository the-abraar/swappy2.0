"""Regression tests for CLI size parsing and preflight validation.

Written for the project's lightweight pytest.py runner (no parametrize or raises).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swap import cli, uikit


def assert_exits_with_message(callback, message):
    try:
        callback()
    except SystemExit as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError(f"expected SystemExit containing {message!r}")


def test_parse_size_valid():
    cases = [
        ("0", 0), ("12", 12), ("12B", 12),
        ("2KB", 2 * 1024), ("3MB", 3 * 1024**2),
        ("1GB", 1024**3), (" 2 kb ", 2 * 1024),
        ("4mb", 4 * 1024**2),
    ]
    for value, expected in cases:
        assert cli.parse_size(value) == expected


def test_parse_size_invalid():
    for value in ("", "-1", "1.5GB", "abc", "1TB", "12XB"):
        assert_exits_with_message(lambda value=value: cli.parse_size(value), "invalid size")


def test_run_rejects_zero_chunk_size_before_creating_work_dir(tmp_path):
    for value in ("0", "0KB", "0MB", "0GB"):
        work_dir = tmp_path / ("work-" + value)
        def run():
            cli.main([
                "run", "--folder-a", str(tmp_path / "a"),
                "--folder-b", str(tmp_path / "b"),
                "--work-dir", str(work_dir), "--chunk-size", value,
            ])
        assert_exits_with_message(run, "--chunk-size must be greater than 0")
        assert not work_dir.exists()


def test_ui_rejects_zero_chunk_size(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for value in ("0", "0KB", "0MB"):
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


def test_run_rejects_invalid_safety_margin_before_creating_work_dir(tmp_path):
    work_dir = tmp_path / "work"
    def run():
        cli.main([
            "run", "--folder-a", str(tmp_path / "a"),
            "--folder-b", str(tmp_path / "b"),
            "--work-dir", str(work_dir), "--safety-margin", "invalid",
        ])
    assert_exits_with_message(run, "invalid size")
    assert not work_dir.exists()


def test_ui_validates_safety_margin_and_allows_zero(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = uikit.SwapConfig(
        folder_a=str(a), folder_b=str(b), work_dir=str(tmp_path / "work"),
        safety_margin="invalid",
    )
    assert "Safety margin must be a valid size." in cfg.validate()
    cfg.safety_margin = "0"
    assert cfg.validate() == []

def test_oversized_size_is_rejected_by_cli_and_ui(tmp_path):
    """Huge numeric strings must produce validation errors, not ValueError."""
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    if get_limit is None or get_limit() == 0:
        return  # Python versions without the integer conversion limit
    oversized = "9" * (get_limit() + 1)
    assert_exits_with_message(lambda: cli.parse_size(oversized), "invalid size")

    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    work_dir = tmp_path / "work"
    cfg = uikit.SwapConfig(
        folder_a=str(folder_a), folder_b=str(folder_b),
        work_dir=str(work_dir), safety_margin=oversized,
    )
    assert "Safety margin must be a valid size." in cfg.validate()
    assert_exits_with_message(
        lambda: cli.main([
            "run", "--folder-a", str(folder_a), "--folder-b", str(folder_b),
            "--work-dir", str(work_dir), "--safety-margin", oversized,
        ]),
        "invalid size",
    )
    assert not work_dir.exists()


def test_sqlite_size_limit_for_chunk_and_safety_margin(tmp_path):
    """Both CLI size options must fit SQLite signed 64-bit integers."""
    limit = 2**63 - 1
    assert cli.parse_size(str(limit)) == limit
    assert cli.parse_size("9007199254740991KB") == 9007199254740991 * 1024
    for value in (str(limit + 1), "9007199254740992KB", "8589934592GB"):
        assert_exits_with_message(lambda value=value: cli.parse_size(value), "invalid size")

        for option in ("--chunk-size", "--safety-margin"):
            work_dir = tmp_path / ("work-" + option[2:] + "-" + str(len(value)))
            assert_exits_with_message(
                lambda option=option, value=value, work_dir=work_dir: cli.main([
                    "run", "--folder-a", str(tmp_path / "a"),
                    "--folder-b", str(tmp_path / "b"),
                    "--work-dir", str(work_dir), option, value,
                ]),
                "invalid size",
            )
            assert not work_dir.exists()

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for field, message in (
        ("chunk_size", "Chunk size must be a valid size."),
        ("safety_margin", "Safety margin must be a valid size."),
    ):
        cfg = uikit.SwapConfig(folder_a=str(a), folder_b=str(b), work_dir=str(tmp_path / "work"))
        setattr(cfg, field, str(limit + 1))
        assert message in cfg.validate()
