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

from repair_tool import repo as repo_module
from repair_tool.agent import AgentResult, agent_repair
from repair_tool.runner import RunResult, run_project
from repair_tool.venv_manager import get_fresh_venv_python, get_repo_file_workspace_copy

DEFAULT_TIMEOUT = repo_module.DEFAULT_TIMEOUT
# How much of the per-repo time budget one file gets, by default -- see
# EVALUATION_PILOT_SUMMARY.md for the reasoning (observed real sessions
# have needed up to ~30 agent steps / several hundred thousand tokens for
# one hard notebook).
DEFAULT_PER_FILE_BUDGET_SECONDS = 600


def count_discovered_files(repo_path: str) -> int:
    """How many runnable files repo.discover_runnable_files finds -- used
    by the harness to size this repo's time budget *before* committing to
    the full run, since budget scales with actual file count, not just
    the number of taxonomy-labelled notebooks (a repo can have more
    runnable files than labelled failures)."""
    return len(repo_module.discover_runnable_files(repo_path))


def run_repo_incrementally(
    repo_path: str,
    tmp_root: str,
    max_steps_per_file: int,
    budget_seconds: float,
    timeout: int = DEFAULT_TIMEOUT,
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
    python_exe = get_fresh_venv_python(tmp_root)

    dependency_files_found = repo_module.find_dependency_files(repo_path)
    env_ok, env_log, used = repo_module._install_dependencies(
        python_exe, repo_path, dependency_files_found, repo_module._INSTALL_TIMEOUT
    )
    repo_meta = {
        "env_setup_ok": env_ok,
        "env_setup_log": env_log,
        "dependency_file_used": used,
        "dependency_files_found": dependency_files_found,
    }

    start = time.monotonic()
    budget_exhausted = False

    for rel_path in repo_module.discover_runnable_files(repo_path):
        if not budget_exhausted and (time.monotonic() - start) >= budget_seconds:
            budget_exhausted = True

        if budget_exhausted:
            yield (
                AgentResult(
                    target=rel_path, fixed=False,
                    error=f"time budget exhausted ({budget_seconds:.0f}s for this repo) before this file was attempted",
                ),
                repo_meta,
            )
            continue

        full_path = os.path.join(repo_path, rel_path)
        try:
            initial = run_project(full_path, timeout=timeout, python_exe=python_exe)
        except Exception as exc:  # noqa: BLE001 - one bad file must never abort the repo run
            initial = RunResult(ok=False, returncode=-1, stdout="", stderr=f"{type(exc).__name__}: {exc}")

        if initial.ok:
            yield AgentResult(target=rel_path, fixed=True, trace=[]), repo_meta
            continue

        try:
            file_workspace = get_repo_file_workspace_copy(tmp_root, repo_path, rel_path)
            file_result = agent_repair(
                rel_path, max_steps=max_steps_per_file, python_exe=python_exe, workspace_path=file_workspace
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must never abort the repo run
            file_result = AgentResult(target=rel_path, fixed=False, error=f"{type(exc).__name__}: {exc}")
        yield file_result, repo_meta
