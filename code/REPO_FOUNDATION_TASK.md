# Task: Repo-Level Foundation — take a repository, set it up, run it, collect failures

> **Read `NEW_DIRECTION.md` first.** The thesis scope has expanded from single files
> to whole repositories, with hybrid (classical + LLM) repair and an agentic design
> to come. This task builds ONLY the foundation every version of that needs. It does
> **not** build repo-scale repair, the classical+LLM hybrid, or the agents — those
> wait until the supervisor confirms the priority and the novel contribution.

## Project context (read first)

We are building a tool for **AI-Driven Transparent Repair of Software Dependency
Configurations**. Phases 1–5 + Notebook Support already work on **single files**
(`.py` and `.ipynb`): run → diagnose → propose (rule-based install, or LLM code-vs-
environment fix) → verify by re-running → explain. All of that lives in the
`repair_tool/` package and is fully tested.

The supervisor now requires the tool to operate on **whole repositories** ("like a
real tool, solving the real problem"), not isolated files. This task adds the
repository *front end*: take a repo, set up one shared environment for it, run its
files, and collect the failures across the project. The existing single-file engine
is reused as-is underneath.

## Why this is safe to build before the details are settled

Every planned version of the tool (fixed loop, classical+LLM hybrid, or fully
agentic) needs the same base: **take a repo → set up its environment → run its files
→ collect what failed.** This task builds exactly that base and stops there, so no
work here is wasted regardless of how the agentic/hybrid design is decided later.

## Goal

Given a local repository (a folder), the tool should:
1. find the repo's dependency declarations,
2. set up one shared isolated environment for the whole repo,
3. discover its runnable files (`.py` scripts and `.ipynb` notebooks),
4. run each in that shared environment (reusing the existing runner),
5. collect a per-file result (ran / failed + the diagnosis), and produce a
   repo-level summary.

**Do not repair anything at repo scale in this task** — just run and diagnose. Repair
orchestration comes later.

## New module and what it does

| File | Responsibility |
|---|---|
| `repair_tool/repo.py` (new) | `analyze_repo(repo_path)`: find dependency files, build one shared venv, discover runnable files, run each, return a `RepoResult` (per-file `RunResult` + `Diagnosis`, plus a summary) |

Reuse, unchanged: `runner.run_project`, `notebook.run_notebook`, `diagnose`,
`venv_manager`, `pypi`. If something needs changing in them, prefer extending over
rewriting, and keep single-file behaviour identical.

## Design guidance

- **Input:** a path to a local repo folder. (Cloning from a git URL can be a thin
  wrapper added later; keep this task to a local folder.)
- **Dependency declarations:** look for, in priority order, `requirements.txt`,
  `pyproject.toml`, `setup.py`, `environment.yml`, `Pipfile`. Record which were
  found. If several exist, document the order used; if none, note it and continue
  (the repo may still run, or may fail with a missing dependency — that's fine, it's
  data).
- **One shared environment:** create a single isolated venv for the whole repo (reuse
  `venv_manager`) and install the found declarations into it — not a fresh venv per
  file. This mirrors how a real project is set up.
- **Discover runnable files:** find `.py` and `.ipynb` files. Exclude obvious
  non-entrypoints where reasonable (tests, `setup.py` itself, hidden dirs) but keep
  this simple; over-filtering is worse than running a few extra files.
- **Run and collect:** run each discovered file in the shared venv via the existing
  runner, capture its `RunResult`, and classify failures with `diagnose`. Collect
  everything into a `RepoResult`.
- **"Repo status" must be configurable — do not hard-code it.** Whether a repo counts
  as "passing" (all files run? a threshold? a designated entrypoint?) is a decision
  the supervisor may set. So report **per-file results** and make the pass rule a
  parameter/setting (default: report counts, e.g. "7/10 files ran"), rather than
  baking in one definition. This is deliberate — it must be trivial to change later.

## Constraints

- Do not break single-file behaviour; all existing tests stay green.
- Isolation: one shared venv per repo; never install into the host interpreter.
- Never mutate the repo's original files (work read-only / on a copy, as elsewhere).
- Keep the never-raises / timeout contract: a file that hangs or errors is a recorded
  result, not a crash of the whole repo run. One bad file must not abort the others.
- **Out of scope (do not build here):** repo-scale repair, the classical+LLM hybrid,
  agentic orchestration, dataset evaluation. This task only *runs and diagnoses* a
  repo.

## Required behaviour / interface

```python
from repair_tool.repo import analyze_repo

result = analyze_repo("path/to/some/repo")
# a shared environment was set up and every runnable file was executed in it:
assert result.env_setup_ok in (True, False)
assert len(result.files) >= 1
# each file has a RunResult and (if it failed) a Diagnosis:
for f in result.files:
    assert hasattr(f, "run_result")
    # f.diagnosis is set when f.run_result.ok is False
# a repo-level summary is available, and the pass rule is configurable, not fixed:
assert result.summary  # e.g. counts of ran/failed, failures grouped by diagnosis kind
```

## Tests

- Create a small fixture repo under `tests/fixtures/` (a folder with a
  `requirements.txt` and a couple of `.py`/`.ipynb` files, at least one that runs and
  at least one that fails on a dependency error).
- Offline where possible; guard the parts that need real installs/network, consistent
  with the existing test split (`SKIP_NETWORK_TESTS`).
- Assert: dependency files are found; one shared venv is created; every runnable file
  is executed in it; per-file results and diagnoses are collected; a passing file and
  a failing file are both reported correctly; one failing file does not abort the run.
- Do not weaken any existing single-file tests.

## Definition of done

- `analyze_repo(path)` sets up one shared environment, runs all runnable files in it,
  and returns per-file results + diagnoses + a summary.
- The pass/fail rule for the whole repo is configurable, not hard-coded.
- Single-file behaviour and all prior tests are unchanged.
- One bad file is recorded, not fatal; timeouts/crashes are handled per the existing
  contract.
- The repo's original files are never modified.

## Out of scope (explicitly, pending supervisor confirmation)

- Repo-scale *repair* (fixing the whole project, not just diagnosing it).
- The classical (graph-based) + LLM **hybrid** repair strategy.
- **Agentic** orchestration (multiple agents).
- Dataset evaluation, taxonomy application, Phase 4.
- The exact **novel contribution** and the build priority — these are being confirmed
  with the supervisor; this task deliberately builds only the foundation common to
  all of them.
