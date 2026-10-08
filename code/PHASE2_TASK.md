# Task: Phase 2 — Diagnose the captured error

## Project context (read first)

We are building a tool that **automatically repairs broken Python dependency
configurations** (thesis: "AI-Driven Transparent Repair of Software Dependency
Configurations"). The loop is: **run the project → read the error → propose a fix
→ apply it → re-run to verify → explain every decision.**

**Phase 1 is complete.** `runner.py` provides `run_project(path, timeout=60) ->
RunResult`, where `RunResult` has `ok`, `returncode`, `stdout`, `stderr`. This
task, Phase 2, adds the **second step: understanding the error.** It reads the
captured `stderr` and classifies the failure into a structured `Diagnosis`.

Do **not** implement fixing, installing, virtual environments, or any LLM calls.
Those are Phase 3 and later. Phase 2 only *reads and classifies* — it changes
nothing and runs nothing to repair.

## Goal

Given the error text produced by a broken project, decide **what kind of
dependency problem it is** and extract the relevant names (which package, which
symbol). This structured diagnosis is what the next phase will act on.

## Deliverables

Create `diagnose.py` (at the repo root, next to `runner.py`, so existing import
paths stay intact) containing:

1. A dataclass `Diagnosis` with these fields:
   - `kind: str` — one of the categories listed below
   - `module: str = ""` — the full dotted module named in the error
     (e.g. `sklearn.externals`), when applicable
   - `package: str = ""` — the top-level package (e.g. `sklearn`), when applicable
   - `symbol: str = ""` — the specific name/attribute involved (e.g. `float`,
     `joblib`, `imread`), when applicable
   - `detail: str = ""` — the single most relevant raw error line, for the report

2. A function `diagnose(stderr: str) -> Diagnosis` that classifies the error text.

3. A convenience function `diagnose_result(result) -> Diagnosis` that accepts a
   Phase 1 `RunResult`: if `result.ok` is True it returns `Diagnosis(kind="none")`;
   otherwise it calls `diagnose(result.stderr)`.

4. A CLI entry point that ties Phase 1 and Phase 2 together:
   ```
   python diagnose.py <path-to-target.py>
   ```
   It should run the target via `run_project`, then print the resulting
   `Diagnosis` in a readable form.

## Categories to detect (the `kind` values)

Recognise these, in this priority order, using pattern matching on the error text:

1. `missing_module` — `ModuleNotFoundError` / `ImportError: No module named 'X'`.
   Set `module` to the full name in quotes and `package` to its top-level part.
2. `import_name` — `ImportError: cannot import name 'Y' from 'X'`.
   Set `symbol='Y'`, `module='X'`, `package=` top-level of `X`.
3. `module_attribute_removed` — `AttributeError: module 'X' has no attribute 'Y'`.
   Set `module='X'`, `package=` top-level of `X`, `symbol='Y'`.
4. `object_attribute_error` — `AttributeError: 'T' object has no attribute 'Y'`.
   Set `symbol='Y'` (and `detail` should keep the type `T`). `package` may be "".
5. `unknown` — anything not matched above (e.g. a `TypeError` from a changed
   function signature). Put the last non-empty error line in `detail`.

When several tracebacks/lines are present, classify on the **final** exception
line (the actual error), which is the last non-empty line of the traceback.

## Required behaviour / interface

```python
from diagnose import diagnose, Diagnosis

d = diagnose("AttributeError: module 'numpy' has no attribute 'float'")
assert d.kind == "module_attribute_removed"
assert d.package == "numpy"
assert d.symbol == "float"

d2 = diagnose("ModuleNotFoundError: No module named 'seaborn'")
assert d2.kind == "missing_module"
assert d2.package == "seaborn"
```

## Constraints

- Python 3.10+. Standard library only (`re`, `dataclasses`, plus importing
  `RunResult`/`run_project` from `runner`). No third-party packages.
- Pure classification: no side effects, no execution of repairs, no installs.
- Robust to messy input: multi-line tracebacks, extra whitespace, and an empty
  string (empty input → `kind="unknown"`, no crash).
- Small and readable; short docstrings; each regex commented with an example line.

## Test set and expected results

Use the eight files in `broken_examples/` (run each through `run_project`, then
`diagnose_result`). With the `dev` dependencies installed (so examples reach their
"interesting" error), the expected classifications are:

| File | Expected `kind` | Key extracted fields |
|------|------------------|----------------------|
| 01_missing_package.py | `missing_module` | package=`seaborn` |
| 02_numpy_float.py | `module_attribute_removed` | package=`numpy`, symbol=`float` |
| 03_numpy_int_bool.py | `module_attribute_removed` | package=`numpy`, symbol=`int` |
| 04_sklearn_externals_joblib.py | `missing_module` | module=`sklearn.externals`, package=`sklearn` |
| 05_pandas_append.py | `object_attribute_error` | symbol=`append` |
| 06_scipy_imread.py | `import_name` | module=`scipy.misc`, symbol=`imread` |
| 07_collections_abc.py | `import_name` | module=`collections`, symbol=`Mapping` |
| 08_pyyaml_load.py | `unknown` | detail contains the `TypeError` line |

Add these as `unittest` cases alongside the existing Phase 1 suite. Do not weaken
or delete any Phase 1 tests; both suites must pass.

## Definition of done

- `diagnose(...)` returns the correct `kind` and extracted fields for all cases in
  the table above, and the assertions in "Required behaviour" pass.
- `diagnose_result(result)` returns `kind="none"` for a successful run
  (e.g. `hello.py`) and the right diagnosis for a failing one.
- Empty or unrecognised error text yields `kind="unknown"` without raising.
- `python diagnose.py broken_examples/02_numpy_float.py` prints a readable
  diagnosis naming numpy and float.
- The full test suite (Phase 1 + Phase 2) passes locally and in CI.

## Out of scope (do NOT do in this task)

- No fixing, no `pip install`, no virtual environments (Phase 3).
- No mapping from an import name to its PyPI package name yet. NOTE for later:
  import names and PyPI names often differ (e.g. `sklearn` → `scikit-learn`,
  `cv2` → `opencv-python`, `PIL` → `Pillow`). Phase 2 only extracts the name as it
  appears in the error; resolving it to an installable package is Phase 3.
- No LLM calls (Phase 5). The `unknown` bucket is expected and correct for now —
  it marks exactly the cases a later LLM step will handle.
- No `.ipynb` handling.

## Suggested manual check

```bash
python diagnose.py broken_examples/01_missing_package.py   # missing_module: seaborn
python diagnose.py broken_examples/06_scipy_imread.py       # import_name: imread from scipy.misc
python diagnose.py hello.py                                  # none (ran fine)
```
