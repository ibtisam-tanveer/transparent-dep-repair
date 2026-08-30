"""Extract notebooks whose execution failed for a Python-dependency reason.

Read-only on db.sqlite. Writes dataset/dependency_failures.csv. See
dataset/NOTES.md for the evidence behind the filter used here (which is a
DRAFT for supervisor confirmation, not a finished definition) — in short,
executions.reason came back (on inspection) holding the raised exception's
class name (occasionally "ClassName: message") for real execution attempts,
and the two classes that mean "a dependency was missing" are ModuleNotFoundError
and ImportError -- matching the original GigaScience authors' own
get_repro_missing_dependencies() in analyses/analysis_helpers_executions.py.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys

DEFAULT_DB_PATH = "computational-reproducibility-pmc/analyses/db.sqlite"
DEFAULT_OUTPUT_PATH = "dataset/dependency_failures.csv"
MAX_ERROR_MESSAGE_LENGTH = 300

# mode IN (3, 5): the two "real notebook execution attempt" modes present in
# this database (dependencies installed / anaconda env with declared
# dependencies) -- see NOTES.md. processed & 4 == 4: the E_EXCEPTION bit
# (archaeology/consts.py), i.e. the execution actually raised.
QUERY = """
SELECT
    r.repository            AS repository,
    n.name                  AS notebook_path_or_name,
    e.reason                AS reason_raw,
    n.language_version      AS python_version,
    r.id                    AS repository_id
FROM executions e
JOIN notebooks n     ON e.notebook_id = n.id
JOIN repositories r  ON n.repository_id = r.id
WHERE e.mode IN (3, 5)
  AND (e.processed & 4) = 4
  AND (
        e.reason = 'ModuleNotFoundError'
     OR e.reason = 'ImportError'
     OR e.reason LIKE 'ModuleNotFoundError:%'
     OR e.reason LIKE 'ImportError:%'
     OR e.reason LIKE '%No module named%'
  )
ORDER BY r.repository, n.name;
"""


def connect_readonly(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def split_reason(reason_raw: str) -> tuple[str, str]:
    """'ModuleNotFoundError' -> ('ModuleNotFoundError', same).
    'FileNotFoundError: [Errno 2] ...' -> ('FileNotFoundError', full text).
    In this dataset ModuleNotFoundError/ImportError never carry a message
    (see NOTES.md), so error_type and error_message end up identical here --
    kept as two columns anyway since the task asks for both and other
    databases/versions might populate a message.
    """
    if ":" in reason_raw:
        error_type, _, _ = reason_raw.partition(":")
        return error_type.strip(), reason_raw.strip()
    return reason_raw.strip(), reason_raw.strip()


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

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "repository",
                "notebook_path_or_name",
                "error_type",
                "error_message",
                "python_version",
                "repository_id",
            ]
        )
        for repository, notebook, reason_raw, python_version, repository_id in rows:
            error_type, error_message = split_reason(reason_raw)
            writer.writerow(
                [
                    repository,
                    notebook,
                    error_type,
                    error_message[:MAX_ERROR_MESSAGE_LENGTH],
                    python_version,
                    repository_id,
                ]
            )

    print(f"Wrote {len(rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
