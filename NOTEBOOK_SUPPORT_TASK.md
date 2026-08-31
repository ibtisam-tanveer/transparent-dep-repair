# Task: Notebook Support — run the loop against `.ipynb` files

> **Status: DRAFT**, written by Claude following `PROJECT_STATUS.md`'s
> "Suggested next step," not supplied by the supervisor. Treat the design
> decisions below as proposed, not settled — confirm with the supervisor
> before or during implementation, the same way `PHASE3_ADDENDUM.md` and
> `dataset/NOTES.md` flagged their own open calls.

## Context (read first)

Phases 1, 2, 3, and 5 are complete and `.py`-only, by deliberate choice —
see `PHASE5_TASK.md`'s "Upcoming dependency" note: the LLM repair logic
needed to be easy to build and test on plain scripts first. That choice is
now paid off; this task is the deliberate next step it deferred.

**Why this blocks everything after it**: the evaluation dataset
(`dataset/dependency_failures.csv`, 1,362 rows from the GigaScience
reproducibility corpus) is entirely `.ipynb` notebooks. No dataset
evaluation can start until the tool can run, diagnose, and repair a
notebook the same way it does a script.

**Build order**: `1 → 2 → 3 → 5 → Notebook Support → 4` (see
`PROJECT_STATUS.md`). Phase 4 (metadata verification) still comes after
this, deliberately.

**A related, separate concern — do not fold into this task's scope**:
`PROJECT_STATUS.md` flags a real limitation found during Phase 5 hardening
— a code fix that introduces a new import (e.g. `06`'s `from imageio import
imread`) can't succeed, because nothing installs the new import. This will
very likely surface *more* often once real notebooks are in play (the
GigaScience corpus is full of "replace a removed API with a different
library's function" cases). This task does not fix it — that needs its own
design decision — but whoever scopes the eventual fix should know it will
matter more, not less, once this task lands.

## Goal

Extend the existing loop — run → diagnose → propose → apply → verify —
to accept a `.ipynb` file as `repair()`'s target, producing the same kind
of `RepairResult`/`Attempt`/transparency-report data a `.py` target does,
without duplicating or forking the loop's control flow.

## The strategy (proposed; confirm before building)

**Reuse everything that doesn't need to change.** `diagnose.py`,
`repair.py` (`propose`, `propose_hard_case`, `STRATEGY_ORDER`), `llm.py`,
`pypi.py`, and `venv_manager.py` (venv creation/reuse, `get_workspace_copy`
— already extension-agnostic, no code change needed) all operate on
already-abstracted inputs (error text, a `Diagnosis`, a package name) and
need **zero changes**. Only two things are genuinely notebook-specific:
**running** the target and **applying a code edit** to it.

1. **Running a notebook and capturing the result as a `RunResult`.**
   New module `repair_tool/notebook.py`:
   ```python
   def run_notebook(path: str, timeout: int = 60, python_exe: str | None = None) -> RunResult
   ```
   Same return shape as `runner.run_project` — `ok`, `returncode`,
   `stdout`, `stderr` — so every downstream consumer (`diagnose_result`,
   the loop's stall-detection, everything) works unmodified. Internally:
   execute the notebook's cells in order using `nbclient.NotebookClient`
   (proposed dependency — actively maintained, doesn't require a full
   Jupyter install, used programmatically rather than shelling out to the
   `jupyter` CLI), with the kernel pointed at `python_exe` (the target's
   isolated venv, same as `.py` targets). On a cell raising an exception,
   `nbclient` raises `CellExecutionError`, carrying the exception
   name/value/traceback — map that into `RunResult.stderr` in the same
   shape a subprocess traceback already has, so `diagnose.py`'s existing
   regex-based classification needs no notebook-specific logic at all.
   `ok=True`/`returncode=0` if every cell runs; synthetic `returncode=-1`
   for the timeout/malformed-notebook failure cases, matching
   `runner.run_project`'s existing convention for synthetic failures.

2. **Applying a code edit to a notebook.**
   New function, likely in `notebook.py`:
   ```python
   def apply_code_edit(edits: list[dict], workspace_path: str) -> tuple[bool, str]
   ```
   Same signature and same "never crash, a non-matching `find` is a clean
   failure" contract as `apply.apply_code_edit`. Internally: parse the
   `.ipynb` JSON (via `nbformat`, proposed dependency), search **all** code
   cells' source for each edit's `find` string (not just one predetermined
   cell — the LLM is given the whole notebook's failing cell context, but
   an edit could legitimately target an earlier cell, e.g. an import cell),
   apply the first match, write the notebook back out preserving its
   structure (`nbformat.write`). If `find` doesn't appear verbatim in *any*
   cell, that's a failure, exactly like the `.py` case.

3. **Dispatch in `loop.py`, not a forked loop.** `repair()` picks
   `run_project`/`apply.apply_code_edit` vs. `notebook.run_notebook`/
   `notebook.apply_code_edit` based on the target's file extension, at the
   two call sites that currently hard-code the `.py`-shaped functions.
   Everything else in `repair()` — the outer loop, `MAX_ATTEMPTS`, stall
   detection, the hard-case orchestration, the workspace-copy-from-the-start
   discipline — is unchanged and shared between both file types.

## Where it plugs in

- `repair_tool/notebook.py` (new): `run_notebook`, `apply_code_edit`, and
  whatever notebook-JSON helpers they need.
- `repair_tool/loop.py`: extend the two dispatch points (`run_project`/
  `apply_code_edit` calls) to branch on `path.endswith(".ipynb")`.
- `pyproject.toml`: add `nbformat` and `nbclient` as core dependencies
  (same reasoning as `openai`/`python-dotenv` in Phase 5 — needed for the
  tool to function, but keep the imports isolated to `notebook.py` so the
  rest of the tool stays importable/testable without them, same pattern
  `llm.py` already established).

## Constraints

- Isolation, never-mutate-the-original, and "verify by actually re-running"
  all still apply, unchanged, to notebooks.
- `venv_manager.get_workspace_copy` already works file-extension-agnostically
  (`shutil.copyfile` + `os.path.basename`) — confirm this with a test, but
  expect no code change needed there.
- No changes to `diagnose.py`'s classification rules — a notebook's
  captured traceback must be classified by the exact same logic a script's
  is, since the whole point of this design is that `RunResult`/`Diagnosis`
  don't know or care what kind of file produced them.
- Do not attempt to fix the "correct fix, missing import" gap here (see
  Context above) — out of scope, flag it in `PHASE4_TASK.md` or wherever it
  eventually gets addressed instead.

## Tests

- Offline (no kernel execution, no network): notebook JSON parsing/editing
  logic — `apply_code_edit` finding/replacing across multiple cells,
  failing cleanly when `find` isn't present anywhere, never mutating the
  original file (mirroring `tests/test_apply.py`'s existing coverage for
  the `.py` case).
- A small number of real, hand-made broken `.ipynb` files (analogous to
  `broken_examples/`) — at minimum, a notebook version of one already-known
  case (e.g. a notebook that does `np.float(3.14)`) to confirm the whole
  loop reaches the same `FIXED` outcome via `nbclient` execution that the
  `.py` version already reaches via subprocess execution.
- One guarded real end-to-end test (real venv, real kernel execution, and
  where a hard case is involved, a real LLM call) — same
  `SKIP_NETWORK_TESTS`/missing-key-skips-cleanly pattern as every other
  guarded test in this codebase.
- Do not weaken any Phase 1–3/5 test — the `.py` path must behave
  identically to how it does today.

## Definition of done

- `repair("some_notebook.ipynb")` reaches `RepairResult(fixed=True, ...)`
  for a notebook whose only problem is a resolvable missing package or an
  LLM-fixable removed/changed API, verified by actually re-executing the
  notebook, not by inspecting its cell outputs after the fact.
- A notebook that can't be fixed (or can't even be parsed/loaded) is
  reported honestly — same "not handled yet" / "not fixed" discipline as
  the `.py` path, never a crash, never a false success.
- The original `.ipynb` input file is never modified — same
  workspace-copy discipline as `.py` targets.
- `diagnose.py`, `repair.py`, `llm.py`, `pypi.py`, `venv_manager.py` need
  **no changes** — if implementation reveals one of them does need a
  change, that's a signal the notebook-specific logic leaked into the
  wrong layer and the design should be revisited before proceeding.
- Full suite (Phases 1–3, 5, this task) green locally and in CI, with the
  same offline/guarded-live split as every prior phase.

## Out of scope (do NOT do here)

- No dataset evaluation run — this task only makes evaluation *possible*,
  it doesn't perform it. Evaluation still waits on the supervisor's
  dataset/benchmark decisions (the 2026-08-20 email).
- No fix for the "correct fix, missing import" gap (see Context).
- No Phase 4 (metadata verification) work.
- No UI.
- Multi-kernel support (R notebooks, Julia notebooks, etc.) — the
  GigaScience corpus's Python notebooks are the target; don't generalize
  beyond what's needed for that.

## Suggested manual check

```bash
# once implemented:
python -m repair_tool.loop some_broken_notebook.ipynb
```
