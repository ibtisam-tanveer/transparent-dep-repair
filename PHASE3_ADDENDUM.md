# Phase 3 Addendum — decisions that strengthen `PHASE3_TASK.md`

`PHASE3_TASK.md` (from your supervisor/course) is the source of truth for
scope and deliverables. This addendum resolves three things it left as
implementation judgment calls, decided *before* writing code so they're
deliberate design choices, not accidents discovered mid-implementation.
Read alongside the task doc; implementation should follow both.

## 1. `apply()` needs an explicit install timeout

The task doc doesn't mention a timeout for the `pip install` that `apply()`
runs. Every other subprocess call in this codebase enforces one (`runner.py`'s
`run_project` — "a timed-out run must be returned as a failed RunResult, not
raised") specifically so a hung process can never hang the tool. `pip
install` can hang the same way (dependency resolution stalls, slow mirror,
dead network). `apply()` will follow the exact same pattern:

- `apply(proposal, python_exe, timeout: int = 300) -> (ok, log)`
- Install runs as a subprocess with that timeout.
- A timeout is caught and returned as `ok=False` with an explanatory `log`
  entry (`"install timed out after {timeout}s"`) — never raised to the
  caller, matching `run_project`'s contract exactly.
- 300s (5 min) as the default: generous enough for a slow package with
  compiled wheels, short enough that the loop's `MAX_ATTEMPTS` cap can't turn
  one stalled install into an effectively-infinite run.

## 2. Confidence/source mapping for `resolve_package_name`

The task doc's step 3 wording ("a curated/PyPI-confirmed name is 'verified';
an unresolved one is low confidence") blends two different resolution paths
under one label. They carry different evidence and should be distinguished:

| Resolution path | `source` | `confidence` | Why |
|---|---|---|---|
| Curated alias map hit (e.g. `sklearn` → `scikit-learn`) | `"curated_alias"` | `high` | Hand-verified mapping, no ambiguity. |
| Import name itself exists on PyPI (`package_exists(name)` true) | `"pypi_name_match"` | `medium` | The name exists, but a same-named PyPI package isn't guaranteed to be *the* package the import wanted — name-collision risk. |
| Unresolved (neither path matched) | — | `low` | No install attempted (per the task doc); `Proposal.kind = "none"`, `reason` states the name couldn't be resolved. |

Every `Proposal` where `kind == "none"` (whether from an unresolved name or
from a diagnosis kind Phase 3 doesn't handle at all — `module_attribute_removed`,
`object_attribute_error`, `import_name`, `unknown`) gets `confidence = "low"`
and a `reason` naming specifically why: either "name unresolved" or "kind X
requires code-level understanding, deferred to Phase 5."

## 3. CI policy for the network-dependent integration test

Decided: **the real-PyPI integration test (`repair("broken_examples/01_missing_package.py")`
installing `seaborn` for real) runs locally, not in CI.** CI stays fast and
isn't a source of flakiness from PyPI hiccups or rate limits; CI coverage
comes from the offline unit tests (mocked `apply`/PyPI), which the task doc
already requires and which exercise the same logic paths.

Mechanism (the task doc already specifies the guard; this just decides how
it's set): `.github/workflows/tests.yml` will export
`SKIP_NETWORK_TESTS=1` for the test step. Locally, that variable is unset by
default, so `python -m unittest ...` run on a dev machine executes the real
integration test against live PyPI, same as today's manual-check workflow
for Phases 1-2.

## Net effect on the deliverables table

No new modules or files beyond what `PHASE3_TASK.md` already lists — these
are parameter defaults, field values, and one CI workflow line, not
additional scope.
