"""Dev entry points: uv run test / lint / typecheck / check.

Console-script shims declared in pyproject [project.scripts]; each shells
out to the underlying CLI from the same environment. Dev convenience
only — not runtime code (Constitution, Asynchrony boundary: one-off
utilities are outside the runtime).
"""

from __future__ import annotations

import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

SCOPE_ROOTS = ("src", "tests", "alembic")


def _call(module: str, args: list[str]) -> int:
    return int(subprocess.call([sys.executable, "-m", module, *args]))


def _step(module: str, args: list[str]) -> int:
    """Run one CLI step: echo the command, stream its output, report status."""
    command = " ".join([module, *args])
    print(f"== {command} ==", flush=True)
    started = time.perf_counter()
    code = _call(module, args)
    seconds = time.perf_counter() - started
    status = "ok" if code == 0 else f"FAILED (exit {code})"
    print(f"-- {command}: {status} in {seconds:.1f}s\n", flush=True)
    return code


def _tool_version(module: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", module, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or result.stderr).strip() or f"{module} ?"


def _scope_file_count() -> int:
    return sum(1 for root in SCOPE_ROOTS for _ in Path(root).rglob("*.py"))


def test() -> None:
    """Contract suite with full per-test output."""
    raise SystemExit(_call("pytest", ["-v"]))


def lint() -> None:
    """ruff check + format check."""
    code = _step("ruff", ["check", "."])
    if code == 0:
        code = _step("ruff", ["format", "--check", "."])
    raise SystemExit(code)


def typecheck() -> None:
    """mypy --strict over the project's code roots."""
    raise SystemExit(_step("mypy", list(SCOPE_ROOTS)))


def check() -> None:
    """One gate: environment summary, then ruff (check + format) and mypy.

    Steps run in order and short-circuit on the first failure.
    """
    try:
        project_version = version("gram-mq")
    except PackageNotFoundError:
        project_version = "?"
    print(
        f"gram-mq {project_version} · Python {sys.version.split()[0]} "
        f"({sys.executable})",
        flush=True,
    )
    print(f"{_tool_version('ruff')} · {_tool_version('mypy')}", flush=True)
    roots = ", ".join(SCOPE_ROOTS)
    print(f"scope: {_scope_file_count()} python files in {roots}\n", flush=True)

    steps: list[tuple[str, list[str]]] = [
        ("ruff", ["check", "."]),
        ("ruff", ["format", "--check", "."]),
        ("mypy", list(SCOPE_ROOTS)),
    ]
    started = time.perf_counter()
    code = 0
    done = 0
    for module, args in steps:
        done += 1
        code = _step(module, args)
        if code != 0:
            break
    total = time.perf_counter() - started
    if code == 0:
        print(f"OK: {done}/{len(steps)} gates passed in {total:.1f}s", flush=True)
    else:
        print(f"FAILED: gate {done}/{len(steps)} after {total:.1f}s", flush=True)
    raise SystemExit(code)
