# Repository Guidelines

## Project Structure & Module Organization
This repository is a small Python CLI with a `src/` layout. Main code lives in `src/swap/`; start with `src/swap/pipeline.py`, which runs the five stages (`preflight`, `staging`, `scanner`, `transfer`, `cleanup`). Durable state is centralized in `src/swap/db.py`. The CLI entry point is `src/swap/cli.py`, and Windows-specific filesystem helpers live in `src/swap/winfs.py`.

Tests live in `tests/` and follow the same module split, for example `tests/test_pipeline.py` and `tests/test_scheduler.py`. Repository notes such as `README.md`, `plan.md`, and `unequal-size-exchange.md` describe the architecture and constraints. Generated artifacts under `__pycache__/` and `src/swap.egg-info/` should not be edited by hand.

## Build, Test, and Development Commands
- `python -m pip install -e .` installs the package in editable mode.
- `python -m swap run --folder-a D:\A --folder-b E:\B --work-dir D:\.swap_work` starts a swap session.
- `python -m swap resume --work-dir D:\.swap_work` resumes an interrupted run.
- `python -m swap status --work-dir D:\.swap_work` prints session progress.
- `python pytest.py -q` runs the full test suite through the repo's lightweight pytest shim.

## Coding Style & Naming Conventions
Use Python 3.11+ features already present in the codebase: `Path`, dataclasses, and explicit type hints. Follow the existing style: 4-space indentation, snake_case for functions and modules, CapWords for dataclasses, and short module docstrings where they add orientation. Keep stage logic idempotent and prefer small, single-purpose functions over cross-module coupling. No formatter or linter is configured in `pyproject.toml`, so match the surrounding file style closely.

## Testing Guidelines
Add tests under `tests/test_*.py`, with test functions named `test_*`. Prefer targeted unit tests for scheduler and DB behavior, plus end-to-end pipeline tests using the fake-drive helpers in `tests/conftest.py`. Cover crash-safety, resumability, and low-space edge cases whenever behavior changes.

## Commit & Pull Request Guidelines
This workspace snapshot does not include `.git`, so local history is unavailable. Use short imperative commit subjects scoped to one change, for example `scanner: reject unsupported reparse points`. PRs should explain the user-visible behavior change, list filesystem or recovery invariants affected, and include the exact test command run. For CLI or workflow changes, include a sample command line and note any Windows-only assumptions.
