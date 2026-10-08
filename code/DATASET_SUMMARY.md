# Data Task Summary — Draft dependency-broken notebook list from `db.sqlite`

Status: **done** (as a draft — explicitly not a finished evaluation set,
per the task brief). Independent of `repair_tool/`; touches no phase code.

## What was asked (`DATA_TASK_dependency_notebooks.md`)

Turn the GigaScience reproducibility database into a small, documented,
*provisional* list of notebooks that fail for dependency reasons, for later
use as an evaluation set — explicitly **not Phase 4**, and explicitly a
draft for the supervisor to confirm, not settled fact. Guiding principle
stated in the brief: **report, don't assume** — show evidence for whichever
column is used to define "dependency failure," rather than picking one
silently.

## What was built

| File | Purpose |
|---|---|
| `dataset/explore_db.py` | Read-only: prints every table + row count, columns + 5 sample rows for the 6 tables the brief names, and distinct values/counts for the columns that turned out to hold status/error info |
| `dataset/extract_dependency_failures.py` | Read-only: runs the agreed filter, writes `dataset/dependency_failures.csv`, prints the total |
| `dataset/dependency_failures.csv` | The draft list: 1,362 rows |
| `dataset/NOTES.md` | Full write-up: evidence, reasoning, counts, "to confirm with supervisor" |

## How the filter was actually decided (not guessed)

Before writing any SQL, I read the GigaScience authors' own analysis code
that ships in the same download (`analyses/analysis_helpers_executions.py`,
`archaeology/execution_rules.py`, `archaeology/consts.py`) — exactly what
the brief recommends ("these show how the original authors queried these
tables, and are the most reliable guide to what each column means").

That surfaced three real, checkable facts, not assumptions:

1. **`executions.mode`** is a bitmask encoding `(anaconda, dependencies,
   cellorder)`, not an error type. Only two values occur in this database:
   3 and 5 — both are "real execution attempt" variants.
2. **`executions.processed`** is a bitmask too (`E_EXCEPTION = 4`, `E_TIMEOUT
   = 8`, etc., from the authors' own `consts.py`). `processed & 4 == 4`
   means "this execution raised an exception" — confirmed by matching it
   against the authors' own `get_repro_exceptions()` function, which uses
   the identical check.
3. **`executions.reason`**, for those exception rows, holds the raised
   exception's class name (checked directly against real sample rows, not
   inferred from the column name). The authors' own
   `get_repro_missing_dependencies()` function defines "missing
   dependencies" as `reason` being `ImportError` or `ModuleNotFoundError` —
   so the filter used here is **the same definition the dataset's own
   creators used**, not an independent guess.

## A significant finding: this is the paper's 2021 run, not the 2023 rerun

The task brief expected "~27,000 notebooks." The actual file has 9,625.
Rather than treat this as a possible download error, I checked the file
directly: it's 370,950,144 bytes (matches the brief's "~371 MB" exactly)
and passes SQLite's `PRAGMA integrity_check` — complete and uncorrupted.

Then I checked the published paper (Samuel & Mietchen, GigaScience 2024,
doi: 10.1093/gigascience/giad113) directly. It describes running the same
study **twice, two years apart**:

| Run | Articles | Notebooks | Repositories (with notebooks) |
|---|---|---|---|
| 2021 (initial) | 1,419 | 9,625 | 1,117 |
| 2023 (rerun) | 3,467 | 27,271 | 2,660 |

This `db.sqlite`'s counts match the **2021 run exactly**. So the "~27,000"
mismatch isn't a bad download and isn't (necessarily) an error in the task
brief either — it's that the brief's number describes the 2023 rerun, while
the file actually in hand is the complete database for the earlier run.
This is now the sharpest, best-evidenced open question for the supervisor
meeting: *use the 2021 run as-is, or redo this against a 2023-rerun
database if one exists?*

## Results

| | count |
|---|---|
| Total notebooks (this database) | 9,625 |
| Total execution rows | 4,673 |
| Execution rows that raised an exception | 2,265 |
| ...classified as a dependency failure (`ImportError`/`ModuleNotFoundError`) | **1,362** |
| Distinct repositories represented | 311 |
| Breakdown | `ModuleNotFoundError`: 832 · `ImportError`: 530 |

1,362 / 2,265 ≈ 60% of all execution exceptions in this database are
dependency-related by this definition.

## Verified read-only, provably

Beyond just using the `mode=ro` URI flag, I directly tested that a write
attempt fails at the SQLite driver level (not just by convention):
```
DELETE FROM notebooks WHERE id=1  ->  sqlite3.OperationalError:
attempt to write a readonly database
```

## What was deliberately left out, and why (per "report, don't assume")

- **`AttributeError` (24 rows) and `CalledProcessError` (24 rows)** —
  plausibly dependency-related (an `AttributeError` could be exactly like
  this thesis's own `broken_examples/02_numpy_float.py` — a removed API on
  a newer library version) but could equally be an unrelated bug. Telling
  these apart needs per-row inspection, which the brief explicitly scoped
  out ("do not over-engineer or try to perfect the filter"). Flagged as an
  open item rather than guessed either way.
- **No notebook content was downloaded, cloned, or executed.** The CSV is
  metadata only (repository, notebook name, error type/message, Python
  version) — explicitly out of scope per the brief; running these against
  `repair_tool/` is a separate, later task.

## Definition of done — verified

- `explore_db.py` runs read-only, prints schema overview + distinct
  error/status values with counts — confirmed by running it.
- `extract_dependency_failures.py` produces `dependency_failures.csv`,
  prints the total (1,362) — confirmed, and confirmed one row per notebook
  (no duplicates: 1,362 rows, 1,362 unique repository+notebook pairs).
- `NOTES.md` documents tables/columns used, reasoning, counts, and an
  explicit "to confirm with supervisor" section (now 4 items, updated with
  the 2021/2023-run finding).
- Database file provably unmodified — confirmed via a direct failed-write
  test, not just by using the read-only flag and trusting it.

## How to run / verify

```bash
python dataset/explore_db.py
python dataset/extract_dependency_failures.py
```

Both default to `computational-reproducibility-pmc/analyses/db.sqlite`
(overridable as the first CLI argument); both open read-only.
