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

## Phase 5 — LLM-based repair of the hard cases (code vs. environment)

**The thesis's core contribution.** Phase 3 only fixes a missing package;
everything else (`module_attribute_removed`, `object_attribute_error`,
`import_name`, `unknown`) used to stop at "not handled yet." Phase 5 makes
those fixable — without picking a side in the unsettled code-vs-environment
debate (Vision Doc ref [4]): for each hard case it asks an LLM for **two**
candidate fixes in one call (a code edit and an environment/version pin),
**applies and verifies each by actually re-running the project**, keeps
whichever one works, and records the other as a rejected alternative. See
`PHASE5_TASK.md` for the full spec and `PHASE5_SUMMARY.md` for what
actually happened building it (including a real LLM JSON-escaping bug found
and fixed, and one example that's honestly "not fixed" for real reasons).

| Module | Responsibility |
|---|---|
| `repair_tool/llm.py` | `request_fix(diagnosis, code, error_text)` — the only place `openai`/`OPENAI_API_KEY` are touched; strict JSON via OpenAI's `response_format=json_object`, defensive parsing, never raises |
| `repair_tool/repair.py` | `propose_hard_case(...)` — asks the LLM once, returns both candidates; `STRATEGY_ORDER` decides which to try first per diagnosis kind |
| `repair_tool/apply.py` | `apply_code_edit(edits, workspace_path)` — find/replace edits on a working copy, never the original; a non-matching `find` is a clean failure, not a crash |
| `repair_tool/loop.py` | applies + verifies each candidate in order, picks the winner, reverts a failed code edit before trying the other, records `strategy_won` + `alternatives` |

```python
from repair_tool.loop import repair

result = repair("broken_examples/02_numpy_float.py")
assert result.fixed is True
assert result.attempts[-1].proposal.strategy_won in ("code", "environment")
assert result.attempts[-1].proposal.alternatives   # the rejected candidate, recorded
```

**Verification is always by re-running** — a candidate only counts if
`RunResult.ok` afterward, never the model's own claim. **The original input
file is never touched**: `repair()` makes one working copy (under
`.repair_venvs/<hash>/workspace/`) the moment it starts, and every run/edit
from then on targets that copy.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
echo 'OPENAI_API_KEY=sk-...' > .env   # gitignored; loaded automatically via python-dotenv
python -m unittest tests.test_runner tests.test_diagnose tests.test_pypi tests.test_repair tests.test_apply tests.test_llm tests.test_loop -v
```

`pip install -e ".[dev]"` also registers `repair-tool-run`, `repair-tool-diagnose`,
and `repair-tool-fix` console scripts — see `pyproject.toml`. `openai` and
`python-dotenv` are now core dependencies (Phase 5), but the import stays
isolated to `llm.py` — the rest of the tool works without either installed.

**Network note**: several test modules include tests that hit real
services — PyPI, and (Phase 5) OpenAI, plus real installs into temporary
venvs. Set `SKIP_NETWORK_TESTS=1` to skip all of those (CI does this by
default); leave it unset locally, with a real `OPENAI_API_KEY`, to run them
for real.

### Manual checks

```bash
python -m repair_tool.runner broken_examples/02_numpy_float.py    # prints the numpy AttributeError, tool stays alive
python -m repair_tool.runner hello.py                               # prints OK

python -m repair_tool.diagnose broken_examples/01_missing_package.py   # missing_module: seaborn
python -m repair_tool.diagnose broken_examples/06_scipy_imread.py       # import_name: imread from scipy.misc
python -m repair_tool.diagnose hello.py                                  # none (ran fine)

python -m repair_tool.loop broken_examples/01_missing_package.py         # FIXED (installs seaborn in a venv)
python -m repair_tool.loop broken_examples/02_numpy_float.py             # FIXED (LLM code_edit: np.float -> float)
python -m repair_tool.loop broken_examples/03_numpy_int_bool.py          # FIXED (LLM code_edit)
python -m repair_tool.loop broken_examples/04_sklearn_externals_joblib.py  # NOT FIXED: not handled yet (import_name)
```

### CI

`.github/workflows/tests.yml` runs all test modules on every push, on
Python 3.10 and 3.12, with `SKIP_NETWORK_TESTS=1` set so CI stays fast and
isn't a source of flakiness from PyPI/OpenAI hiccups or API cost — this
directly backs the vision doc's "Reproducibility" non-functional
requirement: the test suite behaves the same on a clean machine as it does
locally (the offline subset, at least; the network subset is a deliberate
local-only check, see `PHASE3_ADDENDUM.md` #3).

## Notebook Support — run and repair `.ipynb` notebooks

Extends the same loop to Jupyter notebooks, since the primary evaluation
dataset (`dataset/`, the GigaScience corpus) is entirely `.ipynb`. See
`NOTEBOOK_SUPPORT_TASK.md` for the design and `NOTEBOOK_SUPPORT_SUMMARY.md`
for what actually happened building it — including two real, subtle bugs
found and fixed by testing against real execution rather than assuming the
design worked: a `KernelManager.kernel_cmd` attribute that looked correct
but was silently never consulted (every notebook was secretly executing
under this tool's own interpreter, defeating isolation entirely — caught by
the kernel-isolation test the task doc specifically called for), and a ZMQ
socket leak from an externally-provided kernel manager not being fully torn
down.

`repair_tool/notebook.py` is the only new module — `diagnose.py`,
`repair.py`, `llm.py`, `pypi.py`, and `venv_manager.py`'s core logic needed
**zero changes**, exactly as designed: `runner.run_project` and
`apply.apply_code_edit` each detect a `.ipynb` target and dispatch there,
so a notebook produces the same `RunResult` shape a `.py` subprocess run
does, and everything downstream stays format-agnostic.

```python
from repair_tool.loop import repair

result = repair("broken_examples/notebooks/missing_package.ipynb")
assert result.fixed is True   # same loop, same transparency report, a notebook this time
```

Manual checks:

```bash
python -m repair_tool.loop broken_examples/notebooks/missing_package.ipynb   # FIXED (install)
python -m repair_tool.loop broken_examples/notebooks/numpy_float.ipynb        # FIXED (LLM code_edit)
python -m repair_tool.loop broken_examples/notebooks/missing_data_file.ipynb  # NOT FIXED, honestly (out of scope: needs external data)
```

`nbclient`/`nbformat` are core dependencies (the execution *driver*, run
from this tool's own environment); `ipykernel` is installed into each
*target* venv instead (`venv_manager.ensure_ipykernel`), since that's what
actually launches a kernel using that venv's packages — getting this split
right is what makes "installing a fix into the venv" actually affect the
notebook run. Test with `python -m unittest tests.test_notebook -v`.

## Roadmap (out of scope so far, tracked here for context)

- **Phase 4** (deferred, not dropped — see `PHASE5_TASK.md`'s "Build order"
  note) — verify each fix against authoritative package metadata beyond
  existence/name, once there's more to harden.
- **The "correct fix, missing import" gap** (found via `06_scipy_imread.py`
  during Phase 5 hardening) — a code fix that introduces a new import can't
  succeed, since nothing installs it; see `PROJECT_STATUS.md`'s flagged
  section. Expected to matter more on real notebooks, not less.
- **Dataset evaluation** — waits on the supervisor's dataset/taxonomy
  decisions (2023 GigaScience rerun; see `PROJECT_STATUS.md`).
- **Later** — assemble the full transparency report (`alternatives` is now
  real for hard cases; richer fields like a full package-metadata
  provenance trail come with Phase 4) that is this thesis's core
  contribution over prior repair tools (Vision Doc, Section 7).

## Project layout

```
repair_tool/runner.py         Phase 1 deliverable — run_project, RunResult
repair_tool/diagnose.py       Phase 2 deliverable — diagnose, diagnose_result, Diagnosis
repair_tool/pypi.py           Phase 3 — PyPI lookups + curated alias map
repair_tool/venv_manager.py   Phase 3 — isolated venv + workspace-copy creation/reuse per target
repair_tool/repair.py         Phase 3 (propose) + Phase 5 (propose_hard_case, STRATEGY_ORDER)
repair_tool/apply.py          Phase 3 (installs) + Phase 5 (apply_code_edit) inside a venv/workspace
repair_tool/llm.py            Phase 5 deliverable — the only module that imports openai
repair_tool/loop.py           Phase 3+5 deliverable — Attempt, RepairResult, repair(), CLI
hello.py                      trivial script used to test the success path
broken_examples/              fixed regression set (8 scripts + MANIFEST.md)
tests/test_runner.py          Phase 1 unittest suite
tests/test_diagnose.py        Phase 2 unittest suite
tests/test_pypi.py            Phase 3 — pypi.py (offline + network-guarded)
tests/test_repair.py          Phase 3 + 5 — propose()/propose_hard_case() (offline + network-guarded)
tests/test_apply.py           Phase 3 + 5 — apply()/apply_code_edit() (offline, mocked subprocess)
tests/test_llm.py             Phase 5 — llm.py (offline mocked + network-guarded real call)
tests/test_loop.py            Phase 3 + 5 — repair() loop logic (offline, mocked) + real end-to-end integration tests
repair_tool/notebook.py       Notebook Support deliverable — run_notebook, edit_notebook_cells
broken_examples/notebooks/    small .ipynb fixtures mirroring the .py set
tests/test_notebook.py        Notebook Support — dispatch, error extraction, kernel isolation, end-to-end
tests/fixtures/hangs.py       infinite loop, used only to test the timeout path
pyproject.toml                packaging + core deps (openai, python-dotenv, nbclient, nbformat) + dev-only extras (numpy, pandas, ipykernel, ...)
.github/workflows/tests.yml   CI: runs all test suites on every push (network tests skipped)
dataset/                      independent data track — see DATASET_SUMMARY.md
```

`runner.py`/`diagnose.py` moved into the `repair_tool/` package as Phase 3's
Step 0 (a pure restructure, committed on its own before any new feature) —
once a third module (`pypi.py`) needed a home, that was the point to
introduce it, exactly as this README used to say it would be.
