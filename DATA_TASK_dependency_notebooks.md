# Data Task: Extract a draft set of dependency-broken notebooks from `db.sqlite`

> **This is a DATA task, not a build phase.** It does not touch the repair tool
> (`repair_tool/`), and it is **not Phase 4**. Its only job is to turn the
> GigaScience reproducibility database into a small, documented, *provisional*
> list of notebooks that fail for dependency reasons, for later use as an
> evaluation set. Its output is a draft to be **confirmed with the supervisor**,
> not a final dataset.

## Context

`db.sqlite` (~371 MB) is the results database from Samuel & Mietchen,
"Computational reproducibility of Jupyter notebooks from biomedical
publications" (GigaScience 2024), downloaded from Zenodo record 6802158. It has
~24 tables describing articles, repositories, notebooks, cells, modules, and
execution results for ~27,000 notebooks. Most notebooks fail to run; a large
share of failures are dependency-related — those are the ones we want.

The same Zenodo zip contains analysis notebooks (`analyses/N8.Execution.ipynb`,
`analyses/N5.Modules.ipynb`, and `analysis_helpers_executions.py`). These show
**how the original authors queried these tables**, and are the most reliable
guide to what each column means. Consult them rather than guessing.

## Goal

Produce (a) a short report of how the database records notebook execution
outcomes and errors, and (b) a CSV listing the notebooks whose failure looks
dependency-related — each with enough metadata to locate and categorise it.

## Guiding principle: REPORT, don't ASSUME

The database has no documentation of which column means "failed because of a
dependency." Do **not** silently pick one. Instead:
- Inspect the real tables and show evidence (column names, sample values,
  distinct error types with counts).
- State explicitly which column(s) you used to define "dependency failure" and
  **why**, citing what you saw in the data or in the authors' analysis notebooks.
- Treat the resulting filter as a **hypothesis** clearly labelled for the
  supervisor to confirm — not as settled fact.

## Deliverables

Create a `dataset/` folder (separate from `repair_tool/`) containing:

1. `explore_db.py` — a **read-only** script that opens `db.sqlite` and prints:
   - every table name with its row count;
   - for `notebooks`, `executions`, `notebook_modules`, `cell_modules`,
     `requirement_files`, and `repositories`: the column names and 5 sample rows;
   - for any column that appears to hold an execution status or error
     (e.g. an exception name/message/type): the distinct values and their counts.
2. `extract_dependency_failures.py` — a **read-only** script that runs the agreed
   filter and writes `dataset/dependency_failures.csv` with, per notebook:
   `repository` (owner/name), `notebook_path_or_name`, `error_type`,
   `error_message` (truncated), and any available `python_version` / repo id.
   Include a total count printed at the end.
3. `dataset/NOTES.md` — a short write-up:
   - which tables/columns were used and how "dependency failure" was defined;
   - the evidence and reasoning behind that definition (with reference to the
     authors' analysis notebooks where relevant);
   - counts: total notebooks, total failing, total dependency-failing;
   - a clearly marked **"To confirm with supervisor"** section listing every
     assumption made, especially the choice of the error/status column.

## Constraints

- **Read-only on the database.** Never modify `db.sqlite`. Open it read-only
  (e.g. `sqlite3.connect("file:db.sqlite?mode=ro", uri=True)`) or work on a copy.
- Python 3.10+, standard library (`sqlite3`, `csv`) — `pandas` is allowed if
  already available but not required.
- Deterministic and re-runnable: the scripts take the `db.sqlite` path as an
  argument or a clear constant at the top; no hard-coded absolute paths.
- Keep it lightweight: a first, documented draft — do **not** over-engineer or
  try to perfect the filter. Getting a defensible first CSV plus honest notes is
  the whole job.

## Draft SQL (adjust column names AFTER inspecting the real schema)

These are starting points, not final — the exact column names must come from
`explore_db.py`'s output, not from this sketch.

```sql
-- shape of the executions table
SELECT * FROM executions LIMIT 20;

-- what kinds of outcomes/errors exist (replace <error_col> with the real column)
SELECT <error_col>, COUNT(*) AS n
FROM executions
GROUP BY <error_col>
ORDER BY n DESC;

-- candidate dependency failures (replace column names once known)
SELECT r.name AS repository, n.name AS notebook,
       e.<error_type_col> AS error_type, e.<error_msg_col> AS error_message
FROM executions e
JOIN notebooks n     ON e.notebook_id = n.id
JOIN repositories r  ON n.repository_id = r.id
WHERE e.<error_type_col> IN ('ModuleNotFoundError', 'ImportError')
   OR e.<error_msg_col> LIKE '%No module named%';
```

## Definition of done

- `explore_db.py` runs read-only and prints the schema overview and the distinct
  error/status values with counts.
- `extract_dependency_failures.py` produces `dependency_failures.csv` with one row
  per dependency-failing notebook and prints the total.
- `NOTES.md` documents the tables/columns used, the reasoning, the counts, and an
  explicit "To confirm with supervisor" list of assumptions.
- The database file is provably unmodified (opened read-only or copied).

## Out of scope (do NOT do here)

- No changes to the repair tool; this task is independent of Phases 1–5.
- Do not download or clone the actual notebook files / repositories yet, and do
  not execute any notebooks — this task only builds the *list* and its metadata.
  Fetching notebook content for real runs is a separate, later task.
- Do not treat the filter as final — it is a draft for supervisor confirmation.
- No `.ipynb` handling, no LLM calls.

## Why this is provisional

The single most important decision here — which column identifies a dependency
failure — depends on schema knowledge only the dataset's authors (the supervisor)
have with certainty. The agent's output is a well-reasoned first draft that makes
the supervisor conversation concrete ("here's what I found and how I filtered — is
this the right column?"), not a finished evaluation set.
