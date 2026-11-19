"""Prepare the EnvBench Python subset for the evaluation harness -- TASK_envbench_evaluation.md section 1.

EnvBench (Eliseeva et al., arXiv:2503.14443): 994 repositories, 329 of them Python. The Python subset is the split
`python_baseline_failure` of the Hugging Face dataset JetBrains-Research/EnvBench: the Python repositories that
EnvBench's own deterministic baseline script could not configure (the benchmark's "genuine configuration challenge"
filter). Each entry is `repository` + the one pinned commit `revision`. Only the Python subset is used here: the
JVM subset (Java/Kotlin) is out of scope for a Python-only tool.

The entry list is downloaded by hand into its own directory, outside the code of this repository:

    curl -L -o ../datasets/envbench_data/python_baseline_failure.jsonl \\
      https://huggingface.co/datasets/JetBrains-Research/EnvBench/resolve/<REVISION>/splits/python_baseline_failure.jsonl

and this script turns it into the failures-CSV format that run_eval.py reads (one row per repository, the check file
`envbench_check.py` being its single target, `clone_url` = `pinned:<sha>:<github url>`, see envbench_source.py):

    python evaluation/envbench_prepare.py --dataset-file ../datasets/envbench_data/python_baseline_failure.jsonl \\
        --hf-revision d1e96e6b10335cad40ac7b4f709f46a2c579765a --code-commit 1ba2695018080df3a3606b100fc1dbfad96ea115

Writes evaluation/samples/envbench_py_all.csv and evaluation/samples/envbench_py_all.meta.json (the revisions, the
count and the SHA-256 of the whole list: the fingerprint a later re-run checks). Nothing is executed here.

    --check-fetch   additionally tries to shallow-fetch every pinned commit from GitHub (no checkout kept) and
                    records which repositories are still served; written to envbench_py_all.fetch_check.json.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evaluation import envbench_check, envbench_source  # noqa: E402

CSV_FIELDS = ["repository", "clone_url", "notebook_path", "error_type", "error_message", "category", "tier",
              "python_version", "repository_id", "revision"]
DEFAULT_OUT = os.path.join(ROOT, "evaluation", "samples", "envbench_py_all.csv")
EXPECTED_PYTHON_REPOS = 329  # the count the EnvBench paper and README give


def load_entries(path: str) -> list[dict]:
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entry = json.loads(line)
                entries.append({"repository": entry["repository"], "revision": entry["revision"]})
    return entries


def build_rows(entries: list[dict]) -> list[dict]:
    """One row per repository, in a stable order (sorted by name). Raises on a malformed or duplicated entry."""
    seen = set()
    rows = []
    for index, entry in enumerate(sorted(entries, key=lambda e: e["repository"])):
        repo, sha = entry["repository"], entry["revision"]
        if repo in seen:
            raise ValueError(f"duplicate repository in the entry list: {repo}")
        seen.add(repo)
        spec = envbench_source.spec_for(repo, sha)
        envbench_source.parse_spec(spec)  # validates the repo name and the 40-hex commit
        rows.append({"repository": repo, "clone_url": spec, "notebook_path": envbench_check.TARGET_NAME,
                     "error_type": "", "error_message": "", "category": "A", "tier": "confirmed",
                     "python_version": "", "repository_id": index, "revision": sha})
    return rows


def list_fingerprint(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: r["repository"]):
        h.update(f"{row['repository']}@{row['revision']}\n".encode())
    return h.hexdigest()


def write_csv(rows: list[dict], out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def docker_pyright_version(image: str) -> str:
    """The pyright version baked into the runtime image ('' if docker or the image is unavailable)."""
    try:
        done = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", image,
                               envbench_check.PYRIGHT_BIN_DEFAULT, "--version"],
                              capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (done.stdout or "").strip().splitlines()[-1] if done.returncode == 0 and done.stdout.strip() else ""


def check_one(row: dict) -> dict:
    sha, url, repo = envbench_source.parse_spec(row["clone_url"])
    path, log = envbench_source.fetch_pinned(url, sha, timeout=300)
    if path is None:
        return {"repository": repo, "github": False, "log": log, "kb": None}
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(path) for f in fs
               if not os.path.islink(os.path.join(dp, f)))
    shutil.rmtree(path, ignore_errors=True)
    return {"repository": repo, "github": True, "log": "", "kb": round(size / 1024)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-file", required=True, help="python_baseline_failure.jsonl of the EnvBench dataset")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--hf-revision", default=envbench_source.HF_REVISION, help="Hugging Face dataset revision the list came from")
    parser.add_argument("--code-commit", default="", help="commit of github.com/JetBrains-Research/EnvBench that was read")
    parser.add_argument("--image", default="repair-tool-runtime-envbench:1-linux-amd64")
    parser.add_argument("--no-image-query", action="store_true", help="do not start docker to read the pyright version")
    parser.add_argument("--check-fetch", action="store_true", help="try to shallow-fetch every pinned commit from GitHub")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)

    rows = build_rows(load_entries(args.dataset_file))
    write_csv(rows, args.out)
    meta = {
        "dataset": "EnvBench, Python subset (split python_baseline_failure)",
        "paper": "Eliseeva et al., arXiv:2503.14443",
        "source": "https://huggingface.co/datasets/JetBrains-Research/EnvBench",
        "hf_revision": args.hf_revision,
        "code_repository": "https://github.com/JetBrains-Research/EnvBench",
        "code_commit": args.code_commit,
        "repositories": len(rows),
        "expected_python_repositories": EXPECTED_PYTHON_REPOS,
        "list_sha256": list_fingerprint(rows),
        "pyright_version": "" if args.no_image_query else docker_pyright_version(args.image),
        "check": "pyright <repo> --level error --outputjson --pythonpath <env python>; PASS = 0 reportMissingImports",
        "csv": os.path.relpath(args.out, ROOT),
    }
    with open(args.out.replace(".csv", ".meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    print(f"{len(rows)} repositories -> {args.out}")
    if len(rows) != EXPECTED_PYTHON_REPOS:
        print(f"WARNING: expected {EXPECTED_PYTHON_REPOS} Python repositories, found {len(rows)}", file=sys.stderr)
    if args.check_fetch:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(check_one, rows))
        with open(args.out.replace(".csv", ".fetch_check.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
        served = sum(1 for r in results if r["github"])
        print(f"GitHub still serves {served} of {len(results)} pinned commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
