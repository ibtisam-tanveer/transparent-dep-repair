"""Lift the single-file tool-calling agent (agent.py) to a whole
repository -- see TASK_repo_scale_agent.md.

This module adds no new discovery, environment-setup, or per-file repair
logic of its own: it orchestrates repo.py's existing discovery/env-setup
functions and agent.py's existing agent_repair() unchanged. Every file in
the repo runs in **one shared environment** (one venv, discovered and set
up exactly as repo.analyze_repo already does it) -- a package the agent
installs while fixing one file is then genuinely present for the next one,
which is correct and realistic for a repository, not a bug.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field

from . import repo as repo_module
from .agent import MAX_STEPS, AgentResult, agent_repair
from .interpreter import build_repair_environment
from .runner import RunResult, run_project
from . import workdir
from .venv_manager import get_fresh_repo_copy, get_fresh_venv_python, get_persistent_repo_copy, get_venv_python, venv_dir_for

DEFAULT_TIMEOUT = repo_module.DEFAULT_TIMEOUT


@dataclass
class RepoAgentResult:
    """Outcome of one agent_repair_repo() run: the shared-environment setup
    info (reused from repo.py unchanged) plus one AgentResult per
    discovered file. A file that already passed before any repair is
    attempted is recorded with `fixed=True` and an **empty trace** -- that
    empty trace is exactly how callers (including build_repo_report) tell
    "already passing" apart from "the agent fixed it".
    """

    repo_path: str
    dependency_files_found: list[str] = field(default_factory=list)
    dependency_file_used: str = ""
    env_setup_ok: bool = True
    env_setup_log: str = ""
    files: list[AgentResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    error: str = ""
    # TASK_interpreter_repair.md Part C: which interpreter the repo ran on and why
    # (requested/actual version, declared-vs-inferred source, mechanism).
    interpreter: dict = field(default_factory=dict)


def _empty_summary() -> dict:
    return {"total": 0, "already_passing": 0, "fixed": 0, "still_failing": 0}


def _build_summary(files: list[AgentResult]) -> dict:
    total = len(files)
    already_passing = sum(1 for f in files if f.fixed and not f.trace)
    fixed = sum(1 for f in files if f.fixed and f.trace)
    still_failing = total - already_passing - fixed
    return {"total": total, "already_passing": already_passing, "fixed": fixed, "still_failing": still_failing}


def agent_repair_repo(
    repo_path: str,
    max_steps_per_file: int = MAX_STEPS,
    timeout: int = DEFAULT_TIMEOUT,
    fresh: bool = False,
    repair_interpreter: bool = True,
) -> RepoAgentResult:
    """Repair every runnable file in `repo_path`, all inside one shared
    environment. Reuses repo.py entirely for discovery and dependency
    installation (find_dependency_files/_install_dependencies/
    discover_runnable_files) and agent.py's agent_repair() entirely for
    each failing file's actual repair -- nothing here reimplements either.

    Files are processed in repo.discover_runnable_files' existing stable,
    sorted order. No dependency-order inference is attempted between
    files: a package installed while fixing an earlier file is genuinely
    present for a later one (one repo, one shared environment, which is
    the whole point of repo scope), so file order can matter, and that's
    simply recorded as real data rather than something to engineer around.

    One bad file can never abort the whole run -- reuses repo.py's own
    per-file try/except discipline.

    `fresh=True` uses a brand-new, throwaway venv for the *whole repo* and
    fresh copies of every file, ignoring any cached venv for this repo --
    see agent.agent_repair's `fresh` docstring for why dataset evaluation
    must use this: the default (persistent, cached) mode would otherwise
    make a repo a prior run already fixed look like it needed no repair at
    all on a second pass.
    """
    if not os.path.isdir(repo_path):
        message = f"not a directory: {repo_path!r}"
        return RepoAgentResult(
            repo_path=repo_path, env_setup_ok=False, env_setup_log=message, summary=_empty_summary(), error=message
        )

    if fresh:
        tmp_root = tempfile.mkdtemp(prefix="repair_tool_agent_repo_fresh_")
        try:
            python_exe = get_fresh_venv_python(tmp_root)
            repo_copy_root = get_fresh_repo_copy(repo_path, tmp_root)
            return _run(repo_path, repo_copy_root, python_exe, max_steps_per_file, timeout,
                        env_dir=tmp_root, repair_interpreter=repair_interpreter)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    python_exe = get_venv_python(repo_path)  # repo.py's own shared-venv convention, persistent cache
    venv_dir = venv_dir_for(repo_path)  # the same directory get_venv_python just ensured exists
    repo_copy_root = get_persistent_repo_copy(repo_path, venv_dir)
    return _run(repo_path, repo_copy_root, python_exe, max_steps_per_file, timeout,
                env_dir=venv_dir, repair_interpreter=repair_interpreter)


def _run(
    repo_path: str, repo_copy_root: str, python_exe: str, max_steps_per_file: int, timeout: int,
    env_dir: str = "", repair_interpreter: bool = True,
) -> RepoAgentResult:
    # TASK_fix_repo_execution.md Part A: every file runs in place within
    # repo_copy_root (a whole-repo copy), never the original repo_path --
    # a local sibling import or relative-path data read needs its sibling
    # files physically present, exactly as a real user would have them.
    # TASK_version_decay_diagnosis.md Part B: caps an undeclared reactive
    # install to a version contemporary with this repo -- see
    # agent_tools.install_package. None means no era-capping.
    era_cutoff_date = repo_module.repo_last_commit_date(repo_copy_root)
    # TASK_interpreter_repair.md: pick/provision the interpreter *before* any
    # package install, so declared pins land on the interpreter they target.
    interpreter_env = build_repair_environment(
        repo_copy_root, os.path.join(env_dir or repo_copy_root, "interp"), python_exe,
        era_cutoff_date=era_cutoff_date, enabled=repair_interpreter and bool(env_dir),
    )
    python_exe = interpreter_env.python_exe
    dependency_files_found = repo_module.find_dependency_files(repo_copy_root)
    env_ok, env_log, used = repo_module._install_dependencies(
        python_exe, repo_copy_root, dependency_files_found, repo_module._INSTALL_TIMEOUT, era_date=era_cutoff_date
    )

    workdir.reset()  # resolutions are per repo copy
    file_results: list[AgentResult] = []
    for rel_path in repo_module.discover_runnable_files(repo_path):
        full_path = os.path.join(repo_copy_root, rel_path)
        try:
            initial = run_project(full_path, timeout=timeout, python_exe=python_exe, repo_root=repo_copy_root)
        except Exception as exc:  # noqa: BLE001 - one bad file must never abort the repo run
            initial = RunResult(ok=False, returncode=-1, stdout="", stderr=f"{type(exc).__name__}: {exc}")

        if initial.ok:
            file_results.append(AgentResult(
                target=rel_path, fixed=True, trace=[],
                working_directory=workdir.describe(full_path, repo_copy_root),
            ))
            continue

        try:
            file_result = agent_repair(
                rel_path, max_steps=max_steps_per_file, python_exe=python_exe, workspace_path=full_path,
                era_cutoff_date=era_cutoff_date, repo_root=repo_copy_root,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must never abort the repo run
            file_result = AgentResult(target=rel_path, fixed=False, error=workdir.describe_exception(exc))
        file_result.working_directory = workdir.describe(full_path, repo_copy_root)
        file_results.append(file_result)

    return RepoAgentResult(
        repo_path=repo_path,
        dependency_files_found=dependency_files_found,
        dependency_file_used=used,
        env_setup_ok=env_ok,
        env_setup_log=env_log,
        files=file_results,
        summary=_build_summary(file_results),
        interpreter=interpreter_env.meta,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Let an LLM agent repair every runnable file in a repository, in one shared environment."
    )
    parser.add_argument("repo", help="path to a local repo folder")
    parser.add_argument("--max-steps-per-file", type=int, default=MAX_STEPS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--fresh", action="store_true",
        help="use a brand-new venv/workspace for the whole repo instead of the cached one (required for dataset evaluation)",
    )
    parser.add_argument(
        "--no-interpreter-repair", action="store_true",
        help="do not provision the repo's declared/era Python; run on this tool's own interpreter",
    )
    parser.add_argument("--report", action="store_true", help="print a human-readable transparency report")
    args = parser.parse_args()

    result = agent_repair_repo(
        args.repo, max_steps_per_file=args.max_steps_per_file, timeout=args.timeout, fresh=args.fresh,
        repair_interpreter=not args.no_interpreter_repair,
    )

    if args.report:
        from .report import build_repo_report

        print(build_repo_report(result))
        return 0 if result.summary.get("still_failing", 1) == 0 else 1

    if result.error:
        print(f"ERROR: {result.error}")
        return 1

    print(f"Repo: {result.repo_path}")
    for file_result in result.files:
        label = "already passing" if file_result.fixed and not file_result.trace else (
            "FIXED" if file_result.fixed else "NOT FIXED"
        )
        print(f"  {label:16} {file_result.target}")
    print()
    print(f"Summary: {result.summary}")

    return 0 if result.summary.get("still_failing", 1) == 0 else 1


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
