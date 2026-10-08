# Evaluation Task — run the tool on real data and measure it

**This is the pivot from building to measuring.** The system is essentially
complete: it repairs whole repositories of notebooks and explains every fix with a
two-axis, provenance-tagged trust model. This task does **not** add repair features.
It builds the evaluation: run the tool on real data, and produce the numbers and
findings the thesis reports.

Read `PROJECT_STATUS.md`, `REPO_AGENT_SUMMARY.md`, `PROVENANCE_REPORT_SUMMARY.md`,
and **`DEPENDENCY_FAILURE_TAXONOMY.md`** (the taxonomy doc this task refers to —
it defines categories A–E, the confirmed/candidate/excluded tiers, and how to label
the failure set) first. Nothing in the `repair_tool/` engine should need new repair
logic here — if it does, stop and flag it.

---

## 0. Prerequisites — two evaluation-readiness fixes (do these FIRST)

Both were surfaced by earlier tasks and will silently corrupt results if skipped.

1. **Fresh environment per run (already built — just enforce it).** The harness must
   call the agent with `fresh=True` for every repo, so each run starts from the
   project's real, unmodified state. A cached, already-fixed workspace would make a
   target look "already working" and inflate success rates. This capability exists
   (`agent_repair_repo(..., fresh=True)`); the harness must always use it.
2. **Make `MAX_STEPS` configurable and set it sensibly.** The current `MAX_STEPS = 10`
   cap caused a substantively-complete fix to report `fixed=False` when a file needed
   an install plus two fix rounds. Real notebooks fail in *many* layers, so a low cap
   **systematically under-reports success on the hardest cases.** Expose
   `max_steps_per_file` (already a parameter on `agent_repair_repo`) up through the
   harness, and use a higher value for evaluation (e.g. 25–40 — justify the choice).
   Record the value used with the results, and record when a run hit the cap (so
   "failed" can be split into "genuinely couldn't fix" vs. "ran out of steps").

---

## 1. The dataset

**Source: the GigaScience 2023 rerun** (Samuel & Mietchen). Zenodo record
**8226725** (`computational-reproducibility-pmc.zip`, ~415 MB), which contains the
`db.sqlite` for the 2023 run (27,271 notebooks / 3,467 articles). **Do not use the
2021 run** — the old `dataset/dependency_failures.csv` was built against it and is
superseded.

> Ask the user before downloading the 415 MB archive — they agreed to defer the
> fetch until this work starts. Once fetched, keep it gitignored (as the 2021 one
> was); never commit the database or cloned repos.

### 1.1 Build the taxonomy-labelled failure set

Rebuild the extraction (the 2021-run scripts `dataset/explore_db.py` /
`extract_dependency_failures.py` are the starting point, but update for the 2023 db
and the **taxonomy**, not the old single `ImportError`/`ModuleNotFoundError` filter):

- Use the authors' own definitions where they exist (the 2021 work already matched
  `get_repro_missing_dependencies()` — reuse that approach for the 2023 db).
- Classify each failing execution into the **dependency-failure taxonomy**
  (categories A–E from the taxonomy doc: missing dependency, moved/renamed import,
  removed/changed API, version conflict, other/not-dependency). `AttributeError` is
  in scope as "removed/changed API" **as a candidate** — confirmed only where
  evidence supports a version cause; the taxonomy doc defines the rule.
- Output `dataset/failures_2023.csv`: one row per failing notebook with its
  repository, notebook path, error type, and assigned taxonomy category.
- Record the per-category counts (this is itself a reportable result, and lets the
  evaluation report success *per category*).

---

## 2. The evaluation harness

Add `evaluation/` (separate from `repair_tool/`, like `dataset/`). Build
`evaluation/run_eval.py` that, given a list of target repos/notebooks:

- for each repo: clone it (the agent already has `analyze_repo_url` / git-clone
  support), run `agent_repair_repo(repo, fresh=True, max_steps_per_file=N)`, and
  record the result;
- write one structured result row per notebook (see §3), appending to a results file
  as it goes;
- **be resumable / checkpointed:** record which repos are already done and skip them
  on re-run, so a crash or a stop doesn't lose hours of work (this is essential —
  runs are long and cost money);
- **be cost- and time-aware:** take a `--sample N` (process only N repos) and a
  `--limit` on time or cost; log the number of LLM calls and (if available) token
  usage per repo, so total API cost is visible and boundable;
- clean up each clone and its fresh venv after recording the result (don't fill the
  disk across hundreds of repos);
- never crash the whole run on one bad repo — record it as an error row and continue.

### 2.1 Start small, deliberately

Do **not** run all of it first. The first milestone is a **pilot**: ~20–50 repos,
end to end, producing real result rows. Confirm the harness, the cost per repo, and
the result format are right *before* scaling up. Report the pilot results; decide the
full-run size with the user based on observed cost/time.

---

## 3. What to measure (the result rows + the report)

For each notebook, record at least: repository, notebook path, taxonomy category,
whether it was fixed (verified by re-running), which strategy/strategies were used,
the fix actions' grounding/verification/confidence, number of agent steps, whether
`MAX_STEPS` was hit, and any error.

From those rows, `evaluation/analyze_results.py` (or a notebook) produces the
thesis's headline numbers:

- **Effectiveness overall:** % of previously-failing notebooks now runnable.
- **Effectiveness per taxonomy category:** the key breakdown — where the tool is
  strong (likely missing-dependency) vs. the hard cases (removed API, conflicts).
  A single aggregate number hides exactly the interesting story; report per category.
- **Grounded vs. model-proposed:** of the accepted fixes, how many were
  `metadata_grounded` vs. `llm_proposed`, and the confidence distribution
  (high / medium / medium-high / low). This is the transparency contribution,
  measured.
- **Cap-limited failures:** how many "failures" were actually `MAX_STEPS` exhaustion
  vs. genuine inability — so success isn't under-stated.
- **Cross-file interaction (the repo-scale finding):** how often fixing one file in a
  repo made another file pass "for free" (empty-trace pass after an earlier fix), and
  any cases where a shared-environment fix for one file broke another. This is a
  genuinely novel repo-scale observation — capture it as first-class data, not an
  anecdote.

Keep everything reproducible: record the model used, `MAX_STEPS`, the dataset
version, and the date, alongside the results.

---

## 4. Constraints & honest cautions

- **Cost is real.** Each notebook may trigger several LLM calls; hundreds of repos is
  real money. Hence the pilot-first, sample, checkpoint, and cost-logging
  requirements above. Do not launch a full run without the user's go-ahead on cost.
- **Not every notebook is in scope.** Many real notebooks need missing data files,
  credentials, GPUs, network services, or run for hours — these are *out of scope*
  (per the vision doc's selection criteria) and must be recorded honestly as
  "skipped / not runnable", **not** counted as repair failures.
- **This task adds no repair capability.** If a result looks wrong because the engine
  mis-repairs something, record it as a finding — do not "fix" the engine inside the
  evaluation harness. Engine changes are separate, deliberate tasks.
- `repair_tool/` stays untouched except for the two prerequisite items in §0 (making
  `MAX_STEPS` configurable is the only engine-facing change, and it's a parameter, not
  new logic). Keep all existing tests green.

## 5. Out of scope

- More agents, the knowledge-graph tool (`kg_grounded` stays reserved), HTML/UI.
- The RQ4 human trust study — separate; this task produces the *effectiveness* and
  *transparency-statistics* half of the evaluation, not the developer study.
- Changing the repair engine's behaviour (beyond `MAX_STEPS` being configurable).

## 6. Definition of done

- The 2023 GigaScience db is in hand (gitignored); `dataset/failures_2023.csv` exists
  with per-notebook taxonomy labels and per-category counts reported.
- `MAX_STEPS` is configurable end to end; `fresh=True` is enforced by the harness.
- `evaluation/run_eval.py` runs repos end to end, is resumable, cost-logged,
  sample-able, and cleans up after itself.
- A **pilot run (~20–50 repos)** has produced real result rows and a first results
  table, with per-category effectiveness, grounded-vs-proposed stats, cap-limited
  count, and the cross-file-interaction count.
- `analyze_results.py` reproduces those numbers from the result rows.
- A short `EVALUATION_PILOT_SUMMARY.md` written: what was run, the numbers, surprises,
  cost per repo, and a recommendation on full-run size — same style as prior
  summaries.

## 7. Why this is the right (and overdue) next step

The system is built; a thesis needs *measured results on real data*, not more
features. This task turns "I built a transparent repo-scale dependency-repair system"
into "…and here is how well it works, per failure category, with this much of its
output grounded vs. model-proposed, including a novel finding about cross-file
dependency interactions." That is the thesis. Multi-agent and the KG tool remain
optional stretch goals, to attempt only if time remains after there are results.
