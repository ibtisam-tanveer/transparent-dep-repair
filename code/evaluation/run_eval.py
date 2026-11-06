"""Evaluation harness -- TASK_evaluation.md section 2.

Given the taxonomy-labelled failure set (dataset/failures_2023.csv), clone
each sampled repository, repair it file by file (reusing agent.agent_repair()
and repo.py's discovery/env-setup -- see _repo_runner.py), and record one
result row per taxonomy-labelled notebook, as soon as that notebook's own
result is known. Resumable/checkpointed (a crash or Ctrl-C doesn't lose
already-recorded repos), cost/time-aware (--sample, --limit-minutes,
--max-llm-calls, --per-file-budget-minutes), and cleans up every clone
after recording its result.

Does not add repair capability -- it only orchestrates repo.py/agent.py
exactly as they already exist. If a result looks wrong because the engine
mis-repairs something, that's a finding for EVALUATION_PILOT_SUMMARY.md,
not something to patch in here.

Each repo's repair runs in an isolated subprocess (see _repo_worker.py), so
a time budget can actually kill a stuck/slow repo instead of just hoping
it finishes. Two real findings from the pilot smoke test drove this
module's design:

1. A *fixed* per-repo timeout is unfair across repo sizes -- an
   11-notebook repository and a 2-notebook one need very different
   budgets. The budget is instead scaled by how many runnable files the
   repo actually has (--per-file-budget-minutes * file count), computed
   right after cloning, before the subprocess is even started.
2. A timeout used to discard *everything* for that repo, including
   notebooks that had already finished successfully, because results were
   only written once, at the very end. _repo_worker.py now streams one
   result per file as it completes (_repo_runner.run_repo_incrementally),
   so a cut-off repo keeps whatever it already finished.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataset.taxonomy import classify as classify_taxonomy
from evaluation._repo_runner import (
    BUDGET_EXHAUSTED_ERROR_PREFIX,
    DEFAULT_PER_FILE_BUDGET_SECONDS,
    count_target_files,
)
from repair_tool import diagnose as diagnose_module
from repair_tool import pypi
from repair_tool.agent import DEFAULT_NO_PROGRESS_LIMIT, TraceStep
from repair_tool.diagnose import _find_exception_line
from repair_tool import era as era_module
from repair_tool import levers as levers_module
from repair_tool.localmods import find_local_module
from evaluation.runtime_reasons import classify_runtime_reason
from repair_tool.proc import REGISTRY_ENV, reap_registered, run_group
from repair_tool.runtime import DEFAULT_PLATFORM, docker_available, remove_container

DEFAULT_FAILURES_CSV = "dataset/failures_2023.csv"
DEFAULT_RESULTS_DIR = "evaluation/results"
# 120s was too short for large data-science repos: e-stan/covid_19_analysis is
# ~800 MB even shallow and failed at 120s on every smoke run, silently
# removing 35 notebooks from the sample (they were recorded as clone failures).
CLONE_TIMEOUT = 900
WORKER_MODULE = "evaluation._repo_worker"
# Extra time allowed, on top of the computed per-repo budget, before the
# OS-level subprocess kill fires -- a backstop against one single file's
# agent_repair() call hanging past its own internal bounds (shouldn't
# happen given MAX_STEPS/the OpenAI client timeout, but this is cheap
# insurance); the *graceful* stop between files is what normally applies
# the budget, not this hard kill.
SUBPROCESS_KILL_BUFFER_SECONDS = 300
# Interpreter provisioning (a conda solve and/or a CPython download) happens
# inside the worker *before* the per-repo budget clock starts but *inside*
# the OS-level kill timeout -- without this allowance a slow build is killed
# and the repo's finished results are lost to a backstop that was meant for
# hung agent calls (TASK_interpreter_repair.md).
INTERPRETER_SETUP_ALLOWANCE_SECONDS = 900
# TASK_containerize.md: --runtime docker adds image/container start, copying the
# repo in, and the environment build (conda solves are slow under emulation).
CONTAINER_SETUP_ALLOWANCE_SECONDS = 3600
# TASK_improve_fix_rate.md: a downgrade search re-runs the project once per candidate version and
# an interpreter step-down rebuilds the whole environment, so lever runs need a longer hard backstop.
LEVER_ALLOWANCE_SECONDS = 3600

# TASK_evaluation.md section 0.2: the default MAX_STEPS=10 cap caused a
# substantively-complete fix (install + two separate removed-API edits) to
# report fixed=False one tool call short of the budget (see
# REPO_AGENT_SUMMARY.md / PROVENANCE_REPORT_SUMMARY.md's test-reliability
# finding). Real notebooks fail in many layers -- a missing package often
# reveals a removed-API error underneath once installed, sometimes more
# than once. 30 gives roughly 3x headroom over the single-file cases
# observed so far (max seen: 7 steps for a 2-round fix), while still
# capping a stuck agent's cost per notebook.
DEFAULT_MAX_STEPS_PER_FILE = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# PILOT_DECISIONS.md #3: the real pilot is drawn only from the confirmed,
# in-scope dependency-caused categories -- the failures the tool is actually
# meant to fix. B is unobservable from the static labels (never appears at
# any tier) and E is explicitly out of scope (missing data, GPU, credentials).
#
# PILOT_DECISIONS.md #3 says confirmed tier only. Checked against the real
# dataset.csv: category A has 5,562 confirmed rows, but C and D have ZERO
# confirmed rows -- only candidate (94 and 85 respectively). A literal
# confirmed-only filter would silently turn "sample A/C/D" into an A-only
# pilot, leaving #4's per-category breakdown unable to say anything about C
# or D at all. Deviation, confirmed with the user: keep A at confirmed tier
# (where real confirmed data exists) and allow C/D at candidate tier too,
# since candidate is the only tier they have -- every other PILOT_DECISIONS.md
# #3 restriction (A/C/D only, category E and B excluded) still applies.
IN_SCOPE_TIERS_BY_CATEGORY: dict[str, set[str]] = {
    "A": {"confirmed"},
    "C": {"confirmed", "candidate"},
    "D": {"confirmed", "candidate"},
}


def load_failures_by_repo(csv_path: str, in_scope_confirmed_only: bool = False) -> dict[str, list[dict]]:
    """repository -> list of its taxonomy-labelled failure rows.

    `in_scope_confirmed_only` restricts this to rows in IN_SCOPE_TIERS_BY_CATEGORY
    -- per PILOT_DECISIONS.md #3 (as adjusted above for C/D's missing
    confirmed tier), for drawing the real pilot's sample. Off by default so
    the smoke test and other ad-hoc runs are unaffected.
    """
    by_repo: dict[str, list[dict]] = defaultdict(list)
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if in_scope_confirmed_only and row["tier"] not in IN_SCOPE_TIERS_BY_CATEGORY.get(row["category"], set()):
                continue
            by_repo[row["repository"]].append(row)
    return by_repo


def load_checkpoint(path: str) -> set[str]:
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_done(checkpoint_path: str, repository: str) -> None:
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(repository + "\n")


CLONE_ATTEMPTS = 3
CLONE_RETRY_DELAYS_SECONDS = (5, 20)
# A clone failure that retrying cannot fix (the repo is gone / private); anything else
# (connection reset, early EOF, DNS) is treated as transient.
_PERMANENT_CLONE_FAILURE_MARKERS = (
    "not found", "does not exist", "could not read username", "authentication failed", "access denied",
    "repository access blocked", "dmca",
)


def clone_repo(clone_url: str, timeout: int = CLONE_TIMEOUT, full_history: bool = False) -> tuple[str | None, str]:
    """Clone `clone_url` into a fresh temp dir. Returns (path_or_None, log).
    Never raises -- a bad/missing/private repo comes back as (None, log),
    same contract as repo.analyze_repo_url's own clone step.

    A transient network failure ("Connection reset by peer", "early EOF" --
    seen when two clones ran at once, in the first docker pilot) is retried
    with a short backoff instead of being recorded as a repository that
    "could not be cloned"; a permanent one (gone / private) is not."""
    log = ""
    for attempt in range(CLONE_ATTEMPTS):
        tmpdir = tempfile.mkdtemp(prefix="repair_tool_eval_clone_")
        try:
            completed = run_group(
                # lever era_date needs the history (the notebook file's own dates); every other run stays shallow.
                ["git", "clone", *([] if full_history else ["--depth", "1"]), clone_url, tmpdir],
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None, f"git clone timed out after {timeout}s"
        except OSError as exc:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None, f"could not run git: {exc}"

        if completed.returncode == 0:
            return tmpdir, "cloned ok" if attempt == 0 else f"cloned ok (attempt {attempt + 1})"

        shutil.rmtree(tmpdir, ignore_errors=True)
        log = f"git clone failed: {(completed.stdout + completed.stderr).strip()[:500]}"
        if any(marker in log.lower() for marker in _PERMANENT_CLONE_FAILURE_MARKERS):
            return None, log
        if attempt < CLONE_ATTEMPTS - 1:
            time.sleep(CLONE_RETRY_DELAYS_SECONDS[min(attempt, len(CLONE_RETRY_DELAYS_SECONDS) - 1)])
    return None, log + f" (after {CLONE_ATTEMPTS} attempts)"


def _read_jsonl_rows_tolerantly(path: str | None) -> list[dict]:
    """Read whatever complete JSON lines are in `path`. A subprocess
    killed by a timeout can leave its last line half-written -- that one
    line is dropped (via a per-line try/except), everything written
    before it is kept. Never raises."""
    rows: list[dict] = []
    if not path or not os.path.isfile(path):
        return rows
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    break  # an incomplete trailing line from a kill mid-write
    except OSError:
        pass
    return rows


def run_repo_worker(
    repository: str,
    clone_url: str,
    clone_path: str,
    failure_rows: list[dict],
    run_id: str,
    max_steps_per_file: int,
    budget_seconds: float,
    timeout_seconds: int,
    no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT,
    repair_interpreter: bool = True,
    run_postbuild: bool = True,
    runtime: str = "host",
    platform: str = DEFAULT_PLATFORM,
    container_scale: float = 1.0,
    container_name: str | None = None,
) -> tuple[list[dict], str]:
    """Run one repo's repair in an isolated subprocess (_repo_worker.py,
    which streams one result row per file as it finishes), enforcing
    `timeout_seconds` as a hard OS-level backstop on top of the worker's
    own graceful `budget_seconds` stop between files.

    Returns (rows, status). `rows` is whatever was actually written before
    the worker returned, timed out, or crashed -- on a timeout or crash
    this can be a *partial* list (every file that finished before the
    cut-off), never discarded wholesale. `status` is "ok", or an
    explanation the caller uses as the `error` for any labelled notebook
    `rows` doesn't cover. Never raises.
    """
    input_path = None
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(
                {
                    "repository": repository, "clone_url": clone_url, "clone_path": clone_path,
                    "failure_rows": failure_rows, "run_id": run_id, "max_steps_per_file": max_steps_per_file,
                    "budget_seconds": budget_seconds, "no_progress_limit": no_progress_limit,
                    "repair_interpreter": repair_interpreter, "run_postbuild": run_postbuild,
                    "runtime": runtime, "platform": platform, "container_scale": container_scale,
                    "container_name": container_name,
                },
                f,
            )
            input_path = f.name
        output_fd, output_path = tempfile.mkstemp(suffix=".jsonl")
        os.close(output_fd)
        registry_fd, registry_path = tempfile.mkstemp(suffix=".kernels")
        os.close(registry_fd)

        # TASK_prepilot_fixes.md #1: the worker runs in its own process
        # group and a timeout kills that whole group (not just the worker
        # process, which used to leave grandchildren holding the output
        # pipes open and hang this call). Notebook kernels are started in
        # their own sessions by jupyter_client, so the worker also records
        # their process groups in a registry file; whatever is still alive
        # there once the worker is gone -- including after a SIGKILL that
        # gave it no chance to clean up -- is reaped below.
        worker_env = dict(os.environ, **{REGISTRY_ENV: registry_path})
        try:
            completed = run_group(
                [sys.executable, "-m", WORKER_MODULE, "--input", input_path, "--output", output_path],
                text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds, env=worker_env,
            )
        except subprocess.TimeoutExpired:
            return _read_jsonl_rows_tolerantly(output_path), f"timed out after {timeout_seconds}s"
        except OSError as exc:
            return [], f"could not run worker: {exc}"
        finally:
            reap_registered(registry_path)
            if os.path.isfile(registry_path):
                os.remove(registry_path)
            if runtime == "docker":
                # A killed worker cannot clean up its own container.
                remove_container(container_name or "")

        if completed.returncode != 0:
            tail = (completed.stdout + completed.stderr).strip()[-1000:]
            return _read_jsonl_rows_tolerantly(output_path), f"worker exited {completed.returncode}: {tail}"

        return _read_jsonl_rows_tolerantly(output_path), "ok"
    finally:
        for path in (input_path, output_path):
            if path and os.path.isfile(path):
                os.remove(path)


def _trace_step_summary(step: TraceStep) -> dict:
    return {
        "tool": step.tool_called,
        "grounding": step.grounding,
        "verification": step.verification,
        "confidence": step.confidence,
        # TASK_version_decay_diagnosis.md Part B: which version an install
        # actually used and why (era_capped/unconstrained) -- "" for a
        # non-install step, where these keys don't exist on tool_result.
        "resolved_package": step.tool_result.get("resolved_package", ""),
        "version_source": step.tool_result.get("version_source", ""),
        # TASK_prepilot_fixes.md #2: an install the guard refused because the
        # repo ships its own module of that name.
        "refused": bool(step.tool_result.get("refused")),
        "refusal_reason": str(step.tool_result.get("refusal_reason", "") or ""),
        # lever era_strict: what the agent was told (the spec itself is in the message), so a reviewer can see what was refused
        "refusal_detail": (str(step.tool_result.get("log", ""))[:220] if step.tool_result.get("refusal_reason") == "era_window" else ""),
        # TASK_improve_fix_rate.md: which lever acted in this step and what it did, in one line.
        "lever": _lever_of(step),
        "lever_detail": _lever_detail(step),
        # What an edit_code step actually changed (find -> replace, truncated): a "fix" that makes a
        # notebook pass by deleting the part that failed is not a real repair, so a reader must be
        # able to see the edit, not just that one happened.
        "edit_preview": _edit_preview(step),
        # lever era_resolve: how this install was resolved (mechanism, date, fallback) -- {} when off.
        "era_resolution": step.tool_result.get("era_resolution") or {},
    }


def _era_agent_summary(trace: list[TraceStep]) -> dict:
    if not (levers_module.enabled(levers_module.ERA_RESOLVE) or levers_module.enabled(levers_module.ERA_STRICT)):
        return {}
    metas = [s.tool_result.get("era_resolution") or {} for s in trace if s.tool_called == "install_package"]
    out = era_module.summarize(metas)
    out["strict_refusals"] = sum(1 for s in trace if s.tool_result.get("refusal_reason") == "era_window")
    out["strict_resolver_refusals"] = sum(1 for m in metas if m.get("strict_refused"))
    return out


def _edit_preview(step: TraceStep, max_edits: int = 5, max_chars: int = 240) -> list[dict]:
    if step.tool_called != "edit_code":
        return []
    edits = step.tool_input.get("edits") if isinstance(step.tool_input, dict) else None
    if not isinstance(edits, list):
        return []
    return [
        {"find": str(e.get("find", ""))[:max_chars], "replace": str(e.get("replace", ""))[:max_chars]}
        for e in edits[:max_edits] if isinstance(e, dict)
    ]


def _first_failure_error(trace: list[TraceStep]) -> str:
    """The error line of the FIRST failing run -- the problem the tool was asked to fix. Kept
    for fixed notebooks too (it is the 'why' of the repair); '' if the notebook passed untouched."""
    for step in trace:
        if step.tool_called in ("run_target", "verify") and not step.tool_result.get("ok"):
            stderr = str(step.tool_result.get("stderr", ""))
            line = _find_exception_line(stderr) or next((l for l in reversed(stderr.splitlines()) if l.strip()), "")
            return line[:300]
    return ""


_LEVER_TOOLS = ("downgrade_search", "lower_python")


def _lever_of(step: TraceStep) -> str:
    if step.tool_called == "downgrade_search":
        return levers_module.DOWNGRADE
    if step.tool_called == "lower_python":
        return levers_module.INTERPRETER_DOWN
    return str(step.tool_result.get("lever", "") or "")


def _lever_detail(step: TraceStep) -> str:
    r = step.tool_result
    if step.tool_called == "downgrade_search":
        tried = ", ".join(f"{t.get('version')}:{t.get('outcome')}" for t in (r.get("tried") or []))
        return (f"{r.get('package')} {r.get('original_version')} -> {r.get('final_version')}; "
                f"stopped_because={r.get('stopped_because')}; tried [{tried}]")[:600]
    if step.tool_called == "lower_python":
        return (f"python {r.get('from_version')} -> {r.get('to_version')} via {r.get('mechanism')}; "
                f"ok={r.get('ok')}; {str(r.get('message', ''))[:160]}")
    if r.get("system_dependency"):
        sd = r["system_dependency"]
        return f"system dependency via conda: specs={sd.get('specs')} ok={sd.get('ok')} {sd.get('unavailable', '')}"[:400]
    if r.get("conda_fallback"):
        cf = r["conda_fallback"]
        return f"pip build failed -> conda: specs={cf.get('specs')} ok={cf.get('ok')} {cf.get('unavailable', '')}"[:400]
    return ""


def _looks_like_local_module(module_name: str, repo_path: str, target_rel: str | None = None) -> bool:
    """TASK_fix_repo_execution.md Part B: a simple "does a file/module of
    this name exist in the repo" check -- the missing name from
    "No module named 'X'" could be a repo-local file (`X.py`, an `X/`
    package) rather than a real PyPI dependency. Checked anywhere in the
    repo tree (not just next to the failing file), since Part A's
    whole-repo copy means a local module could sit in any directory a
    `sys.path` tweak reaches. Cheap and simple on purpose, per the task's
    own framing -- this is a safety net for the residual cases that still
    fail even after Part A, not a guarantee.
    """
    from_file = os.path.join(repo_path, target_rel) if target_rel else None
    return find_local_module(module_name, repo_path, from_file=from_file) is not None


def _runtime_observed_detail(trace: list[TraceStep], repo_path: str, target_rel: str | None = None) -> dict:
    """PILOT_DECISIONS.md #4 + TASK_fix_repo_execution.md Part B +
    TASK_next_steps.md step 1: what the agent's last failing run actually
    showed.

    A notebook's static CSV label (A/C/D) is a pre-run guess -- the *actual*
    failure observed when the agent really ran it can turn out to be
    out-of-scope (missing data, GPU, credentials) or, specifically for a
    ModuleNotFoundError, a repo-local module rather than a missing PyPI
    package.

    Returns {category, local_module_import, error, reason}:
    - category: '' when no observed failure applies (fixed, no observation,
      or local_module_import is True instead); only "E" changes how
      analyze_results.py counts a row.
    - error: the exception line (<= 300 chars) -- WHAT was observed.
    - reason: runtime_reasons slug -- WHY it matters for scope (missing data,
      download timeout, GPU, ... or a dependency kind).
    """
    for step in reversed(trace):
        if step.tool_called not in ("run_target", "verify"):
            continue
        if step.tool_result.get("ok"):
            continue
        stderr = str(step.tool_result.get("stderr", ""))
        stdout = str(step.tool_result.get("stdout", ""))
        exception_line = _find_exception_line(stderr)
        error = (exception_line or next((l for l in reversed(stderr.splitlines()) if l.strip()), ""))[:300]
        if exception_line is None:
            cell = str(step.tool_result.get("stuck_cell", ""))
            reason = classify_runtime_reason(None, stderr, stdout, "", "", False, cell_source=cell)
            return {"category": "", "local_module_import": False, "error": error, "reason": reason,
                    "progress": str(step.tool_result.get("progress", "")), "cell": cell[:300], "detail": stderr[-500:]}
        diag = diagnose_module.diagnose(stderr)
        category: str | None = None
        local = False
        if diag.kind == "missing_module":
            if _looks_like_local_module(diag.module, repo_path, target_rel):
                local = True
            elif pypi.resolve_package_name(diag.module) is not None:
                category = "A"
            # Neither a confirmed local module nor a confirmed PyPI
            # package -- falls through to the general taxonomy classifier
            # below, same as any other unresolved missing_module case.
        if local:
            category_out = ""
        elif category is not None:
            category_out = category
        else:
            category_out, _tier, _class_name, _message = classify_taxonomy(exception_line)
        cell = str(step.tool_result.get("stuck_cell", ""))
        reason = classify_runtime_reason(exception_line, stderr, stdout, category_out, diag.kind, local, cell_source=cell)
        return {"category": category_out, "local_module_import": local, "error": error, "reason": reason,
                "progress": str(step.tool_result.get("progress", "")), "cell": cell[:300],
                "detail": stderr[-500:]}
    return {"category": "", "local_module_import": False, "error": "", "reason": "unobserved",
            "progress": "", "cell": "", "detail": ""}


def _runtime_observed_outcome(trace: list[TraceStep], repo_path: str) -> tuple[str, bool]:
    """(runtime_observed_category, local_module_import) -- the original
    two-value view of _runtime_observed_detail, kept for existing callers."""
    detail = _runtime_observed_detail(trace, repo_path)
    return detail["category"], detail["local_module_import"]


def build_result_row(
    repository: str,
    clone_url: str,
    failure: dict,
    file_result,
    repo_meta: dict,
    run_id: str,
    max_steps_per_file: int,
    any_real_fix_seen_before_this_file: bool,
    repo_path: str = "",
) -> dict:
    """One row for one taxonomy-labelled notebook, given the AgentResult
    repo_runner.run_repo_incrementally() yielded for it (or None, if this
    notebook was never discovered at all -- renamed, deleted, or the 2023
    snapshot has since diverged from the repo's current state).

    `any_real_fix_seen_before_this_file` must reflect only files processed
    *earlier* in the same repo run (the caller tracks this across the
    incremental stream) -- a "free pass" can only be caused by a fix that
    already happened, not one still to come later in the same run.

    `repo_path` (the real cloned repo) is used only for Part B's
    local-module check -- optional and defaulted so existing callers/tests
    that don't care about that distinction are unaffected.
    """
    row = {
        "run_id": run_id,
        "repository": repository,
        "clone_url": clone_url,
        "notebook_path": failure["notebook_path"],
        "taxonomy_category": failure["category"],
        "taxonomy_tier": failure["tier"],
        "env_setup_ok": repo_meta.get("env_setup_ok"),
        "dependency_file_used": repo_meta.get("dependency_file_used"),
        # TASK_interpreter_repair.md Part C: interpreter choice as a
        # provenance-tagged repair decision, alongside package provenance.
        "python_version_requested": repo_meta.get("python_version_requested"),
        "python_version_actual": repo_meta.get("python_version_actual"),
        "python_version_source": repo_meta.get("python_version_source"),
        "python_grounding": repo_meta.get("python_grounding"),
        "python_mechanism": repo_meta.get("python_mechanism"),
        "python_note": repo_meta.get("python_note"),
        "interpreter_unprovisionable": bool(repo_meta.get("interpreter_unprovisionable")),
        # TASK_prepilot_fixes.md #4: binder/postBuild, as repo2docker runs it.
        "postbuild_found": bool(repo_meta.get("postbuild_found")),
        "postbuild_ran": bool(repo_meta.get("postbuild_ran")),
        "postbuild_ok": bool(repo_meta.get("postbuild_ok")),
        # TASK_containerize.md Part C: the container IS part of the reproduced
        # environment -- runtime, platform, image, and the resolved Python
        # (python_version_* above already reflect the in-container resolution).
        "runtime": repo_meta.get("runtime", "host"),
        "container_platform": repo_meta.get("container_platform", ""),
        "container_image": repo_meta.get("container_image", ""),
        "container_image_id": repo_meta.get("container_image_id", ""),
        "container_timeout_scale": repo_meta.get("container_timeout_scale", 1.0),
        "container_notebook_executor": repo_meta.get("container_notebook_executor", "kernel"),
        "container_error": repo_meta.get("container_error", ""),
        "container_setup_seconds": repo_meta.get("container_setup_seconds"),
        "max_steps_per_file": max_steps_per_file,
    }

    if file_result is None:
        row.update({"discovered": False, "fixed": False, "error": "notebook not found by discover_runnable_files"})
        return row

    fix_steps = [s for s in file_result.trace if s.tool_called in ("install_package", "edit_code", *_LEVER_TOOLS)]
    passed_without_agent_action = file_result.fixed and not file_result.trace
    # TASK_prepilot_fixes.md #2/#3: repo-local-module handling, on the row.
    refused_local_installs = sum(
        1 for s in file_result.trace
        if s.tool_called == "install_package" and s.tool_result.get("refused")
    )
    workdir_info = getattr(file_result, "working_directory", None) or {}
    lower_steps = [s for s in file_result.trace if s.tool_called == "lower_python" and s.tool_result.get("ok")]
    row.update({
        # TASK_improve_fix_rate.md: the experiment configuration and what the levers did
        "levers": levers_module.to_env_value(levers_module.active()),
        # lever era_resolve: the date everything was resolved against, what happened while the environment
        # was built, and a roll-up over the agent's own installs ({} / zeros when the lever is off).
        "era_cutoff_date": repo_meta.get("era_cutoff_date"),
        # lever era_date: which date was used, where it came from, whether that source is grounded, and every
        # candidate date (so a reader sees what any other rule would have chosen). Always present: the
        # baseline reports its source as repo_last_commit.
        "era_date_source": repo_meta.get("era_date_source", ""),
        "era_date_grounded": bool(repo_meta.get("era_date_grounded")),
        "era_date_capped_by_last_commit": bool(repo_meta.get("era_date_capped_by_last_commit")),
        "era_date_candidates": repo_meta.get("era_date_candidates") or {},
        "era_date_note": repo_meta.get("era_date_note", ""),
        "era_strict_refusals": sum(1 for s in file_result.trace if s.tool_result.get("refusal_reason") == "era_window"),
        "era_resolution_setup": repo_meta.get("era_resolution_setup") or {},
        "era_resolution_agent": _era_agent_summary(file_result.trace),
        "initial_error": _first_failure_error(file_result.trace),
        "fallback_nudges": list(getattr(file_result, "fallback_nudges", None) or []),
        "python_version_final": (lower_steps[-1].tool_result.get("to_version") if lower_steps else None),
        "refused_local_module_installs": refused_local_installs,
        "working_directory": workdir_info.get("working_directory", ""),
        "working_directory_source": workdir_info.get("working_directory_source", ""),
        "working_directory_module": workdir_info.get("working_directory_module", ""),
        "working_directory_searched": bool(workdir_info),
        "working_directory_recovered": bool(workdir_info.get("working_directory_recovered")),
    })
    detail = (
        {"category": "", "local_module_import": False, "error": "", "reason": "", "progress": "", "cell": "", "detail": ""}
        if file_result.fixed else _runtime_observed_detail(file_result.trace, repo_path, file_result.target)
    )
    runtime_observed_category = detail["category"]
    local_module_import = detail["local_module_import"]

    row.update({
        "discovered": True,
        "fixed": file_result.fixed,
        "error": file_result.error,
        "final_strategy": file_result.final_strategy,
        "num_agent_steps": len(file_result.trace),
        "max_steps_hit": (not file_result.fixed) and len(file_result.trace) >= max_steps_per_file,
        # TASK_agent_stopping.md: which STOP_* condition actually ended this
        # run (fixed/no_progress/step_cap/model_gave_up/model_stopped/error)
        # -- more precise than max_steps_hit alone, and useful data for the
        # results chapter on its own.
        "stop_reason": file_result.stop_reason,
        "llm_calls": file_result.llm_calls,
        "total_tokens": file_result.total_tokens,
        # PILOT_DECISIONS.md #2: distinct outcome, not a repair failure --
        # excluded from the effectiveness denominator in analyze_results.py.
        "too_large_for_context": file_result.too_large_for_context,
        # Found running the real pilot smoke test: the API key's own credit
        # balance ran out mid-run (OpenAI's "insufficient_quota" error) --
        # a billing wall, not a repair failure. Same exclusion treatment as
        # too_large_for_context, for the same reason.
        "quota_exhausted": file_result.quota_exhausted,
        # PILOT_DECISIONS.md #4: never actually attempted (this repo's time
        # budget ran out first) -- also excluded from the denominator,
        # reported separately, not as a failure.
        "budget_exhausted": file_result.error.startswith(BUDGET_EXHAUSTED_ERROR_PREFIX),
        # PILOT_DECISIONS.md #4: the real, observed runtime failure
        # reclassified through the same taxonomy classifier the CSV labels
        # came from -- '' when not applicable (fixed, or no observed
        # failure), else the category it actually turned out to be. Only
        # "E" (out of scope: missing data/GPU/credentials/etc.) changes how
        # analyze_results.py counts this row; A/B/C/D here just means the
        # real failure matched its static label and is a plain non-fix.
        "runtime_observed_category": runtime_observed_category,
        # TASK_next_steps.md step 1: WHAT the last failing run showed and WHY
        # it matters for scope (evaluation/runtime_reasons.py slugs) -- so
        # "out-of-scope" is explained, not just asserted.
        "runtime_observed_error": detail["error"],
        "runtime_observed_reason": detail["reason"],
        # how far the notebook got and which cell it stopped in (see runner.RunResult.progress)
        "runtime_observed_progress": detail["progress"],
        "runtime_observed_cell": detail["cell"],
        "runtime_observed_detail": detail["detail"],
        # TASK_fix_repo_execution.md Part B: a ModuleNotFoundError for a
        # repo-local module (not a real PyPI package) -- excluded from the
        # category-A denominator, reported separately, same treatment as
        # too_large_for_context/quota_exhausted/budget_exhausted.
        "local_module_import": local_module_import,
        "fix_actions": [_trace_step_summary(s) for s in fix_steps],
        "passed_without_agent_action": passed_without_agent_action,
        # the repo-scale finding TASK_evaluation.md section 3 asks for:
        # this notebook passed on its own, and a *strictly earlier* file in
        # the same repo run genuinely required a fix -- i.e. this one
        # plausibly benefited "for free" from shared-environment state a
        # prior fix left behind, not from something that happens later.
        "cross_file_free_pass": passed_without_agent_action and any_real_fix_seen_before_this_file,
    })
    return row


def run(
    failures_csv: str,
    results_dir: str,
    sample: int,
    seed: int,
    max_steps_per_file: int,
    limit_minutes: float | None,
    max_llm_calls: int | None,
    tag: str,
    per_file_budget_seconds: float = DEFAULT_PER_FILE_BUDGET_SECONDS,
    in_scope_confirmed_only: bool = False,
    no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT,
    workers: int = 1,
    repair_interpreter: bool = True,
    run_postbuild: bool = True,
    runtime: str = "host",
    platform: str = DEFAULT_PLATFORM,
    container_scale: float = 1.0,
    levers_spec: str = "",
) -> None:
    active_levers = levers_module.parse(levers_spec)  # raises on a typo: never run a different experiment silently
    os.environ[levers_module.ENV] = levers_module.to_env_value(active_levers)
    if runtime == "docker":
        usable, why = docker_available()
        if not usable:
            raise SystemExit(f"--runtime docker is not usable: {why}")
    os.makedirs(results_dir, exist_ok=True)
    results_path = os.path.join(results_dir, f"{tag}.jsonl")
    checkpoint_path = os.path.join(results_dir, f"{tag}.done_repos.txt")
    meta_path = os.path.join(results_dir, f"{tag}.meta.json")

    by_repo = load_failures_by_repo(failures_csv, in_scope_confirmed_only=in_scope_confirmed_only)
    all_repos = sorted(by_repo.keys())
    rng = random.Random(seed)
    rng.shuffle(all_repos)
    sampled_repos = all_repos[:sample] if sample else all_repos

    done = load_checkpoint(checkpoint_path)
    todo = [r for r in sampled_repos if r not in done]

    run_id = f"{tag}-{_now_iso()}"
    meta = {
        "run_id": run_id,
        "started_at": _now_iso(),
        "dataset": failures_csv,
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "max_steps_per_file": max_steps_per_file,
        "per_file_budget_seconds": per_file_budget_seconds,
        "sample": sample,
        "seed": seed,
        "in_scope_confirmed_only": in_scope_confirmed_only,
        "no_progress_limit": no_progress_limit,
        "repair_interpreter": repair_interpreter,
        "run_postbuild": run_postbuild,
        "levers": levers_module.to_env_value(active_levers),
        "runtime": runtime,
        "platform": platform if runtime == "docker" else "",
        "container_scale": container_scale if runtime == "docker" else 1.0,
        "total_repos_in_sample": len(sampled_repos),
        "already_done_at_start": len(done),
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"Run {run_id}: {len(sampled_repos)} repos sampled, {len(done)} already done, {len(todo)} to process.")

    start_time = time.monotonic()
    total_llm_calls_this_run = 0
    processed_count = 0
    error_count = 0

    def process_one(repository: str) -> tuple[str, list[dict], bool, int]:
        """One repo's full clone-repair-cleanup cycle. Factored out of the
        dispatch loop below so --workers can run several of these
        concurrently (each already isolated in its own clone + subprocess)
        without duplicating this logic for the sequential and concurrent
        paths. Returns (repository, rows, errored, llm_calls_used)."""
        failure_rows = by_repo[repository]
        clone_url = failure_rows[0]["clone_url"]

        clone_path, clone_log = clone_repo(clone_url, full_history=levers_module.enabled(levers_module.ERA_DATE))
        errored = False
        llm_calls_used = 0
        try:
            if clone_path is None:
                rows = [{
                    "run_id": run_id, "repository": repository, "clone_url": clone_url,
                    "notebook_path": f["notebook_path"], "taxonomy_category": f["category"],
                    "taxonomy_tier": f["tier"], "discovered": False, "fixed": False,
                    "error": f"clone failed: {clone_log}",
                } for f in failure_rows]
            else:
                try:
                    num_files = max(count_target_files(clone_path, [f["notebook_path"] for f in failure_rows]), 1)
                    scale = container_scale if runtime == "docker" else 1.0
                    budget_seconds = num_files * per_file_budget_seconds * scale
                    kill_buffer = SUBPROCESS_KILL_BUFFER_SECONDS + (
                        INTERPRETER_SETUP_ALLOWANCE_SECONDS if repair_interpreter else 0
                    ) + (CONTAINER_SETUP_ALLOWANCE_SECONDS if runtime == "docker" else 0) + (
                        LEVER_ALLOWANCE_SECONDS if active_levers else 0
                    )
                    timeout_seconds = int(budget_seconds + kill_buffer)

                    rows, status = run_repo_worker(
                        repository, clone_url, clone_path, failure_rows, run_id, max_steps_per_file,
                        budget_seconds, timeout_seconds, no_progress_limit=no_progress_limit,
                        repair_interpreter=repair_interpreter, run_postbuild=run_postbuild,
                        runtime=runtime, platform=platform, container_scale=container_scale,
                        container_name=f"repair-{os.getpid()}-{abs(hash(repository)) % 10**8}" if runtime == "docker" else None,
                    )
                    covered_paths = {r["notebook_path"] for r in rows}
                    # status == "ok" means the worker exited cleanly, which only
                    # happens after it has already backfilled every labelled
                    # notebook itself -- so this gap shouldn't occur on an "ok"
                    # status in practice; if it somehow does, say so plainly
                    # rather than recording the misleading reason "ok".
                    backfill_reason = status if status != "ok" else "worker completed without a result for this notebook (unexpected)"
                    for f in failure_rows:
                        if f["notebook_path"] not in covered_paths:
                            rows.append({
                                "run_id": run_id, "repository": repository, "clone_url": clone_url,
                                "notebook_path": f["notebook_path"], "taxonomy_category": f["category"],
                                "taxonomy_tier": f["tier"], "discovered": False, "fixed": False,
                                "error": backfill_reason,
                            })
                    if status != "ok":
                        errored = True
                    llm_calls_used = sum(r.get("llm_calls", 0) for r in rows)
                except Exception as exc:  # noqa: BLE001 - one bad repo must never abort the whole run
                    rows = [{
                        "run_id": run_id, "repository": repository, "clone_url": clone_url,
                        "notebook_path": f["notebook_path"], "taxonomy_category": f["category"],
                        "taxonomy_tier": f["tier"], "discovered": False, "fixed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    } for f in failure_rows]
                    errored = True
        finally:
            if clone_path is not None:
                shutil.rmtree(clone_path, ignore_errors=True)

        return repository, rows, errored, llm_calls_used

    def record(repository: str, rows: list[dict], errored: bool, llm_calls_used: int) -> None:
        """Write one repo's finished rows and mark it done -- always called
        from the main thread (even under --workers), so the results file
        and checkpoint are never written to concurrently."""
        nonlocal error_count, total_llm_calls_this_run, processed_count
        with open(results_path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        mark_done(checkpoint_path, repository)
        if errored:
            error_count += 1
        total_llm_calls_this_run += llm_calls_used
        processed_count += 1

    def limit_reached() -> bool:
        if limit_minutes is not None and (time.monotonic() - start_time) / 60 >= limit_minutes:
            print(f"Time limit ({limit_minutes} min) reached; stopping new repos.")
            return True
        if max_llm_calls is not None and total_llm_calls_this_run >= max_llm_calls:
            print(f"LLM call budget ({max_llm_calls}) reached; stopping new repos.")
            return True
        return False

    if workers <= 1:
        for i, repository in enumerate(todo):
            if limit_reached():
                break
            print(f"[{i + 1}/{len(todo)}] {repository} ({len(by_repo[repository])} labelled notebook(s))...", flush=True)
            record(*process_one(repository))
    else:
        # TASK_agent_stopping.md #4: each repo is already isolated in its
        # own clone + subprocess, so a thread pool of blocking
        # subprocess.run calls is enough -- no process pool needed. Results
        # are only ever written from the main thread (via `record`, as
        # each future completes), so the file/checkpoint are never touched
        # by more than one thread at a time regardless of how many repos
        # run concurrently.
        print(f"Running with up to {workers} repos concurrently.", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            submitted = 0
            futures = {}
            todo_iter = iter(todo)
            for repository in todo_iter:
                if limit_reached():
                    break
                futures[pool.submit(process_one, repository)] = repository
                submitted += 1
                if submitted >= workers:
                    break
            while futures:
                done_futures, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done_futures:
                    del futures[future]
                    record(*future.result())
                    print(f"[{processed_count}/{len(todo)}] finished", flush=True)
                    if not limit_reached():
                        try:
                            repository = next(todo_iter)
                            futures[pool.submit(process_one, repository)] = repository
                        except StopIteration:
                            pass

    elapsed = (time.monotonic() - start_time) / 60
    print(f"Done this run: {processed_count} repos processed ({error_count} errored), {elapsed:.1f} min, "
          f"~{total_llm_calls_this_run} LLM calls. Results: {results_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failures-csv", default=DEFAULT_FAILURES_CSV)
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--tag", default="eval", help="base name for this run's result/checkpoint files")
    parser.add_argument("--sample", type=int, default=0, help="max number of repos to process (0 = all)")
    parser.add_argument("--seed", type=int, default=42, help="shuffle seed for repo sampling (reproducible)")
    parser.add_argument("--max-steps-per-file", type=int, default=DEFAULT_MAX_STEPS_PER_FILE)
    parser.add_argument("--limit-minutes", type=float, default=None, help="stop starting new repos after this many minutes")
    parser.add_argument("--max-llm-calls", type=int, default=None, help="stop starting new repos after this many LLM calls")
    parser.add_argument(
        "--per-file-budget-minutes", type=float, default=DEFAULT_PER_FILE_BUDGET_SECONDS / 60,
        help="this repo's total time budget is this value times its discovered file count; "
             "exhausting it stops the repo gracefully between files, keeping whatever already finished",
    )
    parser.add_argument(
        "--in-scope-confirmed-only", action="store_true",
        help="PILOT_DECISIONS.md #3: restrict sampling to confirmed-tier, "
             "in-scope (A/C/D) failures only -- use this for the real pilot",
    )
    parser.add_argument(
        "--no-progress-limit", type=int, default=DEFAULT_NO_PROGRESS_LIMIT,
        help="TASK_agent_stopping.md: stop a file's repair early once the observed failure is "
             "unchanged this many consecutive times, instead of burning the full step budget "
             "on a model that doesn't recognise it's stuck",
    )
    parser.add_argument(
        "--no-interpreter-repair", action="store_true",
        help="TASK_interpreter_repair.md: do NOT provision the repo's declared/era Python; run on the "
             "harness interpreter as before (the ablation baseline for before/after comparisons)",
    )
    parser.add_argument(
        "--levers", default="",
        help="TASK_improve_fix_rate.md: comma list of repair levers to enable (default none = the baseline). "
             "Valid: " + ", ".join(levers_module.ALL_LEVERS) + ", or 'all' / 'none'",
    )
    parser.add_argument(
        "--runtime", choices=("host", "docker"), default="host",
        help="TASK_containerize.md: where the executing actions run. 'host' (default) as before; 'docker' builds the "
             "environment and runs every install/run/edit inside a Linux container (the LLM calls stay on the host)",
    )
    parser.add_argument(
        "--platform", default=DEFAULT_PLATFORM,
        help="container platform for --runtime docker (default linux/amd64, so old x86_64 wheels resolve even on an "
             "arm64 host; recorded on every row)",
    )
    parser.add_argument(
        "--container-scale", type=float, default=3.0,
        help="multiplier applied to time limits (per-cell, per-file budget) under --runtime docker, to account for "
             "emulation overhead on a non-native platform; calibrate with the measured slowdown",
    )
    parser.add_argument(
        "--no-postbuild", action="store_true",
        help="TASK_prepilot_fixes.md #4: do NOT run the repo's binder/postBuild script (it is arbitrary "
             "repo-supplied shell code; it runs with a throwaway $HOME and is recorded, never fatal)",
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="TASK_agent_stopping.md #4: process this many repos concurrently (each already "
             "isolated in its own clone + subprocess). Start small (2-4) and confirm the LLM "
             "backend doesn't just serialize/degrade under concurrent load before raising it",
    )
    args = parser.parse_args()

    run(
        failures_csv=args.failures_csv,
        results_dir=args.results_dir,
        sample=args.sample,
        seed=args.seed,
        max_steps_per_file=args.max_steps_per_file,
        limit_minutes=args.limit_minutes,
        max_llm_calls=args.max_llm_calls,
        per_file_budget_seconds=args.per_file_budget_minutes * 60,
        tag=args.tag,
        in_scope_confirmed_only=args.in_scope_confirmed_only,
        no_progress_limit=args.no_progress_limit,
        workers=args.workers,
        repair_interpreter=not args.no_interpreter_repair,
        run_postbuild=not args.no_postbuild,
        runtime=args.runtime,
        platform=args.platform,
        container_scale=args.container_scale,
        levers_spec=args.levers,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
