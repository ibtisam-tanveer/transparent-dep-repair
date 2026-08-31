# Phase 5 Summary — LLM-based repair of the hard cases (code vs. environment)

Status: **done**. This is the thesis's core contribution — the first phase
where the transparency report's `alternatives` field carries real content,
and where "never trust, always verify" gets applied to an LLM's own claims,
not just to `pip install`.

## What was asked (`PHASE5_TASK.md`, revised version)

Make the diagnosis kinds Phase 3 leaves as "not handled yet"
(`module_attribute_removed`, `object_attribute_error`, `import_name`,
`unknown`) actually fixable, without picking a side in the unsettled
code-vs-environment debate: ask an LLM for two candidate fixes in one call,
apply and verify each by re-running (never trust the model's word), keep
the winner, record the loser as a rejected alternative, and record which
strategy won for later aggregation.

## What was built

| File | Purpose |
|---|---|
| `repair_tool/llm.py` | `request_fix(diagnosis, code, error_text)` — the only module that imports `openai` or reads `OPENAI_API_KEY`; strict-JSON prompt, defensive parsing, never raises |
| `repair_tool/repair.py` (extended) | `propose_hard_case(...)`, `STRATEGY_ORDER`, `HARD_CASE_KINDS`; `Proposal` gained `edits`/`alternatives`/`strategy_won` |
| `repair_tool/apply.py` (extended) | `apply_code_edit(edits, workspace_path)` — find/replace on a working copy, clean failure (not a crash) on a non-matching `find` |
| `repair_tool/venv_manager.py` (extended) | `get_workspace_copy(target_path)` — the one-time copy every run/edit targets from then on |
| `repair_tool/loop.py` (rewritten) | orchestrates apply+verify+pick-winner for hard cases; Phase 3's missing_module path is untouched |
| `tests/test_llm.py` | 8 new tests (offline mocked + one network-guarded real call) |
| Extensions to `test_repair.py`, `test_apply.py`, `test_loop.py` | 6 hard-case scenarios in `test_loop.py` alone, offline and fully mocked |

## Design decisions and why

- **`propose()` stays pure; a new `propose_hard_case()` sits beside it,
  not inside it.** Phase 3's `propose(diagnosis) -> Proposal` is completely
  unchanged — still pure, still returns `kind="none"` for every non-`missing_module`
  kind when called directly (all its existing tests pass unmodified).
  `propose_hard_case()` makes the single LLM call and returns *both*
  candidates without applying anything — the apply-verify-pick-winner
  orchestration lives in `loop.py`, which is where side effects belonged
  already (Phase 3's `apply()`/`loop.py` split, extended rather than broken).
- **`loop.repair()` now works on a working copy from the very first run**,
  not just once a code edit is needed. `get_workspace_copy()` is called once
  at the top of `repair()`; every subsequent `run_project` call (Phase 3's
  install path included) targets that copy. This uniformly guarantees the
  original input file is never touched, for any diagnosis kind, without
  needing a conditional "which path do I run" branch anywhere in the loop.
- **A hard-case win short-circuits immediately rather than looping again.**
  A winning candidate's own verification run already showed `RunResult.ok
  is True` — the *entire* project ran, not just "this one error is gone" —
  so there is nothing left to reveal by re-running once more. This avoids
  paying for a redundant subprocess run (or, worse, a redundant LLM call
  were one ever needed) on every successful hard-case fix.
- **A failed code-edit candidate is reverted before trying the other
  strategy.** Without this, a losing code edit would still be sitting in
  the workspace file when the environment-fix candidate gets tried next,
  contaminating that attempt and making "which strategy alone fixes it" an
  unreliable measurement. Snapshot-and-restore around each hard-case
  resolution round keeps the two candidates genuinely independent. (Not
  applied to a losing environment-fix's pip state — the task doc only
  requires reversibility for code edits, and this is noted as an accepted,
  intentional scope limit rather than something silently skipped.)
- **`env_fix` reuses Phase 3's install path unchanged** — its
  `package`+`constraint` (e.g. `numpy` + `<1.24`) are simply concatenated
  into one package spec string (`"numpy<1.24"`) and passed to the exact
  same `apply()` that installs a plain missing package. No new install
  machinery needed.

## A real bug found and fixed during verification, not assumed away

Manually running `broken_examples/05_pandas_append.py` against the real API
surfaced a genuine defect: the model's JSON response contained an
**unescaped literal `"` inside a string value** — e.g.
`"find": "df = df.append({"a": 3}, ignore_index=True)"` — because the
source code itself contains a Python dict literal with quotes. `json.loads`
correctly rejected this as invalid JSON, and `llm.py`'s defensive parsing
correctly degraded to "no fix" instead of crashing — but that's a real,
avoidable failure mode, not something to just accept. **Fix**: added
`response_format={"type": "json_object"}` to the OpenAI call, which
constrains the API to only ever emit syntactically valid JSON (the model
literally cannot produce output the endpoint would accept otherwise). Not
a workaround — this is the standard, correct way to get reliable structured
output from this API, and the task doc's own requirement ("requests strict
JSON only") is better satisfied by a server-side guarantee than by asking
nicely in the prompt. Confirmed fixed: re-ran the exact same case
end-to-end afterward with no parse failure.

## Real end-to-end results (real API key, real venvs, real installs)

| Example | Result | What happened |
|---|---|---|
| `02_numpy_float.py` | **FIXED** | `code_edit` won (`np.float` → `float`); no alternative was applicable, so `environment` wasn't even tried |
| `03_numpy_int_bool.py` | **FIXED** | `code_edit` won (`np.int`/`np.bool` → `int`/`bool`) |
| `05_pandas_append.py` | **NOT FIXED — honestly** | Both candidates tried and both genuinely failed, for two different real reasons (see below) |

**Why `05` is an honest "not fixed," not a bug**, and worth documenting
precisely because it's a good demonstration of the design working as
intended:

1. **The code-fix candidate had a real logic error, and verification caught
   it.** The model's find/replace edits produced
   `pd.DataFrame({"a": 3}, ignore_index=True)` — syntactically valid Python,
   but `ignore_index` is a `pd.concat()` parameter, not a `pd.DataFrame()`
   one, so it raises at runtime. `apply_code_edit` applied it successfully
   (both `find` strings matched verbatim), but the re-run afterward failed,
   so it was correctly rejected rather than reported as a false success.
   This is precisely the scenario "never trust, always verify" exists to
   catch — an LLM producing something that *looks* right but isn't.
2. **The environment-fix candidate hit a genuine, unfixable environment
   constraint.** `pandas<2.0`'s last releases predate Python 3.14 by two to
   three years; no prebuilt wheel for that combination has ever existed or
   ever could, so `pip install` fell back to a source build, which failed.
   Retried twice (LLM calls aren't perfectly deterministic even at
   `temperature=0`); both attempts produced the identical code-fix bug and
   hit the identical build failure, confirming this is a stable result, not
   flakiness.

This isn't a shortfall to explain away — it's the definition-of-done's own
"When neither strategy works, the tool reports 'not fixed' honestly ...
never a false success" working exactly as specified, on a case where
neither available strategy could actually succeed in this environment.

## How each definition-of-done item was verified

- `02_numpy_float.py` and `03_numpy_int_bool.py` repaired end-to-end with a
  real key, verified by re-running — confirmed via the CLI and via
  `TestRepairIntegration.test_llm_fixes_removed_numpy_api_end_to_end`.
- `05_pandas_append.py` correctly reports "not fixed" with both attempts
  recorded and real reasons — see above; not silently treated as a failure
  of the phase.
- Every hard-case fix records `reason`, `source`, `alternatives`,
  `verification`, `confidence`, `strategy_won` — verified in both the
  offline mocked tests and the real runs' printed CLI output.
- Neither-strategy-works path reports honestly, never a false success —
  `test_neither_candidate_verifies_reports_not_fixed_honestly` (offline) and
  the real `05` result (online) both confirm this.
- Original input files never modified — `test_original_input_file_is_never_mutated_by_a_code_fix`
  re-reads the file *after* `repair()` runs (not before, which would prove
  nothing) and confirms byte-for-byte equality; also true by construction
  since `get_workspace_copy` reads the original exactly once.
- Offline suite passes with a mocked LLM; the live tests skip cleanly
  without a key or with `SKIP_NETWORK_TESTS=1` — confirmed both ways.
- Full suite: **79 tests** (56 from Phases 1-3 + 23 new), all passing, both
  offline (72 run, 7 skipped) and fully online (all 79 run, ~35s including
  two real venv/install cycles and multiple real LLM calls).
- Phase 1-3 tests: one test's *scenario* (not its safety guarantee) needed
  updating — `module_attribute_removed` used to be the example proving
  "Phase 3 never installs anything for kinds it doesn't handle," but that
  kind is now legitimately routed to Phase 5's LLM path. Updated to use an
  unresolvable `missing_module` instead, which is still the one kind that
  genuinely stays "not handled yet" at the rule-based level; the underlying
  guarantee (no install attempted, no crash, honest reporting) is unchanged
  and still tested, just via a scenario that's still accurate post-Phase-5.

## Explicitly out of scope (per the spec, unchanged)

No dataset evaluation run (waits on the supervisor's dataset decision — see
`DATASET_SUMMARY.md`). No user study (RQ4). No UI. No `.ipynb` handling —
Notebook Support is the required next phase, before any evaluation can run
against the GigaScience corpus (which is entirely `.ipynb`).

## How to run / verify

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
echo 'OPENAI_API_KEY=sk-...' > .env   # your own key; gitignored
SKIP_NETWORK_TESTS=1 python -m unittest tests.test_runner tests.test_diagnose tests.test_pypi tests.test_repair tests.test_apply tests.test_llm tests.test_loop -v
# unset SKIP_NETWORK_TESTS to also run the real PyPI/OpenAI tests locally

python -m repair_tool.loop broken_examples/02_numpy_float.py   # FIXED
python -m repair_tool.loop broken_examples/05_pandas_append.py # NOT FIXED (honestly, for real reasons -- see above)
```
