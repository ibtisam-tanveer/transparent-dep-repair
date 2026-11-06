"""Reproducible selection of the pilot sample (TASK_next_steps.md step 3).

Breadth across repositories matters more than count, so: ONE notebook per
repository, every repository distinct across the whole sample, drawn at random
(seeded) from the in-scope taxonomy-labelled failures.

Pools (and why):
  A  tier=confirmed   -- the confirmed in-scope dependency failures
                         (ModuleNotFoundError). In failures_2023.csv this is the
                         ONLY category with a confirmed tier.
  C  tier=candidate   -- API-removal failures (AttributeError); no confirmed tier exists
  D  tier=candidate   -- install/build/version-conflict failures; no confirmed tier exists
The task asked for confirmed A/C/D, but confirmed C and D do not exist in the
dataset, and an A-vs-C-vs-D breakdown needs them: so a small C/D slice is drawn
from the candidate tier and is labelled as such everywhere (analyze_results
reports both the confirmed-only and the confirmed+candidate framing).

Repositories with more than --max-labelled-per-repo labelled notebooks are
excluded (a proxy for very large clones that would dominate the run).

The 2023 snapshot is old: repos move, rename and delete notebooks. By default
each candidate is checked against the repository's CURRENT tree (a tree-only
`git clone --filter=blob:none --no-checkout`, no file contents are fetched) and
a repository whose labelled notebooks no longer exist, or that cannot be
cloned, is skipped and the next one drawn -- otherwise sample slots are spent
on notebooks the tool could never run. How many drawn repositories were stale
is recorded in the .json (it is itself a finding about the dataset).

Writes <out>.csv (same columns as failures_2023.csv, runnable by run_eval.py) and
<out>.json (every parameter and count needed to reproduce / audit the draw).
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
from collections import Counter
from typing import Callable

DEFAULT_QUOTAS = {"A": 18, "C": 4, "D": 4}
POOL_TIER = {"A": "confirmed", "C": "candidate", "D": "candidate"}


def current_tree_paths(clone_url: str, timeout: int = 120) -> set[str] | None:
    """File paths at the repository's current HEAD, or None if it cannot be
    cloned. Tree only: nothing but metadata is downloaded."""
    tmp = tempfile.mkdtemp(prefix="sample_tree_")
    try:
        cloned = subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--quiet", clone_url, tmp],
            capture_output=True, text=True, timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if cloned.returncode != 0:
            return None
        listing = subprocess.run(["git", "-C", tmp, "ls-tree", "-r", "--name-only", "HEAD"],
                                 capture_output=True, text=True, timeout=timeout)
        return set(listing.stdout.splitlines()) if listing.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def select(
    rows: list[dict], quotas: dict[str, int], seed: int, max_labelled_per_repo: int,
    tree_lookup: Callable[[str], set[str] | None] | None = None,
) -> tuple[list[dict], dict]:
    """`tree_lookup(clone_url)` -> the repo's current file paths (None = unreachable);
    when given, only notebooks that still exist are eligible."""
    per_repo = Counter(r["repository"] for r in rows)
    eligible_repos = {repo for repo, n in per_repo.items() if n <= max_labelled_per_repo}

    rng = random.Random(seed)
    chosen: list[dict] = []
    used_repos: set[str] = set()
    pool_sizes: dict[str, dict] = {}
    skipped = {"repository_unreachable": [], "labelled_notebooks_no_longer_exist": []}
    tree_cache: dict[str, set[str] | None] = {}

    for category in sorted(quotas):  # fixed order => reproducible
        pool = [
            r for r in rows
            if r["category"] == category and r["tier"] == POOL_TIER[category] and r["repository"] in eligible_repos
        ]
        by_repo: dict[str, list[dict]] = {}
        for r in pool:
            by_repo.setdefault(r["repository"], []).append(r)
        pool_sizes[category] = {"tier": POOL_TIER[category], "notebooks": len(pool), "repositories": len(by_repo)}

        repos = sorted(repo for repo in by_repo if repo not in used_repos)
        rng.shuffle(repos)
        taken = 0
        for repo in repos:
            if taken >= quotas[category]:
                break
            candidates = sorted(by_repo[repo], key=lambda r: r["notebook_path"])
            if tree_lookup is not None:
                url = candidates[0]["clone_url"]
                if url not in tree_cache:
                    tree_cache[url] = tree_lookup(url)
                tree = tree_cache[url]
                if tree is None:
                    skipped["repository_unreachable"].append(repo)
                    continue
                candidates = [r for r in candidates if r["notebook_path"] in tree]
                if not candidates:
                    skipped["labelled_notebooks_no_longer_exist"].append(repo)
                    continue
            pick = candidates[rng.randrange(len(candidates))]
            chosen.append(pick)
            used_repos.add(repo)
            taken += 1

    audit = {
        "seed": seed, "quotas": quotas, "max_labelled_per_repo": max_labelled_per_repo,
        "repositories_excluded_as_too_large": sum(1 for n in per_repo.values() if n > max_labelled_per_repo),
        "pools": pool_sizes,
        "verified_against_current_tree": tree_lookup is not None,
        "skipped_repositories": {k: len(v) for k, v in skipped.items()},
        "skipped_repository_names": skipped,
        "selected": len(chosen),
        "selected_by_category": dict(Counter(r["category"] for r in chosen)),
        "selected_by_tier": dict(Counter(r["tier"] for r in chosen)),
        "selected_by_python_version": dict(Counter((r["python_version"] or "?") for r in chosen)),
        "distinct_repositories": len({r["repository"] for r in chosen}),
        "notebooks": [
            {"repository": r["repository"], "notebook_path": r["notebook_path"], "category": r["category"],
             "tier": r["tier"], "python_version": r["python_version"]}
            for r in chosen
        ],
    }
    return chosen, audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--failures-csv", default="dataset/failures_2023.csv")
    parser.add_argument("--out", default="evaluation/samples/pilot_sample")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-labelled-per-repo", type=int, default=60)
    parser.add_argument("--no-verify", action="store_true",
                        help="do not check candidates against each repository's current tree (offline; stale paths possible)")
    parser.add_argument("--quota", action="append", default=[], metavar="CAT=N", help="e.g. --quota A=18 (repeatable)")
    args = parser.parse_args()

    quotas = dict(DEFAULT_QUOTAS)
    for item in args.quota:
        cat, _, n = item.partition("=")
        quotas[cat.strip().upper()] = int(n)

    with open(args.failures_csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    chosen, audit = select(
        rows, quotas, args.seed, args.max_labelled_per_repo,
        tree_lookup=None if args.no_verify else current_tree_paths,
    )
    audit["source_csv"] = args.failures_csv

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out + ".csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(chosen)
    with open(args.out + ".json", "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2)
    print(f"selected {audit['selected']} notebooks from {audit['distinct_repositories']} repositories -> {args.out}.csv / .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
