# Next Task: Two-Axis Provenance + the First Transparency Report

**Read `AGENTIC_DIRECTION_AND_FIRST_TASK.md` and `AGENT_FIRST_TASK_SUMMARY.md`
first.** The single tool-calling agent (`agent.py`) now works and records a
provenance-tagged decision trace. This task does **not** add more agents. It
sharpens the thing that is actually the thesis's contribution — the trust/provenance
model — and produces the first human-readable transparency report from the trace.

## Why this task (the motivation — read it)

Running `02_numpy_float.py` exposed a real weakness in the current single-tag
provenance scheme. The agent installed numpy (a genuinely well-founded action —
numpy demonstrably exists on PyPI), but because the `verify` immediately after it
failed (numpy then revealed the `np.float` error), that install was left tagged
`llm_unverified` — i.e. labelled as if the model had merely guessed. That is
misleading: the action *was* grounded in a reliable source; it simply hadn't made
the whole project run *yet*.

The root cause: the current scheme collapses **two independent questions** into one
tag. They must be separated:

- **Grounding (source):** was this action backed by a reliable, deterministic tool
  (PyPI metadata / resolver / — later — a knowledge-graph tool), or is it only the
  LLM's own suggestion? *This is independent of whether the project ends up running.*
- **Verification (outcome):** did re-running the project confirm it works after this
  action?

An action can be any combination of the two. Representing both — and deriving a
calibrated confidence from the pair — is a richer, more honest transparency model
than any existing dependency-repair tool, and it is the core of the contribution.

## Part A — the two-axis provenance model

Replace the single `provenance` string on each `TraceStep` with **two** fields:

- `grounding`: one of
  - `metadata_grounded` — the action is backed by a deterministic/classical tool
    result (e.g. an install of a package a `lookup_package` call confirmed exists; a
    version constraint confirmed against PyPI metadata). *(Later, a knowledge-graph
    tool's output also counts here — leave room for a `kg_grounded` value, but do not
    build the KG tool now.)*
  - `llm_proposed` — the action comes only from the LLM's own reasoning, with no
    classical-tool backing (e.g. a code rewrite from the model's knowledge).
  - `deterministic` — for the observation tools themselves (`run_target`,
    `diagnose_error`, `verify`, `lookup_package`): they are facts, not proposals.
- `verification`: one of
  - `verified` — a `verify` call *after* this action returned `ok=True`.
  - `unverified` — no passing `verify` has confirmed this action (either none yet, or
    the `verify` after it failed).
  - `n/a` — for observation tools (nothing to verify).

So the numpy install now reads honestly as `grounding=metadata_grounded`,
`verification=unverified` — well-founded, but it didn't make the project run on its
own. A later `edit_code` that a passing `verify` confirms reads
`grounding=llm_proposed`, `verification=verified` — it worked, but it was the model's
idea, so a reviewer may want to glance at it. A pure guess that failed is
`llm_proposed` + `unverified`.

### Derived confidence

From the pair, derive a `confidence` for each *fix* action (install / edit), for the
report and for later evaluation:

| grounding | verification | confidence | meaning |
|---|---|---|---|
| metadata_grounded | verified | **high** | grounded in a reliable tool AND re-running confirmed it |
| metadata_grounded | unverified | **medium** | well-founded, but project not (yet) confirmed working from this alone |
| llm_proposed | verified | **medium-high** | it works, but only the model proposed it — worth a human glance |
| llm_proposed | unverified | **low** | a model guess nothing has confirmed — flag for review |

Keep the upgrade-on-later-verify behaviour already built, but apply it to the
`verification` axis only; `grounding` is set once when the action is taken and never
changes (an action's source doesn't change based on outcome).

## Part B — the first transparency report

Add `repair_tool/report.py` with `build_report(agent_result) -> str` (and a
`--report` flag on `agent.py`'s CLI) that turns an `AgentResult`'s trace into a
clear, human-readable report. It must show, for the whole repair:

- the target and the final outcome (fixed / not fixed);
- for each *fix action* taken, in order: what was done, the reason, its `grounding`,
  its `verification`, and the derived `confidence`;
- the *rejected / superseded* actions too (e.g. an install that was necessary but
  didn't finish the job, or a failed edit) — the report must not hide the path, only
  summarise it honestly;
- a short overall line: how many actions were grounded vs. model-proposed, and the
  confidence of the accepted fix.

Plain text / markdown is fine — **no UI, no HTML in this task.** This is the
first concrete version of the Vision Doc §7 transparency report; richer rendering
comes later.

Example shape (illustrative, not a required format):

```
Repair report — broken_examples/02_numpy_float.py
Outcome: FIXED

Step 1  install numpy
        reason:       numpy was not installed (ModuleNotFoundError)
        grounding:    metadata_grounded (numpy confirmed on PyPI)
        verification: unverified (project still failed afterward — revealed np.float)
        confidence:   medium

Step 2  edit code: np.float -> float
        reason:       np.float was removed in numpy >= 1.24
        grounding:    llm_proposed
        verification: verified (project ran successfully)
        confidence:   medium-high

Summary: 2 fix actions (1 grounded, 1 model-proposed). Project now runs.
         Accepted fix confidence: medium-high — one change was a model proposal
         confirmed only by re-running; a reviewer may wish to check it.
```

## Constraints

- Build on `agent.py`/`agent_tools.py`; do **not** add more agents, no KG tool, no
  repo-scale work. Leave a `kg_grounded` value defined for the future, unused now.
- The fixed loop (`loop.py`) stays untouched; keep all existing tests green.
- Do not change the wrapped engine functions' logic.

## Tests

- Offline (mocked LLM, scripted tool sequences): assert each step gets the correct
  `grounding` and `verification`, and that the numpy-style case yields
  `metadata_grounded` + `unverified` for the install and `llm_proposed` + `verified`
  for the confirming edit; assert the derived `confidence` for each pair in the table.
- `build_report` produces a report that names every fix action with its grounding,
  verification, and confidence, and does not omit a necessary-but-unconfirmed action.
- One guarded real end-to-end: `agent_repair("broken_examples/02_numpy_float.py")`
  then `build_report(...)` — the report shows the two-step story with honest tags.
- Do not weaken any existing test.

## Definition of done

- `TraceStep` carries separate `grounding` and `verification` fields; the old single
  `provenance` tag is gone (or derived from the two for back-compat).
- The numpy install reads `metadata_grounded` + `unverified`, not `llm_unverified`.
- `confidence` is derived per the table and recorded.
- `build_report` renders an honest, human-readable report (text/markdown) including
  rejected/superseded actions.
- All prior tests green; new tests cover the two axes, the confidence mapping, and
  the report.
- A short `PROVENANCE_REPORT_SUMMARY.md` written (what changed, decisions, how
  verified) — same style as prior summaries.

## Out of scope (later)

- More agents (diagnosis/repair/verification split).
- Any knowledge-graph tool (PyEGo/ReadPyE) — `kg_grounded` is reserved, not built.
- Repo-scale agentic repair.
- HTML/interactive rendering of the report.
- Dataset evaluation, taxonomy, Phase 4, RQ4 study.

## Why this is the right next step (not multi-agent)

The multi-agent architecture is not the thesis's novelty — the transparent,
calibrated, provenance-tagged trust model is. The numpy case showed that model has an
unresolved design question; resolving it (two axes) and producing the first real
transparency report advances the actual contribution. Adding more agents is more
engineering on a pattern already proven, and can wait until the trust model is sound.
