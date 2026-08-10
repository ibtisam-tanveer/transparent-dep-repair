# AI-Driven Transparent Repair of Software Dependency Configurations

![tests](https://github.com/ibtisam-tanveer/transparent-dep-repair/actions/workflows/tests.yml/badge.svg)

MSc thesis project (Muhammad Ibtisam Tanveer, supervised by Dr. Sheeba Samuel,
MSc Web Engineering / TUC). See `Vision_Document_v2.docx` for the full research
motivation, related work, and evaluation plan.

The tool's eventual loop:

```
run the project -> read the error -> propose a fix -> apply it -> re-run to verify -> explain every decision
```

This repo is built phase by phase, one link of that loop at a time.

## Phase 1 — run a project and capture its error

`runner.py` runs a target `.py` file **as a subprocess** (never by importing
it, so a broken target can never crash this tool) and returns a structured
`RunResult`.

```python
from runner import run_project

result = run_project("broken_examples/02_numpy_float.py")
assert result.ok is False
assert "AttributeError" in result.stderr
```

CLI:

```bash
python runner.py <path-to-target.py> [--timeout SECONDS]
```

Stdlib only (`subprocess`, `sys`, `os`, `dataclasses`, `argparse`) — no
third-party dependencies, Python 3.10+, cross-platform (uses `sys.executable`,
never a hard-coded `python`).

### `broken_examples/`

Eight small scripts, each broken by one real, known dependency problem (see
`broken_examples/MANIFEST.md` for the full table and the intended fix for
each). They are the fixed regression set for every later phase, not just
Phase 1 — a diagnosis phase can be checked against `MANIFEST.md`'s "type of
problem" column, and a repair phase against its "correct fix" column.

Some of them (02, 03, 04, 05, 06) only reach their *interesting* error once
the underlying package is actually installed — otherwise you just see
`ModuleNotFoundError` for the package itself. The `dev` extra (below) installs
those packages **in a throwaway venv for development/testing only**; it is
deliberately separate from `runner.py`'s own zero-dependency footprint.
`seaborn` is intentionally *not* installed, so example 01 keeps demonstrating
a genuinely missing package.

## Phase 2 — diagnose the captured error

`diagnose.py` reads the `stderr` a failed `RunResult` captured and classifies
it into a structured `Diagnosis` (`kind`, `module`, `package`, `symbol`,
`detail`) — pure classification, no fixing, installing, or LLM calls yet.

```python
from diagnose import diagnose

d = diagnose("AttributeError: module 'numpy' has no attribute 'float'")
assert d.kind == "module_attribute_removed"
assert d.package == "numpy"
assert d.symbol == "float"
```

`diagnose_result(result)` wraps a Phase 1 `RunResult` directly — `kind="none"`
for a successful run, otherwise the same classification as `diagnose(result.stderr)`.

CLI (runs the target via `run_project` first, then diagnoses it):

```bash
python diagnose.py <path-to-target.py> [--timeout SECONDS]
```

The five categories, in priority order: `missing_module`, `import_name`,
`module_attribute_removed`, `object_attribute_error`, and a `unknown` catch-all
for anything else (e.g. a `TypeError` from a changed function signature) —
`unknown` is expected and correct for now; a later LLM step is what will
actually handle those.

Classification works off the traceback's real exception line, found by
scanning from the end of `stderr` for the last unindented `ExceptionName:
message` line, rather than just taking the literal last line — some
libraries (NumPy's deprecation notices, for one) print explanatory text
*after* the actual error line, and naively grabbing the last line would
misclassify those as `unknown`.

**A version-dependent classification note:** running `broken_examples/04_sklearn_externals_joblib.py`
against the `dev` extra's scikit-learn produces `ImportError: cannot import
name 'joblib' from 'sklearn.externals'` (kind=`import_name`), not the
`missing_module` result you might expect from the file's name — scikit-learn
still ships an (empty) `sklearn.externals` shim module rather than deleting
it outright. The file's own header comment already anticipates this
("`EXPECTED ERR : ImportError / ModuleNotFoundError`"); the test suite
asserts against what actually happens, not the older assumption.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # dev/test only, not needed to use runner.py/diagnose.py themselves
python -m unittest tests.test_runner tests.test_diagnose -v
```

`pip install -e ".[dev]"` also registers `repair-tool-run` (≡ `python
runner.py`) and `repair-tool-diagnose` (≡ `python diagnose.py`) console
scripts — see `pyproject.toml`.

### Manual checks

```bash
python runner.py broken_examples/02_numpy_float.py    # prints the numpy AttributeError, tool stays alive
python runner.py hello.py                               # prints OK

python diagnose.py broken_examples/01_missing_package.py   # missing_module: seaborn
python diagnose.py broken_examples/06_scipy_imread.py       # import_name: imread from scipy.misc
python diagnose.py hello.py                                  # none (ran fine)
```

### CI

`.github/workflows/tests.yml` runs both test suites on every push, on Python
3.10 and 3.12 — the same check you'd run locally, just automated. This
directly backs the vision doc's "Reproducibility" non-functional requirement:
the test suite behaves the same on a clean machine as it does locally.

## Roadmap (out of scope so far, tracked here for context)

- **Phase 3** — propose and apply a fix inside an isolated virtual
  environment (no installs or mutation happen in Phases 1-2). Also where
  import-name-to-PyPI-name resolution belongs (e.g. `sklearn` →
  `scikit-learn`, `cv2` → `opencv-python`) — Phase 2 only extracts names as
  they appear in the error.
- **Phase 4** (implied by the vision doc) — verify each fix against
  authoritative package metadata, not just re-running the project.
- **Phase 5** — LLM-driven repair proposals, retrieval of package info; this
  is also what will act on Phase 2's `unknown` bucket.
- **Later** — assemble the transparency report (reason, source/provenance,
  alternatives rejected, verification result, confidence) that is this
  thesis's core contribution over prior repair tools (Vision Doc, Section 7).

## Project layout

```
runner.py                     Phase 1 deliverable
diagnose.py                   Phase 2 deliverable
hello.py                      trivial script used to test the success path
broken_examples/              fixed regression set (8 scripts + MANIFEST.md)
tests/test_runner.py          Phase 1 unittest suite (success, failure, timeout, missing file)
tests/test_diagnose.py        Phase 2 unittest suite (all 5 kinds, all 8 broken examples, edge cases)
tests/fixtures/hangs.py       infinite loop, used only to test the timeout path
pyproject.toml                packaging + dev-only extras (numpy, pandas, ...)
.github/workflows/tests.yml   CI: runs both test suites on every push
```

`runner.py` and `diagnose.py` stay flat modules at the repo root on purpose
— that's the `from <module> import <fn>` / `python <module>.py <path>`
interface both phase specs require, and there's still only two of them.
Once a third module needs a home, that's the point to introduce a package
folder; no need to restructure on a guess before then.
