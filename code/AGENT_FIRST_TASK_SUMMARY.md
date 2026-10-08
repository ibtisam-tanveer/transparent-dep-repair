# Agent First Task Summary — one tool-calling agent, provenance-tagged

Status: **done**. See `AGENTIC_DIRECTION_AND_FIRST_TASK.md` (the staged
agentic build plan this is the first step of) and `NEW_DIRECTION.md` (the
underlying scope pivot, most of which is still pending supervisor
confirmation).

## What was asked

Turn the existing engine into agent-callable tools, and replace `loop.py`'s
fixed decision order (propose → apply → verify) with a **single** LLM agent
that *chooses* which tool to call next, producing a recorded, provenance-
tagged decision trace. Explicitly **not** asked for in this task: multiple
agents, a knowledge-graph tool, repo-scale orchestration, or a rendered
transparency report — all deferred to later, staged tasks (section 4.6).

## What was built

| File | Purpose |
|---|---|
| `repair_tool/agent_tools.py` | `run_target`, `diagnose_error`, `lookup_package`, `install_package`, `edit_code`, `verify` — plain dict in/out wrappers around the unchanged Phase 1-5 functions, plus `TOOL_SPECS` (the OpenAI `tools=[...]` schema) and `build_dispatch(workspace_path, python_exe)` (binds one session's venv/workspace, returns `{tool_name: callable}`) |
| `repair_tool/agent.py` | `agent_repair(path, max_steps=10) -> AgentResult`, `TraceStep`, `AgentResult`, plus a CLI |
| `tests/test_agent_tools.py` | 13 tests: each wrapper's delegation, dispatch binding, `TOOL_SPECS`/dispatch consistency |
| `tests/test_agent.py` | 7 tests: the tool-calling loop's control flow (offline, scripted/mocked LLM) + one real end-to-end run |

### The tool interface — wrap, don't rewrite

Every function in `agent_tools.py` delegates to an existing module
unchanged: `run_target`/`verify` call `runner.run_project` (which already
dispatches `.ipynb` to `notebook.run_notebook`); `diagnose_error` calls
`diagnose.diagnose_result`; `lookup_package` calls
`pypi.resolve_package_name`/`latest_version`; `install_package` calls
`apply.apply` with an install `Proposal`; `edit_code` calls
`apply.apply_code_edit`. No wrapped function's own logic changed. The only
new code is the JSON-in/JSON-out calling convention and `TOOL_SPECS`.

`install_package`/`edit_code`/`run_target`/`verify` take only the
arguments the *model* should decide (a package spec, a list of edits,
nothing) — `python_exe` and `workspace_path` are session-fixed and bound
once via `build_dispatch()`'s closures, not re-supplied by the model on
every call. This keeps the tool-calling schema small, which matters for
real tool-calling reliability, and mirrors how `loop.py` already treats
those two as fixed per-session state.

`diagnose_error` takes the *exact* dict `run_target`/`verify` just
returned (not a separate `stderr` argument) — the agent already has that
dict from the previous tool call, and it keeps the calling convention
anchored to what `diagnose.diagnose_result()` itself expects (a full
`RunResult`, not just its stderr).

### The agent — a ReAct-style loop, the checker always has final say

`agent_repair()` opens with a system prompt describing the tools and the
rule "call verify after every change; a fix is only accepted once it
returns ok=true," then loops: call the model with `tools=TOOL_SPECS`, run
whichever tool(s) it picked, feed the result back as a `tool` message,
repeat — up to `MAX_STEPS = 10` tool calls total, matching `loop.py`'s
`MAX_ATTEMPTS` role as a hard ceiling against infinite back-and-forth. The
model never gets to declare success on its own: the loop only returns
`fixed=True` when a `verify` tool call's own result has `ok=True` — an
install or an edit never counts on its own, exactly matching
`AGENTIC_DIRECTION_AND_FIRST_TASK.md`'s "model owns search, checker owns
authority" principle, and the same contract `loop.repair()` already
enforces by re-running after every attempt.

### Provenance tagging — precise about what was actually confirmed, and when

Each `TraceStep` gets one of:

- `metadata_verified` — a `lookup_package` call (a classical/deterministic
  PyPI fact).
- `execution_verified` — `run_target`, `diagnose_error`, or `verify` (all
  deterministic, derived from an actual run).
- `llm_unverified` — an `install_package` or `edit_code` call, the moment
  it's applied. Nothing has confirmed it yet; it is the model's proposal,
  not a fact.

When a later `verify` call succeeds, the *specific* `install_package`/
`edit_code` step it immediately followed is upgraded in place: to
`"metadata_verified + execution_verified"` if a `lookup_package` call
happened since the last `verify`, or plain `"execution_verified"`
otherwise — exactly the two combinations
`AGENTIC_DIRECTION_AND_FIRST_TASK.md` section 2 gives as examples. If
`verify` instead fails, the pending action is left at `llm_unverified`
permanently and the "lookup seen" flag resets, so a *later* action that
actually gets confirmed isn't credited with a lookup that belonged to a
different, abandoned attempt.

**A deliberate edge case, found while running `02_numpy_float.py` for
real**: the agent's first move was to install numpy (fixing the
`ModuleNotFoundError`), then `verify` revealed numpy's own
`AttributeError: module 'numpy' has no attribute 'float'` — a second,
different failure. That `verify` call reports `ok=False`, so the
`install_package` step is *not* upgraded, even though installing numpy was
a genuinely necessary part of the eventual fix. This is the correct,
literal reading of the provenance contract: the tag answers "did a
passing re-run directly confirm *this* action," not "did this action turn
out to matter" — the agent went on to diagnose the new error and fix it
with `edit_code`, and *that* step is the one upgraded once the next
`verify` finally passes. A looser design (crediting every action in a
successful run) would blur exactly the distinction the transparency
report exists to make.

## Design decisions and why

- **One parallel entry point, not a replacement.** `loop.py` is untouched
  — every one of its 149 pre-existing tests still passes unmodified. This
  keeps the deterministic fixed loop available as the baseline `NEW_DIRECTION.md`
  and `AGENTIC_DIRECTION_AND_FIRST_TASK.md` both call for (RQ3: does an
  agent choosing tools do better than a fixed pipeline?).
- **Classical tool first, by construction, not by convention.** The system
  prompt instructs the model to call `lookup_package` before
  `install_package`, but nothing in the code *enforces* that order — an
  agent that skips straight to `install_package` is still possible and
  still gets run, just without the `metadata_verified` tag. This is
  intentional: the thesis's claim is about what's *recorded*, not about
  constraining the model's choices more than a real agentic system would.
- **`MAX_STEPS` counts tool calls, not model turns.** A single model turn
  can request multiple tool calls; capping by trace length (not by the
  number of API requests) means the ceiling is really "at most 10 actions
  taken," which is the quantity that matters for cost and for bounding how
  long a stuck agent can spin.
- **The dataclasses stay plain, JSON-shaped dicts at the tool boundary.**
  `agent_tools.py` functions take/return plain `dict`s (via
  `dataclasses.asdict`), not the engine's own dataclasses
  (`RunResult`/`Diagnosis`) directly — an LLM tool-calling API needs
  JSON-serialisable arguments and results, and keeping that conversion in
  one place (not scattered through `agent.py`) keeps the wrapping honest
  ("wrap, don't rewrite").

## How each definition-of-done item was verified

- **The engine is callable as documented tools** — `tests/test_agent_tools.py`
  exercises every wrapper directly (offline, each at the seam it actually
  touches: `pypi`, `apply`, `runner`) plus `TOOL_SPECS`/`build_dispatch`
  consistency (every spec name has a dispatch entry and vice versa).
- **`agent_repair()` fixes the missing-package case and a removed-API case
  by choosing tools, verified by re-running** — confirmed for real (a live
  model, live PyPI, a live isolated venv):
  - `agent_repair("broken_examples/01_missing_package.py")` → `fixed=True`,
    5 steps (`run_target → diagnose_error → lookup_package →
    install_package → verify`), `final_strategy="install"`.
  - `agent_repair("broken_examples/02_numpy_float.py")` → `fixed=True`,
    7 steps across two diagnose/fix rounds (install numpy, re-diagnose the
    `AttributeError` that install exposed, `edit_code`, verify again),
    `final_strategy="code_edit"` — the agent reached, by choosing each step
    itself, the same two-layer result `loop.py`'s fixed pipeline reaches via
    two separate hard-coded code paths (Phase 3's install branch, then
    Phase 5's hard-case branch).
- **A decision trace is recorded for every run, each step provenance-tagged**
  — both real runs above produced a full trace with every step tagged;
  offline tests assert the exact tag (including the combined
  `"metadata_verified + execution_verified"` tag) for each tool.
- **The existing fixed loop and all 149 prior tests are unchanged and
  green** — `loop.py` was not edited; the full suite is 169/169 passing
  (149 prior + 20 new) both offline and fully online.
- **Mocked-LLM tests assert tool choice, stopping, provenance, and
  `MAX_STEPS`** — `tests/test_agent.py`'s offline suite scripts exact tool-
  call sequences (via a mocked `openai.OpenAI().chat.completions.create`)
  for: the install path, the code-edit path, a `MAX_STEPS` cap with
  `verify` never succeeding, the model stopping without any tool call, a
  missing API key, and an LLM call raising mid-loop — all degrade cleanly,
  never a crash.
- **One guarded real end-to-end test** —
  `TestAgentRepairNetwork.test_required_behaviour_real_agent_fixes_numpy_float`
  (skipped under `SKIP_NETWORK_TESTS=1` or without `OPENAI_API_KEY`, same
  convention as `test_llm.py`/`test_repo.py`) asserts
  `agent_repair("broken_examples/02_numpy_float.py")` ends `fixed=True`
  with a non-empty trace — run for real, passing, during this task.

## No new bugs found in the wrapped functions

Unlike every other phase so far, running this task's real end-to-end tests
surfaced no bug in the underlying engine — `run_project`, `diagnose_result`,
`apply`, `apply_code_edit`, and `venv_manager` all behaved exactly as their
own test suites already describe. The one real surprise was behavioral,
not a bug: an agent given only a bare `ModuleNotFoundError` for numpy has
no way to know in advance that fixing it will reveal a *second*, unrelated
failure — it has to actually try, see the new error, and adapt. The
provenance design above (recording that the first action was *not* the one
a passing verify confirmed) is a design response to that, not a fix for a
defect.

## Explicitly out of scope (per the task, unchanged)

Multiple agents (diagnosis/repair/verification split). Integrating
PyEGo/ReadPyE or any knowledge graph. Repo-scale agentic repair. The full
human-readable transparency report/UI (the trace is captured now;
rendering it is later). Dataset evaluation, the failure taxonomy, Phase 4.

## How to run / verify

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
SKIP_NETWORK_TESTS=1 python -m unittest tests.test_agent_tools tests.test_agent -v
# unset SKIP_NETWORK_TESTS, with a real OPENAI_API_KEY, to also run the real agent test

python -m repair_tool.agent broken_examples/01_missing_package.py   # FIXED (install)
python -m repair_tool.agent broken_examples/02_numpy_float.py       # FIXED (install, then code_edit)
```
