# Raising the in-scope fix rate — six repair levers, measured

Task: `TASK_improve_fix_rate.md`. Every change is a measured experiment; each lever is a separate flag
(`--levers`, default **off** = the baseline all earlier results used). Runs: Docker `linux/amd64`, same model
(kiste Qwen3.6-35B), same 9 notebooks, same day. Nothing is committed or pushed.

## 0. In plain words

- I added six "levers" to help the tool fix the package problems it missed in the pilot.
- **Result: the in-scope fix rate rose from 1 of 13 to 2 of 13** (7.7% → 15.4%) — one extra notebook, `h5n1-cambodia`.
  That is a small, real gain on a tiny sample, **not** a general result: the exact 95% interval on 2/13 is about 2%–45%.
- The extra fix is a chain: install **R through conda** (`system_deps`) → search **older `rpy2`** releases
  (`downgrade`) → a **code edit** replacing the removed `pandas.DataFrame.from_csv` with `read_csv`
  (a genuine API migration, not a deletion).
- The other levers **did act correctly** on other notebooks and cleared the first error on several, but none of those
  notebooks reached "passes" within the step budget; they fail on the *next* layer of version problems.
- **Cost:** about 41% more model calls (122 → 172) and three runs that now hit the 30-step cap instead of stopping early.
- `SAMPL8` stays unfixable (paid OpenEye licence), as expected.

## 1. Before → after, per notebook (9 package problems)

| Notebook | Pilot v4 | Baseline today (levers off) | **All six levers** | What the levers did |
|---|---|---|---|---|
| modulome_mtb | **fixed** | **fixed** | **fixed** | nothing needed (still fixed — no regression) |
| h5n1-cambodia | not fixed (`rpy2` missing) | not fixed | **FIXED** | `system_deps` + `downgrade` + code edit (§3) |
| MaaSSim | `get_nearest_node` import error | same | not fixed — error **cleared**, next layer failed | `downgrade_search` `osmnx` 2.1.1 → 0.15.1 removed the original error; then a pandas/numpy cascade used the 30 steps |
| matrix_paper | missing/old `cooltools` | `cannot import snipping` | not fixed — next layer | `conda_fallback` built `cooltools=0.3.2` ✔; then `pandas.is_categorical` removed (needs older pandas); 30 steps used |
| interpretable-generative | `scvi` missing | training cell timed out* | not fixed | `downgrade_search` ×3 on `mudata`/`anndata` (wasted); ended `scvi.dataset` missing |
| grp_paper | `abutils` missing | same | not fixed | `conda_fallback`: `nwalign3` ✔, `abutils` ✘ (not on the channels); `lower_python` 3.9 → 3.8; still missing |
| TeachOpenCADD | `opencadd` missing | same | not fixed | `lower_python` 3.14 → 3.13 → 3.12 ✔ (worked), still missing |
| SAMPL8 | `openeye` missing | same | not fixed | **fair "cannot"**: paid licence |
| ProteinUnfolding2D | `NoneType … norm2temp` | same | not fixed | not a package problem (code/data) |

\* the baseline run reached the model-training cell (cell 15 of 30) and timed out at the 180 s cell limit — its dependency
problem was solved, the rest is compute time. This notebook varies between runs (pilot: `scvi` missing; baseline: timeout;
levers: `scvi.dataset` missing) — it is a variance example, not a lever effect.

**In-scope rate** (13 = 9 package problems + the 4 shell-command notebooks the study files as category D; the 4 are
unaffected by these levers and were not re-run):

| | fixed | rate |
|---|---|---|
| Pilot v4 (earlier code) | 1 / 13 | 7.7% |
| Baseline today, levers off | 1 / 13 | 7.7% |
| **All six levers** | **2 / 13** | **15.4%** |

Package problems only (9): baseline 1/9 → levers 2/9.

## 2. What each lever did (observed in the all-levers run)

| Lever | Acted | Outcome |
|---|---|---|
| 1 `downgrade` (iterative version search) | 7 searches on 4 notebooks | all 7 ended with the original error gone. In 2 notebooks that was real progress (`osmnx` for MaaSSim, `rpy2` for h5n1 — **part of the one extra fix**); in the others the model had picked a package that was not the cause (`seaborn` down to 0.5.1, `mudata`/`anndata`), so the search only moved the failure |
| 2 `conda_fallback` (pip build failure → conda) | 2 successes (`nwalign3`, `cooltools`), 1 failure (`abutils`) | works where conda has the package; honest limit: only inside a **conda** environment, never a plain venv |
| 3 `system_deps` (system component via conda) | 1 (R + `rpy2`) | **necessary** for h5n1 — nothing gets past R without it. Needed a fix of its own: conda environments must be run **activated** (R_HOME etc.), otherwise `rpy2` fails with "R_HOME cannot be None" |
| 4 `interpreter_down` (Python step-down) | 3 steps on 2 notebooks | rebuilt environments correctly (3.14→3.13→3.12 for TeachOpenCADD, 3.9→3.8 for grp_paper); **never decisive**. Leave-one-out: not needed |
| 5 `bounded_fallback` (nudge before the no-progress stop) | 5 nudges on 4 notebooks | fired as designed (bounded to 3, never repeated); no fix traced to a nudge |
| 6 `code_forward` (prefer code migration for removed APIs) | prompt + 1 nudge | the h5n1 fix ends in exactly this kind of edit (`DataFrame.from_csv` → `read_csv`) |

## 3. Attribution on h5n1 (the one extra fix)

Single notebook, many runs (kiste/LLM runs are not deterministic; each row below is **one** run unless stated):

| Levers on | Result |
|---|---|
| none (4 runs: pilot, today ×2, ablation) | **0 / 4** — stops at `No module named 'rpy2'` |
| `downgrade` only | not fixed — still `rpy2` missing (no R) |
| `system_deps` only | not fixed — ends at `DataFrame.from_csv` removed |
| `system_deps` + `downgrade` | not fixed — ends at `DataFrame.append` removed |
| `system_deps` + `code_forward` | not fixed — ends at an R-package error (`ggplot2`) |
| `system_deps` + `code_forward` + `bounded_fallback` | not fixed — `DataFrame.append` removed |
| `system_deps` + `downgrade` + `code_forward` | not fixed — `DataFrame.append` removed |
| **all six** | **FIXED 2 / 2** — `system_deps` → `downgrade_search` → `edit_code` |
| all **minus** `interpreter_down` | **FIXED 1 / 1** → this lever is not needed here |
| all **minus** `downgrade` | not fixed (1 run) |
| all **minus** `conda_fallback` | not fixed (2 runs) |
| all **minus** `bounded_fallback` | not fixed (2 runs) |
| all **minus** `code_forward` | not fixed (1 valid run; a 2nd run lost to a model-connection error and was discarded) |

**What can and cannot be concluded.**
- *Mechanistically, from the trace:* the fix is `system_deps` (R) → `downgrade` (`rpy2` 3.5.11 → 3.0.5) → `edit_code`
  (`from_csv` → `read_csv`). `system_deps` is **necessary** (every run without it stops at `rpy2`).
- *Statistically:* the full set fixed it 3 of 3 times (counting the run without `interpreter_down`); every other subset
  failed (0 of 12 valid partial-set runs). That is unlikely to be luck (exact one-sided p ≈ 0.002), **but** removing `conda_fallback`
  or `bounded_fallback` also broke it, and **neither acted in the successful runs** (no conda retry, no nudge). So the
  fix is **not additive per lever**: part of the effect is probably how the *whole set* changes the model's prompt and
  tool list, or ordinary run-to-run variance. I cannot separate those with this few runs.
- *So the honest statement for the thesis:* "with all levers (or all but `interpreter_down`) the tool fixed h5n1 in 3 of 3 runs vs 0 of 4 baseline runs;
  the repair chain is X; individual lever attribution beyond `system_deps` (necessary), `downgrade` (needed in the one
  leave-out run) and `interpreter_down` (not needed) is not established."

## 4. Costs and new failure modes

- **Model calls:** 122 → 172 (+41%); agent steps 122 → 173.
- **Stop reasons (9 notebooks):** baseline: 8 no-progress + 1 fixed. Levers: 4 no-progress + 2 fixed + **3 step-cap** (MaaSSim,
  interpretable-generative, matrix_paper). The levers make the agent keep trying instead of stopping — useful when it
  reaches the next layer, wasteful when the model picks the wrong package for `downgrade_search`.
- **Wall time:** baseline 9 notebooks ≈ 18 min; levers ≈ 28 min (+55%) in the first paired runs (later runs overlapped other work).
- **Wasted effort seen:** `downgrade_search` on `seaborn` (down to 0.5.1) and on `mudata`/`anndata` — the model chose a package that
  was not the cause. A guard that only offers packages named in the error would reduce this (not built; out of scope here).

## 5. Why most misses are still misses (the plateau)

The levers solve the **first** version error; the notebooks then fail on a **second and third**: MaaSSim goes
`osmnx` → `numpy.exceptions` → `Series.rides`; matrix_paper goes `cooltools` → `pandas.is_categorical`. A 2019-era notebook
needs *every* package at its 2019 version, and installing one older package pulls newer transitive dependencies. The levers
fix packages one at a time, and 30 steps run out. The measured next step is **era-consistent resolution** (resolve the
whole environment as of the repo's last-commit date, e.g. with `uv --exclude-newer`) — that is a seventh lever, **not
implemented here**, and a candidate before any knowledge-graph tool.

## 6. Other things found while measuring (all fixed, with tests)

1. conda-provisioned environments were run **without activation** → fixed (`repair_tool/condaenv.py`); required for lever 3.
2. Result rows now keep the **original error** and the **find → replace text of every code edit**, so a reviewer can check an
   edit repaired the code and did not just delete what failed (the h5n1 edit is a correct API migration).
3. A reviewer-friendly plain-language report: `python evaluation/repair_url.py <github link> [--runtime docker] [--levers all]`.
4. I tried to relabel `unverified` steps that were needed but followed by a failing verify; a test showed the current rule is a
   **deliberate** part of the two-axis trust model (an install that does not make the run pass stays `unverified`), so I
   reverted it and explain the label in the report instead.

## 7. Limits (state these in the write-up)

- 9 notebooks / 13 in-scope; one model; one seed; one machine; emulated x86_64. A gain of one notebook is **not** a rate.
- Single runs per configuration (LLM nondeterminism is real: `interpretable-generative` changed outcome across identical runs).
- The 4 shell-command notebooks and the other 12 sample notebooks were not re-run with levers.
- Levers `conda_fallback` and `system_deps` work only in conda environments (20 of 25 pilot repos in the container; a plain venv
  cannot take conda packages — recorded as the reason when it happens).
- The curated system-dependency table is small (7 entries) and is a heuristic.

## 8. Reproduce

```bash
python evaluation/run_eval.py --failures-csv evaluation/samples/lever_misses.csv --sample 0 --seed 42 \
    --in-scope-confirmed-only --tag my_run --runtime docker --workers 2 --levers all     # or --levers none / a comma list
python evaluation/analyze_results.py evaluation/results/my_run.jsonl
```

Results: `lever_base2.jsonl`, `lever_all2.jsonl` (the paired 9-notebook runs); `abl_h5n1_*.jsonl`, `loo_*.jsonl`, `loo2_*.jsonl`
(h5n1 attribution). Earlier `lever_base.jsonl` / `lever_all.jsonl` are superseded (before the conda-activation fix).
Code: `repair_tool/levers.py`, `versions.py`, `condafallback.py`, `condaenv.py`, `pyladder.py`, `agent.py`, `agent_tools.py`,
`runtime.py`, `container_rpc.py`. Tests: `tests/test_levers.py` (+ the existing suite).
