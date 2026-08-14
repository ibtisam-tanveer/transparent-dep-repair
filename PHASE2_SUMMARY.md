# Phase 2 Summary — Diagnose the captured error

Status: **done**, pushed to `github.com/ibtisam-tanveer/transparent-dep-repair` (commit `1d7f38c`, branch `main`).

## What was asked (`PHASE2_TASK.md`)

Read the `stderr` a Phase 1 `RunResult` captured and classify it into a
structured `Diagnosis` — five categories (`missing_module`, `import_name`,
`module_attribute_removed`, `object_attribute_error`, `unknown`), each
extracting `module`/`package`/`symbol` where applicable. Pure classification
only: no fixing, no installs, no LLM calls.

## What was built

| File | Purpose |
|---|---|
| `diagnose.py` | The deliverable: `Diagnosis` dataclass + `diagnose(stderr)` + `diagnose_result(result)` + a CLI (`python diagnose.py <path> [--timeout N]`) |
| `tests/test_diagnose.py` | 19-case `unittest` suite (required behaviour, all 8 broken examples, empty/whitespace input, and 4 hardening edge cases) |

Also touched: `pyproject.toml` (registered `diagnose` module + `repair-tool-diagnose` script), `.github/workflows/tests.yml` (now runs both suites), `README.md`.

## Design decisions and why

- **Classify on the real exception line, not the literal last line.** The
  spec says "classify on the final exception line ... the last non-empty
  line of the traceback," but NumPy's actual deprecation message prints
  several explanatory lines *after* the `AttributeError` line itself (see
  `broken_examples/02_numpy_float.py`'s real output). A naive "take the last
  line" would misclassify that as `unknown`. Instead, `_find_exception_line`
  scans `stderr` from the end and returns the last line matching an
  unindented `Identifier: message` shape (`_EXCEPTION_LINE_RE`) — Python
  traceback detail lines (`  File "...", line N` / indented code) never
  match that shape, so this naturally skips them without needing to check
  indentation explicitly. This same scan-from-the-end approach is also what
  correctly resolves chained exceptions (`"During handling of the above
  exception..."`) to the final, actually-relevant exception.
- **Priority-ordered regex matching**, exactly as specified: `missing_module`
  → `import_name` → `module_attribute_removed` → `object_attribute_error` →
  `unknown` fallback. Two categories share the same exception class
  (`ImportError` can be either `missing_module` or `import_name`;
  `AttributeError` can be either `module_attribute_removed` or
  `object_attribute_error`) — disambiguated by matching on the *message
  shape*, not the class name, checked in the spec's stated order.
- **`detail` always holds the full raw exception line**, not just for the
  `unknown` bucket. The spec calls it out as required for
  `object_attribute_error` ("detail should keep the type T"), but populating
  it consistently for every kind makes it directly usable in the
  transparency report later without special-casing.
- **Stdlib only** (`re`, `dataclasses`, plus importing from `runner`) — same
  constraint as Phase 1, for the same reason: nothing about classification
  needs a dependency.

## A real discrepancy between the task's expected table and reality

`broken_examples/04_sklearn_externals_joblib.py` is listed in `PHASE2_TASK.md`'s
table as expecting `kind=missing_module`. With the scikit-learn version
actually installed via the `dev` extra (1.9.x), `sklearn.externals` still
exists as an empty shim module — it just no longer re-exports `joblib` — so
the real error is:

```
ImportError: cannot import name 'joblib' from 'sklearn.externals'
```

which correctly classifies as `import_name` (module=`sklearn.externals`,
package=`sklearn`, symbol=`joblib`) per the spec's own rule definitions, not
`missing_module`. This isn't a bug: the broken example's own header comment
already anticipates both outcomes ("`EXPECTED ERR : ImportError /
ModuleNotFoundError`"), and `broken_examples/MANIFEST.md` was written
knowing behavior is version-dependent. `tests/test_diagnose.py` asserts
against the actual, empirically-verified output (checked by directly running
`run_project` over all 8 examples and reading the real `stderr`), with a
comment explaining why, rather than encoding an assumption that doesn't hold
for the environment this was built and tested in. All other 7 examples match
the task's table exactly.

**Forward note for Phase 3:** this same discrepancy turns out not to matter
there either — Phase 3 only auto-fixes `missing_module`, so whether example
04 classifies as `import_name` or an unresolvable `missing_module`, it ends
up "not handled yet" either way.

## Hardening added beyond the literal spec (agreed with you before building)

Four extra test cases targeting inputs the controlled `broken_examples/` set
doesn't exercise, since Phase 2's classifier will eventually see messier
real-world tracebacks from EnvBench/GigaScience notebooks (Vision Doc,
Section 8):

- **Chained exceptions** (`"During handling of the above exception..."`) —
  confirmed classification correctly follows the *final* exception, not the
  one that triggered it.
- **A warning raised as an error** (`warnings.filterwarnings("error")`
  turning e.g. a `DeprecationWarning` into a raised exception) — confirmed
  the class-name-shape match works regardless of whether the name ends in
  `Error` or `Warning`.
- **Replacement characters from bad decoding** (`�`, which
  `runner.py`'s `errors="replace"` can produce) both around and *inside* a
  token — confirmed `diagnose()` never raises, degrading to `unknown` when
  the corruption breaks pattern matching rather than crashing or silently
  returning a wrong answer.

## How each definition-of-done item was verified

- `diagnose(...)` returns the correct `kind` + extracted fields for 7/8
  table rows exactly, and the 8th (`04`) for the classification that
  actually occurs (see discrepancy note above) — verified via
  `tests/test_diagnose.py`'s per-example cases, generated the same way
  Phase 1's were (dict of filename → expected fields, one test per file).
- The exact assertions in "Required behaviour" pass
  (`test_required_behaviour_missing_module`,
  `test_required_behaviour_module_attribute_removed`).
- `diagnose_result(result)` → `kind="none"` for `hello.py`, correct
  diagnosis otherwise — verified directly and via a synthetic `RunResult`.
- Empty and whitespace-only input → `kind="unknown"`, no crash.
- `python diagnose.py broken_examples/02_numpy_float.py` prints a readable
  diagnosis naming `numpy` and `float` — verified manually, output matches
  the spec's suggested check.
- Full suite (Phase 1 + Phase 2, 31 tests) passes locally; CI updated to run
  both.

## Explicitly out of scope (per the spec, unchanged)

No fixing, no `pip install`, no virtual environments (Phase 3). No mapping
from import name to PyPI package name (also Phase 3 — e.g. `sklearn` →
`scikit-learn`). No LLM calls (Phase 5) — the `unknown` bucket is expected
and correct for now, marking exactly the cases a later LLM step will handle.
No `.ipynb` handling.

## How to run / verify

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m unittest tests.test_runner tests.test_diagnose -v
python diagnose.py broken_examples/01_missing_package.py   # missing_module: seaborn
python diagnose.py broken_examples/06_scipy_imread.py       # import_name: imread from scipy.misc
python diagnose.py hello.py                                  # none (ran fine)
```

## Note (Phase 3)

`diagnose.py` later moved to `repair_tool/diagnose.py` as part of Phase 3's
Step 0 restructure — see `PHASE3_SUMMARY.md` once written. The interface
described above (`diagnose`, `diagnose_result`, `Diagnosis`) is unchanged;
only its import path is.
