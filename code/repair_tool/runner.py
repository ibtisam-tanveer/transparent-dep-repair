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

from .proc import run_group


@dataclass
class RunResult:
    """Structured outcome of attempting to run a target Python file."""

    ok: bool
    returncode: int
    stdout: str
    stderr: str
    # TASK_prepilot_fixes.md #3: the working directory the run actually used
    # and why ("file_dir" by default; see workdir.py). Empty on results that
    # never reached a run (missing file etc.).
    cwd: str = ""
    cwd_source: str = ""
    # TASK_next_steps.md step 1: for a failed NOTEBOOK run, how far it got --
    # "last started code cell K of N" -- and the head of the source of that
    # cell, so "timed out" can be told apart as "stuck in cell 2 of 40" vs
    # "ran 38 cells then a slow one". Empty for scripts / passing runs.
    progress: str = ""
    stuck_cell: str = ""


def run_project(
    path: str, timeout: int = 60, python_exe: str | None = None, repo_root: str | None = None,
) -> RunResult:
    """Run the Python file at `path` in a subprocess and capture the result.

    `python_exe` selects the interpreter to run it with (e.g. an isolated
    venv's python from Phase 3's venv_manager); defaults to the current
    interpreter, so Phase 1/2 callers are unaffected.

    `repo_root` (set when running inside a whole-repo copy) enables working-
    directory resolution: if the run fails importing a module the repo itself
    provides, it is retried from the directory the file's relative sys.path
    entries expect -- see workdir.py.

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

    if repo_root:
        from . import workdir

        return workdir.run_with_resolution(
            path, repo_root, lambda cwd: _run_once(path, timeout, python_exe, cwd)
        )
    return _run_once(path, timeout, python_exe, None)


def _run_once(path: str, timeout: int, python_exe: str | None, cwd: str | None) -> RunResult:
    """One execution of `path` from `cwd` (None = the file's own directory)."""

    # The only branch notebook support adds here -- see NOTEBOOK_SUPPORT_TASK.md.
    # Everything below is the unchanged .py subprocess path.
    if path.endswith(".ipynb"):
        from .notebook import run_notebook

        if cwd is None:
            return run_notebook(path, timeout=timeout, python_exe=python_exe)
        return run_notebook(path, timeout=timeout, python_exe=python_exe, cwd=cwd)

    executable = python_exe or sys.executable

    # TASK_fix_repo_execution.md: run with cwd set to the file's own
    # directory, matching nbconvert's default behaviour for notebooks --
    # a relative-path data read (not just a sibling import, which Python
    # already resolves via sys.path[0]) needs this to behave like a real
    # user running the file in place.
    try:
        # run_group: own process group, killed as a whole on timeout, so a
        # script that spawned children can't outlive its budget or hold the
        # output pipes open (TASK_prepilot_fixes.md #1).
        from . import condaenv

        completed = run_group(
            [executable, path],
            env=condaenv.activation_env(executable),
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd or os.path.dirname(os.path.abspath(path)),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        stderr += f"\nTimeoutExpired: process exceeded {timeout}s and was killed"
        return RunResult(ok=False, returncode=-1, stdout=stdout, stderr=stderr)
    except OSError as exc:
        # e.g. `executable` doesn't exist or isn't runnable -- same
        # never-raise contract as the timeout/missing-file cases above.
        return RunResult(ok=False, returncode=-1, stdout="", stderr=f"OSError: could not run {executable!r}: {exc}")

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
