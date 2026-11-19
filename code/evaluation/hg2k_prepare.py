"""Prepare the HG2.9K (Gistable "hard gists") dataset for the evaluation harness -- TASK_hg2k_evaluation.md Part A.

HG2.9K = the 2,891 single-file Python gists of DockerizeMe's `hard-gists/` directory: gists that still
fail with an ImportError after Gistable's own naive resolution (Horton & Parnin, ICSE 2019). Source:
https://github.com/dockerizeme/dockerizeme (directory `hard-gists/`, one directory per gist with one
`snippet.py`). The dataset is downloaded by hand into its own directory, outside this repository:

    git clone --depth 1 https://github.com/dockerizeme/dockerizeme.git <dir>

and this script turns it into the failures-CSV format that `run_eval.py` reads (one row per gist, the gist
being its own single-file "repository", `clone_url` = `local:hard-gists/<id>`; run_eval copies it, never
modifies it). Nothing here executes a gist: the static columns come from `ast.parse` and `hashlib` only.

    python evaluation/hg2k_prepare.py --dataset-root ../datasets/dockerizeme

Writes evaluation/samples/hg2k_all.csv and evaluation/samples/hg2k_all.meta.json (dataset commit, counts,
SHA-256 of the whole list -- the fingerprint a later re-run, e.g. the knowledge-graph experiment, checks).
Which gists are run is chosen by run_eval's own `--sample N --seed S` (a seeded shuffle of the sorted ids,
so the sample of size N is a prefix of the sample of any larger size with the same seed).
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import subprocess
import sys
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_FIELDS = ["repository", "clone_url", "notebook_path", "error_type", "error_message", "category", "tier",
              "python_version", "repository_id", "gist_id", "sha256", "lines", "py3_parses"]
SNIPPET = "snippet.py"


def static_facts(source: str) -> dict:
    """Facts readable without running anything. `py3_parses` False means the file is not valid Python 3
    source (almost always Python 2 syntax: print statements, `except X, e`, backticks, exec statements)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # gists are full of invalid-escape DeprecationWarnings; irrelevant here
        try:
            ast.parse(source)
            parses = True
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            parses = False
    return {"py3_parses": parses, "lines": len(source.splitlines())}


_CONTAINER_PARSER = r"""
import ast, json, os, sys, warnings
warnings.simplefilter("ignore")
out = {}
for gid in sorted(os.listdir("/data/hard-gists")):
    src = open(os.path.join("/data/hard-gists", gid, "snippet.py"), "rb").read().decode("utf-8", "replace")
    try:
        ast.parse(src); out[gid] = True
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        out[gid] = False
print(json.dumps({"python": sys.version.split()[0], "parses": out}))
"""
DOCKER_IMAGE = "repair-tool-runtime:1-linux-amd64"


def parse_in_container(dataset_root: str, image: str = DOCKER_IMAGE) -> tuple[str, dict[str, bool]]:
    """Which gists parse as Python 3, decided by the interpreter the tool actually runs gists on (the
    runtime image's Python 3.11), not by whatever Python the host has: Python 3.14 (PEP 758) accepts the
    Python-2 spelling `except A, e:`, so a host parse under-counts Python-2 files. The gists are only
    parsed, never executed, and the dataset is mounted read-only."""
    out = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/amd64", "-v", f"{os.path.abspath(dataset_root)}:/data:ro",
         "--entrypoint", "python3", image, "-c", _CONTAINER_PARSER],
        capture_output=True, text=True, timeout=600,
    )
    if out.returncode != 0:
        raise SystemExit(f"container parse failed: {out.stderr[-500:]}")
    data = json.loads(out.stdout.strip().splitlines()[-1])
    return data["python"], data["parses"]


def build_rows(dataset_root: str, parses: dict[str, bool] | None = None) -> list[dict]:
    hard = os.path.join(dataset_root, "hard-gists")
    rows = []
    for gist_id in sorted(os.listdir(hard)):
        path = os.path.join(hard, gist_id, SNIPPET)
        if not os.path.isfile(path):
            raise SystemExit(f"unexpected layout: {path} missing")
        raw = open(path, "rb").read()
        facts = static_facts(raw.decode("utf-8", errors="replace"))
        if parses is not None:
            facts["py3_parses"] = parses[gist_id]
        rows.append({
            "repository": f"hg2k/{gist_id}", "clone_url": f"local:hard-gists/{gist_id}", "notebook_path": SNIPPET,
            # The harness wants a taxonomy label; every HG2.9K gist is, by construction, an import failure in a
            # clean environment, so it is filed as a plain missing-package case (A) -- the label is a placeholder,
            # the analysis in analyze_hg2k.py uses what the run observed, not this.
            "error_type": "ImportError", "error_message": "", "category": "A", "tier": "confirmed",
            "python_version": "", "repository_id": "0",
            "gist_id": gist_id, "sha256": hashlib.sha256(raw).hexdigest(), "lines": facts["lines"],
            "py3_parses": str(facts["py3_parses"]),
        })
    return rows


def _git_head(dataset_root: str) -> str:
    try:
        return subprocess.run(["git", "-C", dataset_root, "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-root", required=True, help="the clone of dockerizeme/dockerizeme")
    parser.add_argument("--out", default=os.path.join(ROOT, "evaluation", "samples", "hg2k_all.csv"))
    parser.add_argument("--no-docker-parse", action="store_true",
                        help="parse with the host Python instead of the runtime image's (NOT recommended: see parse_in_container)")
    args = parser.parse_args(argv)

    if args.no_docker_parse:
        parser_python, parses = sys.version.split()[0] + " (host)", None
    else:
        parser_python, parses = parse_in_container(args.dataset_root)
        parser_python += " (runtime image)"
    rows = build_rows(args.dataset_root, parses)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    ids_hash = hashlib.sha256("\n".join(f"{r['gist_id']} {r['sha256']}" for r in rows).encode()).hexdigest()
    meta = {
        "dataset": "HG2.9K (DockerizeMe hard-gists)", "source": "https://github.com/dockerizeme/dockerizeme",
        "source_commit": _git_head(args.dataset_root), "gists": len(rows),
        "py3_parses": sum(r["py3_parses"] == "True" for r in rows),
        "py3_syntax_error_likely_python2": sum(r["py3_parses"] == "False" for r in rows),
        "parsed_with_python": parser_python, "list_sha256": ids_hash, "csv": os.path.relpath(args.out, ROOT),
    }
    with open(args.out.replace(".csv", ".meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
