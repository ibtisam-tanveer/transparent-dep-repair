# Notebook Support Summary — run and repair `.ipynb` notebooks

Status: **done**. See `NOTEBOOK_SUPPORT_TASK.md` (final revision, refined
through two review passes) for the full design. This document covers what
actually happened building it — including two real, subtle bugs that only
surfaced by testing against genuine execution rather than trusting the
design on paper.

## What was asked

Extend the same run→diagnose→propose→apply→verify loop to `.ipynb`
notebooks, required before any evaluation against the GigaScience dataset
(entirely notebooks). Core constraint: `diagnose.py`/`repair.py`/`llm.py`/
`pypi.py`/`venv_manager.py`'s core logic need **zero changes** — this is an
adapter at the front of the pipeline, not a rewrite.

## What was built

| File | Purpose |
|---|---|
| `repair_tool/notebook.py` | `run_notebook()` (executes a notebook, returns a `RunResult`), `edit_notebook_cells()` (find/replace across code cells) |
| `repair_tool/runner.py` (extended) | `run_project` detects `.ipynb` and dispatches to `notebook.run_notebook` |
| `repair_tool/apply.py` (extended) | `apply_code_edit` detects `.ipynb` and dispatches to `notebook.edit_notebook_cells` |
| `repair_tool/venv_manager.py` (extended) | `ensure_ipykernel(python_exe)` — installs `ipykernel` into a target venv if not already present |
| `broken_examples/notebooks/` | 3 fixtures: `missing_package.ipynb`, `numpy_float.ipynb` (mirroring the `.py` set), `missing_data_file.ipynb` (out-of-scope case) |
| `tests/test_notebook.py` | 26 tests: dispatch, error-extraction helpers, `edit_notebook_cells`, a kernel-isolation test, guarded real end-to-end runs |

`diagnose.py`, `repair.py`, `llm.py`, `pypi.py` — genuinely unchanged, as
designed. `loop.py` — also unchanged; it calls `run_project`/`apply_code_edit`
exactly as before and has no idea it's ever looking at a notebook.

## Two real bugs found and fixed — not assumed away

The design on paper (and in both reviewed drafts of `NOTEBOOK_SUPPORT_TASK.md`)
looked correct. Manual end-to-end testing found it wasn't, twice, in ways
that would have silently corrupted every result if left unfound.

### 1. `KernelManager.kernel_cmd` is silently ignored

The plan was to point a kernel launch at an arbitrary venv's python via
`km.kernel_cmd = [python_exe, "-m", "ipykernel_launcher", "-f", "{connection_file}"]`.
This looked reasonable and is a commonly-suggested pattern online. It does
nothing in the installed `jupyter_client` (8.10.0): `format_kernel_cmd()`
builds the launch command from `self.kernel_spec.argv`, and never reads a
`kernel_cmd` attribute — which isn't even a real trait on `KernelManager`
(confirmed: `"kernel_cmd" in km.trait_names()` is `False`). Setting it is
just setting an unused instance attribute.

**Consequence, caught only because the kernel-isolation test exists**: every
notebook was silently executing under *this tool's own dev interpreter*,
not the target venv's — completely defeating isolation. `numpy_float.ipynb`
"passed" anyway, by pure accident (numpy happens to already be in the dev
`.venv` too, for exercising `broken_examples/`). Only `missing_package.ipynb`
(needing `seaborn`, deliberately absent from the dev venv) exposed it:
installing `seaborn` into the target venv had no effect, because the
notebook was never running there in the first place. This is precisely the
failure mode `NOTEBOOK_SUPPORT_TASK.md`'s kernel-isolation test was written
to catch, and it caught it on the first real run.

**Fix**: assign a manually-constructed `KernelSpec` (with `argv` pointing at
the target python) directly to `KernelManager`'s private `_kernel_spec`
slot, bypassing the by-name kernelspec lookup entirely. Confirmed directly,
twice: `sys.executable` printed from inside the kernel matches the intended
target, and a package installed only in the target venv becomes importable
there and nowhere else.

### 2. Kernel processes and ZMQ sockets were leaking

`nbclient` does not take ownership of an externally-provided `KernelManager`
— confirmed by observing `km.has_kernel` stayed `True` after `execute()`
returned, and separately, `ResourceWarning: Unclosed socket` on the kernel
client's ZMQ channels when running with `-W error::ResourceWarning`. Left
unfixed, every `run_notebook()` call would leak one kernel process and its
communication sockets — fine for a handful of manual runs, a real problem
for evaluating hundreds of dataset notebooks in one process.

**Fix**: explicit cleanup in a `finally` block, in order — `client.kc.stop_channels()`
then `km.shutdown_kernel(now=True)` — both defensively wrapped so a cleanup
failure still can't crash the tool. Confirmed fixed by re-running the
kernel-isolation test under `-W error::ResourceWarning`: no warning at all.

## How each definition-of-done item was verified

- `run_project` on `missing_package.ipynb`/`numpy_float.ipynb` returns a
  `RunResult` whose `stderr` `diagnose.py` classifies correctly
  (`missing_module`/`package=seaborn`, `module_attribute_removed`/`symbol=float`)
  — verified directly and via `tests/test_notebook.py`.
- Execution stops at the first failing cell, no `allow_errors` — verified
  with a 3-cell fixture where cell 3 would raise a *different* exception if
  it ever ran; confirmed it never appears in the captured `stderr`.
- Notebooks run under the **target venv's** kernel — the kernel-isolation
  test: a notebook importing `seaborn` fails under the host interpreter and
  succeeds once `seaborn` is installed into a real target venv and the same
  notebook is run against *that* venv's python. This is the test that
  caught bug #1 above.
- The full loop fixes `missing_package.ipynb` (Phase 3 path) and
  `numpy_float.ipynb` (Phase 5 path, real LLM call) end-to-end — including
  the same layered-error behavior as the `.py` examples: a fresh venv means
  `numpy_float.ipynb` first hits `missing_module` (numpy itself absent),
  gets it installed, *then* the LLM fixes the real `np.float` error on the
  next run.
- Code fixes modify the correct cell on a working copy; the original
  `.ipynb` is never modified — verified by re-reading the original file
  *after* `repair()` runs (not before, which would prove nothing) and
  comparing byte-for-byte.
- `missing_data_file.ipynb` (needs a nonexistent external file) is reported
  honestly as not fixed — the LLM correctly determines neither a code fix
  nor a version pin can help; no crash, no false success.
- `nbclient`/`nbformat` are `repair_tool` core dependencies; `ipykernel` is
  installed only into target venvs (`venv_manager.ensure_ipykernel`) — plus
  added to the `dev` extra, so a direct `run_project()`/`run_notebook()`
  call with no explicit `python_exe` (defaulting to `sys.executable`, this
  repo's own dev `.venv`) works too, matching the doc's "Required
  behaviour" example literally.
- `.py` behaviour and all 92 prior tests unchanged — confirmed unmodified,
  full suite (118 tests) green both offline (104 run, 14 skipped) and fully
  online (~123s, including two real venv/install cycles and multiple real
  LLM calls).

## Explicitly out of scope (per the task, unchanged)

No dataset evaluation run. No out-of-order cell execution. No dependency-failure
taxonomy work. No Phase 4. No fix for the `06` "correct fix, missing import"
gap (still flagged in `PROJECT_STATUS.md`, expected to matter more once real
notebooks are evaluated, not less). No UI.

## How to run / verify

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
SKIP_NETWORK_TESTS=1 python -m unittest tests.test_notebook -v
# unset SKIP_NETWORK_TESTS (with a real OPENAI_API_KEY) to also run the
# kernel-isolation test and the real end-to-end repairs

python -m repair_tool.loop broken_examples/notebooks/missing_package.ipynb   # FIXED
python -m repair_tool.loop broken_examples/notebooks/numpy_float.ipynb        # FIXED (real LLM call)
python -m repair_tool.loop broken_examples/notebooks/missing_data_file.ipynb  # NOT FIXED, honestly
```
