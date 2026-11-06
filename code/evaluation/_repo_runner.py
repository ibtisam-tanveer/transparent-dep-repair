"""Per-file repo-repair driver for the evaluation harness: incremental
per-file results and a file-count-scaled time budget.

Why this exists instead of just calling agent_repo.agent_repair_repo():
that function is all-or-nothing -- it returns one RepoAgentResult only
once *every* discovered file has been processed, and nothing is
observable, or recoverable if interrupted, before that. Found necessary
during the pilot smoke test: a fixed per-repo timeout killed an
11-notebook repo and discarded every one of its results, even though some
notebooks may have already finished -- and a fixed timeout is unfair
across repo sizes regardless (a 2-notebook repo and an 11-notebook repo
need very different budgets). This module fixes both: it yields one
(AgentResult, repo-level facts) pair per file as soon as that file
finishes, and the caller stops asking for more once a file-count-scaled
time budget runs out, with the remaining files recorded as cut off, not
silently lost.

Nothing in repair_tool/ is duplicated or reimplemented. This reuses
repo.py's and venv_manager's own primitives -- the exact same discovery/
dependency-install/fresh-venv functions agent_repo.py itself calls -- and
agent.agent_repair() per file, just re-sequenced with a per-file yield
instead of agent_repo.py's own all-at-once loop. The same justification
agent_repo.py gives for not calling repo.analyze_repo() as a black box
(REPO_AGENT_SUMMARY.md) applies one layer up here.
"""

from __future__ import annotations

import os
import time
from typing import Iterator

from repair_tool import era as era_module
from repair_tool import eradate
from repair_tool import levers as levers_module
from repair_tool import repo as repo_module
from repair_tool import workdir
from repair_tool.agent import DEFAULT_NO_PROGRESS_LIMIT, AgentResult, agent_repair
from repair_tool.interpreter import build_repair_environment
from repair_tool.runner import RunResult, run_project
from repair_tool.runtime import DEFAULT_PLATFORM, HOST_DESCRIPTION, ContainerError, DockerRuntime
from repair_tool.venv_manager import get_fresh_repo_copy, get_fresh_venv_python

DEFAULT_TIMEOUT = repo_module.DEFAULT_TIMEOUT
# How much of the per-repo time budget one file gets, by default -- see
# EVALUATION_PILOT_SUMMARY.md for the reasoning (observed real sessions
# have needed up to ~30 agent steps / several hundred thousand tokens for
# one hard notebook).
DEFAULT_PER_FILE_BUDGET_SECONDS = 600

# The fixed prefix of the error message below -- exported so run_eval.py's
# build_result_row can tell "never attempted, budget ran out" apart from a
# genuine repair failure without re-deriving this string (PILOT_DECISIONS.md #4).
BUDGET_EXHAUSTED_ERROR_PREFIX = "time budget exhausted"


def count_target_files(repo_path: str, target_paths) -> int:
    """TASK_prepilot_fixes.md #4: how many of the taxonomy-LABELLED
    notebooks (`target_paths`) repo.discover_runnable_files actually finds --
    this, not the repo's total runnable-file count, sizes the per-repo time
    budget. Previously the budget was sized by every runnable file while the
    worker also repaired every one of them (recording only the labelled
    rows), so a one-notebook target in a big repo got a repo-sized budget
    spent mostly on notebooks nobody asked about."""
    return len(set(repo_module.discover_runnable_files(repo_path)) & set(target_paths))


def count_discovered_files(repo_path: str) -> int:
    """How many runnable files repo.discover_runnable_files finds -- used
    by the harness to size this repo's time budget *before* committing to
    the full run, since budget scales with actual file count, not just
    the number of taxonomy-labelled notebooks (a repo can have more
    runnable files than labelled failures)."""
    return len(repo_module.discover_runnable_files(repo_path))


def _era_date_for(repo_path: str, repo_copy_root: str, only_paths, era_hints: dict | None) -> tuple[str | None, dict]:
    """The era date for this repository and the record of where it came from.

    Lever era_date: the notebook's true era (paper date / the file's own git history), see repair_tool/eradate.py.
    Otherwise the unchanged baseline -- the repo's last commit -- reported as such, so every row says what it used."""
    if levers_module.enabled(levers_module.ERA_DATE):
        info = eradate.resolve(repo_path, sorted(only_paths) if only_paths else None, era_hints)
        return info.date, info.as_meta()
    date = repo_module.repo_last_commit_date(repo_copy_root)
    return date, {"era_date_source": eradate.REPO_LAST if date else "none", "era_date_grounded": False,
                  "era_date_capped_by_last_commit": False, "era_date_candidates": {}, "era_date_note": ""}


def run_repo_incrementally(
    repo_path: str,
    tmp_root: str,
    max_steps_per_file: int,
    budget_seconds: float,
    timeout: int = DEFAULT_TIMEOUT,
    no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT,
    repair_interpreter: bool = True,
    only_paths=None,
    run_postbuild: bool = True,
    runtime: str = "host",
    platform: str = DEFAULT_PLATFORM,
    container_scale: float = 1.0,
    container_name: str | None = None,
    era_hints: dict | None = None,
) -> Iterator[tuple[AgentResult, dict]]:
    """Yield (AgentResult, repo_meta) one discovered file at a time, in
    repo.discover_runnable_files' existing stable order, as each one
    finishes -- so a caller writing each result out immediately never
    loses a file that already completed, however the run ends.

    `repo_meta` (env_setup_ok/env_setup_log/dependency_file_used/
    dependency_files_found) is computed once and repeated on every yield,
    exactly as agent_repo.RepoAgentResult carries it once for the whole
    repo. One shared *fresh* venv for the whole repo, at `tmp_root`
    (caller-owned lifecycle, same contract as venv_manager.get_fresh_venv_python) --
    this function always runs in fresh mode; the evaluation harness must
    never use the persistent cache (TASK_evaluation.md section 0.1).

    Once elapsed wall time passes `budget_seconds`, checked *between*
    files only (a file already in progress is never interrupted), every
    remaining file is yielded as a `fixed=False` AgentResult explaining
    why, instead of being attempted or silently dropped.
    """
    if runtime == "docker":
        # TASK_containerize.md: same loop, but the environment is built and
        # every executing action runs in a container (see repair_tool/runtime.py).
        yield from _run_repo_in_container(
            repo_path, tmp_root, max_steps_per_file, budget_seconds, timeout, no_progress_limit,
            repair_interpreter, only_paths, run_postbuild, platform, container_scale, container_name, era_hints,
        )
        return

    python_exe = get_fresh_venv_python(tmp_root)
    # TASK_fix_repo_execution.md Part A: run files in place within a fresh
    # whole-repo copy, not a stripped single-file workspace -- a local
    # sibling import (`import helper`) or a relative-path data read needs
    # its sibling files physically present, exactly as a real user running
    # the file in place would have. The original `repo_path` is never
    # touched (not even by the initial pass/fail check below).
    repo_copy_root = get_fresh_repo_copy(repo_path, tmp_root)

    # TASK_version_decay_diagnosis.md Part B: caps an undeclared reactive
    # install to a version contemporary with this repo instead of today's
    # latest -- see agent_tools.install_package. None (not a git repo, or
    # the lookup failed) means "no era-capping", same as before this task.
    era_cutoff_date, era_dating = _era_date_for(repo_path, repo_copy_root, only_paths, era_hints)

    # TASK_interpreter_repair.md: the interpreter is part of the dependency
    # configuration. Decided *before* any package install so the declared
    # pins land on the interpreter they were written for (numpy==1.17.3
    # cannot install on 3.14 at all). The default venv above is kept as the
    # baseline and returned unchanged whenever no better one is provisioned.
    interpreter_env = build_repair_environment(
        repo_copy_root, os.path.join(tmp_root, "interp"), python_exe,
        era_cutoff_date=era_cutoff_date, enabled=repair_interpreter,
    )
    python_exe = interpreter_env.python_exe

    dependency_files_found = repo_module.find_dependency_files(repo_copy_root)
    era_setup_metas: list = []
    # (lever era_resolve; empty record when the lever is off)
    env_ok, env_log, used = repo_module._install_dependencies(
        python_exe, repo_copy_root, dependency_files_found, repo_module._INSTALL_TIMEOUT,
        era_date=era_cutoff_date, era_meta_out=era_setup_metas,
    )
    # TASK_prepilot_fixes.md #4: repo2docker runs the repo's postBuild once
    # the environment exists; do the same (recorded, never fatal).
    postbuild = (
        repo_module.run_postbuild(repo_copy_root, python_exe, os.path.join(tmp_root, "postbuild_home"))
        if run_postbuild else {"found": repo_module.find_postbuild(repo_copy_root) is not None, "ran": False,
                               "ok": False, "path": "", "returncode": None, "log_tail": "disabled (--no-postbuild)"}
    )
    repo_meta = {
        "postbuild_found": postbuild["found"], "postbuild_ran": postbuild["ran"],
        "postbuild_ok": postbuild["ok"], "postbuild_log_tail": postbuild["log_tail"],
        "env_setup_ok": env_ok,
        "env_setup_log": env_log,
        "dependency_file_used": used,
        "dependency_files_found": dependency_files_found,
        "era_cutoff_date": era_cutoff_date,
        **era_dating,
        "era_resolution_setup": era_module.setup_record(era_cutoff_date, era_setup_metas, interpreter_env.meta),
        **interpreter_env.meta,
    }

    workdir.reset()  # resolutions are per repo copy
    start = time.monotonic()
    budget_exhausted = False

    files = repo_module.discover_runnable_files(repo_path)
    if only_paths is not None:
        # Only the labelled targets are run (and budgeted); see count_target_files.
        files = [f for f in files if f in only_paths]

    for rel_path in files:
        if not budget_exhausted and (time.monotonic() - start) >= budget_seconds:
            budget_exhausted = True

        if budget_exhausted:
            yield (
                AgentResult(
                    target=rel_path, fixed=False,
                    error=f"{BUDGET_EXHAUSTED_ERROR_PREFIX} ({budget_seconds:.0f}s for this repo) before this file was attempted",
                ),
                repo_meta,
            )
            continue

        # Runs in place within repo_copy_root, never the original repo_path
        # -- sibling files/relative paths resolve exactly as they would for
        # a real user (TASK_fix_repo_execution.md Part A).
        full_path = os.path.join(repo_copy_root, rel_path)
        try:
            initial = run_project(full_path, timeout=timeout, python_exe=python_exe, repo_root=repo_copy_root)
        except Exception as exc:  # noqa: BLE001 - one bad file must never abort the repo run
            initial = RunResult(ok=False, returncode=-1, stdout="", stderr=f"{type(exc).__name__}: {exc}")

        if initial.ok:
            yield AgentResult(
                target=rel_path, fixed=True, trace=[],
                working_directory=workdir.describe(full_path, repo_copy_root),
            ), repo_meta
            continue

        try:
            file_result = agent_repair(
                rel_path, max_steps=max_steps_per_file, python_exe=python_exe, workspace_path=full_path,
                no_progress_limit=no_progress_limit, era_cutoff_date=era_cutoff_date, repo_root=repo_copy_root,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must never abort the repo run
            file_result = AgentResult(target=rel_path, fixed=False, error=workdir.describe_exception(exc))
        file_result.working_directory = workdir.describe(full_path, repo_copy_root)
        yield file_result, repo_meta


def _container_failure_result(rel_path: str, message: str) -> AgentResult:
    return AgentResult(target=rel_path, fixed=False, error=message)


def _run_repo_in_container(
    repo_path: str, tmp_root: str, max_steps_per_file: int, budget_seconds: float, timeout: int,
    no_progress_limit: int, repair_interpreter: bool, only_paths, run_postbuild: bool, platform: str,
    container_scale: float, container_name: str | None, era_hints: dict | None = None,
) -> Iterator[tuple[AgentResult, dict]]:
    """`--runtime docker`: the host keeps the loop and the LLM calls; the
    container builds the environment and runs every executing action."""
    repo_copy_root = get_fresh_repo_copy(repo_path, tmp_root)
    era_cutoff_date, era_dating = _era_date_for(repo_path, repo_copy_root, only_paths, era_hints)
    files = repo_module.discover_runnable_files(repo_path)
    if only_paths is not None:
        files = [f for f in files if f in only_paths]

    rt = DockerRuntime(platform=platform, timeout_scale=container_scale, container_name=container_name)
    try:
        try:
            rt.start(repo_copy_root)
            setup = rt.setup(era_cutoff_date, repair_interpreter, run_postbuild)
        except ContainerError as exc:
            # The container could not be provided at all -- say so on every
            # file instead of pretending the tool ran (analysis reports this
            # as its own bucket, not as a repair failure).
            meta = {**HOST_DESCRIPTION, **rt.describe(), "container_error": str(exc)[:500], "env_setup_ok": False}
            for rel_path in files:
                yield _container_failure_result(rel_path, f"container unavailable: {exc}"), meta
            return

        postbuild = setup["postbuild"]
        repo_meta = {
            **rt.describe(),
            "postbuild_found": postbuild["found"], "postbuild_ran": postbuild["ran"],
            "postbuild_ok": postbuild["ok"], "postbuild_log_tail": postbuild["log_tail"],
            "env_setup_ok": setup["env_setup_ok"], "env_setup_log": setup["env_setup_log"],
            "dependency_file_used": setup["dependency_file_used"],
            "dependency_files_found": setup["dependency_files_found"],
            "era_cutoff_date": era_cutoff_date, "container_setup_seconds": setup["setup_seconds"], **era_dating,
            "era_resolution_setup": setup.get("era_resolution_setup", {}),
            **setup["interpreter_meta"],
        }
        python_exe = setup["python_exe"]

        start = time.monotonic()
        budget_exhausted = False
        for rel_path in files:
            if not budget_exhausted and (time.monotonic() - start) >= budget_seconds:
                budget_exhausted = True
            if budget_exhausted:
                yield (
                    AgentResult(
                        target=rel_path, fixed=False,
                        error=f"{BUDGET_EXHAUSTED_ERROR_PREFIX} ({budget_seconds:.0f}s for this repo) before this file was attempted",
                    ),
                    repo_meta,
                )
                continue

            full_path = os.path.join(repo_copy_root, rel_path)
            initial = rt.run_project(rel_path, timeout=timeout, python_exe=python_exe)
            if initial.ok:
                yield AgentResult(target=rel_path, fixed=True, trace=[], working_directory=rt.workdir_describe(rel_path)), repo_meta
                continue

            try:
                file_result = agent_repair(
                    rel_path, max_steps=max_steps_per_file, python_exe=python_exe, workspace_path=full_path,
                    no_progress_limit=no_progress_limit, era_cutoff_date=era_cutoff_date, tool_runtime=rt,
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must never abort the repo run
                file_result = AgentResult(target=rel_path, fixed=False, error=workdir.describe_exception(exc))
            file_result.working_directory = rt.workdir_describe(rel_path)
            rt.pull(rel_path, full_path)  # sync the (possibly edited) file back to the host copy
            yield file_result, repo_meta
    finally:
        rt.close()
