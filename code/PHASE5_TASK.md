# Task: Phase 5 — LLM-based repair of the hard cases (code vs. environment)

## Project context (read first)

We are building a tool for **AI-Driven Transparent Repair of Software Dependency
Configurations**. The loop is: **run → diagnose → propose fix → apply → re-run to
verify → explain every decision.**

**Phases 1–3 are complete** and live in the `repair_tool/` package:
- `runner.py` → `run_project(path, timeout=60, python_exe=None) -> RunResult`
- `diagnose.py` → `diagnose_result(result) -> Diagnosis(kind, module, package, symbol, detail)`
  kinds: `missing_module`, `import_name`, `module_attribute_removed`,
  `object_attribute_error`, `unknown`.
- `pypi.py`, `venv_manager.py`, `repair.py` (`propose`), `apply.py`, `loop.py`.
  Phase 3 fixes the one deterministic case (`missing_module`) inside an isolated
  venv and verifies by re-running. Every other kind is reported "not handled yet."

This task, Phase 5, makes those "not handled yet" cases actually fixable using an
LLM. **This is the core contribution of the thesis**, so the transparency and
verification rules below are not optional polish — they are the point.

> **Upcoming dependency (do not lose track of this):** Phase 5 stays `.py`-only on
> purpose, to keep the LLM work testable on simple scripts. But the evaluation
> dataset (the GigaScience corpus) is `.ipynb` notebooks, so a **Notebook Support
> phase is required next**, before any evaluation can run. It is out of scope
> *here* but must not be forgotten — it is the immediate follow-up to this phase.

> **Build order / where is Phase 4?** Phase 4 (verify fixes against authoritative
> package *metadata* — confirming an installed package really provides the missing
> import, and detecting declared version conflicts) is **deferred, not dropped.**
> Phase 5 is being done first because it adds new repair *capability*, whereas
> Phase 4 *hardens* fixes that already work. The intended build order is:
> **1 → 2 → 3 → 5 → Notebook Support → 4.** The small "check the version constraint
> against PyPI" note in the `source` field below is a lightweight overlap, **not**
> Phase 4 folded in; the fuller metadata verification remains its own later phase.
> (Whether Phase 4 stays fully separate or is partly absorbed once the LLM
> verification exists is worth a one-line confirmation with the supervisor, but does
> not block Phase 5.)

## The strategy (decided; build exactly this)

There is a genuine debate in the literature (see the vision document, ref [4],
MLEModernizer) about whether it is better to **fix the environment** (pin an older
dependency version so the removed/changed API exists again) or **fix the code**
(rewrite it to match a modern environment, e.g. `np.float` → `float`). The field
has not settled this.

So the tool does **not** pick a side. For each hard case it:
1. asks the LLM for **two candidate fixes** in a single call — one code fix and
   one environment fix — each with its own reason;
2. **applies and verifies each candidate by actually re-running the project** in
   the isolated venv (never trusting the model's claim that it worked);
3. **keeps whichever candidate makes the project run**, and records the other as a
   rejected **alternative** with the reason it was rejected (it did not run);
4. **records which strategy won** (`code` or `environment`) so this can be
   aggregated across the dataset later — this is a measurable thesis result.

To cap cost, run the candidates in order and stop at the first that verifies; only
try the second if the first fails. Default order is a documented constant
(`STRATEGY_ORDER`, default: try code fix first for `*_attribute_*` kinds, env fix
first otherwise) — but always try the other on failure.

## Where it plugs in

Extend the existing loop; do **not** rewrite it. In `repair.py`'s `propose(...)`:
- `missing_module` → keep the Phase 3 rule-based install (unchanged).
- `module_attribute_removed`, `object_attribute_error`, `import_name` (that is not
  a plain install), and `unknown` → route to the new LLM proposer.

New module `repair_tool/llm.py` holds the model call and prompt; `repair.py`
orchestrates candidate selection; `apply.py`/`loop.py` gain the ability to apply a
**code edit** (not just an install) and to record which strategy won.

## LLM interaction (structured, one call)

`repair_tool/llm.py` sends the failing **code**, the **error/traceback**, and the
structured **Diagnosis** to the model and requests **strict JSON only** of the form:

```json
{
  "understanding": "one sentence on what went wrong",
  "code_fix":  {"applicable": true, "reason": "...", "edits": [{"find": "np.float", "replace": "float"}]},
  "env_fix":   {"applicable": true, "reason": "...", "package": "numpy", "constraint": "<1.24"}
}
```

- Either fix may be `"applicable": false` with a reason (e.g. no code change can
  help, or no version has the symbol) — then that strategy is skipped.
- Use OpenAI (chosen provider). Model name is a configurable constant/env var
  (e.g. `OPENAI_MODEL`, default a current small model such as `gpt-4o-mini`);
  set `temperature=0` for reproducibility. Parse defensively: strip code fences,
  reject non-JSON, never crash on a malformed response — treat it as "no fix."

## Applying and verifying each candidate

- **Environment fix:** install the constrained package (e.g. `numpy<1.24`) into the
  venv via the existing `apply.py`, then re-run. Reuse Phase 3 machinery.
- **Code fix:** apply the edits to a **working copy** of the project in an isolated
  workspace — **never modify the user's original input file** — then run that copy
  in the venv. Keep the edit reversible and recorded. **If an edit's `find` string
  does not match the source verbatim** (e.g. different whitespace), do **not** crash:
  treat that candidate as failed-to-apply → a verification failure → fall back to
  the other strategy. (A future robustness upgrade is to have the LLM return the
  full corrected file instead of find/replace pairs; note it, don't build it yet.)
- **Verification is always by re-running.** A candidate counts as successful only
  if the project actually runs (`RunResult.ok`). The model's own opinion is never
  the deciding signal — this is the thesis's "never trust, always verify" rule and
  must hold here of all places.
- If neither candidate verifies, report the case as **not fixed**, honestly, with
  both attempts and their failure reasons recorded. Never claim a fix that did not
  re-run cleanly.

## Transparency fields — now filled with real content

Extend the existing `Proposal`/`Attempt` records so each hard-case fix carries:
- **reason** — the LLM's reason for the winning change.
- **source** — `"LLM suggestion, verified by re-running"` (and, for an env fix,
  note if the version constraint was also checkable against PyPI).
- **alternatives** — the *rejected* strategy and why it was rejected (e.g.
  "environment fix: pinned numpy<1.24 → project still failed"). This field, empty
  until now, is the direct payoff of the try-both design.
- **verification** — the actual re-run outcome.
- **confidence** — `high` if it verified by re-running; `low` if applied only on the
  model's word (which should not happen, since unverified fixes are not applied).
- **strategy_won** — `"code"` or `"environment"` (for later aggregation).

## API key safety

- Read the key from the `OPENAI_API_KEY` environment variable. **Never hard-code
  it, never log it, never write it to disk.** If it is missing, fail with a clear
  message telling the user to set it — do not crash obscurely.
- `openai` becomes a real dependency: add it to `pyproject.toml`, but keep the
  import isolated to `llm.py` so the rest of the tool (runner, diagnose, pypi) stays
  importable and testable without it.

## Required behaviour / interface

```python
from repair_tool.loop import repair

# hard case: removed API. With a (mocked or real) LLM, this should now fix.
result = repair("broken_examples/02_numpy_float.py")
assert result.fixed is True
# and the record says which strategy won and shows the rejected alternative:
assert result.attempts[-1].proposal.strategy_won in ("code", "environment")
assert result.attempts[-1].proposal.alternatives   # non-empty
```

## Constraints

- Build on Phases 1–3; do not regress them. Isolation (venv per project) still
  mandatory; original input files never mutated.
- **Verify by execution, always.** No fix is applied and kept unless a re-run
  passes.
- LLM calls: `temperature=0`, one call per hard case (returns both candidates),
  defensive JSON parsing, graceful handling of a missing key or bad response.
- Keep the loop's `MAX_ATTEMPTS` cap and no-progress stall detection intact — the
  layered-error behaviour from Phase 3 must still work (install a missing package,
  then LLM-fix the removed API revealed underneath).

## Tests

- **Offline unit tests (no network, no API): mock the LLM.** Inject a fake JSON
  response and assert: both candidates are tried in order; the winner is the one
  that re-runs; the loser is recorded as an alternative; `strategy_won` is set;
  a malformed LLM response degrades to "not fixed" without crashing; a missing API
  key gives a clear error. Mocking is essential because real LLM calls are
  non-deterministic and cost money.
- **One guarded integration test (real API):** `repair("broken_examples/02_numpy_float.py")`
  actually calls OpenAI and ends `fixed is True`. Skip (not fail) it when
  `OPENAI_API_KEY` is unset or `SKIP_NETWORK_TESTS=1`.
- Do not weaken any Phase 1–3 tests.

## Definition of done

- The removed-API examples (`02_numpy_float.py`, `03_numpy_int_bool.py`,
  `05_pandas_append.py`) that Phase 3 reported "not handled yet" are now repaired
  end-to-end (with a real key), verified by re-running.
- Each hard-case fix records reason, source, the rejected alternative, verification,
  confidence, and `strategy_won`.
- When neither strategy works, the tool reports "not fixed" honestly with both
  attempts recorded — never a false success.
- Original input files are never modified; all work happens on copies in the venv
  workspace.
- Offline suite passes with a mocked LLM; the live test skips cleanly without a key.
- Full suite (Phases 1–5) green locally and in CI.

## Out of scope (do NOT do here)

- No dataset evaluation run yet (that is the separate evaluation step, and it waits
  on the supervisor's dataset decision).
- No user study (RQ4).
- No UI.
- **No `.ipynb` notebook handling in this phase — `.py` only. This is a deliberate
  scope limit, not a permanent one.** The LLM repair logic is easier to build and
  test on plain scripts, so Phase 5 stays on `.py`. **However, notebook support is
  a REQUIRED follow-up, not optional:** the chosen evaluation dataset (the
  GigaScience reproducibility corpus) is made of `.ipynb` notebooks, so the tool
  cannot be evaluated on it until notebooks are supported. This is the planned
  **next phase after Phase 5** (a Notebook Support phase), and it must land before
  any dataset evaluation. See the "Upcoming dependency" note at the top.

## Why this design matters (keep it intact)

The "propose both, verify by running, keep the winner, record which won" shape is
deliberate: it fills the transparency report's *alternatives* and *verification*
fields with genuine content, it applies "never trust, always verify" to the choice
of strategy itself, and — aggregated over the dataset later — it turns the
unsettled code-vs-environment debate into a measured result. Do not simplify it to
"pick one strategy," as that removes the contribution.

## Suggested manual check

```bash
export OPENAI_API_KEY=sk-...
python -m repair_tool.loop broken_examples/02_numpy_float.py   # FIXED; report shows winning + rejected strategy
python -m repair_tool.loop broken_examples/05_pandas_append.py # FIXED (pd.concat, or pinned pandas<2.0)
```
