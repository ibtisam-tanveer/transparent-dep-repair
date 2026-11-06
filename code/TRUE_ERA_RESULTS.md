# True-era dating + strict refusal — measured

Task: `TASK_true_era_dating.md`, the follow-up to `ERA_RESOLUTION_RESULTS.md`. Two new levers, **both default off**:
`era_date` (date the era from the notebook's true era, not the repo's last commit) and `era_strict` (refuse an
agent install that no release by the era date can satisfy). Runs: Docker `linux/amd64`, kiste Qwen3.6-35B, seed 42,
the same 9 package-problem notebooks. Nothing is committed or pushed.

## 0. In plain words

- **Result: even with a correct date and strict refusal, the version cascade persists.** On the three cascade
  notebooks (`MaaSSim`, `matrix_paper`, `interpretable-generative`) the full configuration fixed **0 of 12** runs;
  plain `era_resolve` also 0 of 12; the six-lever baseline fixed 1 of 12 (and that one fix is questionable, §3).
- On the 9 notebooks: baseline six levers 1/9, + `era_resolve` 2/9, + `era_resolve` + true date 2/9, + true date +
  strict refusal 2/9. The only notebook that differs, `h5n1-cambodia`, is the one already shown to flip with or
  without any era lever (`ERA_RESOLUTION_RESULTS.md` §3). **No gain is demonstrated.**
- **What did change:** `MaaSSim` got further into its notebook with the full configuration (it reached cell 9 of 12
  in 4 of 4 runs, versus 1 of 4 for the baseline and 3 of 4 for plain `era_resolve`) and then failed on a
  `Series … 'rides'` error every time. The cascade is not cleared, but it moves deeper. 4 runs per cell: an indication, not a result.
- **Why the date could not rescue the cascade**, found from the data (§2, §4): for two of the three cascade notebooks the
  true date is the same as the old one (the paper was published *after* the last commit, so the date is capped at the
  last commit), so those two were never really tested with a different date; `matrix_paper` reaches the end of its
  dependency problems and then dies on a variable the notebook never defines plus data files that are not in the repo;
  `interpretable-generative` solves its dependencies and then runs out of the 180 s cell limit.
- **A new failure mode of the paper date:** for `TeachOpenCADD` the 2019 paper date is *older than the notebook as it
  exists today* (last changed 2026-04), so the tool built Python 3.7 and the notebook failed on modern syntax.
- Cost: strict refusal made the agent spend steps on refused installs (18 refusals in the 9 repeat runs; a matrix_paper
  run used 8 of its 30 steps on refusals and ended with `seaborn` still not installed).

## 1. What was built

| Piece | What it does |
|---|---|
| `era_date` (`repair_tool/eradate.py`) | picks a date per notebook in the task's order — **1 paper date** (GigaScience `article.published_date`; accepted/received date when empty) → **2 the notebook file's own last git change** (needs a full clone) → **3 repo first commit** → **4 repo last commit** — and records `era_date_source`, `era_date_grounded` (true for 1 and 2) and every candidate date on each row |
| bound on the date | **an era date is never later than the repo's last commit** (the code cannot postdate its last commit). Where the paper date is later, the date is capped and the row says `era_date_capped_by_last_commit`. This is my addition to the task's order; without it, 4 of the 9 notebooks (`h5n1`, `matrix_paper`, `interpretable-generative`, `modulome_mtb`) would have been dated *later* than before |
| `era_strict` (`repair_tool/era.py`) | before an agent install, checks PyPI release dates: if **no release satisfying the spec existed by the era date** it refuses with an actionable message ("no release of `matplotlib>=3.6` existed by 2021-02-01; the newest as of then was 3.3.4 …"), bounded at 6 refusals per notebook (further ones still refuse, with a limit notice; the modern version is **never** silently installed). With `era_resolve` also on, a resolver "no consistent set" result is a refusal too — no fallback to plain pip |
| inputs | `evaluation/add_paper_dates.py` reads the paper dates from the 2023 database (read-only) into `evaluation/samples/lever_misses_dated.csv`; the harness clones with full history only when `era_date` is on |
| record | per row: `era_date_source`, `era_date_grounded`, `era_date_capped_by_last_commit`, `era_date_candidates`, `era_strict_refusals`; per install: whether it was date-constrained (`era_resolution`), and (from now on) what was refused (`refusal_detail`) |

Tests: `tests/test_true_era.py` (22 tests incl. real git histories); full suite **605 tests OK** (run before one more test was added for the exploratory date ordering; that module passes, 22 tests).
`era_date` also feeds the interpreter inference (the Python chosen "as of that date") and the era-capping of bare
package names — it replaces the date everywhere, so Python versions change with it (§2).

## 2. The dates (full configuration, run `true_C9`)

| Notebook | Date used (source) | Repo last commit (old date) | File last change | File first commit | Python (old → new) |
|---|---|---|---|---|---|
| MaaSSim | **2022-05-25** (paper, accepted) | 2023-02-27 | 2023-02-27 | 2020-10-15 | 3.11 → 3.10 |
| matrix_paper | 2021-01-18 (paper 2021-09-03, **capped**) | 2021-01-18 | 2020-12-22 | 2020-12-22 | 3.9 → 3.9 |
| interpretable-generative | 2021-07-05 (paper 2022-01-06, **capped**) | 2021-07-05 | 2021-07-05 | 2021-05-14 | 3.9 → 3.9 |
| h5n1-cambodia | 2019-11-26 (paper 2022-09-02, **capped**) | 2019-11-26 | 2019-11-14 | 2019-11-14 | 3.8 → 3.8 |
| modulome_mtb | 2021-11-26 (paper 2022-03-21, **capped**) | 2021-11-26 | 2021-07-01 | 2021-07-01 | 3.10 → 3.10 |
| ProteinUnfolding2D | 2022-11-14 (paper) | 2022-11-22 | 2022-11-22 | 2020-12-18 | 3.11 → 3.11 |
| grp_paper | **2020-09-08** (paper) | 2021-06-14 | 2018-07-11 | 2018-07-11 | 3.9 → 3.8 |
| SAMPL8 | **2022-05-26** (paper) | 2026-05-21 | 2021-07-20 | 2021-07-20 | 3.14 → 3.10 |
| TeachOpenCADD | **2019-04-08** (paper) | 2026-10-26 | **2026-04-22** | 2020-12-15 | 3.14 → 3.7 |

**Only four dates actually changed materially** (MaaSSim by 9 months; grp_paper, SAMPL8, TeachOpenCADD by 1–7 years) and none of the last
three is a cascade case. For `matrix_paper` and `interpretable-generative` the "true" date equals the old one, so **the "wrong date"
explanation of `ERA_RESOLUTION_RESULTS.md` could only be tested on MaaSSim.**

## 3. Before → after, 9 notebooks (one run each)

B = six levers (`era_off6`, 30 Oct); A = B + `era_resolve` (`era_on7`, 30 Oct); D = A + `era_date` (`true_D9`+`true_D9b`, 31 Oct);
C = D + `era_strict` (`true_C9`, 31 Oct). B and A are the previous day's runs (same code, new flags off); the same-day matched data is §4.

| Notebook | B | A | D (+ true date) | **C (+ strict)** |
|---|---|---|---|---|
| modulome_mtb | fixed | fixed | fixed | **fixed** |
| h5n1-cambodia | not fixed | fixed | fixed | **fixed** (noisy: §3 of the era report) |
| MaaSSim | not fixed (`numpy.warnings`) | not fixed (`Series.rides`) | not fixed (`Series.rides`) | not fixed (`Series.rides`) |
| matrix_paper | not fixed (step cap; `cooler.tools`) | not fixed (`numpy.linalg`) | not fixed (`seaborn-white` style) | not fixed (`matplotlib.cbook`) |
| interpretable-generative | not fixed (cell timeout 180 s) | not fixed (`remove_na`) | not fixed (cell timeout) | not fixed (cell timeout) |
| grp_paper | not fixed (`abutils`) | same | same | same — needs conda |
| TeachOpenCADD | not fixed (`opencadd`) | same | not fixed (**`SyntaxError`: Python too old**) | same as D |
| SAMPL8 | not fixed (`openeye`) | same | same | same — paid licence |
| ProteinUnfolding2D | not fixed (code/data) | same | same | same |
| **Fixed** | **1/9** | **2/9** | **2/9** | **2/9** |
| **In-scope (of 13)** | 1/13 | 2/13 | 2/13 | **2/13** — no change from the current 2/13 |

Cost, 9 notebooks: agent steps B 171 / A 179 / D 164 / C 164 (D and C did not use more; model calls = steps ± 1); wall time B 36.3 / A 45.2 / C 38.5 min
(D 42.5 + 15.1 min for the three retried). Wall times overlapped other work: indicative only.

## 4. The cascade notebooks, repeated (same day, 3 runs per configuration + the 9-notebook run = 4 runs per cell)

Fixed counts (a fix = the notebook passes in the harness), and how far into the notebook the last failing run got:

| Notebook | B baseline | A + era_resolve | **C + true date + strict** | E: C with the notebook's *first commit* as date (exploratory) |
|---|---|---|---|---|
| MaaSSim (12 cells) | **0/4**; reached cell 9 in 1 of 4 (the rest stop at cell 4) | **0/4**; cell 9 in 3 of 4 | **0/4**; cell 9 in **4 of 4**, always `Series … 'rides'` | **0/3**; stopped at **cell 2** in 3 of 3 (`matplotlib.cbook`; `No module named 'pandas'` ×2) |
| matrix_paper (7 cells) | **0/4**; past the imports (cell 3) in 1 of 4 | **0/4**; cell 3 in 2 of 4 | **0/4**; cell 3 in 1 of 4 | **0/3**; cell 2 in 3 of 3 |
| interpretable-generative (30 cells) | **1/4** (see below) | **0/4** | **0/4** | **0/2** (a third run lost to a model-timeout and discarded) |
| **all three** | **1/12** | **0/12** | **0/12** | **0/8** |

- **MaaSSim:** the full configuration consistently reaches cell 9 of 12 and then fails on `Series.rides`; it is **not** fixed. The first-commit
  date (2020-10, the notebook's own era) is *worse*: it never gets past cell 2. A date that is too early also removes packages the
  environment needs, so "earlier is better" is false.
- **matrix_paper:** when its imports finally succeed (cell 3 of 7), it fails with `NameError: name 'cooler_path' is not defined`. I read the notebook:
  cell 2 is `path=cooler_path`, a **parameter that was meant to be injected from outside** (papermill-style), and later cells open `.mcool`
  data files that are not in the repository. **The dependency cascade can be cleared and the notebook still cannot run** — it is
  effectively out of scope for dependency repair. This happened in 4 of 12 runs across B, A and C alike: the date did not make it more likely.
- **interpretable-generative:** every configuration solves the dependencies (`scvi`) and stops at the training cell (cell 15 of 30), which exceeds the 180 s
  cell limit. The **one fix** (B, repeat 2) reduced `n_epochs = 200` to `n_epochs = 5` in the notebook so it finishes in time: the notebook "passes", but it is
  a **different experiment**. I count it as the harness does (fixed), and flag it as **not a faithful reproduction**; without it B is 0/12 too.
- **A new failure mode of `era_strict`:** 18 refused installs in the 9 repeat runs (24 with the 9-notebook run), 15 of them in `matrix_paper`.
  In two runs the agent spent 6–8 of 30 steps being refused and ended with `seaborn` / `multiprocess` not installed. (What exactly was
  refused is not stored in those rows; `refusal_detail` records it from now on.) The bound of 6 refusals did not prevent this — it only stops the advice.

## 5. What the lever did and did not do (honest limits, Part C)

- **conda is still unconstrained.** The packages that decide these notebooks (`cooltools` via conda, R, old compiled bio packages) come from
  conda; the date mechanism cannot reach them. Every row records, per install, whether it was date-constrained; agent installs in the
  `era_resolve` repeats: 55 total, 45 constrained, 7 fell back to plain pip, 3 conda (not applicable). 7 of the 9 environments are conda-built.
- **The date also changes the Python version** (it is inferred "as of that date"): SAMPL8 3.14 → 3.10, TeachOpenCADD 3.14 → 3.7, MaaSSim 3.11 → 3.10,
  grp_paper 3.9 → 3.8. So `era_date` is not a pure "date" change; its effect on a notebook includes a different interpreter.
- **The paper date is not always the right era.** It ignores later maintenance (TeachOpenCADD: notebook changed 2026, dated 2019 → Python 3.7 → `SyntaxError`
  in the notebook as it is today), and it is later than the code when the paper came out after the last commit (4 of 9; handled by the cap).
- This lever only touches the **in-scope** package cases; the roughly two-thirds out-of-scope notebooks are unaffected, and the headline of the thesis does not change.
- **Small and noisy:** 4 runs per cell, one model, one seed. Fixed counts of 0 vs 1 are not evidence in either direction. `h5n1` flips on its own.
- **Validity incident.** My first set of repeats (and three of the date-only notebooks) was invalid: the machine lost its network for part of the time
  (`Could not resolve host: github.com`, model timeouts) while unattended. I discarded those rows and re-ran them under new tags (`c3b_*`, `true_D9b`); one more run of
  the exploratory set lost one notebook to a model timeout and is excluded. Every number above comes from valid runs.
- B and A of §3 are from the previous day; the §4 repeats are same-day.

## 6. The honest reading (for the Evaluation chapter)

**Neither a correct era date nor strict refusal recovers the cascade notebooks.** The diagnosis in `ERA_RESOLUTION_RESULTS.md` (wrong date; the agent
defeating the cap) was partly right and partly not the bottleneck:

1. The date explanation could be tested on one cascade notebook only (MaaSSim), because the other two already had a correct date; there it moved the failure *deeper*
   (cell 4 → cell 9) but did not fix it. A still-earlier date made it worse.
2. Closing the fallback loophole did not help: it turned silent modern installs into 18 refusals that cost the agent steps, with no fix.
3. The decisive blockers are elsewhere: packages from conda (outside the date mechanism), the notebooks' own problems (`matrix_paper`: an undefined parameter and missing data;
   `interpretable-generative`: compute time), and deep transitive conflicts (`MaaSSim`: `Series.rides`).

This supports the thesis's central point rather than weakening it: a large share of "dependency failures" are not repairable by dependency repair, and the
repairable ones need more than a date. It does **not** show that era-consistent environments cannot help in general: a solver that resolves the whole environment
jointly (including conda), a date that tracks the notebook's own content, or longer cell limits could behave differently. These are options for future work, not
something this sample supports.

## 7. Reproduce

```bash
python evaluation/add_paper_dates.py --in evaluation/samples/lever_misses.csv --out evaluation/samples/lever_misses_dated.csv \
    --db ../computational-reproducibility-pmc/computational-reproducibility-pmc/analyses/db.sqlite
SIX=downgrade,conda_fallback,system_deps,interpreter_down,bounded_fallback,code_forward
python evaluation/run_eval.py --failures-csv evaluation/samples/lever_misses_dated.csv --sample 0 --seed 42 --in-scope-confirmed-only \
    --tag my_run --runtime docker --workers 2 --levers "$SIX,era_resolve,era_date,era_strict"
python evaluation/compare_runs.py evaluation/results/era_off6.jsonl evaluation/results/era_on7.jsonl evaluation/results/true_C9.jsonl
# exploratory ordering (notebook's first commit as the date):  REPAIR_TOOL_ERA_DATE_PREFER=file_first
```

Results: `true_C9.jsonl` (full), `true_D9.jsonl` + `true_D9b.jsonl` (date only), `c3b_{B,A,C}_r{1,2,3}.jsonl` (cascade repeats), `c3e_E_r{1,2,3}.jsonl` (exploratory);
inputs `lever_misses_dated.csv`, `era_cascade3_dated.csv`, `era_d9_retry.csv`. Superseded and invalid (network lost): `true_D9.jsonl` rows for three notebooks, `c3_*_r*.jsonl`.
New code: `repair_tool/eradate.py`, `repair_tool/era.py` (strict part), `evaluation/add_paper_dates.py`; tests `tests/test_true_era.py`.
