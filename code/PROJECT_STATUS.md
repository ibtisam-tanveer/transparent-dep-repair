# Project Status — AI-Driven Transparent Repair of Software Dependency Configurations

MSc thesis project (Muhammad Ibtisam Tanveer, supervised by Dr. Sheeba Samuel).
This is a snapshot of what's actually built right now, meant to be updated as
phases land. For the full reasoning behind each phase's decisions, see
[PHASE1_SUMMARY.md](PHASE1_SUMMARY.md), [PHASE2_SUMMARY.md](PHASE2_SUMMARY.md),
[PHASE3_ADDENDUM.md](PHASE3_ADDENDUM.md) (Phase 3 design decisions, made
before implementation), [PHASE3_SUMMARY.md](PHASE3_SUMMARY.md),
[PHASE5_SUMMARY.md](PHASE5_SUMMARY.md) (the thesis's core contribution so
far), [PHASE5_IMPROVEMENTS_SUMMARY.md](PHASE5_IMPROVEMENTS_SUMMARY.md)
(hardening pass on Phase 5), [NOTEBOOK_SUPPORT_TASK.md](NOTEBOOK_SUPPORT_TASK.md) +
[NOTEBOOK_SUPPORT_SUMMARY.md](NOTEBOOK_SUPPORT_SUMMARY.md),
[NEW_DIRECTION.md](NEW_DIRECTION.md) (the scope pivot below) +
[REPO_FOUNDATION_TASK.md](REPO_FOUNDATION_TASK.md) +
[REPO_FOUNDATION_SUMMARY.md](REPO_FOUNDATION_SUMMARY.md),
[AGENTIC_DIRECTION_AND_FIRST_TASK.md](AGENTIC_DIRECTION_AND_FIRST_TASK.md)
(the staged agentic build plan) + [AGENT_FIRST_TASK_SUMMARY.md](AGENT_FIRST_TASK_SUMMARY.md),
[TASK_provenance_and_report.md](TASK_provenance_and_report.md) +
[PROVENANCE_REPORT_SUMMARY.md](PROVENANCE_REPORT_SUMMARY.md),
[TASK_repo_scale_agent.md](TASK_repo_scale_agent.md) +
[REPO_AGENT_SUMMARY.md](REPO_AGENT_SUMMARY.md),
[TASK_evaluation.md](TASK_evaluation.md) + [DEPENDENCY_FAILURE_TAXONOMY.md](DEPENDENCY_FAILURE_TAXONOMY.md)
+ [EVALUATION_PILOT_SUMMARY.md](EVALUATION_PILOT_SUMMARY.md), and
[DATASET_SUMMARY.md](DATASET_SUMMARY.md) (the earlier, now-superseded 2021
data track).

## ⚠ Scope pivot (2026-09-20, refined 2026-10-03) — read `NEW_DIRECTION.md` first

Following a supervisor meeting, the thesis direction has expanded from
single-file repair to **whole-repository** repair, a **hybrid**
classical+LLM strategy, and an **agentic** architecture. Confirmed vs.
pending is spelled out precisely in `NEW_DIRECTION.md` — the short version:
repo-level scope and `AttributeError`-in-taxonomy are confirmed; the exact
novel contribution, build priority, which classical tool to study, and the
model/API key details are still pending written confirmation.
`AGENTIC_DIRECTION_AND_FIRST_TASK.md` (2026-10-03) then staged the agentic
layer into small, independently-testable steps, `TASK_provenance_and_report.md`
(2026-10-03) sharpened the first step's trust model, and
`TASK_repo_scale_agent.md` (2026-10-04) lifted it to whole repositories —
**built so far under the new direction: the repo-analysis foundation, a
single LLM agent choosing tools (on single files), a two-axis provenance
model + the first transparency report, and that same agent at repo scale
in one shared environment, with an evaluation-ready `fresh` mode**. All
four were deliberately the next-safest thing to build regardless of how
the pending items resolve. `TASK_evaluation.md` (2026-10-06) is the pivot
from building to measuring — it runs the already-built system against the
real 2023 GigaScience rerun and produces the thesis's results chapter
numbers; see "Evaluation" in `README.md` and `EVALUATION_PILOT_SUMMARY.md`.
Everything from Phases 1-5 + Notebook Support survives unchanged as the
engine every later layer sits on top of.

## The tool's target loop

```
run the project -> read the error -> propose a fix -> apply it -> re-run to verify -> explain every decision
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    implemented so far, single-file (Phases 1-3, 5, Notebook Support) + one-repo run/diagnose (Repo Foundation)
    + single-file, agent-chosen tool order, now lifted to a whole repo in one shared environment (Repo Agent)
```

**Build order so far (deliberate, not phase-numbered order):**
`Phase 1 → Phase 2 → Phase 3 → Phase 5 → Notebook Support → Repo Foundation → Agentic first task → Provenance & report → Repo Agent`.
Phase 4 was deferred (see `PHASE5_TASK.md`'s "Build order" note) and is now
further behind the remaining agentic layers (multi-agent, a classical KG
tool) per `NEW_DIRECTION.md` — its exact position depends on the pending
priority decision.

## What's implemented

| Phase | File(s) | Status | What it does |
|---|---|---|---|
| 1 | `repair_tool/runner.py` | done, committed+pushed (`4197885`) | Runs a target `.py` file as a subprocess, returns `RunResult(ok, returncode, stdout, stderr)`. Never crashes on a broken target, missing file, or timeout. |
| 2 | `repair_tool/diagnose.py` | done, committed+pushed (`1d7f38c`) | Classifies a `RunResult`'s `stderr` into a `Diagnosis(kind, module, package, symbol, detail)` — 5 categories: `missing_module`, `import_name`, `module_attribute_removed`, `object_attribute_error`, `unknown`. Pure classification, no fixing. `AttributeError`s are already classified here (see the scope-pivot note above re: item 2). |
| 3 | `repair_tool/{pypi,venv_manager,repair,apply,loop}.py` | done, committed+pushed (`172303c` + `cb4c515`) | Propose + apply a fix for the one safe deterministic case (missing package), inside an isolated venv, then re-verify by re-running. Rule-based only, no LLM. |
| 5 | `repair_tool/llm.py` + extensions | done, committed+pushed (`2fb8853`) | LLM-based repair for the "not handled yet" kinds: two candidate fixes (code vs. environment), applied+verified by re-running, winner kept, loser recorded as a rejected alternative, `strategy_won` logged. |
| 5 (hardening) | same files, extended | done, committed+pushed (`2fb8853` + `dce5ce9`) | Isolates the two candidate strategies (snapshot/restore, incl. a real bug fixed), records model identity, adds a non-numpy/pandas example. **Surfaced the `06` limitation — see below.** |
| Notebook Support | `repair_tool/notebook.py` + extensions | done, committed+pushed (`0d56265`) | Extends the loop to `.ipynb`. **Found and fixed two real bugs during verification** — a `KernelManager.kernel_cmd` attribute silently never consulted (every notebook was secretly executing under this tool's own dev interpreter) and a kernel/ZMQ-socket resource leak. See `NOTEBOOK_SUPPORT_SUMMARY.md`. |
| Repo Foundation | `repair_tool/repo.py` | done, committed+pushed (`7356651`/`7b22f43`, venv-detection fix `a589968`) | `analyze_repo()`: one shared venv per repo, dependency-file detection + best-effort install, discovers and runs every `.py`/`.ipynb` file, per-file diagnosis, configurable pass rule. `analyze_repo_url()` added as a follow-up: clones a git URL into a throwaway temp dir, analyzes it, always deletes the clone. Reuses `runner`/`notebook`/`diagnose`/`venv_manager` entirely unchanged. **Run and diagnose only — no repair at repo scale.** See `REPO_FOUNDATION_SUMMARY.md`. (Commit hashes changed from earlier reports after a history rewrite removed co-author trailers; content is unchanged.) |
| Agentic first task | `repair_tool/agent_tools.py`, `repair_tool/agent.py` | done, committed+pushed (`2a2efd5`/`53ffbc2`) | `agent_repair()`: a single ReAct-style tool-calling LLM agent replaces `loop.py`'s fixed propose→apply→verify order with the model *choosing* among 6 tools (`run_target`, `diagnose_error`, `lookup_package`, `install_package`, `edit_code`, `verify`), one new, parallel entry point — `loop.py` is untouched. Verified for real on both `01` (install) and `02` (two rounds: install then code-edit). See `AGENT_FIRST_TASK_SUMMARY.md`. |
| Provenance & report | `repair_tool/agent.py` (extended), `repair_tool/report.py` (new) | done, committed+pushed (`5d2c4d6`/`94a9c8b`) | Replaced the single `provenance` tag with two independent axes, `grounding` (`metadata_grounded`/`llm_proposed`/`deterministic`, `kg_grounded` reserved) and `verification` (`verified`/`unverified`/`n/a`), plus a derived `confidence`. Fixes a real honesty gap: a well-founded `numpy` install now reads `metadata_grounded`+`unverified` instead of the misleading `llm_unverified` when a later check reveals a *different* failure. `report.py`'s `build_report()` renders the first human-readable transparency report (plain text) from the trace, including actions that didn't finish the job or failed outright — nothing hidden. **Found and fixed a real bug during verification**: an already-passing target confirmed via `run_target` alone wasn't being credited the same as one confirmed via `verify`, even though they're the same check. See `PROVENANCE_REPORT_SUMMARY.md`. |
| Repo Agent | `repair_tool/agent_repo.py` (new), `venv_manager.py`/`agent.py`/`report.py` (extended) | done, committed+pushed (`eec8e7f`/`b6ee581`) | `agent_repair_repo()` lifts `agent_repair()` to a whole repository: reuses `repo.py`'s discovery/dependency-install and `agent.py`'s per-file agent unchanged, runs every file in **one shared environment**, returns per-file `AgentResult`s + a repo summary. `build_repo_report()` renders the repo-level transparency report. **Evaluation-ready**: `fresh=True` (on both `agent_repair()` and `agent_repair_repo()`) guarantees a brand-new venv/workspace instead of the persistent cache — required for any dataset run, since the cache would otherwise make an already-fixed target look like it needed no repair on a later pass. Verified for real: a shared-dependency fixture proved one file's install made a second file pass with zero agent involvement, and — found, not engineered — a third file's need was satisfied by the *first* file's **transitive** dependency. See `REPO_AGENT_SUMMARY.md`. |
| Evaluation | `dataset/taxonomy.py`, `dataset/extract_failures_2023.py`, `evaluation/run_eval.py`, `evaluation/_repo_runner.py`, `evaluation/_repo_worker.py`, `evaluation/analyze_results.py` (all new) | harness done+smoke-tested, not yet committed; **the ~20-50 repo pilot itself not yet run** | The pivot from building to measuring. Taxonomy-labels the 2023 GigaScience rerun's real exception population (9,101 rows, `dataset/failures_2023.csv`) into the A-E dependency-failure-cause scheme — found category B is structurally unobservable from this db's bare-exception-name logging. A resumable, per-file-streaming harness clones each sampled repo and repairs it file by file in a fresh env, with a time budget that *scales with the repo's real discovered file count* rather than one fixed number. **Found and fixed two real bugs by running it**: an unbounded OpenAI client that could silently hang a run (now a 120s timeout, permanent in `agent.py`), and a fixed per-repo timeout that both unfairly penalized large repos and discarded every already-finished result on a cutoff (now file-count-scaled + streamed incrementally, so a cutoff keeps whatever already succeeded). Verified for real: a repo that previously wasted 11/11 notebooks on a blanket timeout now yields 4 genuinely-attempted + 7 honestly-"not reached," never silently lost. See `EVALUATION_PILOT_SUMMARY.md`. |
| Remaining agentic layers (multi-agent, classical KG tool), repo-scale repair beyond this, hybrid beyond PyPI+execution | — | **on hold** | Explicitly paused pending the supervisor's confirmation of the novel contribution, build priority, and which classical tool to study (`NEW_DIRECTION.md`); staged order for when it resumes is in `AGENTIC_DIRECTION_AND_FIRST_TASK.md`. |
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
different library" is a common real-world repair pattern, so this will
likely matter *more*, not less, once repo-scale repair is built (more
files, more imports, more chances to hit this).

**Still correctly out of scope** for any task so far. Should be resolved,
or at minimum explicitly acknowledged and worked around, before repo-scale
repair or dataset evaluation are built. Good candidate for a dedicated
thesis discussion point either way.

## Data track — paused behind the re-scope, not abandoned

Progress update + 4 questions were emailed 2026-08-20; a reply was
received with concrete answers (2023 GigaScience rerun via Zenodo record
8226725, a real failure taxonomy needed, GigaScience-primary/EnvBench-
secondary, RQ4 non-blocking). **All of this is now paused** per
`NEW_DIRECTION.md`'s explicit "what is on hold" list — the taxonomy
application, the 2023 dataset download/extraction, Phase 4, and RQ4 all
wait behind the repo-level re-scope being settled first. Nothing about the
supervisor's answers has changed; they simply haven't been actioned yet.

**Old draft dataset (2021 run) status, superseded but not deleted**:
`dataset/dependency_failures.csv` (1,362 rows, committed `5cf3cbd`) was
built against the wrong run and the wrong (too-narrow) filter. Stays in the
repo as-is (harmless, documents the process); not to be used for the
actual evaluation once that work resumes.

## Verification

- **255/255 tests passing** (199 prior + 56 new from the Evaluation task:
  19 for `dataset/taxonomy.py`'s classifier, the rest for the harness —
  `evaluation/run_eval.py` (sampling, checkpointing, budget-scaling,
  partial-row preservation), `evaluation/_repo_runner.py` (the incremental
  per-file driver), and `evaluation/_repo_worker.py` (the subprocess
  streaming contract)), with `SKIP_NETWORK_TESTS=1` and fully online
  locally, including two real 3-repo smoke tests of the evaluation harness
  itself (not the pilot) against live GitHub repos and the real OpenAI API.
- All 8 original files in `broken_examples/` produce the correct diagnosis; one documented discrepancy (example 04, sklearn version-dependent, see `PHASE2_SUMMARY.md`).
- `repair()` verified fixed end-to-end, with a real key, for `01` (Phase 3), `02`/`03`/`04`/`07`/`08`/`09` (Phase 5, real LLM calls), and both notebook fixtures.
- `agent_repair()` verified fixed end-to-end, with a real key, for `01` (one round: install) and `02`/`03` (two rounds: install, then code-edit), plus `fresh=True` genuinely re-fixing a target a cached run had already fixed — see `AGENT_FIRST_TASK_SUMMARY.md` and `PROVENANCE_REPORT_SUMMARY.md`.
- `agent_repair_repo()` verified fixed end-to-end, with a real key, against a fixture repo with a missing-package file, a removed-API file, and a file that only passes because of the first file's shared-environment install (one of them via a transitive dependency, not even a direct one) — see `REPO_AGENT_SUMMARY.md`.
- `05_pandas_append.py`, `06_scipy_imread.py`, and `broken_examples/notebooks/missing_data_file.ipynb` all genuinely, honestly report "not fixed" for real, distinct, documented reasons — never a false success.
- **Eight real bugs found and fixed during verification across the project so far**, none assumed away: (1) LLM JSON responses with unescaped quotes; (2) a downgraded package uninstalled entirely instead of restored during strategy isolation; (3) `KernelManager.kernel_cmd` silently ignored, notebooks secretly running under the wrong interpreter; (4) a kernel/ZMQ-socket resource leak; (5) a fixture-design mistake (not a code bug) where a `setup.py` written only to test discovery-exclusion also got picked up as an installable dependency source and genuinely executed; (6) `agent_repair()` not crediting an already-passing project confirmed via a plain `run_target` call the same as one confirmed via `verify`, even though they run the identical check; (7) the OpenAI client having no request timeout, letting a slow/stuck API response silently stall an evaluation run for minutes; (8) a fixed per-repo evaluation timeout that was both unfair across repo sizes and discarded every already-finished result on a cutoff — see `REPO_FOUNDATION_SUMMARY.md`, `PROVENANCE_REPORT_SUMMARY.md`, and `EVALUATION_PILOT_SUMMARY.md`. All caught by testing against real execution (real repos, real API calls), not by inspecting the design.
- Every "Required behaviour" assertion and "Suggested manual check" from `PHASE1_TASK.md` through `REPO_FOUNDATION_TASK.md`, and every definition-of-done item in `AGENTIC_DIRECTION_AND_FIRST_TASK.md` §4.5, `TASK_provenance_and_report.md`, and `TASK_repo_scale_agent.md`, passes. `TASK_evaluation.md`'s prerequisites (§0) are met; its pilot-scale definition of done (§6) is **not yet** — see `EVALUATION_PILOT_SUMMARY.md`.
- CI (`.github/workflows/tests.yml`) runs all suites on Python 3.10 and 3.12 on every push, with `SKIP_NETWORK_TESTS=1` so it stays fast, non-flaky, and free of API cost.

## Infrastructure

- Git repo: `github.com/ibtisam-tanveer/transparent-dep-repair`. Everything through the Repo Agent task is committed and pushed (history was rewritten once, right after Repo Foundation, to drop co-author trailers from every commit message — content unchanged, hashes changed). The Evaluation task (`dataset/taxonomy.py`, `dataset/extract_failures_2023.py`, `evaluation/`, the `_REQUEST_TIMEOUT_SECONDS` fix in `agent.py`, tests, this doc's updates) is built and verified but **not yet committed**. The 2023 GigaScience archive itself (`computational-reproducibility-pmc.zip`/`computational-reproducibility-pmc/`) is downloaded locally, gitignored, never committed. Remote uses SSH (was HTTPS with an expiring token) to stop the recurring auth-failure loop.
- Packaging via `pyproject.toml`: `openai` (bumped to `>=1.3.0` for tool-calling support), `python-dotenv`, `nbclient`, `nbformat` are core dependencies, each import isolated to the one module that needs it. `ipykernel` is dev-only plus installed per-target-venv at runtime. Console scripts: `repair-tool-agent`, `repair-tool-agent-repo`.
- `OPENAI_API_KEY` is read from the environment, with `.env` support (gitignored; only its presence/length was ever verified, never its value). **May need to change** once the supervisor's model/key details are confirmed (`NEW_DIRECTION.md` item 6) — `llm.py`'s OpenAI-specific bits (e.g. `response_format={"type": "json_object"}`) would need adjusting for a non-OpenAI provider.
- Code lives in `repair_tool/` (moved from flat root modules as Phase 3's Step 0).
- `.repair_venvs/`, `.env`, and `*.sqlite`/`computational-reproducibility-pmc/` are all gitignored.

## Known limitations (expected at this stage, not bugs)

- No UI. Deliberately deferred until the RQ4 human-study design (Vision Doc §9) calls for one — itself paused per the re-scope.
- The "correct fix needs a new import" gap (`06`) — see the flagged section above.
- Phase 3's `missing_module` path only auto-fixes a resolvable PyPI name — by design.
- The draft dataset (`dataset/dependency_failures.csv`) is built against the wrong (2021) run and an over-narrow filter — see Data track above. **Superseded**: `dataset/failures_2023.csv` (the 2023 rerun, all exception causes, taxonomy-labelled) is now the dataset the evaluation actually uses.
- Category B (moved/renamed import) is structurally unobservable from the 2023 db's `reason` column (bare exception class names only, no message) — zero rows are labelled B in `dataset/failures_2023.csv`. Not a tool limitation: the agent's own `diagnose.py` correctly detects B when it actually runs a notebook and sees the real traceback. See `dataset/NOTES_2023.md`.
- Repo Foundation only handles `requirements.txt` and a package-declaring `pyproject.toml`/`setup.py` for real installation; `environment.yml` (conda) and `Pipfile` (pipenv) are recognised but not installed — by design, not a gap to close without adding new tooling.
- **Evaluation runs must use the fresh-environment mode (`fresh=True`)** — the default cached venv/workspace reuse makes a target look already-fixed on a second run and would contaminate evaluation results.

## Suggested next step

Commit and push the Evaluation task (harness built, smoke-tested twice for
real, just not yet in git). Then run the actual pilot (~20-50 repos per
`TASK_evaluation.md` §2.1) — the harness is now validated (two real bugs
found and fixed by smoke-testing it), but the pilot itself hasn't happened
yet, so there are no real effectiveness/grounding/cross-file numbers to
report as results. That's the one concrete, unblocked next action; see
`EVALUATION_PILOT_SUMMARY.md`'s "What's still needed."

Beyond that, everything else is genuinely blocked on the supervisor's
written confirmation of: the exact novel contribution, build priority,
which classical dependency tool to study, and the model/API key details
(`NEW_DIRECTION.md`). No multi-agent or classical-KG-tool work should start
until then — `AGENTIC_DIRECTION_AND_FIRST_TASK.md` section 4.6 and
`TASK_repo_scale_agent.md`'s "Out of scope" list what's deliberately out
of scope for now. The `06` limitation and the paused taxonomy/RQ4 work
remain accurately tracked here, ready to resume once priorities are
confirmed.
