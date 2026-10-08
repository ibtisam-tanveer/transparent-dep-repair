# Notes — dependency-broken notebooks draft dataset

**Status: DRAFT, for supervisor confirmation.** Not a finished evaluation
set — see `DATA_TASK_dependency_notebooks.md` for the full task brief.

## What this actually is

`db.sqlite` here is the `computational-reproducibility-pmc` subset: it has
**9,625 notebooks** across **2,177 repositories**, but only **4,673 rows**
in `executions` — i.e. most notebooks in this copy were never actually
executed (or that data isn't in this particular database file).

**Resolved (checked against the paper directly):** Samuel & Mietchen
(GigaScience 2024, doi: 10.1093/gigascience/giad113) describe running this
study **twice, two years apart**:

| Run | Articles | Notebooks | Repositories (with notebooks) |
|---|---|---|---|
| 2021 (initial) | 1,419 | **9,625** | 1,117 |
| 2023 (rerun) | 3,467 | 27,271 | 2,660 |

This `db.sqlite`'s counts (9,625 notebooks, 1,419 articles) match the
**2021 run exactly** — not the 2023 rerun, whose 27,271-notebook total is
what the task brief's "~27,000" was citing. So this isn't a partial/broken
download; it's the complete database for the *earlier* of the two runs.
**Still worth confirming with the supervisor:** whether the 2021 run is the
intended dataset for this thesis, or whether the 2023 rerun's (larger,
more recent) database should be used instead.

## How "dependency failure" was defined, and why

### The `executions` table

Columns actually inspected via `explore_db.py`:
`id, notebook_id, mode, reason, msg, diff, cell, count, diff_count, timeout, duration, processed, skip, repository_id`.

**`mode`** — an integer 0-7 encoding `(anaconda, dependencies, cellorder)` as
bits (`cellorder + 2*dependencies + 4*anaconda`), per `EXECUTION_MODE` in
the authors' own `archaeology/execution_rules.py`. In this database, `mode`
takes only two values: **3** (2,365 rows: no anaconda, dependencies
installed, in-order) and **5** (2,308 rows: anaconda, no explicit
dependency install, in-order). These are the two "real notebook execution
attempt" variants; other mode values exist in the authors' schema but don't
appear in this database at all.

**`processed`** — a bitmask, not a simple status enum. Bit values come
directly from `archaeology/consts.py`:
```
E_CREATED=0  E_INSTALLED=1  E_LOADED=2  E_EXCEPTION=4
E_TIMEOUT=8  E_SAME_RESULTS=16  E_EXECUTED=32
```
So `processed & 4 == 4` means "this execution raised an exception" — this
is exactly the flag the authors' own `get_repro_exceptions()` (in
`analyses/analysis_helpers_executions.py`) uses. Filtering `mode IN (3, 5)
AND (processed & 4) = 4` gives **2,265 execution rows that actually raised
an exception** — the base population for everything below.

**`reason`** — on inspection (not assumption), for these 2,265 rows this
column holds the **raised exception's class name**, and *sometimes* also a
short message after a colon:
```
'ModuleNotFoundError': 832    'FileNotFoundError: [Errno 2] ...': 256
'ImportError': 530            'NameError': 132
'Malformed Notebook': 76      'TypeError': 32
'IOError: [Errno 2] ...': 29  'CalledProcessError': 24
'AttributeError': 24          'LZMAError': 22
```
(30 distinct values total; full list in `explore_db.py`'s output.)
Checked directly: every `ModuleNotFoundError`/`ImportError` row in this
database is the **bare class name only**, never `"ImportError: <message>"`
— unlike e.g. `FileNotFoundError`, which does carry a message here. So in
this dataset, `error_type` and `error_message` end up identical for our
target rows (see `extract_dependency_failures.py`'s `split_reason()`,
written to still split on a colon in case that changes with a different
database or subset).

**`msg`** — empty for every single row in this database (checked directly:
`SELECT COUNT(*) FROM executions WHERE msg != ''` → 0). It's populated in
the authors' own code for a *different* row population (raw setup.py/
requirements.txt/Pipfile *installation* logs, hex-encoded — see
`interpret_msg()` in `analysis_helpers_executions.py`), which doesn't
apply to the execution-attempt rows this task cares about. Not used here.

### The filter used

```sql
WHERE e.mode IN (3, 5)
  AND (e.processed & 4) = 4
  AND (
        e.reason = 'ModuleNotFoundError'
     OR e.reason = 'ImportError'
     OR e.reason LIKE 'ModuleNotFoundError:%'
     OR e.reason LIKE 'ImportError:%'
     OR e.reason LIKE '%No module named%'
  )
```

The `LIKE` clauses are a safety net for message-carrying variants that
don't occur in *this* database but might in a fuller one; they matched zero
*additional* rows here beyond the two exact-match classes.

**This mirrors the original GigaScience authors' own definition.**
`analysis_helpers_executions.py`'s `get_repro_missing_dependencies()`
defines "missing dependencies" as `reason` (after normalizing common
substrings) equal to `"ImportError"` or `"ModuleNotFoundError"` — the exact
two classes used here. This is the strongest evidence available for the
"why this column, why these values" question the task brief asks for: it's
not a guess, it's the same definition the database's own creators used for
the same purpose.

## Counts

| | count |
|---|---|
| Total notebooks (this database) | 9,625 |
| Total execution rows | 4,673 |
| Execution rows that raised an exception (`mode IN (3,5)`, `processed & 4 = 4`) | 2,265 |
| ...of which classified as a dependency failure (`ImportError`/`ModuleNotFoundError`) | **1,362** |
| Distinct repositories represented in `dependency_failures.csv` | 311 |
| Breakdown | `ModuleNotFoundError`: 832 · `ImportError`: 530 |

1,362 / 2,265 ≈ **60% of all execution exceptions in this database are
dependency-related** by this definition — consistent with the task brief's
framing that dependency issues are "a leading cause" of notebook failure.

## To confirm with supervisor

1. **Which run's `db.sqlite` should be used?** This file exactly matches
   the paper's **2021 initial run** (9,625 notebooks, 1,419 articles), not
   the **2023 rerun** (27,271 notebooks, 3,467 articles) the task brief's
   "~27,000" figure referred to. Confirmed against the paper directly, not
   guessed. Is the 2021 run the intended dataset, or should this be redone
   against a 2023-rerun database instead?
2. **Is `ImportError`/`ModuleNotFoundError` the right (and only) filter?**
   It matches the original authors' own definition, but a few other
   `reason` values are plausibly dependency-adjacent and were deliberately
   *excluded* here: `AttributeError` (24 rows — could be a removed/changed
   API, like `broken_examples/02_numpy_float.py` in this thesis's own test
   set, but could equally be an unrelated bug) and `CalledProcessError` (24
   rows — could be a failed native/system dependency). Left out because
   they need per-row inspection to tell "dependency-related" from "not,"
   which is exactly the kind of judgment call this draft is meant to flag
   rather than make silently.
3. **`mode IN (3, 5)` restricts to the two modes present in this database.**
   If a fuller database has other mode values with exception rows, the
   filter should be revisited — it currently doesn't exclude anything in
   *this* file, since only modes 3 and 5 occur here at all.
4. **No downloading/cloning/running notebooks has happened.** This CSV is
   metadata only (repository name, notebook name, error type/message,
   Python version) — fetching actual notebook content for real runs against
   `repair_tool/` is explicitly a separate, later task per the brief.

## Reproducing

```bash
python dataset/explore_db.py                        # schema + distributions
python dataset/extract_dependency_failures.py        # writes dataset/dependency_failures.csv
```

Both take the db path as an optional first argument (default:
`computational-reproducibility-pmc/analyses/db.sqlite`); both open it via
SQLite's `mode=ro` URI flag, so a write attempt fails at the driver level,
not just by convention (verified directly: `attempt to write a readonly
database`).
