# Task: Notebook Support — run and repair `.ipynb` notebooks

> **Build-order position:** this is the step **after Phase 5** and **before the
> deferred Phase 4**, i.e. 1 → 2 → 3 → 5 → **Notebook Support** → 4. It is required
> before any evaluation, because the primary evaluation dataset (the GigaScience
> 2023 corpus) is entirely `.ipynb` notebooks.

## Project context (read first)

We are building a tool for **AI-Driven Transparent Repair of Software Dependency
Configurations**. The loop is: **run → diagnose → propose fix → apply → re-run to
verify → explain.** It works for `.py` scripts through Phases 1–5, all in the
`repair_tool/` package:
- `runner.py` → `run_project(path, timeout=60, python_exe=None) -> RunResult(ok, returncode, stdout, stderr)`
- `diagnose.py`, `pypi.py`, `venv_manager.py`, `repair.py`, `apply.py`, `llm.py`, `loop.py`
- Phase 3 fixes missing packages; Phase 5 fixes hard cases via an LLM (code fix vs.
  environment fix, verified by re-running, keeps the winner, records which won).

Everything so far assumes the target is a `.py` file. This task adds `.ipynb`
support so the same loop works on notebooks.

## The core idea (keep this simple)

Downstream stages (diagnose, repair, verify) already operate on a `RunResult`'s
error text, **not** on the file format. So the whole task reduces to: **make a
notebook produce a `RunResult` whose `stderr` looks like a normal Python error**,
and make code-fixes apply to notebook cells. Once a `.ipynb` can yield a
`RunResult`, `diagnose.py` classifies it and Phases 3/5 repair it, with no changes
to their logic. This is an **adapter at the front of the pipeline**, not a rewrite.

## Goal

1. `run_project` accepts a `.ipynb` path, executes it in the isolated venv, and
   returns a `RunResult` whose `stderr` carries the first failing cell's real
   Python exception (e.g. `ModuleNotFoundError: No module named 'seaborn'` or
   `AttributeError: module 'numpy' has no attribute 'float'`) in the same shape
   `diagnose.py` already parses.
2. Code-fix application (Phase 5) works on notebook cell source, on a working copy,
   never mutating the original `.ipynb`.
3. The full run → diagnose → repair → verify loop fixes a broken notebook
   end-to-end, exactly as it does for scripts.

## How to execute a notebook (design guidance)

**Execute the notebook natively; do not just convert it to a script.** Executing it
cell by cell in a kernel matches how the GigaScience study ran notebooks (important
for comparability) and preserves per-cell error structure. Converting to `.py`
first is a rejected alternative: it loses cell boundaries and can misreport where or
why a notebook failed.

### Stop at the first error — catch the raised exception; do NOT use `allow_errors`

Execute with the driver's **default** behaviour, which stops at the first failing
cell and raises `CellExecutionError`. Do **not** run with `allow_errors=True`.
Reasoning: the semantics here are first-error, top-to-bottom, so executing the
remaining cells only to discard their results is wasted time **and** unwanted side
effects — a later cell could still write files, hit the network, or run for a long
time even though we will never look at its output. Stopping at the first error is
both cheaper and safer.

- On success (no cell raises): `RunResult.ok = True`, `stdout` = collected stream
  outputs, empty `stderr`.
- On the first failing cell: build `stderr` from the error's **structured fields** —
  `ename` (exception class) and `evalue` (message) — as `f"{ename}: {evalue}"`,
  followed by the traceback. Prefer the structured `ename`/`evalue` over scraping
  the exception's string message. **Fallback:** if the installed driver version does
  not expose `ename`/`evalue` on the raised `CellExecutionError`, read them from the
  failed cell's error output in the executed notebook object instead. Confirm which
  path is available against the actual driver version in use.
- Enforce a timeout (reuse the existing contract: a timed-out notebook is a failed
  `RunResult`, never a raised exception). Use a sentinel `returncode` for notebook
  runs and document it, consistent with Phase 1's handling of synthetic cases.

### Strip ANSI escape codes when building `stderr` (required)

Jupyter tracebacks are colorized — the `traceback` (and sometimes `evalue`) contain
terminal escape codes such as `\x1b[0;31m`. `diagnose.py`'s classifier expects a
clean `ExceptionName: message` line; an ANSI-prefixed line will **silently fail to
match and misclassify as `unknown`** rather than crash — which would quietly inflate
the "unknown" rate across the whole dataset and send you debugging the taxonomy when
the real cause is color codes. So **strip ANSI escape sequences** from the
reconstructed `stderr` before returning the `RunResult`. Add a dedicated test using
a real colorized traceback to prove classification still works.

### First-error semantics

Execute top-to-bottom; "the error" is the first cell that raises. The tool fixes
that, re-runs, and may reveal the next cell's error — the same layered behaviour
Phases 3 and 5 already handle. Do not attempt out-of-order execution.

## Where the execution libraries live (get this split right)

Be precise about placement — getting it wrong either bloats every throwaway venv or
breaks execution:

- **`nbclient` / `nbformat` are the driver**, used by `repair_tool` itself to
  orchestrate execution and read/write notebooks. They belong in **`repair_tool`'s
  own dependencies** (in `pyproject.toml`, like `openai`), **not** in each target
  venv.
- **`ipykernel` must be installed *into each target venv***, because that is what
  launches a kernel running against that venv's packages — which is what makes
  installing a fix into the venv actually affect the notebook run.
- **Both halves are required:** installing `ipykernel` in the venv is not enough on
  its own — the driver must also be **configured to launch that venv's kernel** (via
  the kernel's executable / kernel name), not the host's. Confirm the notebook
  actually runs under the venv's interpreter (see the kernel-isolation test below),
  not under the host Python.

## Deliverables

| File | Change |
|---|---|
| `repair_tool/notebook.py` (new) | Execute a `.ipynb` in a given venv python, catch the first cell error, strip ANSI, return a `RunResult`; plus helpers to read/write notebook cell source for code fixes |
| `repair_tool/runner.py` (extended) | `run_project` detects `.ipynb` by extension and dispatches to `notebook.py`; `.py` path unchanged |
| `repair_tool/apply.py` (extended) | `apply_code_edit` handles `.ipynb`: find/replace within cell source strings on a working copy (same clean-failure-on-non-match rule as for `.py`) |
| `repair_tool/venv_manager.py` (extended) | Ensure each target venv has `ipykernel`, and that the driver targets that venv's kernel |
| `pyproject.toml` (extended) | Add `nbclient`/`nbformat` as `repair_tool` dependencies (driver only) |
| `broken_examples/notebooks/` (new) | Small broken `.ipynb` fixtures mirroring the `.py` ones (see Tests) |
| `tests/test_notebook.py` (new) + extensions | Notebook execution, ANSI stripping, error extraction, code-fix-on-cells, kernel-is-the-venv's, end-to-end |

## Isolation and the original file

- Notebooks execute in the isolated venv. Install `ipykernel` into the venv when the
  target is a notebook; the driver runs from `repair_tool`'s own environment.
- **Never mutate the original `.ipynb`.** Work on a copy in the venv workspace, as
  Phase 5 already does for scripts.

## Scope: which notebooks are in scope

In scope: notebooks that can be executed top-to-bottom and fail because of a
dependency/environment problem. Out of scope (report honestly as "not runnable /
skipped", do not crash): notebooks that require external data files, credentials,
network services, GPUs/special hardware, user interaction, or that exceed the
timeout. This mirrors the project-selection criteria in the vision document; the
point here is graceful handling, not fixing everything.

## Required behaviour / interface

```python
from repair_tool.runner import run_project
from repair_tool.diagnose import diagnose_result
from repair_tool.loop import repair

# a broken notebook yields a parseable RunResult (ANSI stripped, classifiable)
r = run_project("broken_examples/notebooks/missing_package.ipynb")
assert r.ok is False
assert diagnose_result(r).kind == "missing_module"

# and the full loop fixes it end-to-end in a venv
result = repair("broken_examples/notebooks/missing_package.ipynb")
assert result.fixed is True
```

## Constraints

- Do not change diagnose/repair/LLM logic — this task only makes notebooks produce a
  `RunResult` and makes code edits apply to cells. If a downstream stage needs
  changing to work on notebooks, the adapter is not producing a clean `RunResult`;
  fix the adapter, not the downstream stage.
- Keep every existing test green; `.py` behaviour must be identical.
- Keep the timeout / never-raises contract, isolation, and never-mutate-the-original
  guarantees.
- The only new branch in `run_project` is the `.py` vs `.ipynb` detection.

## Tests

- Fixtures under `broken_examples/notebooks/`: at least a missing-package notebook
  and a removed-API notebook (`np.float`), each a couple of cells so "first failing
  cell" is exercised.
- Offline (no network):
  - notebook executes, first cell error extracted into a `RunResult`,
    `diagnose_result` classifies it correctly;
  - **an ANSI-colorized traceback still classifies correctly** (dedicated test);
  - a code fix applies to the right cell on a copy and the original is byte-for-byte
    unchanged after `repair()`;
  - a notebook needing a missing data file is reported "not runnable", not a crash.
- **Kernel-isolation test:** a notebook importing a package present only in the
  target venv succeeds; the same notebook importing a package absent from the venv
  fails — proving execution uses the venv's kernel, not the host.
- Guarded (network + real venv/LLM): end-to-end repair of the missing-package
  notebook (install) and the `np.float` notebook (Phase 5 code fix).
- Do not weaken any Phase 1–5 tests.

## Definition of done

- `run_project` on a broken `.ipynb` returns a `RunResult` whose `stderr` is clean
  (ANSI-stripped) and classifiable by `diagnose.py`.
- Execution stops at the first failing cell (no `allow_errors`), via a caught
  exception, using structured `ename`/`evalue` (or the documented fallback).
- Notebooks run under the **target venv's** kernel, proven by the isolation test.
- `nbclient`/`nbformat` are `repair_tool` deps; only `ipykernel` is installed into
  target venvs.
- The full loop fixes a missing-package notebook (Phase 3 path) and a removed-API
  notebook (Phase 5 path) end-to-end, verified by re-executing the notebook.
- Code fixes modify the correct cell on a working copy; the original `.ipynb` is
  never modified.
- Notebooks that can't be run for out-of-scope reasons are reported honestly.
- `.py` behaviour and all prior tests unchanged; full suite green offline and (with
  a key) online.

## Continuity note — the "correct fix, missing import" gap (`06` finding)

`PROJECT_STATUS.md` records a known limitation from `broken_examples/06_scipy_imread.py`:
a code fix that is *semantically correct* but introduces a **new import** (e.g.
`scipy.misc.imread` → `imageio.imread`) fails verification because the tool cannot
install the newly-introduced package, and the report currently can't distinguish
"wrong fix" from "right fix, needs one more package." This is **out of scope for
this task**, but it is expected to appear **much more often on real notebooks** than
on the handful of `.py` examples, since "rewrite the code *and* install what the
rewrite needs" is a common real-world fix. Keep it flagged in `PROJECT_STATUS.md`;
do not silently drop it — it will matter for interpreting evaluation results.

## Out of scope (do NOT do here)

- No dataset evaluation run yet (that is the next step, once this lands, against the
  2023 GigaScience corpus).
- No out-of-order cell execution, no partial-notebook heuristics.
- No dependency-failure taxonomy work (separate methodology document).
- No Phase 4 metadata verification (still deferred).
- No fix for the `06` "correct fix, missing import" gap (noted above; separate work).
- No UI.

## Suggested manual check

```bash
python -m repair_tool.loop broken_examples/notebooks/missing_package.ipynb   # FIXED (install)
python -m repair_tool.loop broken_examples/notebooks/numpy_float.ipynb        # FIXED (Phase 5 code fix)
```
