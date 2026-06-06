from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


@dataclass(frozen=True)
class Marker:
    name: str
    reason: str | None = None


class _MarkNamespace:
    def skip(self, *, reason: str) -> Marker:
        return Marker(name="skip", reason=reason)

    def __getattr__(self, name: str) -> Callable[..., Callable[[Callable[..., Any]], Callable[..., Any]]]:
        def decorator(*args: Any, **kwargs: Any):
            def apply(func: Callable[..., Any]) -> Callable[..., Any]:
                markers = list(getattr(func, "__pytest_markers__", []))
                markers.append(Marker(name=name, reason=kwargs.get("reason")))
                setattr(func, "__pytest_markers__", markers)
                return func

            if len(args) == 1 and callable(args[0]) and not kwargs:
                return apply(args[0])
            return apply

        return decorator


mark = _MarkNamespace()


def fixture(func: Callable[..., Any] | None = None, *, scope: str = "function"):
    def decorator(target: Callable[..., Any]) -> Callable[..., Any]:
        setattr(target, "__pytest_fixture__", True)
        setattr(target, "__pytest_fixture_scope__", scope)
        return target

    if func is None:
        return decorator
    return decorator(func)


class Parser:
    def __init__(self) -> None:
        self._defaults: dict[str, Any] = {"run_perf": False}

    def addoption(self, name: str, *, action: str, default: Any, help: str) -> None:
        normalized = name.lstrip("-").replace("-", "_")
        self._defaults[normalized] = default

    def parse_args(self, argv: list[str]) -> dict[str, Any]:
        options = dict(self._defaults)
        options["quiet"] = "-q" in argv or "--quiet" in argv
        options["run_perf"] = "--run-perf" in argv
        options["markexpr"] = _mark_expression(argv)
        return options


class Config:
    def __init__(self, options: dict[str, Any]) -> None:
        self._options = options
        self.ini_values: dict[str, list[str]] = {}

    def addinivalue_line(self, name: str, value: str) -> None:
        self.ini_values.setdefault(name, []).append(value)

    def getoption(self, name: str) -> Any:
        normalized = name.lstrip("-").replace("-", "_")
        return self._options[normalized]


class Item:
    def __init__(self, name: str, obj: Callable[..., Any], path: Path) -> None:
        self.name = name
        self.obj = obj
        self.path = path
        self.markers: list[Marker] = list(getattr(obj, "__pytest_markers__", []))
        self.keywords: set[str] = {marker.name for marker in self.markers}
        self.skip_reason: str | None = None

    def add_marker(self, marker: Marker) -> None:
        self.markers.append(marker)
        self.keywords.add(marker.name)
        if marker.name == "skip":
            self.skip_reason = marker.reason or "skipped"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    quiet = "-q" in args or "--quiet" in args

    root = Path.cwd()
    test_root = root / "tests"
    conftest = _load_module(test_root / "conftest.py", "conftest")

    parser = Parser()
    if hasattr(conftest, "pytest_addoption"):
        conftest.pytest_addoption(parser)

    options = parser.parse_args(args)
    quiet = quiet or options.get("quiet", False)
    config = Config(options)

    if hasattr(conftest, "pytest_configure"):
        conftest.pytest_configure(config)

    test_modules = [
        _load_module(path, f"test_{index}")
        for index, path in enumerate(_selected_test_paths(root, test_root, args), start=1)
    ]

    items = _collect_items(test_modules)
    if hasattr(conftest, "pytest_collection_modifyitems"):
        conftest.pytest_collection_modifyitems(config, items)

    items = _filter_mark_expression(items, options.get("markexpr", ""))
    outcomes = _run_items(items, conftest, quiet=quiet)
    passed = outcomes["passed"]
    failed = outcomes["failed"]
    skipped = outcomes["skipped"]

    print(_summary_line(passed, failed, skipped))

    return 0 if failed == 0 else 1


def _summary_line(passed: int, failed: int, skipped: int) -> str:
    parts: list[str] = []
    if passed:
        parts.append(f"{passed} passed")
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    if not parts:
        parts.append("no tests collected")
    return ", ".join(parts)


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _collect_items(modules: list[ModuleType]) -> list[Item]:
    items: list[Item] = []
    for module in modules:
        module_path = Path(module.__file__ or "")
        for name, obj in vars(module).items():
            if name.startswith("test_") and callable(obj):
                items.append(Item(name=name, obj=obj, path=module_path))
    return items


def _selected_test_paths(root: Path, test_root: Path, args: list[str]) -> list[Path]:
    selected = _path_args(args)
    if not selected:
        return sorted(test_root.glob("test_*.py"))

    paths: list[Path] = []
    for arg in selected:
        path = (root / arg).resolve()
        if path.is_dir():
            paths.extend(sorted(path.glob("test_*.py")))
        elif path.is_file():
            paths.append(path)
    return paths


def _path_args(args: list[str]) -> list[str]:
    selected: list[str] = []
    skip_next = False
    options_with_value = {"-m"}
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in options_with_value:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        selected.append(arg)
    return selected


def _mark_expression(args: list[str]) -> str:
    for index, arg in enumerate(args):
        if arg == "-m" and index + 1 < len(args):
            return args[index + 1]
    return ""


def _filter_mark_expression(items: list[Item], expression: str) -> list[Item]:
    expression = expression.strip()
    if not expression:
        return items
    if expression.startswith("not "):
        marker = expression[4:].strip()
        return [item for item in items if marker not in item.keywords]
    return [item for item in items if expression in item.keywords]


def _run_items(items: list[Item], conftest: ModuleType, *, quiet: bool) -> dict[str, int]:
    fixture_registry = _collect_fixtures(conftest)
    results = {"passed": 0, "failed": 0, "skipped": 0}

    for item in items:
        if item.skip_reason is not None:
            results["skipped"] += 1
            if not quiet:
                print(f"s {item.name} ({item.skip_reason})")
            continue

        try:
            kwargs = _resolve_arguments(item.obj, fixture_registry)
            item.obj(**kwargs)
        except Exception:
            results["failed"] += 1
            print(f"F {item.name}")
            traceback.print_exc()
        else:
            results["passed"] += 1
            if not quiet:
                print(f". {item.name}")

    return results


def _collect_fixtures(conftest: ModuleType) -> dict[str, Callable[..., Any]]:
    fixtures: dict[str, Callable[..., Any]] = {}
    for name, obj in vars(conftest).items():
        if callable(obj) and getattr(obj, "__pytest_fixture__", False):
            fixtures[name] = obj
    return fixtures


def _resolve_arguments(func: Callable[..., Any], fixtures: dict[str, Callable[..., Any]]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for name in inspect.signature(func).parameters:
        resolved[name] = _resolve_fixture(name, fixtures, resolved)
    return resolved


def _resolve_fixture(
    name: str,
    fixtures: dict[str, Callable[..., Any]],
    cache: dict[str, Any],
) -> Any:
    if name in cache:
        return cache[name]

    if name == "tmp_path":
        value = Path(tempfile.mkdtemp(prefix="swap-tests-"))
        cache[name] = value
        return value

    fixture_func = fixtures.get(name)
    if fixture_func is None:
        raise RuntimeError(f"unknown fixture: {name}")

    kwargs = {
        parameter_name: _resolve_fixture(parameter_name, fixtures, cache)
        for parameter_name in inspect.signature(fixture_func).parameters
    }
    value = fixture_func(**kwargs)
    cache[name] = value
    return value


if __name__ == "__main__":
    raise SystemExit(main())
