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

`repair_tool/runner.py` runs a target `.py` file **as a subprocess** (never
by importing it, so a broken target can never crash this tool) and returns a
structured `RunResult`.

```python
from repair_tool.runner import run_project

result = run_project("broken_examples/02_numpy_float.py")
assert result.ok is False
assert "AttributeError" in result.stderr
```

CLI:

```bash
python -m repair_tool.runner <path-to-target.py> [--timeout SECONDS]
# or, once pip-installed: repair-tool-run <path-to-target.py>
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

`repair_tool/diagnose.py` reads the `stderr` a failed `RunResult` captured
and classifies it into a structured `Diagnosis` (`kind`, `module`,
`package`, `symbol`, `detail`) — pure classification, no fixing, installing,
or LLM calls yet.

```python
from repair_tool.diagnose import diagnose

d = diagnose("AttributeError: module 'numpy' has no attribute 'float'")
assert d.kind == "module_attribute_removed"
assert d.package == "numpy"
assert d.symbol == "float"
```

`diagnose_result(result)` wraps a Phase 1 `RunResult` directly — `kind="none"`
for a successful run, otherwise the same classification as `diagnose(result.stderr)`.

CLI (runs the target via `run_project` first, then diagnoses it):

```bash
python -m repair_tool.diagnose <path-to-target.py> [--timeout SECONDS]
# or: repair-tool-diagnose <path-to-target.py>
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

## Phase 3 — propose and apply a fix, in isolation, then verify

Closes the loop for the **one safe, deterministic case**: a genuinely
missing package. Everything else is detected and reported honestly as "not
handled yet" — deliberately narrow, since fixing removed/changed APIs needs
code understanding and belongs to the LLM step (Phase 5). Still no LLM here:
rule-based plus real PyPI lookups, a clean baseline to compare the LLM
against later (RQ3). See `PHASE3_TASK.md` and `PHASE3_ADDENDUM.md` (design
decisions made before implementation: install timeout, confidence/source
mapping, CI network-test policy).

| Module | Responsibility |
|---|---|
| `repair_tool/pypi.py` | `package_exists`, `latest_version`, `resolve_package_name` — talks to real PyPI via stdlib `urllib`, plus a curated alias map (`sklearn`→`scikit-learn`, `cv2`→`opencv-python`, etc.) |
| `repair_tool/venv_manager.py` | `get_venv_python(target)` — creates/reuses an isolated venv per target, returns its python executable |
| `repair_tool/repair.py` | `Proposal` + `propose(diagnosis)` — rule-based, decides what to do and records why |
| `repair_tool/apply.py` | `apply(proposal, python_exe)` — runs the install inside the venv, 300s timeout, never raises |
| `repair_tool/loop.py` | `Attempt`, `RepairResult`, `repair(path)` — orchestrates run→diagnose→propose→apply→verify, plus the CLI |

```python
from repair_tool.loop import repair

result = repair("broken_examples/01_missing_package.py")
assert result.fixed is True   # installs seaborn in an isolated venv, verifies by re-running
```

CLI:

```bash
python -m repair_tool.loop <path-to-target.py> [--max-attempts N]
# or: repair-tool-fix <path-to-target.py>
```

**Isolation**: `run_project` now takes an optional `python_exe` (defaults to
`sys.executable`, so Phases 1-2 are unaffected); `loop.repair` passes the
target's venv python, so installs never touch the interpreter running this
tool. Venvs live under `.repair_venvs/` (gitignored), keyed by the target's
absolute path, and are reused across runs.

**The verify loop never trusts an install worked** — success is only
"the project actually ran afterward." Capped at `MAX_ATTEMPTS = 5`, and it
stops early if the same diagnosis (kind/module/package/symbol) repeats,
so a stuck state can't loop forever pretending to make progress. A nice
side effect of real isolation: since each venv starts empty, `02`/`04` often
surface as `missing_module` on the *first* run (numpy/sklearn aren't
installed at all yet) before revealing their actual unhandled error
(`module_attribute_removed`/`import_name`) on the second — exactly the
layered-error behavior `broken_examples/MANIFEST.md` describes.

**Transparency fields are seeded now, not retrofitted.** `Proposal` carries
`reason`/`source`/`confidence`, `Attempt` carries `applied`/`verification` —
the first real pieces of the full transparency report (Vision Doc §7),
captured from the start so later phases don't need to reconstruct this data.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # dev/test only, not needed to use the tool itself
python -m unittest tests.test_runner tests.test_diagnose tests.test_pypi tests.test_repair tests.test_apply tests.test_loop -v
```

`pip install -e ".[dev]"` also registers `repair-tool-run`, `repair-tool-diagnose`,
and `repair-tool-fix` console scripts — see `pyproject.toml`.

**Network note**: `tests.test_pypi`/`test_repair`/`test_loop` include
tests that hit real PyPI and (for `test_loop`'s integration test) do a real
install into a temporary venv. Set `SKIP_NETWORK_TESTS=1` to skip those (CI
does this by default — see `PHASE3_ADDENDUM.md` #3); leave it unset locally
to run them for real.

### Manual checks

```bash
python -m repair_tool.runner broken_examples/02_numpy_float.py    # prints the numpy AttributeError, tool stays alive
python -m repair_tool.runner hello.py                               # prints OK

python -m repair_tool.diagnose broken_examples/01_missing_package.py   # missing_module: seaborn
python -m repair_tool.diagnose broken_examples/06_scipy_imread.py       # import_name: imread from scipy.misc
python -m repair_tool.diagnose hello.py                                  # none (ran fine)

python -m repair_tool.loop broken_examples/01_missing_package.py         # FIXED (installs seaborn in a venv)
python -m repair_tool.loop broken_examples/02_numpy_float.py             # NOT FIXED: not handled yet (removed API)
python -m repair_tool.loop broken_examples/04_sklearn_externals_joblib.py  # NOT FIXED: not handled yet
```

### CI

`.github/workflows/tests.yml` runs all six test modules on every push, on
Python 3.10 and 3.12, with `SKIP_NETWORK_TESTS=1` set so CI stays fast and
isn't a source of flakiness from PyPI hiccups — this directly backs the
vision doc's "Reproducibility" non-functional requirement: the test suite
behaves the same on a clean machine as it does locally (the offline subset,
at least; the network subset is a deliberate local-only check, see the
addendum).

## Roadmap (out of scope so far, tracked here for context)

- **Phase 4** (implied by the vision doc) — verify each fix against
  authoritative package metadata beyond existence/name (richer provenance
  than Phase 3's PyPI existence check).
- **Phase 5** — LLM-driven repair proposals, retrieval of package info; this
  is what will act on the diagnosis kinds Phase 3 leaves as "not handled
  yet" (`module_attribute_removed`, `object_attribute_error`, `import_name`,
  `unknown`), and on any `missing_module` that doesn't resolve to a real
  PyPI package.
- **Later** — assemble the full transparency report (Phase 3 seeds
  `reason`/`source`/`confidence`/`verification`; richer fields like
  "alternatives" come later) that is this thesis's core contribution over
  prior repair tools (Vision Doc, Section 7).

## Project layout

```
repair_tool/runner.py         Phase 1 deliverable — run_project, RunResult
repair_tool/diagnose.py       Phase 2 deliverable — diagnose, diagnose_result, Diagnosis
repair_tool/pypi.py           Phase 3 — PyPI lookups + curated alias map
repair_tool/venv_manager.py   Phase 3 — isolated venv creation/reuse per target
repair_tool/repair.py         Phase 3 — Proposal + propose (rule-based)
repair_tool/apply.py          Phase 3 — carries out an install proposal inside a venv
repair_tool/loop.py           Phase 3 deliverable — Attempt, RepairResult, repair(), CLI
hello.py                      trivial script used to test the success path
broken_examples/              fixed regression set (8 scripts + MANIFEST.md)
tests/test_runner.py          Phase 1 unittest suite
tests/test_diagnose.py        Phase 2 unittest suite
tests/test_pypi.py            Phase 3 — pypi.py (offline + network-guarded)
tests/test_repair.py          Phase 3 — propose() (offline + network-guarded)
tests/test_apply.py           Phase 3 — apply() (offline, mocked subprocess)
tests/test_loop.py            Phase 3 — repair() loop logic (offline, mocked) + real end-to-end integration test
tests/fixtures/hangs.py       infinite loop, used only to test the timeout path
pyproject.toml                packaging + dev-only extras (numpy, pandas, ...)
.github/workflows/tests.yml   CI: runs all test suites on every push (network tests skipped)
```

`runner.py`/`diagnose.py` moved into the `repair_tool/` package as Phase 3's
Step 0 (a pure restructure, committed on its own before any new feature) —
once a third module (`pypi.py`) needed a home, that was the point to
introduce it, exactly as this README used to say it would be.
