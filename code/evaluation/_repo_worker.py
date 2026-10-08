"""Subprocess worker for one repo's repair run.

Why a subprocess: a single Python process has no clean way to forcibly
stop a stuck/slow call from the outside (a thread can't be killed, and
repair_tool/ stays untouched beyond the MAX_STEPS parameter per
TASK_evaluation.md). Running it in a child process lets run_eval.py
enforce a hard OS-level backstop with subprocess.run(..., timeout=...).

Streams one result row per file, via _repo_runner.run_repo_incrementally(),
flushing after each write -- so if this process is killed (by the parent's
backstop, or by the graceful per-repo time budget this module itself
checks first), every file that had already finished is preserved in the
output file; only files not yet reached are missing from it. The parent
(run_eval.run_repo_worker) fills in an honest "not reached" row for any
labelled notebook this file doesn't cover.

Reads its repo + failure-row arguments from a small JSON file (not argv,
since error messages/paths can contain characters argv handles poorly).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation._repo_runner import run_repo_incrementally
from evaluation.run_eval import build_result_row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="path to a JSON file with this job's arguments")
    parser.add_argument("--output", required=True, help="path to stream this repo's result rows to, as JSONL")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        job = json.load(f)

    by_path = {f["notebook_path"]: f for f in job["failure_rows"]}
    seen_paths: set[str] = set()
    any_real_fix_seen_so_far = False

    tmp_root = tempfile.mkdtemp(prefix="repair_tool_eval_fresh_")
    try:
        with open(args.output, "w", encoding="utf-8") as out_f:
            for file_result, repo_meta in run_repo_incrementally(
                job["clone_path"], tmp_root, job["max_steps_per_file"], job["budget_seconds"]
            ):
                seen_paths.add(file_result.target)
                failure = by_path.get(file_result.target)
                if failure is not None:
                    row = build_result_row(
                        job["repository"], job["clone_url"], failure, file_result, repo_meta,
                        job["run_id"], job["max_steps_per_file"], any_real_fix_seen_so_far,
                    )
                    out_f.write(json.dumps(row) + "\n")
                    out_f.flush()

                if file_result.fixed and file_result.trace:
                    any_real_fix_seen_so_far = True

            for notebook_path, failure in by_path.items():
                if notebook_path not in seen_paths:
                    row = build_result_row(
                        job["repository"], job["clone_url"], failure, None, {},
                        job["run_id"], job["max_steps_per_file"], any_real_fix_seen_so_far,
                    )
                    out_f.write(json.dumps(row) + "\n")
                    out_f.flush()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
