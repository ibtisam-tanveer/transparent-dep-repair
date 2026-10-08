"""Extract the taxonomy-labelled failure set from the 2023 GigaScience
rerun's db.sqlite -- TASK_evaluation.md section 1.1.

Read-only on db.sqlite. Writes dataset/failures_2023.csv: one row per
notebook execution that actually raised an exception, labelled with a
dependency-failure-taxonomy category (A-E) and confidence tier (see
dataset/taxonomy.py / DEPENDENCY_FAILURE_TAXONOMY.md).

Population queried: `executions.mode IN (3, 5)` (the two "real execution
attempt" modes -- same as the 2021 run, see dataset/NOTES.md) AND
`executions.processed & 4 = 4` (the E_EXCEPTION bit from the authors' own
archaeology/consts.py: this execution actually raised). This is wider than
the 2021 extraction's ImportError/ModuleNotFoundError-only filter -- every
exception in this population is pulled and classified, not just the two
"missing dependency" classes -- because the taxonomy's whole point is to
separate the *several* causes dependency decay can take, not just flag
missing packages.

Deliberately NOT included here (see dataset/NOTES_2023.md): a separate,
larger population of 5,429 executions where dependency installation
itself failed before the notebook ever got a chance to run and raise a
Python exception (`processed = 0`, reason = the authors' own synthetic
'<Install Dependency Error>' marker) -- a real execution attempt never
happened for these, so there is no Python exception to classify by this
module's rules. Flagged as a candidate for a later, separate pass, not
silently merged into this taxonomy-labelled population.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter

from taxonomy import classify

DEFAULT_DB_PATH = "computational-reproducibility-pmc/computational-reproducibility-pmc/analyses/db.sqlite"
DEFAULT_OUTPUT_PATH = "dataset/failures_2023.csv"
MAX_ERROR_MESSAGE_LENGTH = 500

# Same two "real execution attempt" modes as the 2021 run (dataset/NOTES.md);
# the E_EXCEPTION bit (processed & 4) is the authors' own
# get_repro_exceptions() check, confirmed again directly against this db.
QUERY = """
SELECT
    r.repository       AS repository,
    n.name              AS notebook_path,
    e.reason            AS reason_raw,
    n.language_version  AS python_version,
    r.id                AS repository_id
FROM executions e
JOIN notebooks n     ON e.notebook_id = n.id
JOIN repositories r  ON n.repository_id = r.id
WHERE e.mode IN (3, 5)
  AND (e.processed & 4) = 4
ORDER BY r.repository, n.name;
"""


def connect_readonly(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def clone_url_for(repository: str) -> str:
    """'ncbi/elastic-blast' -> 'https://github.com/ncbi/elastic-blast.git'.
    Every repository in this db is on github.com (confirmed: SELECT
    DISTINCT domain FROM repositories -> only 'github.com')."""
    return f"https://github.com/{repository}.git"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", nargs="?", default=DEFAULT_DB_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    conn = connect_readonly(args.db_path)
    try:
        rows = conn.execute(QUERY).fetchall()
    finally:
        conn.close()

    category_tier_counts: Counter = Counter()
    repositories_seen: set[str] = set()

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "repository",
                "clone_url",
                "notebook_path",
                "error_type",
                "error_message",
                "category",
                "tier",
                "python_version",
                "repository_id",
            ]
        )
        for repository, notebook_path, reason_raw, python_version, repository_id in rows:
            category, tier, error_type, error_message = classify(reason_raw)
            category_tier_counts[(category, tier)] += 1
            repositories_seen.add(repository)
            writer.writerow(
                [
                    repository,
                    clone_url_for(repository),
                    notebook_path,
                    error_type,
                    error_message[:MAX_ERROR_MESSAGE_LENGTH],
                    category,
                    tier,
                    python_version,
                    repository_id,
                ]
            )

    print(f"Wrote {len(rows)} rows to {args.output}")
    print(f"Distinct repositories: {len(repositories_seen)}")
    print()
    print("Per (category, tier) counts:")
    for category in "ABCDE":
        for tier in ("confirmed", "candidate", "excluded"):
            n = category_tier_counts.get((category, tier), 0)
            if n:
                print(f"  {category} / {tier}: {n}")
    print()
    print("Per-category totals (confirmed + candidate, excluding 'excluded'):")
    for category in "ABCDE":
        n = sum(category_tier_counts.get((category, t), 0) for t in ("confirmed", "candidate"))
        print(f"  {category}: {n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
