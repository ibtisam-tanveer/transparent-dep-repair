# Phase 3 Summary — Propose and apply a fix, in isolation, then verify

Status: **done**, pushed to `github.com/ibtisam-tanveer/transparent-dep-repair`.
Step 0 (restructure) committed separately (`172303c`), as `PHASE3_TASK.md`
requires; the new-feature work followed in its own commit (`cb4c515`).
See `PHASE3_ADDENDUM.md` for design decisions made *before* implementation
(install timeout, confidence/source mapping, CI network-test policy) — this
document covers what actually happened building against them.

## What was asked (`PHASE3_TASK.md`)

Close the loop for the one safe, deterministic case: a genuinely missing
package. Resolve its installable name, install it inside an isolated venv,
re-run to verify, and record why every step happened. Every other diagnosis
kind must be detected and reported honestly as "not handled yet" — no
guessing, no installing. Still no LLM; this is the deterministic baseline
Phase 5 gets compared against (RQ3). Step 0, done first and committed
separately: move `runner.py`/`diagnose.py` into a `repair_tool/` package.

## What was built

| File | Purpose |
|---|---|
| `repair_tool/pypi.py` | `package_exists`, `latest_version`, `resolve_package_name` — real PyPI via stdlib `urllib`, curated `ALIASES` map |
| `repair_tool/venv_manager.py` | `get_venv_python(target)` — creates/reuses an isolated venv per target |
| `repair_tool/repair.py` | `Proposal` dataclass + `propose(diagnosis)` — rule-based decision + reasoning |
| `repair_tool/apply.py` | `apply(proposal, python_exe, timeout=300)` — runs the install inside the venv, never raises |
| `repair_tool/loop.py` | `Attempt`, `RepairResult`, `repair(path)`, CLI — orchestrates the full loop |
| `tests/test_pypi.py`, `test_repair.py`, `test_apply.py`, `test_loop.py` | 25 new tests: offline (mocked) + network-guarded, plus one real end-to-end integration test |

Also touched: `repair_tool/runner.py` (`run_project` gained an optional
`python_exe` param, defaulting to `sys.executable` so Phases 1-2 are
unaffected), `pyproject.toml` (`repair-tool-fix` script, `packages` config),
`.gitignore` (`.repair_venvs/`), `.github/workflows/tests.yml`
(`SKIP_NETWORK_TESTS=1`), `README.md`.

## Design decisions and why

- **Step 0 first, committed alone.** Exactly as instructed — restructuring
  and feature work interleaved would make a regression impossible to bisect
  cleanly. All 31 prior tests verified passing, unchanged, before writing
  any new code.
- **Stall detection compares the full diagnosis signature (kind, module,
  package, symbol), not just `kind`.** The task doc says "stop early if the
  same diagnosis repeats" — read literally, comparing only `kind` would
  wrongly treat *genuine* progress (e.g. fixing a missing `seaborn`, then
  discovering a *different* missing `numpy`) as a stall, since both are
  `kind="missing_module"`. `detail` is deliberately excluded from the
  signature since it can vary incidentally (e.g. an embedded file path)
  without the underlying problem actually differing.
- **`resolve_package_name` keeps the exact signature the required-behaviour
  interface specifies** (`str | None`, not a tuple), so `source`/`confidence`
  are derived separately in `repair.propose()` by checking `import_name in
  pypi.ALIASES` — this was a deliberate reconciliation of two things the
  task doc states that don't quite fit together otherwise (an exact return
  type vs. "record which path was taken").
- **Install timeout (300s), confidence/source mapping (curated=high,
  self-match=medium), and the CI network policy** — all per
  `PHASE3_ADDENDUM.md`, decided before writing the code rather than as
  mid-implementation guesses.

## A discrepancy the task doc's own comment doesn't quite resolve

The "Required behaviour" block labels the `propose()` example "no network
needed," but `propose(Diagnosis(kind="missing_module", ..., package="seaborn"))`
internally calls `resolve_package_name("seaborn")` — and `seaborn` isn't a
curated alias, so that call *does* reach PyPI in the real implementation.
Resolved by testing both ways: an offline unit test
(`test_repair.py::test_required_behaviour_seaborn_via_pypi_match`) mocks
`resolve_package_name` so the suite stays fast and deterministic, and a
network-guarded test (`TestProposeNetwork`) reproduces the literal doc
example against live PyPI. Same treatment for `test_pypi.py`'s
`resolve_package_name("seaborn")` assertion.

## An emergent validation worth noting

Because each target gets a genuinely empty venv, running `02_numpy_float.py`
or `04_sklearn_externals_joblib.py` for the *first* time doesn't hit their
"interesting" error at all — numpy/scikit-learn aren't installed yet, so the
first diagnosis is `missing_module`, which Phase 3 *does* handle. The loop
installs it, re-runs, and only *then* hits the real unhandled error
(`module_attribute_removed` / `import_name`) and stops honestly. This is
exactly the layered-error behavior `broken_examples/MANIFEST.md` predicted
("a tool often fixes one layer and reveals the next") — not a bug, and not
something we had to special-case; it fell out of taking isolation seriously.
Verified manually:

```
$ python -m repair_tool.loop broken_examples/02_numpy_float.py
NOT FIXED: broken_examples/02_numpy_float.py
  attempt 1: install numpy          <- first layer: numpy itself was missing
    ...
    verification: pending re-run
  attempt 2: none                   <- second layer: the real, unhandled error
    reason: kind='module_attribute_removed' requires code-level understanding...
    verification: not handled yet
```

## How each definition-of-done item was verified

- Step 0: all 31 prior tests passed unchanged before any new code was written.
- `repair("broken_examples/01_missing_package.py")` → `fixed=True`, verified
  both via a real integration test (creates a temp venv, installs real
  `seaborn`, confirms the *current* interpreter still can't `import seaborn`
  — proving isolation held) and manually via the CLI (~15s first run, ~0.4s
  on reuse).
- Every other `broken_examples/*` file → detected, reported "not handled
  yet," no crash — verified for 02 and 04 manually (see above), and for all
  8 kinds via `test_repair.py`'s offline suite.
- `resolve_package_name` → curated aliases correct (6/6, offline), returns
  `None` for a nonsense name (mocked offline + real network check).
- Loop cannot run forever: `test_caps_at_max_attempts_when_genuinely_making_progress`
  confirms the cap fires even when every iteration is genuine progress (no
  stall triggered), and `test_stops_when_no_progress` confirms the stall
  path fires independently.
- CLI prints reason/source/confidence/verification per attempt — verified
  manually against all three suggested checks.
- Full suite: **56 tests** (31 from Phases 1-2 + 25 new), passing both with
  `SKIP_NETWORK_TESTS=1` (51 run, 5 skipped) and fully offline-and-online
  locally (all 56 run, ~25s including one real install).

## Explicitly out of scope (per the spec, unchanged)

No LLM calls (Phase 5) — removed-API / `unknown` cases stay "not handled
yet." No code rewriting. No metadata-based verification beyond re-running +
PyPI existence/name check (richer provenance is Phase 4). No `.ipynb`
handling. No UI.

## How to run / verify

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
SKIP_NETWORK_TESTS=1 python -m unittest tests.test_runner tests.test_diagnose tests.test_pypi tests.test_repair tests.test_apply tests.test_loop -v
# or, unset SKIP_NETWORK_TESTS to also run the real-PyPI tests locally

python -m repair_tool.loop broken_examples/01_missing_package.py   # FIXED
python -m repair_tool.loop broken_examples/02_numpy_float.py       # NOT FIXED: not handled yet
```
