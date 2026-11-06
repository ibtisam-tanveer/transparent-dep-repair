"""Run the repair tool on a GitHub repository link, from the terminal.

    python evaluation/repair_url.py https://github.com/OWNER/REPO
    python evaluation/repair_url.py https://github.com/OWNER/REPO --file notebooks/analysis.ipynb
    python evaluation/repair_url.py https://github.com/OWNER/REPO --runtime docker --levers all

What it does: clones the repo (shallow) to find its notebooks/scripts, writes a small input
file for the evaluation harness (`run_eval.py`), runs the harness on it, and prints for every
file: FIXED or NOT FIXED, why, and the exact repair steps (package installs with versions,
interpreter changes, edits) with how each step was justified and verified.

Your repository is never modified: the tool works on a throwaway copy. A "fix" is the recipe it
found (printed at the end), not a changed file.

Needs (see docker/README.md and the project README): the project's Python environment, a `.env`
with the model token (KISTE_API_TOKEN) and the university VPN for the model, and Docker running if
you choose `--runtime docker`.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CSV_FIELDS = ["repository", "clone_url", "notebook_path", "error_type", "error_message", "category", "tier",
              "python_version", "repository_id"]


def parse_repo_url(url: str) -> tuple[str, str]:
    """('owner/repo', clone_url) from https://github.com/owner/repo[.git][/...], or ValueError."""
    match = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?(?:[/#?].*)?$", url.strip())
    if not match:
        raise ValueError(f"not a GitHub repository link: {url!r} (expected https://github.com/OWNER/REPO)")
    owner, repo = match.group(1), match.group(2)
    return f"{owner}/{repo}", f"https://github.com/{owner}/{repo}.git"


def build_rows(repository: str, clone_url: str, files: list[str]) -> list[dict]:
    """One input row per file. The harness expects a taxonomy label; for a repo you chose
    yourself there is none, so every file is filed as a plain missing-package case (A)."""
    return [
        {"repository": repository, "clone_url": clone_url, "notebook_path": f, "error_type": "unknown",
         "error_message": "", "category": "A", "tier": "confirmed", "python_version": "", "repository_id": "0"}
        for f in files
    ]


def choose_files(all_files: list[str], requested: list[str], max_files: int) -> tuple[list[str], list[str]]:
    """(files to run, warnings). Requested files must exist; otherwise the notebooks of the repo, capped."""
    warnings: list[str] = []
    if requested:
        known = set(all_files)
        missing = [f for f in requested if f not in known]
        if missing:
            raise ValueError(f"not found in the repository: {missing}. Runnable files include: {all_files[:10]}")
        return list(requested), warnings
    notebooks = [f for f in all_files if f.endswith(".ipynb")]
    pool = notebooks or all_files
    if len(pool) > max_files:
        warnings.append(f"the repository has {len(pool)} runnable files; running the first {max_files} "
                        f"(use --max-files or --file to choose)")
    return pool[:max_files], warnings


_BASIS = {
    "metadata_grounded": "chosen from a fact in the package registry (PyPI)",
    "llm_proposed": "chosen by the AI model",
    "deterministic": "a fixed check, no judgement involved",
}
_LEVER_PLAIN = {
    "downgrade": "found by trying progressively older releases",
    "conda_fallback": "installed through conda because pip could not build it",
    "system_deps": "installed through conda together with a system component it needs (for example R)",
    "interpreter_down": "the Python version was stepped down",
}


def _step_text(a: dict, notebook_fixed: bool) -> list[str]:
    tool = a["tool"]
    if tool == "edit_code":
        head = "edited the notebook's code"
    elif tool == "downgrade_search":
        head = "tried older versions of a package"
    elif tool == "lower_python":
        head = "switched to an older Python"
    else:
        head = f"install {a.get('resolved_package') or '(package)'}"
    lines = [head]
    for e in a.get("edit_preview") or []:
        lines.append(f"    changed  {e.get('find', '')!r}\n    to       {e.get('replace', '')!r}")
    if a.get("lever_detail"):
        lines.append(f"    detail: {a['lever_detail'][:220]}")
    basis = _BASIS.get(a.get("grounding"), a.get("grounding") or "unknown")
    if a.get("lever"):
        basis += "; " + _LEVER_PLAIN.get(a["lever"], a["lever"])
    if a.get("verification") == "verified":
        check = "the notebook ran right after this step"
    elif notebook_fixed:
        check = "on its own this step did not make the notebook run; a later step did"
    else:
        check = "not confirmed -- the notebook still failed"
    lines.append(f"    basis: {basis}\n    check: {check}")
    lines.append(f"    (labels: {a.get('grounding')} / {a.get('verification')} / confidence {a.get('confidence') or 'n/a'})")
    return lines


def summarize(rows: list[dict]) -> str:
    out = []
    for r in rows:
        name = r["notebook_path"]
        if not r.get("discovered", True):
            out.append(f"  NOT RUN       {name}\n      {r.get('error', '')[:160]}")
            continue
        if r.get("fixed"):
            status = "ALREADY RUNS" if r.get("passed_without_agent_action") else "FIXED"
            out.append(f"  {status:13} {name}")
            if status == "ALREADY RUNS":
                out.append("      it ran as-is in the repository's own environment; nothing needed repairing")
        else:
            out.append(f"  NOT FIXED     {name}")
            reason = r.get("runtime_observed_reason") or r.get("stop_reason") or "?"
            out.append(f"      where it ended: {reason} -- {(r.get('runtime_observed_error') or r.get('error') or '')[:150]}")
        if r.get("initial_error") and r.get("fixed") and not r.get("passed_without_agent_action"):
            out.append(f"      problem it had: {r['initial_error'][:170]}")
        elif r.get("initial_error") and not r.get("fixed"):
            out.append(f"      first problem seen: {r['initial_error'][:170]}")
        py = r.get("python_version_final") or r.get("python_version_actual")
        if py:
            out.append(f"      python {py} ({r.get('python_mechanism')}, from {r.get('python_version_source')})")
        actions = r.get("fix_actions") or []
        for i, a in enumerate(actions, start=1):
            lines = _step_text(a, bool(r.get("fixed")))
            out.append(f"      step {i}: {lines[0]}")
            out.extend("      " + l for l in lines[1:])
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="GitHub repository link, e.g. https://github.com/OWNER/REPO")
    parser.add_argument("--file", action="append", default=[], metavar="PATH",
                        help="a notebook/script path inside the repo (repeatable); default: its notebooks")
    parser.add_argument("--max-files", type=int, default=10, help="cap when --file is not given (default 10)")
    parser.add_argument("--runtime", choices=("host", "docker"), default="host",
                        help="host = this machine; docker = a linux/amd64 container (needs Docker running)")
    parser.add_argument("--levers", default="all",
                        help="repair levers: 'all' (default), 'none' (the baseline) or a comma list")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--tag", default=None, help="name for the result files (default: derived from the URL)")
    args = parser.parse_args(argv)

    try:
        repository, clone_url = parse_repo_url(args.url)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    from evaluation.run_eval import clone_repo
    from repair_tool import repo as repo_module

    print(f"cloning {clone_url} ...")
    path, log = clone_repo(clone_url)
    if path is None:
        print(f"error: could not clone the repository: {log}")
        return 1
    try:
        all_files = repo_module.discover_runnable_files(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)

    try:
        files, warnings = choose_files(all_files, args.file, args.max_files)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    if not files:
        print("error: no runnable .ipynb/.py files found in the repository")
        return 2
    for w in warnings:
        print(f"note: {w}")

    tag = args.tag or f"url_{repository.replace('/', '_')}_{time.strftime('%Y%m%d_%H%M%S')}"
    results_dir = os.path.join(ROOT, "evaluation", "results")
    os.makedirs(results_dir, exist_ok=True)
    csv_path = os.path.join(results_dir, f"{tag}.input.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(build_rows(repository, clone_url, files))

    cmd = [sys.executable, os.path.join(ROOT, "evaluation", "run_eval.py"), "--failures-csv", csv_path, "--sample", "0",
           "--tag", tag, "--runtime", args.runtime, "--levers", args.levers, "--workers", str(args.workers),
           "--results-dir", results_dir]
    print(f"running the repair tool on {len(files)} file(s) ({args.runtime}, levers={args.levers}) -- this can take a while ...")
    code = subprocess.call(cmd, cwd=ROOT)
    result_path = os.path.join(results_dir, f"{tag}.jsonl")
    if not os.path.isfile(result_path):
        print(f"error: the run produced no result file (exit code {code}); see the output above")
        return 1
    with open(result_path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    print(f"\nResults for {repository}:")
    print(summarize(rows))
    fixed = sum(1 for r in rows if r.get("fixed"))
    print(f"\n{fixed} of {len(rows)} file(s) run successfully. Details: {result_path}")
    print(f"Full analysis: python evaluation/analyze_results.py {result_path}")
    return 0 if fixed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
