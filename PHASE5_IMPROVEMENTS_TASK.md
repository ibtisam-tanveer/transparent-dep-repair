# Task: Phase 5 Improvements — harden before moving on

## Context

Phase 5 is done and working (see `PHASE5_SUMMARY.md`): the LLM proposes a code
fix and an environment fix, each is verified by re-running, the winner is kept,
the loser is recorded as an alternative, and `strategy_won` is logged. Three real
end-to-end results confirm it (two fixed, one honest "not fixed").

This task fixes the **known weaknesses surfaced in review**, before building the
Notebook Support phase and before any dataset evaluation. It is deliberately
small and focused: a few targeted fixes, not a redesign. Do **not** change the
core try-both / verify / keep-winner design — it works.

## The fixes, in priority order

### 1. Isolate the two strategies so the measurement stays clean (most important)

**Problem.** A losing *code* edit is already reverted before the environment fix
is tried, but a losing *environment* fix's installed packages are **not** reverted.
If an env-fix candidate installs packages, fails, and the code-fix candidate then
runs in that now-polluted venv, the code fix could pass or fail because of leftover
installs — not on its own merits. This corrupts the `strategy_won` statistic, which
is the thesis's measured result, once we run over many notebooks.

**Fix.** Make each hard-case strategy attempt run against a clean environment state,
so the two candidates are genuinely independent. Acceptable approaches (pick the
simplest that works):
- give each candidate its **own fresh venv** for its verification run, or
- **snapshot and restore** the venv's installed-package set around each env-fix
  attempt (e.g. record `pip freeze`, and after a failed candidate, restore to it),
  mirroring the snapshot-and-restore already done for code edits.

**Done when:** an env-fix candidate that installs packages and then loses leaves no
trace in the environment the next candidate is verified in; a test proves a losing
env-fix does not change the outcome of the subsequently-tried code-fix.

### 2. Record the model identity in every result (cheap, high value)

**Problem.** LLM results depend on which model/version was used, and the summary
notes results can vary even at `temperature=0`. Right now the model name is a
config value but is not stored in the output/records, so results aren't fully
reproducible or reportable.

**Fix.** Include the exact model identifier (and `temperature`) in each hard-case
`Proposal`/`Attempt` record and in the CLI output, so every fix is traceable to the
model that produced it. This is needed for the thesis methods section and for
comparing runs.

**Done when:** a repair record for a hard case shows which model produced it, and
the CLI prints it; a test asserts the field is populated for an LLM-driven fix.

### 3. Add a few non-numpy/pandas examples to catch obvious generalisation gaps

**Problem.** All three end-to-end examples are numpy/pandas API removals — a narrow
slice. Three green lights don't show the LLM path generalises across error shapes.

**Fix.** Add 2–3 more hand-made broken examples of *different* shapes already in the
diagnosis taxonomy, e.g. a moved import (`from sklearn.externals import joblib`), a
removed stdlib import path (`from collections import Mapping`), and one
`object_attribute_error` that is genuinely code-fixable. Add them to
`broken_examples/` with the usual header (what's wrong + correct fix) and, where an
API key is available, a guarded end-to-end check. Keep it small — this is a
smoke test for generalisation, not the real evaluation (that's the dataset).

**Done when:** the new examples run through the full loop; each is either fixed or
honestly reported "not fixed," with the transparency fields populated; no crashes.

## Constraints

- Do not change the core Phase 5 design (try both, verify by re-running, keep the
  winner, record which won). These are hardening fixes only.
- Keep every existing test green (all 79). Add tests for fixes 1 and 2.
- Keep "never trust, always verify," isolation, never-mutate-the-original, and the
  offline-mocked / guarded-live test split all intact.
- Cost-consciousness: don't multiply LLM calls. Fix 1 is about the *environment*
  between verification runs, not about extra model calls.

## Out of scope (do NOT do in this task — avoid over-engineering)

- **Do not** replace the find/replace code-edit format with full-file rewriting.
  The graceful-failure-on-non-match behaviour works; leave the full-file idea as a
  noted future upgrade only.
- **Do not** try to make LLM output perfectly deterministic, or add retries/voting.
- **Do not** attempt to handle every exotic failure type; the dataset will reveal
  what actually matters.
- **Do not** start notebook (`.ipynb`) support here — that is its own next phase.
- **Do not** begin dataset evaluation — still waiting on the supervisor's dataset
  decision.

## Suggested sequence

Do fix 1 first (it's the one that protects the thesis result), then 2 (trivial),
then 3 (smoke test). Commit each separately so a regression is easy to bisect,
consistent with how the phases were committed. Then write a short
`PHASE5_IMPROVEMENTS_SUMMARY.md` covering what changed and how each fix was
verified — same pattern as the phase summaries.
