"""Run a target Python file in a separate process and capture the outcome.

Phase 1 of the dependency-repair loop: run -> read error -> (later phases)
propose fix -> apply -> re-verify -> explain. This module only implements
"run and capture" — a broken target must never crash this tool itself.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class RunResult:
    """Structured outcome of attempting to run a target Python file."""

    ok: bool
    returncode: int
    stdout: str
    stderr: str


def run_project(path: str, timeout: int = 60) -> RunResult:
    """Run the Python file at `path` in a subprocess and capture the result.

    Never raises: a missing file, a timeout, or a crashing target all come
    back as a failed RunResult instead of propagating an exception.
    """
    if not os.path.isfile(path):
        return RunResult(
            ok=False,
            returncode=-1,
            stdout="",
            stderr=f"FileNotFoundError: no such file: {path!r}",
        )

    try:
        completed = subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        stderr += f"\nTimeoutExpired: process exceeded {timeout}s and was killed"
        return RunResult(ok=False, returncode=-1, stdout=stdout, stderr=stderr)

    return RunResult(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a Python file as a subprocess and report the result."
    )
    parser.add_argument("path", help="path to the target .py file")
    parser.add_argument(
        "--timeout", type=int, default=60, help="seconds before the run is killed"
    )
    args = parser.parse_args()

    result = run_project(args.path, timeout=args.timeout)

    if result.ok:
        print(f"OK: {args.path} exited 0")
        if result.stdout:
            print("--- stdout ---")
            print(result.stdout)
    else:
        print(f"FAILED: {args.path} (returncode={result.returncode})")
        print("--- stderr ---")
        print(result.stderr)

    return 0 if result.ok else 1


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
