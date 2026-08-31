# Project Status — AI-Driven Transparent Repair of Software Dependency Configurations

MSc thesis project (Muhammad Ibtisam Tanveer, supervised by Dr. Sheeba Samuel).
This is a snapshot of what's actually built right now, meant to be updated as
phases land. For the full reasoning behind each phase's decisions, see
[PHASE1_SUMMARY.md](PHASE1_SUMMARY.md), [PHASE2_SUMMARY.md](PHASE2_SUMMARY.md),
[PHASE3_ADDENDUM.md](PHASE3_ADDENDUM.md) (Phase 3 design decisions, made
before implementation), [PHASE3_SUMMARY.md](PHASE3_SUMMARY.md),
[PHASE5_SUMMARY.md](PHASE5_SUMMARY.md) (the thesis's core contribution),
[PHASE5_IMPROVEMENTS_SUMMARY.md](PHASE5_IMPROVEMENTS_SUMMARY.md) (hardening
pass on Phase 5), and [DATASET_SUMMARY.md](DATASET_SUMMARY.md) (the
independent data track below).

## The tool's target loop

```
run the project -> read the error -> propose a fix -> apply it -> re-run to verify -> explain every decision
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                implemented so far (Phases 1-3, 5 — Phase 4 deferred, see below)
```

**Build order (deliberate, not phase-numbered order):**
`Phase 1 → Phase 2 → Phase 3 → Phase 5 → Notebook Support → Phase 4`.
Phase 5 comes before Phase 4 because it adds new repair *capability*
(fixing the "not handled yet" cases); Phase 4 *hardens* fixes that already
work, so it makes more sense once there's more to harden. Notebook Support
comes before Phase 4 too, since it's a hard blocker for dataset evaluation
(the GigaScience corpus is entirely `.ipynb`) while Phase 4 is not. See
`PHASE5_TASK.md`'s "Build order / where is Phase 4?" note for the original
reasoning.

## What's implemented

| Phase | File(s) | Status | What it does |
|---|---|---|---|
| 1 | `repair_tool/runner.py` | done, committed+pushed (`4197885`) | Runs a target `.py` file as a subprocess, returns `RunResult(ok, returncode, stdout, stderr)`. Never crashes on a broken target, missing file, or timeout. |
| 2 | `repair_tool/diagnose.py` | done, committed+pushed (`1d7f38c`) | Classifies a `RunResult`'s `stderr` into a `Diagnosis(kind, module, package, symbol, detail)` — 5 categories: `missing_module`, `import_name`, `module_attribute_removed`, `object_attribute_error`, `unknown`. Pure classification, no fixing. |
| 3 | `repair_tool/{pypi,venv_manager,repair,apply,loop}.py` | done, committed+pushed (`172303c` + `cb4c515`) | Propose + apply a fix for the one safe deterministic case (missing package), inside an isolated venv, then re-verify by re-running. Rule-based only, no LLM. |
| 4 | — | **deferred, not dropped** | Verify a fix against authoritative package metadata beyond existence/name. Deliberately built *after* Phase 5 (per `PHASE5_TASK.md`'s explicit build-order note: `1→2→3→5→Notebook Support→4`) — Phase 5 adds new capability, Phase 4 hardens fixes that already work. |
| 5 | `repair_tool/llm.py` + extensions to `repair`/`apply`/`loop` | done, committed+pushed (`2fb8853`) | **The thesis's core contribution.** LLM-based repair for the "not handled yet" kinds: asks for two candidate fixes (code vs. environment) in one call, applies+verifies each by re-running, keeps the winner, records the loser as a rejected alternative, records `strategy_won`. See `PHASE5_SUMMARY.md`. |
| 5 (hardening) | same files, extended | done, committed+pushed (`2fb8853` + `dce5ce9`) | 3 targeted fixes per `PHASE5_IMPROVEMENTS_TASK.md`: isolates the two candidate strategies (snapshot/restore package state, including a real bug found — a downgraded package was being uninstalled entirely instead of restored to its prior version), records model identity in every result, adds one non-numpy/pandas example (`09_dict_has_key.py`). See `PHASE5_IMPROVEMENTS_SUMMARY.md`. **Surfaced an architectural limitation, not fixed here — see the flagged section below.** |
| — | — | not started | Notebook Support — required next, before any dataset evaluation (the GigaScience corpus is `.ipynb`; Phase 5 stayed `.py`-only on purpose). |
| — | — | in progress | Full transparency report (Vision Doc §7) — `alternatives` now carries real content (Phase 5); richer package-metadata provenance comes with Phase 4. |

## ⚠ Must revisit before/around dataset evaluation: the "correct fix, missing import" gap

Found while testing `broken_examples/06_scipy_imread.py` during the Phase 5
hardening pass (`PHASE5_IMPROVEMENTS_SUMMARY.md`). The LLM's code-fix
candidate was **semantically correct** — `from imageio import imread`,
exactly `MANIFEST.md`'s own documented correct fix — but `imageio` isn't
installed in the venv, and Phase 5's code-fix mechanism has **no way to
install a new dependency a rewritten import introduces**. The edit applies
cleanly; the re-run then fails with `ModuleNotFoundError`; the tool
honestly reports "not fixed."

**Why this is more than a missed case:** the transparency report currently
cannot distinguish "this fix is wrong" from "this fix is right but needs
one more package" — both produce an identical rejected-candidate record
(applied, re-run failed). For a thesis whose contribution is *transparent,
trustworthy* repair, a case where the tool's own explanation is misleading
about *why* something failed is a real gap, not just a missed fix. It's
also not a rare shape: "replace a removed API with a function from a
different library" — which is exactly how `06` (and, differently, `04`'s
`sklearn.externals`→`joblib` case) are meant to be fixed — is a common
real-world repair pattern, so the dataset evaluation will likely hit this
repeatedly.

**Correctly out of scope for the hardening task** (needs a design decision,
not a targeted patch — e.g., should a code-fix candidate be allowed to
declare an additional package requirement the LLM call already names?). Not
a reason to keep hardening now. But it should be resolved, or at minimum
explicitly acknowledged and worked around in the evaluation methodology,
**before or during dataset evaluation** — evaluating without addressing it
risks two problems at once: an understated success rate, and (worse, given
the thesis's actual claim) some "failures" being mislabeled in exactly the
dimension the thesis cares about — the explanation. Good candidate for a
dedicated thesis discussion point either way ("a limitation identified and
how it would be addressed").

## Data track (independent of the phases above — see `DATASET_SUMMARY.md`)

| Task | Status | What it does |
|---|---|---|
| Draft dependency-failure dataset from GigaScience `db.sqlite` | done (draft), committed+pushed (`5cf3cbd`) | Read-only exploration + extraction: `dataset/explore_db.py`, `dataset/extract_dependency_failures.py` → `dataset/dependency_failures.csv` (1,362 rows, 311 repos). Filter matches the original authors' own definition. |

**Significant finding**: the `db.sqlite` on hand exactly matches the paper's
**2021 initial run** (9,625 notebooks, 1,419 articles) — not the **2023
rerun** (27,271 notebooks) the task brief's "~27,000" figure referred to.
Confirmed directly against the published paper, not guessed. Progress update
+ this question (plus 3 others) emailed to the supervisor on 2026-08-20;
awaiting her reply.

## Verification

- **92/92 tests passing** (79 from Phase 5 + 13 new from the hardening pass), both with `SKIP_NETWORK_TESTS=1` (82 run, 10 correctly skipped) and fully online locally (all 92, including real venvs/installs and multiple real OpenAI calls, ~35s).
- All 8 original files in `broken_examples/` produce the correct diagnosis, checked against real captured output — one documented discrepancy (example 04, sklearn version-dependent, see `PHASE2_SUMMARY.md`).
- `repair()` verified fixed end-to-end, with a real key, for `01` (Phase 3), `02`/`03`/`04`/`07`/`08`/`09` (Phase 5, real LLM calls, real code edits).
- `05_pandas_append.py` and `06_scipy_imread.py` genuinely, honestly report "not fixed" — real candidates tried, real distinct reasons each time (a subtle LLM code-generation bug caught by verification; a real Python-3.14/old-package build incompatibility; and for `06`, a newly-documented limitation — a semantically *correct* code fix that needs a new import Phase 5 has no mechanism to install). All documented in the two Phase 5 summaries as the verify-before-trusting design working correctly, not defects.
- Two real bugs were found and fixed during verification, not assumed away: (1) the LLM's JSON responses could contain unescaped quotes inside string values, fixed with OpenAI's `response_format=json_object`; (2) the hardening pass's own `restore_packages` uninstalled a downgraded package entirely instead of restoring its prior version, fixed to diff by name and reinstall the exact pinned version when it changed rather than vanished.
- Every "Required behaviour" assertion and "Suggested manual check" from `PHASE1_TASK.md` through `PHASE5_IMPROVEMENTS_TASK.md` passes.
- CI (`.github/workflows/tests.yml`) runs all suites on Python 3.10 and 3.12 on every push, with `SKIP_NETWORK_TESTS=1` so it stays fast, non-flaky, and free of API cost.
- Dataset extraction verified read-only at the driver level (a direct write attempt against `db.sqlite` fails with `sqlite3.OperationalError`, not just relying on the `mode=ro` flag by convention).

## Infrastructure

- Git repo: `github.com/ibtisam-tanveer/transparent-dep-repair`. **Everything through Phase 5 + hardening is committed** (`2fb8853`, `dce5ce9`) — not yet pushed.
- Packaging via `pyproject.toml`: `openai` and `python-dotenv` are now core dependencies (Phase 5) — but the import stays isolated to `llm.py`, so the rest of the tool works without either installed. `pip install -e ".[dev]"` registers `repair-tool-run`, `repair-tool-diagnose`, `repair-tool-fix` console scripts.
- `OPENAI_API_KEY` is read from the environment, with `.env` support (via `python-dotenv`, since shell env vars don't persist between separate tool invocations) — `.env` is gitignored and was never pasted into any conversation; only its presence/length was verified, never its value.
- Code lives in `repair_tool/` (moved from flat root modules as Phase 3's Step 0).
- `.repair_venvs/` (per-target isolated venvs + workspace copies), `.env`, and `*.sqlite`/`computational-reproducibility-pmc/` are all gitignored.

## Known limitations (expected at this stage, not bugs)

- Only validated against the 8-file controlled set + hand-written edge cases — not yet run against a real benchmark (EnvBench, GigaScience notebooks). Blocked on Notebook Support (next) and the supervisor's dataset/benchmark decisions.
- No UI. Deliberately deferred until the RQ4 human-study design (Vision Doc §9) calls for one.
- **Open decisions, not yet resolved** (all emailed to the supervisor 2026-08-20): which GigaScience run (2021 vs 2023) to use, dataset filter completeness, benchmark choice (Vision Doc §8), RQ4 study design.
- Phase 3's `missing_module` path only auto-fixes a resolvable PyPI name — by design, unchanged by Phase 5.
- The "correct fix needs a new import" gap (`06`) — see the flagged section above; not repeated here.

## Suggested next step

Push the Phase 5 + hardening commits. Write and start the Notebook Support
task (`NOTEBOOK_SUPPORT_TASK.md`) — the last thing between the working tool
and the GigaScience dataset, needed regardless of how the supervisor's 4
open questions resolve. The `06` limitation above should be at least
explicitly addressed in that task's or the evaluation's design, even if not
fully fixed immediately.
