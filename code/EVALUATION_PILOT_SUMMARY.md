# Evaluation Pilot Summary — harness built, smoke-tested for real, full pilot not yet run

Status: **harness done and verified for real; the ~20-50 repo pilot `TASK_evaluation.md`
asks for has not been run yet.** This document covers what *was* run: the
dataset extraction (full, real) and two small, real smoke tests (3 repos
each) that exist specifically to validate the harness before committing to
the real pilot's cost and time. See `TASK_evaluation.md`, `DEPENDENCY_FAILURE_TAXONOMY.md`,
and `dataset/NOTES_2023.md` for what this builds on.

## What was asked

Build the evaluation: label the real 2023 GigaScience failure set by
dependency-failure-cause, build a harness that runs the already-built
repo-scale agent against real repositories and records structured results,
run a pilot (~20-50 repos), and report effectiveness per category,
grounded-vs-proposed statistics, cap-limited failures, and the cross-file
"fixed for free" finding.

## Part 0 — the two prerequisites

1. **`fresh=True` enforced.** `evaluation/_repo_runner.py` always builds a
   throwaway venv per repo (via `venv_manager.get_fresh_venv_python`); the
   harness never touches the persistent cache. Not optional, not a flag.
2. **`MAX_STEPS` configurable.** Already was, as a parameter
   (`AGENT_FIRST_TASK_SUMMARY.md`) — `evaluation/run_eval.py` exposes it as
   `--max-steps-per-file`, defaulting to **30** (not the engine's own
   default of 10): real notebooks fail in multiple layers (install, then a
   removed API underneath, sometimes more than once), and 10 was shown
   in the Repo Agent task's own testing to end one tool call short of a
   substantively-complete fix. 30 gives roughly 3x headroom over the
   worst single-file case observed before this task (7 steps), while still
   bounding a stuck agent's cost per notebook. No `repair_tool/` change
   was needed for this — it was already a parameter end to end.

## Part 1 — the dataset: 2023 GigaScience, taxonomy-labelled

`dataset/extract_failures_2023.py` queried the real 2023 rerun's `db.sqlite`
(27,271 notebooks, 5,240 repositories — confirmed against the task's own
figures) and classified every real execution-exception row
(`mode IN (3,5)`, `processed & 4 = 4`) with `dataset/taxonomy.py`.

| | count |
|---|---|
| Execution rows classified | **9,101** |
| Distinct repositories represented | **1,282** |

Per-category totals (confirmed + candidate):

| Category | Count |
|---|---|
| A — missing dependency | 6,588 |
| B — moved/renamed import | **0** (see finding below) |
| C — removed/changed API | 94 |
| D — version/incompatibility conflict | 85 |
| E — other/not a dependency failure | 2,150 (+184 excluded) |

**A real finding, not engineered around**: category B is structurally
unobservable from this database — its `reason` column, for the real
exception population, is the bare exception class name only, and the one
signal that would tell "moved import" (B) apart from "missing package" (A)
— the message `cannot import name 'X' from 'Y'` — never appears anywhere
in the 9,101-row population. `diagnose.py`'s own classification (used when
the agent actually runs a notebook for real) can and does detect B
correctly from a real traceback; the taxonomy label from this static CSV
cannot. Full detail in `dataset/NOTES_2023.md`.

## Part 2 — the harness: two real bugs found by running it, not writing it

`evaluation/run_eval.py` + `evaluation/_repo_worker.py` + `evaluation/_repo_runner.py`
clone each sampled repo, repair it, and record one JSONL row per
labelled notebook — resumable (checkpointed per repo), cost-logged
(`llm_calls`/`total_tokens` per notebook), sample-able, and self-cleaning.

Two real problems surfaced from running the harness against real
repositories, not from writing it:

### Bug 1 — an unbounded OpenAI client could silently hang a run

With no client-side timeout, a slow/stuck API response could block an
entire evaluation run for minutes with zero local symptom (no subprocess,
no CPU use — just blocked network I/O), discovered when a smoke-test repo
appeared stuck for 6+ minutes with nothing running. **Fixed**: `agent.py`'s
OpenAI client now has an explicit 120-second request timeout
(`_REQUEST_TIMEOUT_SECONDS`), with a regression test. This is now a
permanent part of `agent.py`, not an evaluation-only patch — it applies to
every call the agent makes, everywhere.

### Bug 2 — a fixed per-repo timeout is unfair, and wastes completed work

The first real smoke test found an 11-notebook repository
(`h2oai/mli-resources`) that ran for **43 minutes** before being manually
stopped — `agent_repair_repo()` correctly processes every discovered file,
not just the labelled failures, so repo *size*, not just labelled failure
count, drives cost and time. Worse: the original design called
`agent_repair_repo()` as one all-or-nothing block, so killing a slow repo
threw away *every* result for it, including notebooks that had already
been successfully fixed before the cut-off.

**Fixed with a redesign, not a bigger number**: `evaluation/_repo_runner.py`'s
`run_repo_incrementally()` reuses `repo.py`'s and `agent.py`'s own
primitives (discovery, dependency install, `agent_repair()` per file — the
same functions `agent_repo.py` itself calls, not duplicated logic) but
yields one result *per file*, as soon as that file finishes. Two
consequences:

1. **The time budget scales with the repo's real file count**
   (`--per-file-budget-minutes`, default 10, times the discovered file
   count — computed from the actual clone, not just the labelled-notebook
   count), so a big repo gets a proportionally bigger budget instead of
   the same fixed number as a small one.
2. **`_repo_worker.py` streams each result to disk immediately, flushed**,
   so if the repo is cut off (by its own graceful internal stop once the
   budget runs out, or by the OS-level subprocess kill that's now only a
   backstop beyond that), whatever already finished is kept. The parent
   harness backfills an honest "time budget exhausted" row only for
   notebooks genuinely never reached.

### Verified for real, after the fix

Re-ran the same 3-repo smoke test (`--per-file-budget-minutes 1.5`, a
deliberately tight budget to exercise the cutoff path on purpose).
`h2oai/mli-resources` (13 real discovered files, 11 labelled) got a
correctly *scaled* budget of 1170s (13 × 90s) and, this time:

- **4 of 11 labelled notebooks were genuinely attempted** — 3 hit the new
  120s OpenAI timeout for real (`LLM call failed: Request timed out.`,
  confirming bug 1's fix fires on real slow responses), 1 was tried and
  genuinely not fixed;
- **7 were honestly recorded as `time budget exhausted ... before this
  file was attempted`** — never pretended to be failures;
- the worker exited cleanly on its own (status `"ok"`, no OS-level kill
  needed) — the internal graceful stop is now the primary mechanism, the
  external kill a backstop that wasn't even triggered here.

Before this fix, this same repo would have reported **zero usable signal**
for all 11 notebooks. 255/255 tests pass (offline and fully online),
including new tests for the incremental runner (`tests/test_repo_runner.py`),
the budget-scaling/partial-row-preservation logic (`tests/test_run_eval.py`),
and the taxonomy classifier (`tests/test_taxonomy.py`).

## The two smoke tests' actual numbers

Both are 3-repo smoke tests (seed 42, so the same 3 repos both times) —
**not** the ~20-50 repo pilot the task asks for; see "What's still needed"
below.

| | Smoke test 1 (pre-fix, 6 min/repo fixed cap) | Smoke test 2 (post-fix, 1.5 min/file scaled budget) |
|---|---|---|
| Repos | 3 | 3 |
| Labelled notebooks | 18 | 18 |
| Wall time | 15.0 min | 34.1 min |
| LLM calls | ~44 | ~157 |
| Usable signal from the 11-notebook repo | 0/11 (all "timed out") | 4/11 attempted, 7/11 honestly "not reached" |

The second run took longer and cost more — it let the agent actually
*work* on files the first run's fixed, too-short cap killed outright. That
trade (more real signal, more real cost) is exactly the one
`TASK_evaluation.md` §2.1 asks to confirm before scaling up, which is why
this stayed at 3 repos rather than going straight to the full pilot.

`evaluation/analyze_results.py`, run against smoke test 2's real results:

```
Effectiveness overall: confirmed only 7.7% (1/13); confirmed+candidate 5.6% (1/18)
Accepted-fix grounding: 0 metadata_grounded, 1 llm_proposed (medium-high confidence)
Cap-limited vs. genuine: 3 ran out of MAX_STEPS; 14 genuinely stopped/not-reached
Cross-file free passes: 0
```

These numbers are **not reportable pilot results** — 3 repos is far too
small a sample, chosen deliberately to validate the harness, not to
measure the tool. They're included here only to prove `analyze_results.py`
runs end to end against real rows and produces the right shape of output.

### The "6%" figure, broken down by actual cause (not taken at face value)

A raw "1/18 fixed" looks like damning evidence the tool doesn't work. Reading
every row's own recorded reason (not the summary percentage) tells a
different story. Of the 17 "not fixed" rows:

| Cause | Count | % of the 17 | What it actually means |
|---|---|---|---|
| Never attempted — repo's time budget ran out first | 9 | 53% | Purely an artifact of this specific test's deliberately tight `--per-file-budget-minutes 1.5` (chosen to exercise the cutoff path cheaply). Not a tool outcome at all — should be excluded from any effectiveness denominator. |
| Ran out of the 30-step repair budget | 3 | 18% | Inconclusive — the agent was still working, not proven unable to fix it. |
| A transient OpenAI slowdown on the first follow-up call | 3 | 18% | Infrastructure flakiness (`LLM call failed: Request timed out.`), unrelated to the notebook's content. |
| **Notebook too large for the model's context window** | 2 | 12% | A genuine, specific, actionable limitation — `context_length_exceeded` (one notebook's own code alone was ~130K tokens, over gpt-4o-mini's 128K limit). Worth fixing before the real pilot (e.g. trim huge cell content before sending it to the model). |

Excluding the 9 never-attempted rows, the real tested sample is **9
notebooks, 1 fixed** — still inconclusive at this sample size, but a very
different picture from "6% success": over half the apparent failures
weren't real attempts, and of the real attempts, a meaningful share were
infrastructure/budget artifacts rather than the repair logic failing on
notebook content it understood.

## Cost observed so far

Across both smoke tests and earlier harness-development runs: roughly
200+ real LLM calls, low hundreds of thousands of tokens, on `gpt-4o-mini`
pricing (cents, not dollars, in total). Wall time is the real constraint,
not $ cost, at this model/price point — a 20-50 repo pilot at the observed
per-repo pace (5-35 min/repo depending on size) could reasonably take
several hours if run sequentially. `--limit-minutes`/`--max-llm-calls` can
bound a single session; the checkpoint means a pilot can be split across
multiple sessions without re-doing finished repos.

## What's still needed (not done in this pass)

- **The actual pilot** (~20-50 repos) — the harness is now validated, but
  no run at that scale has happened. This is the natural next step, with
  the user's go-ahead on time/session-splitting.
- **The context-length-exceeded case** (found above, 2/17 in the smoke
  test) — worth a fix before the real pilot: a notebook whose own code is
  large enough to exceed the model's context window currently fails with
  an unhelpful SDK error rather than a clear, honest "too large for this
  model" result. A reasonable fix is to detect this case and record it
  distinctly (not conflated with a genuine repair failure), and/or trim
  excessively large cell content before it's sent.
- Because of the small sample, no real effectiveness-per-category,
  grounded-vs-proposed, or cross-file-interaction numbers exist yet worth
  reporting as thesis results — `analyze_results.py` is proven to work;
  actual results are the pilot's job, not this smoke-testing pass's.
- `TASK_evaluation.md`'s full definition of done (a pilot results table,
  per-category breakdown, etc.) is therefore not yet met.

## Out of scope (per the task, unchanged)

More agents, the knowledge-graph tool, HTML/UI, the RQ4 human trust study,
and any change to the repair engine's behaviour beyond `MAX_STEPS` being
configurable and the OpenAI client's bounded timeout (a cost-visibility/
reliability fix, not new repair logic).

## How to run / verify

```bash
source .venv/bin/activate
SKIP_NETWORK_TESTS=1 python -m unittest tests.test_taxonomy tests.test_run_eval tests.test_repo_runner tests.test_analyze_results -v

python dataset/extract_failures_2023.py   # writes dataset/failures_2023.csv, prints per-category counts

# a small, cheap smoke test (what this summary reports):
python evaluation/run_eval.py --sample 3 --seed 42 --tag smoke --per-file-budget-minutes 10
python evaluation/analyze_results.py evaluation/results/smoke.jsonl

# the real pilot, once approved:
python evaluation/run_eval.py --sample 30 --tag pilot --per-file-budget-minutes 10
python evaluation/analyze_results.py evaluation/results/pilot.jsonl
```
