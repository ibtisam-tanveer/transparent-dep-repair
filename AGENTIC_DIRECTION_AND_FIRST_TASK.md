# Agentic Direction & First Build Task — for the coding agent

**Read this whole document before writing any code.** It explains (1) where the
project is, (2) the approach we are now building toward, (3) how it is staged so it
stays achievable, and (4) the single concrete task to build *first*. Later tasks
build on this one; do not jump ahead to them.

---

## 1. Context — what exists today

Thesis: **AI-Driven Transparent Repair of Software Dependency Configurations**
(MSc, Muhammad Ibtisam Tanveer, supervised by Dr. Sheeba Samuel).

A working, tested repair **engine** already exists in the `repair_tool/` package
(149 passing tests). Its pieces:

- `runner.py` — runs a `.py` file, returns `RunResult(ok, returncode, stdout, stderr)`; never crashes.
- `notebook.py` — same for `.ipynb` (runs in the target venv's kernel).
- `diagnose.py` — classifies an error into 5 kinds (`missing_module`, `import_name`, `module_attribute_removed`, `object_attribute_error`, `unknown`).
- `pypi.py` — real PyPI facts: package existence, versions, name resolution.
- `venv_manager.py` — one isolated venv per target.
- `apply.py` — applies a fix (install, or code edit on a working copy); never mutates the original.
- `repair.py` / `llm.py` / `loop.py` — the current fixed loop: rule-based install for the easy case, LLM (code-fix vs. environment-fix, verified by re-running, keep the winner, record `strategy_won`) for the hard cases.
- `repo.py` — run-and-diagnose a whole repository (one shared venv, all files). **No repair at repo scale yet.**

**Everything above is the foundation and must keep working.** The new direction
sits *on top* of it; the existing modules become **tools that agents call.**

---

## 2. The approach we are building toward

A **transparent, neuro-symbolic, multi-agent, hybrid** system that repairs
dependency problems in whole repositories (especially notebooks), where:

- **Agentic** — an LLM agent *decides what to do* and calls tools, instead of a
  fixed hard-coded loop.
- **Hybrid / neuro-symbolic** — the agent uses **reliable classical tools** (PyPI
  metadata + a resolver now; optionally a knowledge-graph tool like PyEGo/ReadPyE
  later) as its source of *facts*, and the **LLM** only for the reasoning the
  classical tools can't do. "Neuro" = the LLM; "symbolic" = the deterministic tools.
- **Verified** — nothing is accepted unless re-running (or a solver/metadata check)
  proves it. Principle (from the Schwarz paper): **the model owns search, the
  checker owns authority.** The LLM may *propose* anything; only a deterministic
  check may *approve* it.
- **Transparent** — every repair decision is recorded with its reason, its source,
  and *how it was verified*. This is the thesis's core contribution (see §3).

### The honest novelty framing (build toward this)

The multi-agent / tool-using / solver architecture is **not** itself novel — it
exists in prior work (RepairAgent, CLAUSE, Schwarz). So the contribution is **not**
"a multi-agent system." The contribution is:

1. **Application:** transparent dependency repair of *scientific notebooks at
   repository scale* — unsolved; prior agentic work targets test-bearing repos and
   optimises only success rate.
2. **Transparency as a first-class output:** a complete, auditable **repair trace**
   where every decision carries a **provenance tag** and a **calibrated confidence**
   based on *how it was established*:
   - `solver_proven` / `metadata_verified` — a deterministic tool confirmed it (highest trust),
   - `execution_verified` — re-running the project confirmed it,
   - `kg_grounded` — grounded in a knowledge-graph/classical tool's output,
   - `llm_unverified` — the LLM suggested it and nothing confirmed it (lowest trust, flagged for review).

   No prior dependency-repair tool produces this. The multi-agent machinery is the
   **vehicle** that generates the rich trace; the trace + its evaluation is the
   **contribution**. Build so that this trace is captured from the start, not
   bolted on later.

---

## 3. Staging — keep it achievable (important)

This is an MSc with limited time. Do **not** build the full six-agent system. Stage it:

- **Minimum viable:** start with **one** tool-calling agent, then grow to **three**
  (diagnosis, repair, verification) — not six.
- **Classical tool:** start with the **existing** classical pieces (`pypi.py` +
  resolver + execution checks) as the "symbolic" half. This already makes it hybrid.
  Integrating a real published KG tool (PyEGo/ReadPyE) is a **later, optional** step
  (it needs Neo4j + old Python; treat it as a stretch goal, not a dependency).
- **Scale:** prove the agent pattern on **single files** first (where it's easy to
  test), then extend to repo scale.
- Every step must be independently testable and must not break the existing engine
  or its tests. Same discipline as the earlier phases.

---

## 4. THE FIRST TASK (build only this)

**Goal:** turn the existing engine into **agent-callable tools**, and replace the
loop's *decision-making* with a **single LLM agent** that chooses which tools to
call — producing a recorded **decision trace**. This proves the "agent calls tools,
every action is logged with provenance" pattern on the ground you already have.

### 4.1 Expose the engine as tools

Create `repair_tool/agent_tools.py` wrapping the existing functions as a clean,
documented tool interface the LLM can call (name, description, typed inputs/outputs,
JSON-serialisable results). At minimum:

| Tool | Wraps | Purpose |
|---|---|---|
| `run_target` | `runner.run_project` / `notebook.run_notebook` | run the project, return pass/fail + error |
| `diagnose_error` | `diagnose.diagnose_result` | classify the captured error |
| `lookup_package` | `pypi.package_exists` / `latest_version` / `resolve_package_name` | **classical/symbolic** fact lookup |
| `install_package` | `apply.apply` (install path) | install a package into the venv |
| `edit_code` | `apply.apply_code_edit` | apply a code fix to a working copy |
| `verify` | re-run via `run_target` | the deterministic "checker" |

Do **not** change the wrapped functions' own logic — wrap, don't rewrite. If a
wrapper needs something the function doesn't expose, extend the function minimally
and keep its existing tests green.

### 4.2 A single tool-calling agent

Create `repair_tool/agent.py` with `agent_repair(target_path) -> AgentResult` that:

- Uses the LLM (OpenAI, same key handling as `llm.py`) in a **tool-calling loop**
  (ReAct-style): the model sees the situation, picks a tool, sees the result, picks
  the next, until the target runs or a step/`MAX_STEPS` cap is hit.
- Only the model *proposes*; a fix counts as done only when the `verify` tool
  (re-running) confirms it — **model owns search, checker owns authority.**
- Records a **decision trace**: an ordered list of steps, each with
  `{thought, tool_called, tool_input, tool_result, provenance}` where `provenance`
  is one of the tags in §2 (e.g. an install confirmed by `lookup_package` +
  `verify` is `metadata_verified` + `execution_verified`; an LLM code edit confirmed
  only by re-running is `execution_verified`; an unconfirmed guess is `llm_unverified`).
- Returns `AgentResult(target, fixed, trace, final_strategy)`. The trace is the
  **seed of the transparency report** — capture it now.

### 4.3 Constraints

- **Do not remove or break the existing fixed loop** (`loop.py`) — the agent is a
  *new, parallel* entry point (`agent.py`), so the deterministic baseline still
  exists for comparison (this matters for evaluation / RQ3). Keep all 149 tests green.
- Isolation, never-mutate-the-original, never-raises, timeout caps — all still hold.
- One agent only in this task. No multi-agent, no PyEGo/ReadPyE, no repo-scale
  orchestration, no knowledge graph. Those are later tasks.
- Verify by execution, always. No fix is "fixed" unless `verify` passed.

### 4.4 Tests

- Offline, **mocked LLM**: given a scripted sequence of tool choices, assert the
  agent calls the right tools, stops when `verify` passes, records a trace with
  correct provenance tags, and respects `MAX_STEPS`. (Mocking is required — real LLM
  calls are non-deterministic and cost money.)
- One guarded **real** end-to-end test (skipped without a key / with
  `SKIP_NETWORK_TESTS=1`): `agent_repair("broken_examples/02_numpy_float.py")` ends
  `fixed=True` with a non-empty trace.
- Do not weaken any existing test.

### 4.5 Definition of done

- The engine is callable as documented tools (`agent_tools.py`).
- `agent_repair()` fixes at least the missing-package case and one removed-API case
  (e.g. `01` and `02`) by *choosing* tools, verified by re-running.
- A decision trace is recorded for every run, each step provenance-tagged.
- The existing fixed loop and all 149 tests are unchanged and green.
- A short `AGENT_FIRST_TASK_SUMMARY.md` written, same style as prior phase summaries
  (what was built, decisions, any real bug found, how verified).

### 4.6 Explicitly out of scope (later tasks, not now)

- Multiple agents (diagnosis / repair / verification split) — next task.
- Integrating PyEGo/ReadPyE or any knowledge graph — stretch goal.
- Repo-scale agentic repair — after single-file agent works.
- The full human-readable transparency report / UI — the trace is captured now;
  rendering it is later.
- Dataset evaluation, taxonomy, Phase 4.

---

## 5. Why this first task is the right first step

It converts the risky, vague goal ("build a multi-agent hybrid system") into one
small, testable change on ground you already control: the LLM stops being called in
a fixed place and starts *choosing* tools, and every choice is logged with its
evidence. Once this works, adding a second and third agent, a classical KG tool, and
repo-scale orchestration are each incremental steps on a proven pattern — not a leap.
