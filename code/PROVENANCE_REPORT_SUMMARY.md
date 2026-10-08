# Provenance & Report Summary — the two-axis trust model

Status: **done**. See `TASK_provenance_and_report.md` (this task's spec) and
`AGENT_FIRST_TASK_SUMMARY.md` (the single tool-calling agent this sharpens).

## What was asked

Replace the agent's single `provenance` tag with **two independent axes**
(`grounding`, `verification`), derive a `confidence` from the pair, and
build the first human-readable transparency report from the trace. No new
agents, no knowledge-graph tool, no repo-scale work — this task is entirely
about making the trust model the agent already produces more honest, not
about adding capability.

## Why this task existed — a real weakness, found by running it

`AGENT_FIRST_TASK_SUMMARY.md` documented, as an accepted design limitation,
that fixing `02_numpy_float.py` left the `install numpy` step tagged
`llm_unverified` forever, because the `verify` right after it failed (numpy
revealed `np.float`'s `AttributeError`, a second, different error). Re-
reading that with fresh eyes: `llm_unverified` is actively misleading there
— the install *was* grounded in a real, confirmed PyPI fact (`lookup_package`
had checked it); it just didn't single-handedly make the whole project
pass yet. Collapsing "where did this come from" and "did it work" into one
tag hides that distinction. This task's whole premise is that those are two
separate questions and the trace should say so.

## What was built

| File | Purpose |
|---|---|
| `repair_tool/agent.py` | `TraceStep.provenance` replaced by `TraceStep.grounding` + `TraceStep.verification` + `TraceStep.confidence`; `GROUNDING_METADATA`/`GROUNDING_LLM`/`GROUNDING_DETERMINISTIC` constants (`GROUNDING_KG` reserved, unused); `VERIFICATION_VERIFIED`/`VERIFICATION_UNVERIFIED`/`VERIFICATION_NA`; `_CONFIDENCE` table; `_package_base_name()` to match an install's package spec against a prior `lookup_package` confirmation |
| `repair_tool/report.py` (new) | `build_report(agent_result) -> str` — a plain-text transparency report naming every fix action, its reason, grounding, verification, and confidence, including actions that didn't finish the job or failed outright |
| `tests/test_agent.py` | Updated for the two axes; **new**: a test reproducing the exact numpy-style scenario from the task spec; **new**: a regression test for a real bug found while verifying this task (below) |
| `tests/test_report.py` (new) | 8 tests: every report behaviour the task's definition of done lists, offline, plus one real end-to-end report |

### Part A — grounding and verification, set and updated independently

`grounding` is decided once, when a tool call happens, from what actually
backed it, and **never changes afterward** (per the task's explicit rule
"an action's source doesn't change based on outcome"):

- `install_package` → `metadata_grounded` if its package spec's base name
  (`numpy<1.24` → `numpy`) matches a package a `lookup_package` call has
  confirmed exists *at any point in the run* (not just "since the last
  verify" — a confirmed PyPI fact doesn't go stale because an unrelated
  later check failed); `llm_proposed` otherwise (the model installed
  something it never checked).
- `edit_code` → always `llm_proposed`. Nothing in this task gives a code
  rewrite classical backing; a knowledge-graph tool doing that is exactly
  what `GROUNDING_KG` is reserved for, later.
- `run_target`, `diagnose_error`, `lookup_package`, `verify` → always
  `deterministic` — they observe or compute facts, they don't propose
  fixes, so grounding/verification/confidence don't apply to them the same
  way (`verification="n/a"`, `confidence=""`).

`verification` starts `unverified` the moment a fix action is applied, and
is upgraded to `verified` **in place, on that specific step**, only when a
later check (`verify`, or `run_target` — see the bug below) reports
`ok=true`. A fix action whose own check (`result["ok"]`) fails isn't even
eligible — it stays `unverified` permanently, which is correct: nothing
ever confirmed it.

**Multiple pending actions are handled, not just the single-action case
the task's example shows.** If the model applies two fix actions before
calling `verify` (not exercised in the given example, but not excluded by
it either), a passing `verify` upgrades *all* of them — each was active
when the run that passed happened, so each is equally entitled to credit.
A failing `verify` clears the pending list for the next attempt without
touching the `unverified` tags already there (nothing to undo).

### Confidence — exactly the table, nothing invented

```python
_CONFIDENCE = {
    (GROUNDING_METADATA, VERIFICATION_VERIFIED): "high",
    (GROUNDING_METADATA, VERIFICATION_UNVERIFIED): "medium",
    (GROUNDING_LLM, VERIFICATION_VERIFIED): "medium-high",
    (GROUNDING_LLM, VERIFICATION_UNVERIFIED): "low",
}
```

Recomputed whenever `verification` changes, so a step's `confidence` always
matches its current two tags. Observation tools get `confidence=""` (the
table has no entry for `deterministic`/`n/a` — there's nothing to be
confident *about* for a fact-lookup).

## Part B — the report, built only from what's already on the trace

`report.build_report()` adds no new trust logic — it reads
`grounding`/`verification`/`confidence`/`tool_result` off each `TraceStep`
and describes them in plain English. For each fix action, in order:

```
Step 1  install numpy
        reason:       (whatever the model said when it called this)
        grounding:    metadata_grounded ('numpy' confirmed by a classical lookup)
        verification: unverified (no passing re-run has confirmed this yet)
        confidence:   medium
```

Both unaccepted paths the task specifically called out are shown, not
hidden: an action whose own result was `ok: false` is reported as
"unverified (this action itself failed to apply)" rather than being
silently dropped from the list, and an action that applied fine but never
got confirmed (the numpy case) is reported as "unverified (no passing
re-run has confirmed this yet)" — both appear as real steps with real
tags, never pruned to only show the winning path. The summary line counts
grounded vs. model-proposed actions and names the accepted fix's
confidence, adding a review flag only when that accepted fix was
`llm_proposed` (a grounded, verified fix needs no extra scrutiny prompt).

A real run against `02_numpy_float.py` produces exactly the shape the task
doc's illustrative example describes:

```
Repair report — broken_examples/02_numpy_float.py
Outcome: FIXED

Step 1  install numpy
        reason:       (no reason recorded)
        grounding:    metadata_grounded ('numpy' confirmed by a classical lookup)
        verification: unverified (no passing re-run has confirmed this yet)
        confidence:   medium

Step 2  edit code: x = np.float(3.14) -> x = float(3.14)
        reason:       (no reason recorded)
        grounding:    llm_proposed (no classical tool backed this)
        verification: verified (the project ran successfully afterward)
        confidence:   medium-high

Summary: 2 fix actions (1 grounded, 1 model-proposed). Project now runs.
         Accepted fix confidence: medium-high — a model proposal confirmed
         only by re-running; a reviewer may wish to check it.
```

("(no reason recorded)" appears because the real model didn't attach
message text to those particular tool calls — reported honestly as
missing, not fabricated, per `_describe`/`report.py`'s own design: the
report only ever states what the trace actually contains.)

## A real bug found by running this task's own verification

Running the agent's real end-to-end test twice in a row against the same
file (once from `test_agent.py`, once initially from `test_report.py`
against the same path) surfaced a genuine gap: `venv_manager` caches one
venv and one workspace copy per target's absolute path, so the second run
started from an **already-fixed** workspace. The model, sensibly, called
only `run_target`, saw `ok=true`, and stopped — there was nothing left to
fix. But the agent's loop only treated an explicit `verify` tool call's
success as confirming the project runs, so this came back `fixed=False`
even though the target genuinely passed.

**The actual problem**: `verify` is *implemented* as nothing more than
calling `run_target` again on the same workspace (see `agent_tools.py`) —
they are the same deterministic check under two different names. Treating
only one of those names as authoritative was an arbitrary distinction the
code made, not a real one. **Fix**: any observation of `ok=true` from
either `run_target` or `verify` now confirms the project runs and ends the
loop with `fixed=True` — matching `loop.repair()`'s own existing "already
working project needs no attempts" behaviour for the fixed pipeline.
Verified with a new regression test
(`test_an_already_passing_target_is_confirmed_by_run_target_alone`) that
scripts exactly this: a single `run_target` call returning `ok=true`.

A second, smaller test-design issue surfaced from the same root cause:
`test_report.py`'s real end-to-end test originally also targeted
`02_numpy_float.py`, so when it ran after `test_agent.py`'s real test in
the same process, it hit the same already-fixed workspace and got "no fix
action was taken" instead of the two-step story it was meant to check.
Fixed by pointing it at `03_numpy_int_bool.py` instead (the same shape of
bug, a different file, so each guarded real test gets its own fresh venv)
— not a product bug, a reminder that these real end-to-end tests share
mutable state through `.repair_venvs/` and must pick distinct targets.

## Design decisions and why

- **Grounding persists for the whole run, not just "since the last
  verify."** The earlier single-tag design reset its "lookup seen" flag on
  every verify boundary; this task's semantics are different on purpose —
  a `lookup_package` confirmation is a fact about PyPI, not about this
  particular attempt, so it should still count even if the agent tries
  several different things before finally using it in an install.
- **Matching is by package base name, not string equality.** An agent that
  looks up `numpy` and then installs `numpy<1.24` should still get credit
  — `_package_base_name()` strips the version/marker suffix before
  comparing, the same shape of normalisation `pypi.py`'s alias map already
  relies on elsewhere in spirit (installable name vs. what's actually
  compared).
- **The report adds no logic of its own.** Every fact in it — grounding,
  verification, confidence, whether an action's own result was `ok` —
  already exists on the `TraceStep`; `report.py` only formats. This keeps
  the "first version of the transparency report" honestly a *rendering*
  step, matching the task's "no UI, no HTML — plain text is fine" scope.

## How each definition-of-done item was verified

- **`TraceStep` carries separate `grounding` and `verification` fields;
  the old single `provenance` tag is gone** — `agent.py`'s dataclass now
  has no `provenance` field at all; every reference (loop logic, CLI
  printer, both test files) was updated, nothing left importing the old name.
- **The numpy install reads `metadata_grounded` + `unverified`, not
  `llm_unverified`** — `test_numpy_style_case_a_grounded_install_that_doesnt_finish_the_job`
  reproduces the exact scenario and asserts exactly that pair, then asserts
  the confirming edit reads `llm_proposed` + `verified`; confirmed for real
  against a live model (see the sample report above).
- **`confidence` is derived per the table and recorded** — asserted for
  all four (grounding, verification) pairs across the offline agent tests
  (`high`, `medium`, `medium-high`, `low` all exercised).
- **`build_report` renders an honest report including
  rejected/superseded actions** — `tests/test_report.py` asserts a failed
  edit is shown with "failed to apply," not dropped, and a necessary-but-
  insufficient install is shown with its honest `unverified` tag rather
  than only showing the step that finally worked.
- **All prior tests green** — `loop.py` untouched; full suite is
  **179/179 passing** (169 prior + 10 new: 1 new agent test for the bug fix
  above, 9 in `test_report.py`), offline and fully online.
- **`PROVENANCE_REPORT_SUMMARY.md` written** — this file.

## Out of scope (per the task, unchanged)

More agents. Any knowledge-graph tool (`GROUNDING_KG` is reserved, nothing
produces it). Repo-scale agentic repair. HTML/interactive rendering of the
report. Dataset evaluation, taxonomy, Phase 4, the RQ4 study.

## How to run / verify

```bash
source .venv/bin/activate
SKIP_NETWORK_TESTS=1 python -m unittest tests.test_agent tests.test_report -v
# unset SKIP_NETWORK_TESTS, with a real OPENAI_API_KEY, to also run the two real end-to-end tests

python -m repair_tool.agent broken_examples/02_numpy_float.py --report   # the two-step report above
```
