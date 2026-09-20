"""Repo-level foundation: take a whole repository, set up one shared
isolated environment for it, run every file it contains, and collect what
happened. This does NOT repair anything — see REPO_FOUNDATION_TASK.md.
It's the front end every later version of the repo-scale work (repair,
hybrid classical+LLM, agentic orchestration) needs underneath it, so it's
safe to build before those are decided.

Reuses the single-file engine entirely unchanged: runner.run_project
already dispatches to notebook.run_notebook for .ipynb, diagnose.py
classifies whatever comes back, and venv_manager.get_venv_python happens
to work for a repo path too (it just hashes whatever path string it's
given) -- so "one shared venv for the whole repo" needed no changes there.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

from .diagnose import Diagnosis, diagnose_result
from .runner import RunResult, run_project
from .venv_manager import get_venv_python

DEFAULT_TIMEOUT = 60
_INSTALL_TIMEOUT = 300  # matches apply.DEFAULT_TIMEOUT's reasoning

# Priority order per REPO_FOUNDATION_TASK.md. Only the first *installable*
# one found is actually used -- see _install_dependencies.
DEPENDENCY_FILENAMES = ["requirements.txt", "pyproject.toml", "setup.py", "environment.yml", "Pipfile"]

# Directories never descended into: VCS/cache/venv noise, never a real entrypoint.
_EXCLUDED_DIRS = {".git", "__pycache__", ".venv", "venv", ".repair_venvs", "node_modules", ".ipynb_checkpoints"}


@dataclass
class FileResult:
    """One discovered file's outcome. `path` is relative to the repo root
    for readable reporting; `diagnosis` is only set when the run failed."""

    path: str
    run_result: RunResult
    diagnosis: Diagnosis | None = None


@dataclass
class RepoResult:
    repo_path: str
    dependency_files_found: list[str] = field(default_factory=list)
    dependency_file_used: str = ""  # which one was actually installed from, if any
    env_setup_ok: bool = True
    env_setup_log: str = ""
    files: list[FileResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    # Deliberately not a fixed bool -- see REPO_FOUNDATION_TASK.md: whether a
    # repo counts as "passing" is a decision the supervisor may set later.
    # None means no pass_rule was supplied to analyze_repo(); the per-file
    # results and `summary` counts are always available regardless.
    passed: bool | None = None


def _is_excluded_dir(name: str) -> bool:
    return name in _EXCLUDED_DIRS or name.startswith(".")


def discover_runnable_files(repo_path: str) -> list[str]:
    """`.py` and `.ipynb` files under repo_path, excluding hidden/venv/cache
    directories and obvious non-entrypoints (setup.py itself, test files).
    Returns paths relative to repo_path, sorted for deterministic output.
    Kept intentionally simple: over-filtering is worse than running a few
    extra files, per the task's own guidance.
    """
    found = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not _is_excluded_dir(d)]
        for fname in files:
            if fname == "setup.py":
                continue
            if fname.startswith("test_") or fname.endswith("_test.py") or fname == "conftest.py":
                continue
            if not (fname.endswith(".py") or fname.endswith(".ipynb")):
                continue
            full = os.path.join(root, fname)
            found.append(os.path.relpath(full, repo_path))
    return sorted(found)


def find_dependency_files(repo_path: str) -> list[str]:
    """Which of the known dependency-declaration filenames exist at the
    repo root, in priority order (not necessarily the order they'll be
    used in -- see _install_dependencies for what's actually installable).
    """
    return [name for name in DEPENDENCY_FILENAMES if os.path.isfile(os.path.join(repo_path, name))]


def _pyproject_declares_a_package(path: str) -> bool:
    """Many pyproject.toml files are just tool config (ruff/black/pytest
    settings) with nothing to install. Only [project] or [tool.poetry]
    sections mean `pip install .` has something to do."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return False
    return "[project]" in content or "[tool.poetry]" in content


def _run_pip_install(python_exe: str, args: list[str], timeout: int) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [python_exe, "-m", "pip", "install", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"pip install timed out after {timeout}s"
    return completed.returncode == 0, completed.stdout + completed.stderr


def _install_dependencies(
    python_exe: str, repo_path: str, found: list[str], timeout: int
) -> tuple[bool, str, str]:
    """Install from the highest-priority *installable* format found.

    Not every declared-dependency format is pip-installable on its own:
    requirements.txt and a package-declaring pyproject.toml/setup.py are;
    environment.yml (conda) and Pipfile (pipenv) are not, without tooling
    this project doesn't carry. Returns (ok, log, which_file_was_used) --
    `which_file_was_used` is "" if nothing was found or nothing found was
    installable, which is reported honestly, not silently ignored or
    treated as a hard failure (a repo without a recognisable dependency
    file may still run fine, or may fail with a missing-dependency error --
    that's real data either way, per the task's own guidance).
    """
    if not found:
        return True, "no dependency declaration files found; nothing to install", ""

    for name in found:
        full = os.path.join(repo_path, name)

        if name == "requirements.txt":
            ok, log = _run_pip_install(python_exe, ["-r", full], timeout)
            return ok, log, name

        if name in ("pyproject.toml", "setup.py"):
            if name == "pyproject.toml" and not _pyproject_declares_a_package(full):
                continue  # tool-config-only; not installable, try the next candidate
            ok, log = _run_pip_install(python_exe, [repo_path], timeout)
            return ok, log, name

        # environment.yml / Pipfile: recognised but not pip-installable here.
        continue

    return (
        True,
        f"found {found} but none are pip-installable without extra tooling "
        "(conda/pipenv); skipped installation",
        "",
    )


def _build_summary(files: list[FileResult]) -> dict:
    total = len(files)
    ran = sum(1 for f in files if f.run_result.ok)
    failures_by_kind: dict[str, int] = {}
    for f in files:
        if f.diagnosis is not None:
            failures_by_kind[f.diagnosis.kind] = failures_by_kind.get(f.diagnosis.kind, 0) + 1
    return {"total": total, "ran": ran, "failed": total - ran, "failures_by_kind": failures_by_kind}


def analyze_repo(
    repo_path: str,
    timeout: int = DEFAULT_TIMEOUT,
    pass_rule: Callable[["RepoResult"], bool] | None = None,
) -> RepoResult:
    """Run and diagnose every file in `repo_path`, inside one shared
    isolated venv. Never raises, and never lets one bad file abort the
    rest: a crash while processing a single file is captured as that
    file's own failed result, not propagated. Never modifies the repo's
    original files -- everything here is read-only against the repo,
    all mutation happens inside the isolated venv.
    """
    if not os.path.isdir(repo_path):
        return RepoResult(
            repo_path=repo_path,
            env_setup_ok=False,
            env_setup_log=f"not a directory: {repo_path!r}",
            summary={"total": 0, "ran": 0, "failed": 0, "failures_by_kind": {}},
        )

    dependency_files_found = find_dependency_files(repo_path)
    python_exe = get_venv_python(repo_path)  # one shared venv for the whole repo
    env_ok, env_log, used = _install_dependencies(python_exe, repo_path, dependency_files_found, _INSTALL_TIMEOUT)

    file_results: list[FileResult] = []
    for rel_path in discover_runnable_files(repo_path):
        full_path = os.path.join(repo_path, rel_path)
        try:
            run_result = run_project(full_path, timeout=timeout, python_exe=python_exe)
            diagnosis = None if run_result.ok else diagnose_result(run_result)
        except Exception as exc:  # noqa: BLE001 - one bad file must never abort the repo scan
            run_result = RunResult(ok=False, returncode=-1, stdout="", stderr=f"{type(exc).__name__}: {exc}")
            diagnosis = diagnose_result(run_result)
        file_results.append(FileResult(path=rel_path, run_result=run_result, diagnosis=diagnosis))

    result = RepoResult(
        repo_path=repo_path,
        dependency_files_found=dependency_files_found,
        dependency_file_used=used,
        env_setup_ok=env_ok,
        env_setup_log=env_log,
        files=file_results,
        summary=_build_summary(file_results),
    )

    if pass_rule is not None:
        try:
            result.passed = bool(pass_rule(result))
        except Exception:  # noqa: BLE001 - a bad pass_rule must not crash the analysis either
            result.passed = None

    return result


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up a shared environment for a repo, run every file in it, "
        "and report per-file results (diagnosis only -- no repair at this stage)."
    )
    parser.add_argument("repo_path", help="path to the repo folder")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    result = analyze_repo(args.repo_path, timeout=args.timeout)

    print(f"Repo: {result.repo_path}")
    print(f"Dependency files found: {result.dependency_files_found or 'none'}")
    if result.dependency_file_used:
        print(f"Installed from: {result.dependency_file_used}")
    print(f"Env setup ok: {result.env_setup_ok}")
    if result.env_setup_log:
        print(f"  {result.env_setup_log.strip().splitlines()[-1] if result.env_setup_log.strip() else ''}")
    print()

    for f in result.files:
        status = "OK" if f.run_result.ok else "FAILED"
        label = f" ({f.diagnosis.kind})" if f.diagnosis else ""
        print(f"  {status:6} {f.path}{label}")

    s = result.summary
    print()
    print(f"Summary: {s['ran']}/{s['total']} ran, {s['failed']} failed")
    if s["failures_by_kind"]:
        print(f"  failures by kind: {s['failures_by_kind']}")

    return 0


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
