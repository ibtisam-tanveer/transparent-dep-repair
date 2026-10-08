# Notes — 2023 GigaScience rerun, taxonomy-labelled failure set

See `dataset/NOTES.md` for the 2021 run's notes (superseded as the primary
dataset, kept for reference) and `DEPENDENCY_FAILURE_TAXONOMY.md` for the
A-E scheme this applies.

## The download actually contains two databases, nested

Zenodo record 8226725's `computational-reproducibility-pmc.zip` (415,649,384
bytes, confirmed byte-for-byte against the record's own API metadata before
extracting) unpacks to a GitHub repo snapshot that itself contains **two**
`db.sqlite` files, not one:

| Path | Size | Notebooks | Articles | Matches |
|---|---|---|---|---|
| `computational-reproducibility-pmc/analyses/db.sqlite` | 370,950,144 bytes | 9,625 | 1,419 | the **2021** run — byte-identical count to `dataset/NOTES.md`'s original file |
| `computational-reproducibility-pmc/computational-reproducibility-pmc/analyses/db.sqlite` | 1,492,819,968 bytes | **27,271** | **3,467** | the **2023 rerun** — exact match to the task brief's figures |

The outer path is the Zenodo release wrapper (matches the repo's own
`README.md`, which instructs `cd computational-reproducibility-pmc/computational-reproducibility-pmc`
before running anything); the inner path is the actual rerun database this
task needs. Confirmed directly by querying both (`SELECT COUNT(*) FROM
notebooks`), not assumed from file size alone. `dataset/extract_failures_2023.py`
defaults to the inner (2023) path.

## The query population

Same two "real execution attempt" modes as the 2021 run
(`executions.mode IN (3, 5)`) and the same `E_EXCEPTION` bit
(`processed & 4 = 4`, from the authors' own `archaeology/consts.py`) —
**9,101 execution rows** actually raised an exception, across **1,282**
distinct repositories. Unlike the 2021 extraction, this query pulls
*every* exception in that population, not just `ImportError`/
`ModuleNotFoundError` — the taxonomy's job is to sort the different causes
apart, not pre-filter to one.

**A separate, larger population is deliberately excluded**: 5,429
execution rows have `processed = 0` and `reason = '<Install Dependency
Error>'` (the authors' own synthetic marker, not a raised Python
exception) — the notebook's declared dependencies failed to install, so
it never got a chance to run and raise anything. A further 2,147 rows
carry `reason = '<Skipping notebook>'` (`processed = 3`), also not a real
exception. Both are structurally different from "the notebook ran and
threw" and need their own handling, not silent inclusion in this
taxonomy-labelled set — flagged here for a later pass (the dependency
installation failures in particular are arguably even more directly
"missing/incompatible dependency" evidence than the exception population,
and may be worth a follow-up extraction).

## The classifier (`dataset/taxonomy.py`)

`repair_tool.diagnose.diagnose()` could not be reused: it classifies by
message *content* (e.g. "No module named 'X'"), but this database's
`reason` column, for the real exception population, is overwhelmingly the
**bare exception class name alone** — confirmed directly (as the 2021
notes already found for `ImportError`/`ModuleNotFoundError`; re-confirmed
here for the much larger 2023 population). Feeding a bare class name
through `diagnose()` returns `unknown` every time. `dataset/taxonomy.py`
is a new, dataset-specific classifier instead: a handful of message-content
override rules for the signals that *do* appear in this data (file paths +
"does not exist", "no module named", connection/HTTP errors, "undefined
symbol"), falling back to a per-exception-class default (category, tier)
built from the real distribution of the 232 distinct `reason` values this
db actually contains — not a guess at what exceptions might occur.

## A real finding: category B is structurally invisible in this data

**Zero rows were labelled category B (moved/renamed import)** — not
because repository code in this dataset never hits a moved-import case,
but because the signal needed to tell B apart from a plain missing module
(the message `cannot import name 'X' from 'Y'`) **never appears anywhere
in the 9,101-row exception population** (confirmed: `SELECT ... WHERE
reason LIKE '%cannot import%'` returns zero rows). The bare class name
`ImportError` is indistinguishable, from this column alone, between "the
package truly isn't installed" (A) and "the package is installed but an
import path moved" (B) — both raise `ImportError`, and only the message
(which this database doesn't log for these rows) would disambiguate them.
This is a genuine blind spot of the *stored metadata*, not of the repair
tool: when the agent actually clones and runs these repositories for real,
`diagnose.py` sees the full real traceback and classifies B cases
correctly from the real message (exactly as it already does for
`broken_examples/07_collections_abc.py`). **The taxonomy labels from this
CSV under-represent B; the evaluation's own real per-notebook `diagnose.py`
output, captured when the agent actually runs each notebook, is the more
reliable source for the true A-vs-B split** — worth stating explicitly in
the thesis rather than letting a zero in a static CSV imply the tool never
encounters moved imports.

## Results

| | count |
|---|---|
| Total notebooks (2023 db) | 27,271 |
| Total repositories | 5,240 (all on `github.com` — confirmed: `SELECT DISTINCT domain` → one value) |
| Execution rows that raised an exception (`mode IN (3,5)`, `processed & 4 = 4`) | **9,101** |
| Distinct repositories represented in the exception population | 1,282 |
| Distinct `reason` values | 232 |

Per-category totals (confirmed + candidate, i.e. excluding `excluded`):

| Category | Count | Meaning |
|---|---|---|
| A — missing dependency | 6,588 | 5,562 confirmed (bare `ModuleNotFoundError`, or `ImportError`/`RuntimeError` with an explicit "No module named"/"please install" message) + 1,026 candidate (bare `ImportError`, no message) |
| B — moved/renamed import | 0 | see "A real finding" above — structurally unobservable from this column |
| C — removed/changed API | 94 | all candidate (bare `AttributeError`, matching the taxonomy doc's own example exactly) |
| D — version/incompatibility conflict | 85 | all candidate (`CalledProcessError`, or an `ImportError` with an "undefined symbol" message — a binary/ABI incompatibility) |
| E — other / not a dependency failure | 2,150 | 1,340 confirmed (missing data files, network/HTTP failures) + 810 candidate (author-code-shaped errors: `NameError`, `ValueError`, `TypeError`, etc.) |
| E — excluded | 184 | `SyntaxError`, `IndentationError`, and other clearly-not-dependency classes |

1,282 distinct repositories are represented — this is the population
`evaluation/run_eval.py` samples from.

## Known limits (beyond the taxonomy doc's own, which still apply)

- The long tail of 232 distinct `reason` strings was classified by rule,
  not inspected row by row; the rules were built from (and tested
  against, `tests/test_taxonomy.py`) real samples across the distribution,
  but a handful of rare, idiosyncratic messages may be mis-bucketed —
  exactly the kind of imperfection the confirmed/candidate/excluded tiers
  exist to absorb honestly.
- `CalledProcessError` defaults to D (candidate) as a judgment call — the
  2021 notes already flagged this exact class as ambiguous between "a
  failed native/system dependency" and an unrelated subprocess failure;
  some observed messages (e.g. a `wget` download inside the notebook) lean
  closer to E in substance. Not re-litigated per-row here.

## Reproducing

```bash
python dataset/explore_db.py computational-reproducibility-pmc/computational-reproducibility-pmc/analyses/db.sqlite
python dataset/extract_failures_2023.py   # writes dataset/failures_2023.csv, prints per-category counts
```
