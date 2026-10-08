"""Read-only exploration of db.sqlite (GigaScience reproducibility database).

Prints every table with its row count, then columns + 5 sample rows for the
six tables this task cares about, then distinct values/counts for the
columns that turned out (on inspection, not assumption) to hold execution
status or error information. See dataset/NOTES.md for what was found and why
these specific columns were picked.

Never writes to the database: opened with mode=ro (SQLite's own read-only
URI flag, enforced by SQLite itself, not just by this script's discipline).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

DEFAULT_DB_PATH = "computational-reproducibility-pmc/analyses/db.sqlite"

DETAIL_TABLES = [
    "notebooks",
    "executions",
    "notebook_modules",
    "cell_modules",
    "requirement_files",
    "repositories",
]

# Columns confirmed (see NOTES.md) to carry execution status/error info.
# Not purely name-guessed: 'mode' and 'processed' don't look status-like by
# name alone, but archaeology/consts.py (the authors' own constants file)
# confirms they're bitmask/enum status fields.
STATUS_COLUMNS = {
    "executions": ["mode", "processed", "reason"],
    "notebooks": ["kernel", "language_version"],
}


def connect_readonly(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def print_table_overview(conn: sqlite3.Connection) -> None:
    print("=" * 70)
    print("TABLES (name: row count)")
    print("=" * 70)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    for (name,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"  {name}: {count}")
    print()


def print_table_detail(conn: sqlite3.Connection, table: str) -> None:
    print("=" * 70)
    print(f"TABLE: {table}")
    print("=" * 70)

    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
    col_names = [c[1] for c in columns]
    print("columns:", ", ".join(col_names))
    print()

    print("5 sample rows:")
    rows = conn.execute(f"SELECT * FROM {table} LIMIT 5").fetchall()
    for row in rows:
        print(" ", dict(zip(col_names, row)))
    print()


def print_status_column_distributions(conn: sqlite3.Connection) -> None:
    print("=" * 70)
    print("STATUS/ERROR-LIKE COLUMNS: distinct values with counts")
    print("=" * 70)
    for table, columns in STATUS_COLUMNS.items():
        for col in columns:
            print(f"\n--- {table}.{col} ---")
            rows = conn.execute(
                f"SELECT {col}, COUNT(*) AS n FROM {table} "
                f"GROUP BY {col} ORDER BY n DESC LIMIT 30"
            ).fetchall()
            for value, n in rows:
                print(f"  {value!r}: {n}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "db_path", nargs="?", default=DEFAULT_DB_PATH, help="path to db.sqlite"
    )
    args = parser.parse_args()

    conn = connect_readonly(args.db_path)
    try:
        print_table_overview(conn)
        for table in DETAIL_TABLES:
            print_table_detail(conn, table)
        print_status_column_distributions(conn)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
