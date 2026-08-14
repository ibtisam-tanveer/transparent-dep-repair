# Project Status — AI-Driven Transparent Repair of Software Dependency Configurations

MSc thesis project (Muhammad Ibtisam Tanveer, supervised by Dr. Sheeba Samuel).
This is a snapshot of what's actually built right now, meant to be updated as
phases land. For the full reasoning behind each phase's decisions, see
[PHASE1_SUMMARY.md](PHASE1_SUMMARY.md), [PHASE2_SUMMARY.md](PHASE2_SUMMARY.md),
[PHASE3_ADDENDUM.md](PHASE3_ADDENDUM.md) (Phase 3 design decisions, made
before implementation), and [PHASE3_SUMMARY.md](PHASE3_SUMMARY.md).

## The tool's target loop

```
run the project -> read the error -> propose a fix -> apply it -> re-run to verify -> explain every decision
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                          implemented so far (Phases 1-3)
```

## What's implemented

| Phase | File(s) | Status | What it does |
|---|---|---|---|
| 1 | `repair_tool/runner.py` | done, committed+pushed (`4197885`) | Runs a target `.py` file as a subprocess, returns `RunResult(ok, returncode, stdout, stderr)`. Never crashes on a broken target, missing file, or timeout. |
| 2 | `repair_tool/diagnose.py` | done, committed+pushed (`1d7f38c`) | Classifies a `RunResult`'s `stderr` into a `Diagnosis(kind, module, package, symbol, detail)` — 5 categories: `missing_module`, `import_name`, `module_attribute_removed`, `object_attribute_error`, `unknown`. Pure classification, no fixing. |
| 3 | `repair_tool/{pypi,venv_manager,repair,apply,loop}.py` | **done**; Step 0 restructure committed (`172303c`), new-feature commit pending | Propose + apply a fix for the one safe deterministic case (missing package), inside an isolated venv, then re-verify by re-running. Rule-based only, no LLM. |
| 4 | — | not started | Verify a fix against authoritative package metadata, not just re-running. |
| 5 | — | not started | LLM-driven repair proposals + retrieval; handles the removed-API / `unknown` diagnosis buckets Phase 3 deliberately leaves alone. |
| — | — | not started | Full transparency report (reason, source/provenance, alternatives, verification, confidence) — the thesis's core contribution (Vision Doc §7). Phase 3 seeds the first real pieces of this (`Proposal`/`Attempt` fields) so it isn't retrofitted later. |

## Verification

- **56/56 tests passing** (12 Phase 1 + 19 Phase 2 + 25 Phase 3), both with `SKIP_NETWORK_TESTS=1` (51 run, 5 correctly skipped) and fully online locally (all 56, including one real PyPI install, ~25s).
- All 8 files in `broken_examples/` produce the correct diagnosis, checked against real captured output — one documented discrepancy (example 04, sklearn version-dependent, see `PHASE2_SUMMARY.md`); this doesn't affect Phase 3 since only `missing_module` is auto-fixed either way.
- `repair("broken_examples/01_missing_package.py")` verified fixed both by a real integration test (temp venv, real install, confirms the *current* interpreter stays untouched) and manually via the CLI.
- An emergent, unplanned validation: because Phase 3's venvs start empty, `02`/`04` surface as `missing_module` on their first run (numpy/sklearn not installed yet) before revealing the real unhandled error on the second — exactly the layered-error behavior `broken_examples/MANIFEST.md` predicted, confirmed working without special-casing it.
- Every "Required behaviour" assertion and "Suggested manual check" from `PHASE1_TASK.md`, `PHASE2_TASK.md`, and `PHASE3_TASK.md` passes.
- CI (`.github/workflows/tests.yml`) runs all suites on Python 3.10 and 3.12 on every push, with `SKIP_NETWORK_TESTS=1` so it stays fast and non-flaky.

## Infrastructure

- Git repo: `github.com/ibtisam-tanveer/transparent-dep-repair`. **Phases 1 and 2 are committed and pushed** (`4197885`, `1d7f38c`). **Phase 3's Step 0 restructure is committed** (`172303c`, local — not yet pushed); the new-feature work (pypi/venv_manager/repair/apply/loop + tests) is implemented and verified but not yet committed.
- Packaging via `pyproject.toml`: `pip install -e ".[dev]"` sets up the dev environment and registers `repair-tool-run`, `repair-tool-diagnose`, `repair-tool-fix` console scripts.
- Code now lives in `repair_tool/` (moved from flat root modules as Phase 3's Step 0) — anticipated back in the Phase 1 README note ("once a third module needs a home, that's the point to introduce a package folder"), and that moment arrived exactly on schedule with `pypi.py`.
- `.repair_venvs/` (per-target isolated venvs Phase 3 creates) is gitignored.

## Known limitations (expected at this stage, not bugs)

- Only validated against the 8-file controlled set + hand-written edge cases — not yet run against a real benchmark (EnvBench, GigaScience notebooks).
- No UI. Deliberately deferred until there's a fix + transparency report worth visualizing, or until the RQ4 human-study design (Vision Doc §9) calls for one.
- **Open decision, not yet resolved**: which real benchmark/dataset to evaluate against — Vision Doc §8 explicitly flags this as something to "finalise with your supervisor."
- No `.ipynb` notebook handling yet — only plain `.py` files (per all three task specs so far, out of scope).
- Phase 3 only auto-fixes `missing_module` with a resolvable PyPI name — by design (see `PHASE3_TASK.md`'s "Scope" section), not a gap.

## Suggested next step

Commit the Phase 3 new-feature work (separate from the already-committed
Step 0 restructure, per the task doc's own instruction), then request
`PHASE4_TASK.md` before starting metadata-based verification work.
