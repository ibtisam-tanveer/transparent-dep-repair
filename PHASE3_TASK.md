# Task: Phase 3 — Propose and apply a fix, in isolation, then verify

## Project context (read first)

We are building a tool that **automatically repairs broken Python dependency
configurations** (thesis: "AI-Driven Transparent Repair of Software Dependency
Configurations"). The loop is: **run the project → read the error → propose a fix
→ apply it → re-run to verify → explain every decision.**

**Phases 1 and 2 are complete:**
- `runner.py` → `run_project(path, timeout=60) -> RunResult(ok, returncode, stdout, stderr)`
- `diagnose.py` → `diagnose(stderr)` / `diagnose_result(result) -> Diagnosis(kind, module, package, symbol, detail)`
  with kinds: `missing_module`, `import_name`, `module_attribute_removed`,
  `object_attribute_error`, `unknown`.

This task, Phase 3, closes the loop for the **one safe, deterministic case**: a
genuinely missing package. It proposes an install, carries it out **inside an
isolated environment**, re-runs to **verify**, and records **why** each step was
taken. This is the first phase that changes an environment and the first that
maps to a research question (RQ1: how effectively can we repair).

**Still no LLM in this phase.** Everything here is rule-based plus lookups against
the real PyPI. Keeping it deterministic gives us a clean baseline to compare the
LLM against later (RQ3).

---

## Step 0 — Restructure into a `repair_tool/` package (do this FIRST, on its own)

Before adding any new feature, move the existing code into a package. Do this as a
**pure restructure with no behaviour change**, and confirm all existing tests pass
before writing anything new.

- Create `repair_tool/` with `__init__.py`.
- Move `runner.py` and `diagnose.py` into `repair_tool/`.
- Update imports everywhere: `from runner import ...` → `from repair_tool.runner
  import ...`; `diagnose.py`'s import of `runner` becomes a package-relative import.
- Update `tests/test_runner.py` and `tests/test_diagnose.py` imports.
- Update `pyproject.toml` console scripts to point at the new locations
  (e.g. `repair-tool-run = "repair_tool.runner:main"`,
  `repair-tool-diagnose = "repair_tool.diagnose:main"`).
- Run the full suite. **All 31 existing tests must still pass, unchanged.**

Commit this restructure separately from the new features below, so a regression
is easy to bisect.

---

## Goal (the new feature)

Given a broken project whose failure is a **missing package**, automatically:
resolve the correct installable name, install it into an isolated environment,
re-run the project, and confirm it now works — recording the reason, source, and
outcome of every step. Any other kind of failure must be **attempted-aware**:
detected, reported honestly as "not handled yet", and left for a later phase.

## New modules to add (inside `repair_tool/`)

| Module | Responsibility |
|---|---|
| `pypi.py` | Talk to the real PyPI. `package_exists(name) -> bool`, `latest_version(name) -> str | None`, and `resolve_package_name(import_name) -> str | None` (import name → installable PyPI name). |
| `venv_manager.py` | Create/reuse an isolated virtual environment for a target and return its python executable. |
| `repair.py` | `Proposal` dataclass + `propose(diagnosis) -> Proposal` (rule-based; decides what to do and records WHY). |
| `apply.py` | `apply(proposal, python_exe) -> (ok, log)` — run the install inside the venv. |
| `loop.py` | `Attempt` + `RepairResult` dataclasses, `repair(path) -> RepairResult` orchestrating run→diagnose→propose→apply→verify, and a CLI. |

Also register a console script `repair-tool-fix = "repair_tool.loop:main"` and add
the venv directory to `.gitignore`.

## The three genuinely new pieces (design guidance)

### 1. Isolation (must-have)
Installs must never touch the developer's Python. `venv_manager` creates a fresh
virtual environment (stdlib `venv`, `with_pip=True`) in a git-ignored location,
returns its python executable, and reuses it if it already exists. Extend
`run_project` to accept an optional `python_exe` argument
(`run_project(path, timeout=60, python_exe=None)`, defaulting to `sys.executable`
so Phase 1 behaviour and tests are unchanged); the loop passes the venv's python
so the target runs in the isolated environment.

### 2. Import-name → PyPI-name resolution (the nuance Phase 2 deferred)
The name in the error is often not the installable name. `resolve_package_name`
should:
1. Check a small curated alias map for known mismatches, e.g.
   `sklearn → scikit-learn`, `cv2 → opencv-python`, `PIL → Pillow`,
   `bs4 → beautifulsoup4`, `yaml → PyYAML`, `skimage → scikit-image`.
2. Otherwise, if the import name itself exists on PyPI (`package_exists`), use it.
3. Otherwise return `None` (unresolved) — do NOT guess-install.
Record which path was taken, because it feeds the `source` field (a curated/PyPI
-confirmed name is "verified"; an unresolved one is low confidence and no install
is attempted).

### 3. The verify loop (never trust, always re-run)
Success is decided by **actually re-running the project in the venv**, never by
assuming the install worked. The loop: run → if ok, done; else diagnose → propose
→ if not an install proposal, stop and report honestly → apply → loop back and
re-run to verify. Cap iterations (e.g. `MAX_ATTEMPTS = 5`) so it can never loop
forever, and stop early if the same diagnosis repeats (no progress).

## Scope: which cases Phase 3 actually fixes

- **Fix automatically:** `missing_module` where `resolve_package_name` yields a
  real PyPI package (e.g. example 01, `seaborn`).
- **Detect but do NOT fix (report honestly):** every other kind —
  `module_attribute_removed`, `object_attribute_error`, `import_name`, `unknown`,
  and any `missing_module` that does not resolve to an installable package (e.g. a
  removed submodule like `sklearn.externals`). For these, `propose` returns a
  `Proposal` of kind `none` with a reason, and the loop records it as "not handled
  yet — needs a later phase." Installing is never attempted for these.

This narrow scope is intentional: the removed-API cases need code understanding
and belong to the LLM step (Phase 5).

## Transparency: start recording it now (seed of the core contribution)

The full transparency report is a later phase, but its data must be captured here
so it is not retrofitted. Give `Proposal` these fields: `kind` (`install`/`none`),
`package`, `import_name`, `command`, `reason`, `source`, `confidence`
(`high`/`medium`/`low`). Give `Attempt` a `proposal`, an `applied` flag, and a
`verification` string. `loop.repair` returns a `RepairResult(target, fixed,
attempts)`, and the CLI prints a simple readable summary of each attempt
(action, reason, source, confidence, verification). Keep it minimal — richer
fields like "alternatives" come later.

## Required behaviour / interface

```python
from repair_tool.pypi import resolve_package_name
from repair_tool.repair import propose
from repair_tool.diagnose import Diagnosis
from repair_tool.loop import repair

# name resolution (network)
assert resolve_package_name("sklearn") == "scikit-learn"
assert resolve_package_name("seaborn") == "seaborn"

# proposals (no network needed)
p = propose(Diagnosis(kind="missing_module", module="seaborn", package="seaborn"))
assert p.kind == "install" and p.package == "seaborn"

p2 = propose(Diagnosis(kind="module_attribute_removed", package="numpy", symbol="float"))
assert p2.kind == "none"          # not handled in Phase 3

# end-to-end (network + creates a venv)
result = repair("broken_examples/01_missing_package.py")
assert result.fixed is True
```

## Constraints

- Python 3.10+. Standard library for everything except talking to PyPI over
  HTTP; use `urllib` (stdlib) rather than adding `requests`.
- Isolation is mandatory: never install into the current interpreter.
- Deterministic only: no LLM, no model calls.
- **Network is required** for `pypi.py` and installs (PyPI / pythonhosted). If the
  build environment blocks network, note it — the unit tests must still pass
  offline (mock PyPI), and only the end-to-end test needs network.

## Tests

- Unit tests (no network): `resolve_package_name` via the curated map; `propose`
  returns `install` for `missing_module` and `none` for every other kind; the loop
  logic with a mocked apply/PyPI.
- One integration test (network): `repair("broken_examples/01_missing_package.py")`
  ends with `fixed is True`, in a temporary venv, and does not install `seaborn`
  into the current interpreter. Guard it so it is skipped (not failed) when offline
  or when a `SKIP_NETWORK_TESTS` env var is set.
- Do not weaken or remove any Phase 1 or Phase 2 tests.

## Definition of done

- Step 0 done: code lives in `repair_tool/`, all 31 prior tests pass unchanged.
- `repair("broken_examples/01_missing_package.py")` fixes it end-to-end in an
  isolated venv; the real interpreter is left untouched.
- Every other `broken_examples/*` file is detected and reported as "not handled
  yet" with a reason — no install attempted, no crash.
- `resolve_package_name` maps the known aliases correctly and returns `None` for a
  nonsense name.
- The loop cannot run forever and stops when it stops making progress.
- The CLI (`repair-tool-fix <path>` or `python -m repair_tool.loop <path>`) prints
  a readable per-attempt summary including reason, source, and verification.
- Full suite (Phases 1–3) passes locally and in CI; network-only tests skip
  cleanly when offline.

## Out of scope (do NOT do in this task)

- No LLM or model calls (Phase 5) — the removed-API / `unknown` cases stay
  "not handled yet".
- No fixing of removed APIs, no code rewriting (that is a code change, Phase 5).
- No metadata-based verification beyond re-running and a PyPI existence/name check
  (richer provenance is Phase 4).
- No `.ipynb` handling.
- No UI.

## Suggested manual check

```bash
python -m repair_tool.loop broken_examples/01_missing_package.py   # FIXED (installs seaborn in a venv)
python -m repair_tool.loop broken_examples/02_numpy_float.py       # not handled yet: removed API (numpy.float)
python -m repair_tool.loop broken_examples/04_sklearn_externals_joblib.py  # not handled yet
```
