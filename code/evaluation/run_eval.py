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
from dataclasses import asdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation._repo_runner import DEFAULT_PER_FILE_BUDGET_SECONDS, count_discovered_files
from repair_tool.agent import TraceStep

DEFAULT_FAILURES_CSV = "dataset/failures_2023.csv"
DEFAULT_RESULTS_DIR = "evaluation/results"
CLONE_TIMEOUT = 120
WORKER_MODULE = "evaluation._repo_worker"
# Extra time allowed, on top of the computed per-repo budget, before the
# OS-level subprocess kill fires -- a backstop against one single file's
# agent_repair() call hanging past its own internal bounds (shouldn't
# happen given MAX_STEPS/the OpenAI client timeout, but this is cheap
# insurance); the *graceful* stop between files is what normally applies
# the budget, not this hard kill.
SUBPROCESS_KILL_BUFFER_SECONDS = 300

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


def load_failures_by_repo(csv_path: str) -> dict[str, list[dict]]:
    """repository -> list of its taxonomy-labelled failure rows."""
    by_repo: dict[str, list[dict]] = defaultdict(list)
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
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


def clone_repo(clone_url: str, timeout: int = CLONE_TIMEOUT) -> tuple[str | None, str]:
    """Clone `clone_url` into a fresh temp dir. Returns (path_or_None, log).
    Never raises -- a bad/missing/private repo comes back as (None, log),
    same contract as repo.analyze_repo_url's own clone step."""
    tmpdir = tempfile.mkdtemp(prefix="repair_tool_eval_clone_")
    try:
        completed = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, tmpdir],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, f"git clone timed out after {timeout}s"
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, f"could not run git: {exc}"

    if completed.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, f"git clone failed: {(completed.stdout + completed.stderr).strip()[:500]}"

    return tmpdir, "cloned ok"


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
                    "budget_seconds": budget_seconds,
                },
                f,
            )
            input_path = f.name
        output_fd, output_path = tempfile.mkstemp(suffix=".jsonl")
        os.close(output_fd)

        try:
            completed = subprocess.run(
                [sys.executable, "-m", WORKER_MODULE, "--input", input_path, "--output", output_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return _read_jsonl_rows_tolerantly(output_path), f"timed out after {timeout_seconds}s"
        except OSError as exc:
            return [], f"could not run worker: {exc}"

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
    }


def build_result_row(
    repository: str,
    clone_url: str,
    failure: dict,
    file_result,
    repo_meta: dict,
    run_id: str,
    max_steps_per_file: int,
    any_real_fix_seen_before_this_file: bool,
) -> dict:
    """One row for one taxonomy-labelled notebook, given the AgentResult
    repo_runner.run_repo_incrementally() yielded for it (or None, if this
    notebook was never discovered at all -- renamed, deleted, or the 2023
    snapshot has since diverged from the repo's current state).

    `any_real_fix_seen_before_this_file` must reflect only files processed
    *earlier* in the same repo run (the caller tracks this across the
    incremental stream) -- a "free pass" can only be caused by a fix that
    already happened, not one still to come later in the same run.
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
        "max_steps_per_file": max_steps_per_file,
    }

    if file_result is None:
        row.update({"discovered": False, "fixed": False, "error": "notebook not found by discover_runnable_files"})
        return row

    fix_steps = [s for s in file_result.trace if s.tool_called in ("install_package", "edit_code")]
    passed_without_agent_action = file_result.fixed and not file_result.trace

    row.update({
        "discovered": True,
        "fixed": file_result.fixed,
        "error": file_result.error,
        "final_strategy": file_result.final_strategy,
        "num_agent_steps": len(file_result.trace),
        "max_steps_hit": (not file_result.fixed) and len(file_result.trace) >= max_steps_per_file,
        "llm_calls": file_result.llm_calls,
        "total_tokens": file_result.total_tokens,
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
) -> None:
    os.makedirs(results_dir, exist_ok=True)
    results_path = os.path.join(results_dir, f"{tag}.jsonl")
    checkpoint_path = os.path.join(results_dir, f"{tag}.done_repos.txt")
    meta_path = os.path.join(results_dir, f"{tag}.meta.json")

    by_repo = load_failures_by_repo(failures_csv)
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

    for repository in todo:
        if limit_minutes is not None and (time.monotonic() - start_time) / 60 >= limit_minutes:
            print(f"Time limit ({limit_minutes} min) reached; stopping. {len(todo) - processed_count} repos left for next run.")
            break
        if max_llm_calls is not None and total_llm_calls_this_run >= max_llm_calls:
            print(f"LLM call budget ({max_llm_calls}) reached; stopping. {len(todo) - processed_count} repos left for next run.")
            break

        failure_rows = by_repo[repository]
        clone_url = failure_rows[0]["clone_url"]
        print(f"[{processed_count + 1}/{len(todo)}] {repository} ({len(failure_rows)} labelled notebook(s))...", flush=True)

        clone_path, clone_log = clone_repo(clone_url)
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
                    num_files = max(count_discovered_files(clone_path), 1)
                    budget_seconds = num_files * per_file_budget_seconds
                    timeout_seconds = int(budget_seconds + SUBPROCESS_KILL_BUFFER_SECONDS)

                    rows, status = run_repo_worker(
                        repository, clone_url, clone_path, failure_rows, run_id, max_steps_per_file,
                        budget_seconds, timeout_seconds,
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
                        error_count += 1
                    total_llm_calls_this_run += sum(r.get("llm_calls", 0) for r in rows)
                except Exception as exc:  # noqa: BLE001 - one bad repo must never abort the whole run
                    rows = [{
                        "run_id": run_id, "repository": repository, "clone_url": clone_url,
                        "notebook_path": f["notebook_path"], "taxonomy_category": f["category"],
                        "taxonomy_tier": f["tier"], "discovered": False, "fixed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    } for f in failure_rows]
                    error_count += 1
        finally:
            if clone_path is not None:
                shutil.rmtree(clone_path, ignore_errors=True)

        with open(results_path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

        mark_done(checkpoint_path, repository)
        processed_count += 1

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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
