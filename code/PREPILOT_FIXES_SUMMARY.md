# Pre-pilot fixes — results summary

Task: `TASK_prepilot_fixes.md`. Clear the harness/tool blockers surfaced by
`INTERPRETER_REPAIR_SUMMARY.md` before a bigger pilot is worth running. These are
fixes to the harness/tool, not new repair features.

**Run used for the numbers:** `kiste_smoke_v6` — same 3 repos / 51 labelled notebooks
as v2/v3/v5 (seed 42, confirmed-only, model `Qwen3.6-35B-A3B-MLX-8bit` on kiste,
2-minute per-file budget, no-progress limit 3, Apple-Silicon Mac, host runtime).

## 1. Headline

| | v5 (before) | **v6 (after)** |
|---|---|---|
| Fixed | 1 | **1** (a different notebook; see §3) |
| Genuine fail (in scope, attempted) | 16 | 16 |
| Repo-local module (out of scope) | 23 | **7** |
| Out of scope at run time (category E) | 10 | **27** |
| Budget exhausted | 1 | **0** |
| LLM backend unreachable / not discovered | 0 / 0 | 0 / 0 |
| Interpreter unprovisionable | 0 | 0 |
| In-scope denominator | 17 | 17 |
| Effectiveness | 1/17 (5.9%) | 1/17 (5.9%) |
| Wall time | — | 108 min, no stray processes, no worker timeout |

**Do not read 1/17 → 1/17 as "no change".** v5's fix was a measurement artifact (§2),
and v6 is the first run in which timeouts are actually enforced. Compare future runs
against v6, not against v2–v5.

Per repo (v6):

| Repo | Fixed | Genuine fail | Local module | Out of scope (E) |
|---|---|---|---|---|
| IBM/wntrac (6) | 0 | 0 | 0 | 6 |
| PBrockmann/ipython_ferretmagic (10) | 0 | 10 | 0 | 0 |
| e-stan/covid_19_analysis (35) | 1 | 6 | 7 | 21 |

## 2. Fix 1 — the hang (root cause found, not just contained)

**Symptom:** a Jupyter kernel stayed alive ~90 minutes past the worker's limit; the
2700 s worker timeouts in v3/v5 and the full-repo ablation.

**My first hypothesis was wrong.** I assumed `subprocess.run(timeout=...)` blocked on
output pipes held by a grandchild. On POSIX, Python's `subprocess.run` does not do
that after a kill, so I removed that test and corrected the docs.

**Actual root cause (reproduced in isolation, no changes of mine involved):**
`run_notebook` passed a *synchronous* `KernelManager` to `nbclient`. nbclient polls
kernel messages with `kc.iopub_channel.get_msg(timeout=1)`, which on a sync client is
a blocking zmq `poll()` executed on the asyncio loop. That starves the task that
enforces the per-cell timeout, so **a hung cell never timed out**. A plain
`time.sleep(300)` cell with `timeout=5` ran to completion.
Fix: use `AsyncKernelManager`. Same cell now ends in **5.6 s** with `CellTimeoutError`.

**Defence in depth (also implemented, new `repair_tool/proc.py`):**
- every script/worker/`git clone` runs in its own process group; on timeout the whole
  group gets SIGTERM, then SIGKILL (`run_group`, `kill_process_group`);
- `jupyter_client` starts every kernel with `start_new_session=True`, so a worker kill
  alone would miss kernels: kernel process groups are written to a registry file and
  reaped by the harness after the worker ends, even if the worker was killed;
- a whole-notebook wall-clock watchdog (`total_timeout`, default
  `max(5 x cell timeout, 300 s)`) kills the kernel's group from outside nbclient;
- kernel/child cleanup in a `finally`.

**Regression tests** (`tests/test_proc.py`, real processes and a real kernel): a hanging
cell is killed at the budget and a result is still returned; a child spawned by a cell
does not survive; the whole-notebook deadline holds even when every cell is individually
within its limit; a worker killed at its timeout still has its kernel group reaped and
its partial row preserved.

## 3. Consequence: the v5 "fix" was an artifact

Because timeouts were never enforced in v2–v5, slow cells ran unbounded. The one fix
credited to interpreter repair, `WNTRAC_NPI_Index.ipynb`, almost certainly depended on
that: its first data cell downloads a large Google mobility CSV. A direct probe of the
agent on that notebook (installs `pandas`, `plotly`, `wget` correctly) then hits
`CellTimeoutError` on the download cell and ends `no_progress`, which is correctly
classified out-of-scope (data download). *Caveat:* I did not re-measure v5's download
time; "almost certainly" rests on the download now exceeding the enforced 60 s limit.

Effects on earlier numbers: the v5 "1/17", and the earlier "budget exhausted" and
"genuine fail" counts, are not comparable to v6. An erratum was added to
`INTERPRETER_REPAIR_SUMMARY.md`.

**Open policy decision:** the 60 s per-cell limit now applies to every step, so a
legitimately slow cell (training, big downloads) times out. Options: keep 60 s and
treat such notebooks as out-of-scope (current behaviour), or raise it (e.g. 300 s) at
the cost of slower runs and different budget math. Not changed without a decision.

## 4. Fix 2 — refuse name-collision installs

In v5 the agent installed the unrelated PyPI package `combat==0.3.0` 22 times because
the repo ships `src/combat.py`.

- New `repair_tool/localmods.py` (`find_local_module`), shared with the evaluation's
  local-module classification.
- `install_package` refuses when the repo provides a module of that name: no pip call;
  the result carries `refused`, `refusal_reason: repo_local_module`,
  `local_module_path` and a "do not retry" message. The dispatch answers a repeated
  attempt instantly ("Already refused").
- **Strict mode for refusal:** a directory only counts if it contains Python, so a data
  folder that happens to be named like a real package (e.g. `figures/seaborn/`) can
  never block a legitimate install. Classification keeps the original broad rule.
- Result: **6 refused installs in v6** (vs 22 in v5). Rows record
  `refused_local_module_installs`.
- Tests: local `foo.py` + repeated `install foo` never calls pip; a real dependency is
  still installed; single-file mode (no repo root) is unaffected.

## 5. Fix 3 — working-directory resolution (how many of the 23 are recovered)

New `repair_tool/workdir.py`, called from `run_project` (so the first run, the agent's
`run_target`, and `verify` all share it). Only when a run fails with
`ModuleNotFoundError` for a name the repo itself provides, it retries from the
notebook's ancestor directories (nearest first), preferring one where the notebook's
own literal `sys.path.append/insert("...")` entries resolve to that module. The first
directory that makes the local-import error go away is kept and remembered. The
directory used and why are recorded (`cwd`, `cwd_source`, and per row
`working_directory`, `working_directory_source`, `working_directory_recovered`).

Result on the covid repo in `kiste_smoke_v6`:

- Local-import failures investigated: **26**; **recovered by a different working
  directory: 19** (all from `notebooks/`, inferred from `sys.path.append("../src/")`).
- Of the **23 notebooks v5 excluded as "local module": 17 are no longer local-module**
  (their import now resolves); **6 remain** local-module. Final outcomes of those 23:
  **1 fixed, 1 genuine fail, 15 out-of-scope (E), 6 still local-module.**
- The 6 that remain: they `import combat` but add **no** `sys.path` entry at all, so no
  working directory can make them resolve. They stay out of scope with that exact reason.
- **The honest reading:** the fix correctly removes a false exclusion, but it did not
  move many notebooks into the in-scope denominator. After the import resolves, 15 of
  the 17 fail on something that is not a dependency problem and are reclassified E.
  Only 2 became genuinely attempted (1 fixed, 1 genuine fail).
- A no-LLM preview in a bare environment (17 of 22 `combat` notebooks resolved) matched
  the real run.
- The one fixed covid notebook, `model_metabolites_check_int_long_ICU_b1-9.ipynb`, was
  fixed by a **model-proposed `edit_code`** (`llm_proposed`, verified, medium-high
  confidence) — not a grounded fix.

Tests include real-kernel end-to-end cases: control (fails from its own folder), recovery
from `notebooks/`, caching (no second search), scripts, an unresolvable case that stays
failing, a non-local missing module (no search), and a passing notebook untouched.

## 6. Fix 4 — postBuild and the per-repo budget

- **postBuild:** `repair_tool/repo.py` `find_postbuild`/`run_postbuild` run
  `binder/postBuild` (else `.binder/`, else root) after the environment is built, as
  repo2docker does. Runs from the repo copy root with the repair interpreter first on
  `PATH` and a throwaway `$HOME`; killed at a timeout with no orphans; failure/timeout is
  recorded (`postbuild_found/ran/ok`), never raised. `--no-postbuild` switches it off.
  **Security note:** it is arbitrary repo-supplied shell code running on the host (same
  trust level as the repo's notebooks and pip installs); the container runtime is the
  right long-term place.
  In v6: found only in ferretmagic; it ran and failed (the script writes to Binder's
  `/srv/conda/...` path), recorded as such.
- **Budget by labelled notebooks:** previously the budget was sized by every runnable
  file and the worker also repaired every one of them (recording only the labelled
  rows), so most budget went to notebooks nobody asked about. Now the budget is sized by
  the labelled targets (`count_target_files`) and only they are run (`only_paths`).
  Trade-off: incidental installs made while repairing unlabelled notebooks no longer
  carry over to labelled ones (a possible, unverified, contributor to the wntrac change).
  v6: budget-exhausted 0.

## 7. Defect found in v6 and fixed

Two covid rows ended with `TypeError: 'NoneType' object is not subscriptable` and no stop
reason, and were counted as genuine failures. The only place the agent indexes a
possibly-empty server reply is `response.choices[0]`, outside the LLM-error handler.
Fixed: a reply with no `choices` is now an LLM-error stop (`stop_reason=error`) with a
regression test. **I did not prove this was the cause** (no traceback was stored). Crash
rows now include the failing file and line (`describe_exception`) so any other cause will
be visible.

## 8. Tests

- Added: `tests/test_proc.py`, `tests/test_prepilot_fixes.py`, plus changes in
  `test_agent.py`, `test_agent_tools.py`, `test_run_eval.py`.
- The full offline suite ran **407 tests**; the model-dependent ("Network") tests failed
  only while the VPN was off and pass with it on.
- One pre-existing test-isolation weakness: the cached-workspace mode kept an
  already-fixed copy of a broken example, making a real-model report test fail until the
  stale copy was removed. Not caused by these changes; not fixed.
- Nothing is committed.

## 9. Not done / limits

- **The 27 out-of-scope (E) rows do not record the error that triggered the
  classification.** I cannot say whether they are missing data, network downloads or
  cell timeouts. Recommend adding a `runtime_observed_error` field to rows before the pilot.
- No `PYTHONPATH` fallback for the 6 unresolvable `combat` notebooks (a guess the repo
  does not support). Possible, would be recorded as a distinct heuristic source.
- The wntrac probe (one notebook) differed from the v6 row (14 steps vs 4): model variance.
- The container runtime and the native-`linux/amd64` pilot (`TASK_containerize.md`) are
  the next steps in the task's sequence and have not been started.
- n = 51 notebooks, 3 hard repos, one model, one seed, one machine: a smoke test, not the
  thesis evaluation.

## 10. Files

New: `repair_tool/proc.py`, `repair_tool/localmods.py`, `repair_tool/workdir.py`,
`tests/test_proc.py`, `tests/test_prepilot_fixes.py`.
Changed: `repair_tool/notebook.py`, `runner.py`, `agent.py`, `agent_tools.py`,
`agent_repo.py`, `repo.py`; `evaluation/run_eval.py`, `_repo_runner.py`,
`_repo_worker.py`, `analyze_results.py`; the matching tests; `INTERPRETER_REPAIR_SUMMARY.md`
(erratum).
Results: `evaluation/results/kiste_smoke_v6.jsonl` (+ `.meta.json`, `.done_repos.txt`).
