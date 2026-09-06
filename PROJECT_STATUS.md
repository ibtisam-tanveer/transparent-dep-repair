# Project Status — AI-Driven Transparent Repair of Software Dependency Configurations

MSc thesis project (Muhammad Ibtisam Tanveer, supervised by Dr. Sheeba Samuel).
This is a snapshot of what's actually built right now, meant to be updated as
phases land. For the full reasoning behind each phase's decisions, see
[PHASE1_SUMMARY.md](PHASE1_SUMMARY.md), [PHASE2_SUMMARY.md](PHASE2_SUMMARY.md),
[PHASE3_ADDENDUM.md](PHASE3_ADDENDUM.md) (Phase 3 design decisions, made
before implementation), [PHASE3_SUMMARY.md](PHASE3_SUMMARY.md),
[PHASE5_SUMMARY.md](PHASE5_SUMMARY.md) (the thesis's core contribution),
[PHASE5_IMPROVEMENTS_SUMMARY.md](PHASE5_IMPROVEMENTS_SUMMARY.md) (hardening
pass on Phase 5), [NOTEBOOK_SUPPORT_TASK.md](NOTEBOOK_SUPPORT_TASK.md) +
[NOTEBOOK_SUPPORT_SUMMARY.md](NOTEBOOK_SUPPORT_SUMMARY.md), and
[DATASET_SUMMARY.md](DATASET_SUMMARY.md) (the independent data track below).

## The tool's target loop

```
run the project -> read the error -> propose a fix -> apply it -> re-run to verify -> explain every decision
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
              implemented so far (Phases 1-3, 5, Notebook Support — Phase 4 deferred, see below)
```

**Build order (deliberate, not phase-numbered order):**
`Phase 1 → Phase 2 → Phase 3 → Phase 5 → Notebook Support → Phase 4`.
Phase 5 comes before Phase 4 because it adds new repair *capability*
(fixing the "not handled yet" cases); Phase 4 *hardens* fixes that already
work, so it makes more sense once there's more to harden. Notebook Support
came before Phase 4 too, since it was a hard blocker for dataset evaluation
(the GigaScience corpus is entirely `.ipynb`) while Phase 4 is not. See
`PHASE5_TASK.md`'s "Build order / where is Phase 4?" note for the original
reasoning.

## What's implemented

| Phase | File(s) | Status | What it does |
|---|---|---|---|
| 1 | `repair_tool/runner.py` | done, committed+pushed (`4197885`) | Runs a target `.py` file as a subprocess, returns `RunResult(ok, returncode, stdout, stderr)`. Never crashes on a broken target, missing file, or timeout. |
| 2 | `repair_tool/diagnose.py` | done, committed+pushed (`1d7f38c`) | Classifies a `RunResult`'s `stderr` into a `Diagnosis(kind, module, package, symbol, detail)` — 5 categories: `missing_module`, `import_name`, `module_attribute_removed`, `object_attribute_error`, `unknown`. Pure classification, no fixing. |
| 3 | `repair_tool/{pypi,venv_manager,repair,apply,loop}.py` | done, committed+pushed (`172303c` + `cb4c515`) | Propose + apply a fix for the one safe deterministic case (missing package), inside an isolated venv, then re-verify by re-running. Rule-based only, no LLM. |
| 5 | `repair_tool/llm.py` + extensions | done, committed+pushed (`2fb8853`) | **The thesis's core contribution.** LLM-based repair for the "not handled yet" kinds: two candidate fixes (code vs. environment), applied+verified by re-running, winner kept, loser recorded as a rejected alternative, `strategy_won` logged. |
| 5 (hardening) | same files, extended | done, committed+pushed (`2fb8853` + `dce5ce9`) | Isolates the two candidate strategies (snapshot/restore, incl. a real bug fixed — a downgraded package was uninstalled entirely instead of restored), records model identity, adds a non-numpy/pandas example. **Surfaced the `06` limitation — see below.** |
| Notebook Support | `repair_tool/notebook.py` + extensions | **done, implemented+verified, not yet committed** | Extends the loop to `.ipynb`. **Found and fixed two real bugs during verification** — a `KernelManager.kernel_cmd` attribute that was silently never consulted (every notebook was secretly executing under this tool's own dev interpreter, defeating isolation) and a kernel/ZMQ-socket resource leak. See `NOTEBOOK_SUPPORT_SUMMARY.md`. |
| 4 | — | **deferred, not dropped** | Verify a fix against authoritative package metadata beyond existence/name. |
| — | — | in progress | Full transparency report (Vision Doc §7) — `alternatives` now carries real content (Phase 5); richer package-metadata provenance comes with Phase 4. |

## ⚠ Must revisit before/around dataset evaluation: the "correct fix, missing import" gap

Found while testing `broken_examples/06_scipy_imread.py` during the Phase 5
hardening pass. The LLM's code-fix candidate was **semantically correct** —
`from imageio import imread`, exactly `MANIFEST.md`'s own documented correct
fix — but `imageio` isn't installed in the venv, and Phase 5's code-fix
mechanism has **no way to install a new dependency a rewritten import
introduces**. The edit applies cleanly; the re-run then fails with
`ModuleNotFoundError`; the tool honestly reports "not fixed."

**Why this is more than a missed case:** the transparency report currently
cannot distinguish "this fix is wrong" from "this fix is right but needs
one more package" — both produce an identical rejected-candidate record
(applied, re-run failed). For a thesis whose contribution is *transparent,
trustworthy* repair, a case where the tool's own explanation is misleading
about *why* something failed is a real gap, not just a missed fix. It's
also not a rare shape: "replace a removed API with a function from a
different library" is a common real-world repair pattern, so the dataset
evaluation will likely hit this repeatedly — Notebook Support's own testing
already reconfirmed this is a real, not theoretical, concern.

**Still correctly out of scope** for any task so far (needs a design
decision, not a targeted patch). Should be resolved, or at minimum
explicitly acknowledged and worked around in the evaluation methodology,
**before or during dataset evaluation**. Good candidate for a dedicated
thesis discussion point either way.

## Data track — supervisor has replied, next steps are now unblocked

Progress update + 4 questions were emailed 2026-08-20. **Reply received.**
Concrete answers and what they mean:

1. **Use the 2023 rerun, not 2021.** Correct database: Zenodo record
   [8226725](https://doi.org/10.5281/zenodo.8226725) (`computational-reproducibility-pmc.zip`,
   415.6 MB). **Not yet downloaded** — deliberately deferred until the
   taxonomy work actually starts (agreed: no reason to fetch it earlier).
2. **Don't restrict to `ImportError`/`ModuleNotFoundError`.** Build and
   justify a real taxonomy instead — at minimum: missing dependency,
   moved/renamed import, removed/changed API, potentially incompatible
   dependency/API usage, other/unknown. Explicitly don't casually label
   every `AttributeError` as dependency-related. **Worth noting**: this
   taxonomy maps closely onto `diagnose.py`'s existing 5-kind
   classification — a genuine opportunity to make the dataset's failure
   taxonomy and the tool's own diagnosis taxonomy the *same* scheme, citing
   Table 5 of the paper (doi: 10.1093/gigascience/giad113) for
   justification, as asked.
3. **GigaScience notebook set is the primary benchmark; EnvBench secondary**
   if feasible, to help show the approach isn't overfitted to one dataset.
4. **RQ4 human study**: worth doing if the thesis timeframe allows; look at
   related work's study designs first. Explicitly non-blocking — doesn't
   gate any current implementation work.

**Old draft dataset (2021 run) status, superseded but not deleted**: `dataset/dependency_failures.csv`
(1,362 rows, committed `5cf3cbd`) was built against the wrong run per (1)
above. It stays in the repo as-is (harmless, documents the process) but
should not be used for the actual evaluation — a redo against the 2023
data is required.

## Verification

- **118/118 tests passing** (92 from Phase 5 + hardening + 26 new from
  Notebook Support), both with `SKIP_NETWORK_TESTS=1` (104 run, 14 correctly
  skipped) and fully online locally (all 118, including two real
  venv/install cycles, multiple real LLM calls, and a real kernel-isolation
  proof, ~123s).
- All 8 original files in `broken_examples/` produce the correct diagnosis; one documented discrepancy (example 04, sklearn version-dependent, see `PHASE2_SUMMARY.md`).
- `repair()` verified fixed end-to-end, with a real key, for `01` (Phase 3), `02`/`03`/`04`/`07`/`08`/`09` (Phase 5, real LLM calls), and both notebook fixtures (`missing_package.ipynb`, `numpy_float.ipynb`).
- `05_pandas_append.py`, `06_scipy_imread.py`, and `broken_examples/notebooks/missing_data_file.ipynb` all genuinely, honestly report "not fixed" for real, distinct, documented reasons — never a false success.
- **Four real bugs found and fixed during verification across the project so far**, none assumed away: (1) LLM JSON responses with unescaped quotes, fixed via `response_format=json_object`; (2) a downgraded package being uninstalled entirely instead of restored during strategy isolation; (3) `KernelManager.kernel_cmd` silently ignored, meaning every notebook secretly ran under the wrong interpreter; (4) a kernel/ZMQ-socket resource leak. All four were caught by testing against real execution, not by inspecting the design.
- Every "Required behaviour" assertion and "Suggested manual check" from `PHASE1_TASK.md` through `NOTEBOOK_SUPPORT_TASK.md` passes.
- CI (`.github/workflows/tests.yml`) runs all suites on Python 3.10 and 3.12 on every push, with `SKIP_NETWORK_TESTS=1` so it stays fast, non-flaky, and free of API cost.
- Dataset extraction verified read-only at the driver level (a direct write attempt against `db.sqlite` fails with `sqlite3.OperationalError`, not just relying on the `mode=ro` flag by convention).

## Infrastructure

- Git repo: `github.com/ibtisam-tanveer/transparent-dep-repair`. Everything through the Phase 5 hardening pass is committed and pushed. **Notebook Support is implemented and fully verified but not yet committed.**
- Packaging via `pyproject.toml`: `openai`, `python-dotenv`, `nbclient`, `nbformat` are core dependencies — each import stays isolated to the one module that needs it (`llm.py`, `notebook.py`), so the rest of the tool works without any of them installed. `ipykernel` is dev-only (for the dev `.venv` itself) plus installed per-target-venv at runtime (`venv_manager.ensure_ipykernel`) — deliberately not a core dependency, since it only needs to live where a kernel actually launches.
- `OPENAI_API_KEY` is read from the environment, with `.env` support (`.env` is gitignored; only its presence/length was ever verified, never its value).
- Code lives in `repair_tool/` (moved from flat root modules as Phase 3's Step 0).
- `.repair_venvs/` (per-target isolated venvs + workspace copies), `.env`, and `*.sqlite`/`computational-reproducibility-pmc/` are all gitignored.

## Known limitations (expected at this stage, not bugs)

- No UI. Deliberately deferred until the RQ4 human-study design (Vision Doc §9) calls for one — and per the supervisor's reply, RQ4 itself is non-blocking for now.
- The "correct fix needs a new import" gap (`06`) — see the flagged section above.
- Phase 3's `missing_module` path only auto-fixes a resolvable PyPI name — by design.
- The draft dataset (`dataset/dependency_failures.csv`) is built against the wrong (2021) GigaScience run — see Data track above; not to be used for real evaluation as-is.

## Suggested next step

1. Commit and push Notebook Support.
2. Start the dataset rework: download the 2023 rerun (Zenodo 8226725, 415.6 MB — ask before fetching, per the earlier agreement to defer this), then rebuild `dataset/explore_db.py`/`extract_dependency_failures.py` around a real taxonomy (see Data track above), citing Table 5 of the paper.
3. The `06` limitation remains unresolved and should be addressed or explicitly worked around before/during dataset evaluation.
