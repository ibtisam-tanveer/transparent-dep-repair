"""Add the paper's date to a failures CSV (input for lever era_date).

    python evaluation/add_paper_dates.py --in evaluation/samples/lever_misses.csv \
        --out evaluation/samples/lever_misses_dated.csv \
        --db ../computational-reproducibility-pmc/computational-reproducibility-pmc/analyses/db.sqlite

The 2023 GigaScience database ties every repository to an article (`repositories.article_id` ->
`article`). `paper_date` is the article's published date; when that is empty the accepted date, then the
received date, is used and `paper_date_kind` says which. Read-only on the database; a repository the
database does not know gets empty columns (the harness then falls back to the next date source).
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys

QUERY = """
SELECT a.published_date, a.accepted_date, a.received_date
FROM repositories r JOIN article a ON a.id = r.article_id
WHERE r.id = ?
"""


def paper_date_for(conn: sqlite3.Connection, repository_id: str) -> tuple[str, str]:
    """(date, kind) -- ('', '') when unknown. Never invents: only values stored in the database."""
    try:
        row = conn.execute(QUERY, (int(repository_id),)).fetchone()
    except (ValueError, sqlite3.Error):
        return "", ""
    if not row:
        return "", ""
    for kind, value in zip(("published", "accepted", "received"), row):
        if value and str(value).strip():
            return str(value).strip()[:10], kind
    return "", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="src", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--db", required=True, help="the 2023 GigaScience db.sqlite (opened read-only)")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    with open(args.src, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = [c for c in reader.fieldnames if c not in ("paper_date", "paper_date_kind")] + ["paper_date", "paper_date_kind"]
        rows = list(reader)
    for r in rows:
        r["paper_date"], r["paper_date_kind"] = paper_date_for(conn, r.get("repository_id", ""))
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {args.out}; paper date found for {sum(1 for r in rows if r['paper_date'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
