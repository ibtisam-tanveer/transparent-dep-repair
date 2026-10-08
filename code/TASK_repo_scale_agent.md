# Next Task: Repo-Scale Agentic Repair (evaluation-ready)

**Read `AGENTIC_DIRECTION_AND_FIRST_TASK.md`, `AGENT_FIRST_TASK_SUMMARY.md`, and
`PROVENANCE_REPORT_SUMMARY.md` first.** The single-file agent (`agent.py`) now
repairs one `.py`/`.ipynb` file, records a two-axis provenance trace (`grounding` ×
`verification` → `confidence`), and renders a transparency report (`report.py`).
This task lifts that from one file to a **whole repository**, and makes it
**evaluation-ready** so results won't be contaminated when it is later run across the
GigaScience dataset.

It does **not** add more agents, a knowledge-graph tool, or a UI.

## Why this task

Repository-level repair is the confirmed thesis scope (real projects — and the
GigaScience notebook corpus — are repositories, not lone files). The engine already
has `repo.py` that can *run and diagnose* a whole repo; the single-file *agent* can
*repair* one file with a trust trace. This task joins them: an agent that repairs a
**whole repository** and produces a repository-level transparency report.

## Part A — repo-scale agentic repair

Add `agent_repair_repo(repo_path, ...) -> RepoAgentResult` (new function, likely in
a new `repair_tool/agent_repo.py`, reusing `repo.py`'s discovery and `agent.py`'s
per-file agent). Behaviour:

- Use `repo.py` to set up **one shared environment** for the repo and discover its
  runnable files (`.py` + `.ipynb`), exactly as `repo.py` already does. Do **not**
  re-implement discovery or env setup — reuse it.
- For each runnable file that fails, run the existing single-file agent
  (`agent_repair`) **against that file, in the repo's shared environment** (pass the
  repo's shared `python_exe`/venv, not a fresh per-file venv — a repo is one
  environment shared across its files, which is the whole point of repo scope).
- Collect each file's `AgentResult` (fix outcome + trust trace) into a
  `RepoAgentResult` with: per-file results, and a repo-level summary (how many files
  now run, how many fixed, how many still failing).
- Keep the existing per-file trust model unchanged — each file's trace still carries
  `grounding`/`verification`/`confidence` exactly as now.

**Important ordering nuance (shared environment):** because all files share one
environment, a package the agent installs while fixing file A is then present for
file B. That is correct and realistic (it mirrors how a real repo works), but it
means file order can matter. Do the simple, honest thing: process files in a stable,
documented order (e.g. sorted path), and record in each file's result what was
already installed when it started. Do **not** try to be clever about dependency
ordering between files — note it as a limitation if it comes up.

## Part B — repository-level transparency report

Extend `report.py` with `build_repo_report(repo_agent_result) -> str`: a
repo-level transparency report that, in plain text/markdown:

- states the repo and the overall outcome (e.g. "7 of 10 files now run");
- lists each file with its outcome and its accepted fix's confidence;
- for fixed files, references the per-file fix actions (reason, grounding,
  verification, confidence) — reuse the existing per-file `build_report` for the
  detail;
- gives an honest overall summary: how many fixes were grounded vs. model-proposed,
  how many files remain broken and why (the honest "not fixed" cases, not hidden).

No UI, no HTML — plain text/markdown, same as the single-file report.

## Part C — make it evaluation-ready (the fresh-workspace fix — do NOT skip)

**Problem (found during the provenance task):** `venv_manager` caches and *reuses* a
venv and workspace copy per target path. On a second run of the same target, the
agent sees an **already-fixed** workspace and reports it as passing without doing
anything. For normal use that's fine, but for **evaluation across many repos/files
it silently corrupts results** — a file can look "already working" because a previous
run fixed it.

**Required:** provide a way to run from a **guaranteed-fresh** environment and
workspace, so each evaluation run starts from the project's real, unmodified state.
Concretely:

- Add a `fresh: bool = False` option (or equivalent) to `agent_repair` /
  `agent_repair_repo` that, when set, creates a brand-new isolated venv and a fresh
  workspace copy for that run, ignoring any cached one (and cleans up appropriately).
- The default behaviour (cached reuse) stays unchanged for interactive use; `fresh`
  is what the eventual evaluation harness will use.
- Document clearly (in code and in the summary) that **any dataset evaluation must
  use `fresh=True`**, so results reflect real repair from scratch, not leftover state.

This does not build the evaluation itself — it just makes the capability exist and be
correct, so the later evaluation isn't contaminated.

## Constraints

- Reuse `repo.py` (discovery, shared env) and `agent.py` (per-file agent) — wrap and
  orchestrate, do **not** duplicate or rewrite them.
- No new agents (still one per-file agent), no knowledge-graph tool, no UI.
- The fixed loop (`loop.py`), the single-file agent, and all existing tests stay
  green and unchanged in behaviour.
- Isolation, never-mutate-the-original, never-raises, step/attempt caps — all hold.
  One bad file must not abort the whole-repo run (reuse `repo.py`'s per-file
  try/except discipline).

## Tests

- Offline (mocked LLM, a small fixture repo with one passing file, one
  missing-package file, one removed-API file): assert the repo agent sets up one
  shared env, runs the per-file agent on each failing file, collects per-file results
  + a repo summary, and that one failing file doesn't abort the others.
- Assert the shared-environment behaviour: a package installed while fixing file A is
  present when file B runs (use a fixture where B imports what A installed).
- Assert `fresh=True` ignores a cached fixed workspace: fix a file once, then run
  again with `fresh=True` and confirm it starts from the broken original (the agent
  actually does the repair again), whereas the default reuses the cache.
- `build_repo_report` names every file, its outcome and confidence, and does not hide
  still-broken files.
- One guarded real end-to-end: a tiny real repo (or a local fixture repo) with a
  missing-package file and a removed-API file → `agent_repair_repo` fixes both,
  report shows the per-file trust detail.
- Do not weaken any existing test.

## Definition of done

- `agent_repair_repo(repo_path)` repairs a whole repository in one shared
  environment, reusing `repo.py` + the per-file agent, returning per-file results +ex
  a repo summary.
- `build_repo_report` renders an honest repository-level transparency report
  (including still-broken files).
- `fresh=True` guarantees a from-scratch run (fresh venv + workspace), and it is
  documented that evaluation must use it; default behaviour unchanged.
- Shared-environment behaviour is correct and tested (A's install is visible to B).
- All prior tests green; new tests cover repo orchestration, the shared env, the
  `fresh` option, and the repo report.
- A short `REPO_AGENT_SUMMARY.md` written (what was built, decisions, any real bug
  found, how verified) — same style as prior summaries.

## Out of scope (later / not now)

- More agents (diagnosis/repair/verification split).
- Any knowledge-graph tool (PyEGo/ReadPyE) — `kg_grounded` stays reserved.
- The dataset evaluation run itself, the failure taxonomy, Phase 4, RQ4 study.
- HTML/interactive rendering.

## Also (housekeeping, outside the code task)

Add a line to `PROJECT_STATUS.md` under known items: *"Evaluation runs must use the
fresh-environment mode (`fresh=True`) — the default cached venv/workspace reuse makes
a target look already-fixed on a second run and would contaminate evaluation
results."* (This is the measurement risk the provenance task surfaced; recording it
keeps the results chapter honest.)

## Why this is the right next step

Repo scale is the confirmed thesis scope and the shape of the real dataset, and the
`fresh` fix makes the whole thing evaluation-ready so later results are trustworthy.
After this, the project has enough *capability* to move from building to **measuring**
— which is what turns the system into a thesis. More agents and a knowledge-graph
tool remain optional stretch goals, to attempt only if time allows after there are
results.
