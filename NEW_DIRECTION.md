# New Direction — Requirements Update

**Purpose:** orient anyone (including the coding agent) to a scope change in this
thesis after a supervisor meeting. Read this before picking up `REPO_FOUNDATION_TASK.md`
or any later task. Some items here are **confirmed**; others are **pending** the
supervisor's written confirmation and are marked as such — do not build the pending
parts yet.

## Where the project was

A working, tested tool that repairs **single files** (`.py` and `.ipynb`):
run → diagnose → propose (rule-based install, or LLM code-vs-environment fix) →
verify by re-running → keep the winner → explain. This is the `repair_tool/` package
(Phases 1–5 + Notebook Support), 118 tests passing.

## What the supervisor now wants (from the meeting)

1. **Repo-level scope (confirmed).** The tool must operate on **whole repositories**,
   like a real tool solving the real problem — not isolated single files. Real
   projects are repos (many files, shared dependencies), so single-file repair does
   not address the actual reproducibility problem.
2. **`AttributeError` is in scope (confirmed).** For the failure taxonomy, removed/
   changed-API failures that surface as `AttributeError` are to be treated as
   dependency-related (handled, not excluded).
3. **Hybrid classical + LLM, for efficiency (confirmed in direction).** The strongest
   approach is not "LLM for everything." It combines **classical, graph-based
   dependency tools** (fast, deterministic — the pre-LLM approach) with the **LLM**
   for the harder cases, to be faster and better than either alone. Practically:
   cheap/fast/deterministic methods first; LLM only where they fall short.
4. **Agentic design (confirmed in direction).** Move from one fixed pipeline to an
   **agentic** system — different agents doing different jobs (e.g. diagnosis,
   repair, verification, orchestration) and coordinating across a repo.
5. **A novel contribution / advance on the state of the art (confirmed as a goal;
   the exact contribution is PENDING).** The thesis should contribute something new,
   not just apply existing techniques. The precise novel contribution — transparency
   at repo scale, the hybrid efficiency, the agentic architecture, or the combination
   — is being pinned down with the supervisor and is **not yet fixed**.
6. **Model API key (confirmed; details pending).** The supervisor will provide an API
   key. The exact provider/model is to be confirmed; `llm.py` may need adjusting if
   it is not OpenAI.
7. **An existing dependency tool was named (pending).** The supervisor referenced an
   existing (likely classical/graph-based) Python dependency tool to study and
   possibly build on / compare against. Its name needs to be confirmed, then read.

## What carries over (not wasted)

The entire single-file **repair engine survives** and becomes the set of tools the
higher layers use:
- `runner` / `notebook` — running files and capturing errors,
- `diagnose` — classifying failures,
- `pypi`, `venv_manager` — package facts and isolation,
- `apply`, `repair`, `llm` — proposing and applying fixes,
- the core principles — **verify by re-running, keep the winner, transparency** —
  apply unchanged at repo scale.

The single-file work is the **foundation**, not a detour: the repo/hybrid/agentic
layers sit on top of it.

## What is new (the layers to add, in rough order)

1. **Repo foundation** — take a repo, set up one shared environment, run its files,
   collect failures. *(This is the task to start now: `REPO_FOUNDATION_TASK.md`.)*
2. **Repo-scale repair** — fix the whole project, not one file. *(Pending.)*
3. **Hybrid strategy** — classical/graph-based resolution first, LLM for the rest.
   *(Pending; depends on which classical tool.)*
4. **Agentic orchestration** — specialized agents replace the fixed loop. *(Pending.)*

## What is on hold until the supervisor confirms

- The exact **novel contribution** and the **build priority/order** (items 5).
- Repo-scale repair, the **hybrid** design, and the **agentic** architecture
  (items 2–4 above) — do not build these until priority and the classical tool are
  confirmed.
- The **model/key** swap (item 6) and studying the **named tool** (item 7).
- Previously-queued work now waiting behind the re-scope: the dependency-failure
  **taxonomy** application, the **2023 dataset** download/extraction, **Phase 4**, and
  the **RQ4** study.

## What to do now

Build **only** `REPO_FOUNDATION_TASK.md` — the repo input/setup/run/diagnose
foundation. It is needed under every possible version of the new direction, so it is
safe to build before the pending items are resolved. Stop before repo-scale repair,
the hybrid, and the agents.

## Honest caveat

This summary is based on a verbal meeting relayed second-hand. Treat the **confirmed**
items as direction and the **pending** items as genuinely open until the supervisor
confirms them in writing. The safe foundation task does not depend on any pending
item, which is exactly why it is the right thing to start.
