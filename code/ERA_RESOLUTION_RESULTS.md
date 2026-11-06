# Era-consistent resolution — the 7th lever, measured

Task: `TASK_era_consistent_resolution.md`. New lever `era_resolve` (flag `--levers era_resolve`, **default off**).
Runs: Docker `linux/amd64`, same model (kiste Qwen3.6-35B), seed 42, same 9 package-problem notebooks, same day.
Nothing is committed or pushed.

## 0. In plain words

- I built the lever: every pip install is resolved **as of the repository's last-commit date**
  (`uv pip install --exclude-newer <date>`), so the packages and their transitive dependencies come from one era.
- **Result: no demonstrable gain.** With the lever, 2 of the 9 notebooks were fixed; without it (the same six other
  levers), 1 of 9. The whole difference is one notebook, `h5n1-cambodia`, and that notebook flips between fixed and
  not fixed from run to run **with or without** the lever: repeated 3 more times each, it was fixed 2 of 4 times
  with the lever off and 2 of 4 times with it on (§3). So the one-notebook difference is run-to-run noise, not an effect.
- The two "version cascade" notebooks the task targeted, `MaaSSim` and `matrix_paper`, **were not fixed**. The lever
  changed which versions were installed and where the failure ended, but it did not clear the cascade (§2).
- Why it could not help much, found by looking at the data: (a) 3 of the 9 repos have a last-commit date in 2023–2026,
  far later than the notebook's era, so "as of that date" is already almost "today"; (b) the agent's own explicit
  version requests are often newer than the repo date, and then the resolver cannot satisfy them (§4).
- Cost: +4.7% model calls (171 → 179), +9 minutes of wall time in the paired runs (36 → 45 min; the two runs overlapped
  other work, so treat the time as indicative).

## 1. What was built

| Where it applies | Where it does not (recorded in every result row) |
|---|---|
| declared requirements / `setup.py` / `pyproject` / `environment.yml` pip entries | conda / micromamba installs (`conda_fallback`, `system_deps`): conda has no clean date exclusion → reason `conda_install` |
| the agent's reactive `install_package` | a repo with no git date → reason `no_repo_date` (none occurred here: all 9 had one) |
| the version search of `downgrade_search`, and the reinstall after `lower_python` | pins that conda itself installed when it built the environment from a recipe |

If uv finds no consistent set for the date, the install **falls back to plain pip** and the row says so (`mechanism: "pip (fallback)"`,
the resolver's message in `era_failure_tail`), so the lever can never make an install impossible that plain pip could do.
Code: `repair_tool/era.py`, `apply.py` (`apply_ex`), `repo.py`, `agent_tools.py`, `container_rpc.py`, `runtime.py`, `pyladder.py`;
rows: `era_cutoff_date`, `era_resolution_setup`, `era_resolution_agent`, and `era_resolution` on every install step.
`--levers all` now includes `era_resolve`; the six-lever set earlier results used is `levers.SIX_LEVERS`.
Tests: `tests/test_era.py` (12, offline). Full suite: 584 tests OK (572 before this lever).

## 2. Before → after, per notebook (9 package problems)

"Off" = the six earlier levers (run `era_off6`); "on" = the six plus `era_resolve` (run `era_on7`). One run each.

| Notebook | Repo date | **Off** (six levers) | **On** (+ era_resolve) | Cleared the cascade? |
|---|---|---|---|---|
| modulome_mtb | 2021-11-26 | **fixed** (4 steps) | **fixed** (4 steps) | n/a — no cascade; no regression |
| h5n1-cambodia | 2019-11-26 | not fixed (`RInterpreterError`) | **fixed** (16 steps) | **no evidence** — see §3 |
| MaaSSim | 2023-02-27 | not fixed (`numpy … no attribute 'warnings'`) | not fixed (step cap; `Series … no attribute 'rides'`) | **No.** Different versions were installed (`osmnx` 1.3.0 at the repo date vs 2.1.1) and the run reached the *third* layer of the known cascade (`Series.rides`), but it did not pass |
| matrix_paper | 2021-01-18 | not fixed (step cap; `cooler.tools` missing) | not fixed (step cap; `numpy.linalg … '__all__'`) | **No.** `cooltools==0.3.2` was installed via conda (not date-constrained); the earlier `pandas.is_categorical` error does not appear in either run's trace, and a different numpy/scipy conflict ended the run |
| interpretable-generative | 2021-07-05 | not fixed (training cell timed out at 180 s) | not fixed (step cap; `pandas … remove_na`) | No — the off run only failed on compute time; the on run spent its 30 steps on a `h5py`/`tables` version loop with 5 resolver fallbacks |
| grp_paper | 2021-06-14 | not fixed (`abutils` missing) | not fixed (`abutils` missing) | no change — not on PyPI-era packages (needs conda) |
| TeachOpenCADD | 2026-10-26 | not fixed (`opencadd` missing) | not fixed (`opencadd` missing) | no change — repo date is "today", the date constraint is empty |
| SAMPL8 | 2026-05-21 | not fixed (`openeye`) | not fixed (`openeye`) | no change — paid licence, the one fair "cannot" |
| ProteinUnfolding2D | 2022-11-22 | not fixed (`NoneType … norm2temp`) | not fixed (same) | no change — a code/data problem, not a package one |

| | fixed (9 package problems) | **in-scope (13 = 9 + the 4 shell-command notebooks, not re-run)** |
|---|---|---|
| Earlier six-lever run (`lever_all2`, 22 Oct) | 2 / 9 | 2 / 13 (15.4%) |
| Six levers today (`era_off6`) | 1 / 9 | 1 / 13 (7.7%) |
| **Six levers + `era_resolve` (`era_on7`)** | **2 / 9** | **2 / 13 (15.4%)** |

The new count equals the "current 2/13", **but** the same configuration without the lever also scored 1/13 today and 2/13 on
22 October. A one-notebook swing is within what this tool does to *itself* between identical runs. **This is not a gain from the lever.**

## 3. h5n1 — the only notebook that differs, and why it does not show an effect

The earlier attribution (`LEVERS_RESULTS.md` §3) fixed h5n1 with all six levers in 3 of 3 runs. Today the same six levers fixed it
0 of 1 in the paired run. To see whether the lever matters, I repeated it three more times per configuration (`h5_off_r1-3`, `h5_on_r1-3`, one notebook each, all valid runs):

| Run | Six levers (off) | Six + `era_resolve` (on) |
|---|---|---|
| paired 9-notebook run | not fixed | **fixed** |
| repeat 1 | **fixed** | **fixed** |
| repeat 2 | not fixed (step cap, `RInterpreterError`) | not fixed (`RInterpreterError`) |
| repeat 3 | **fixed** | not fixed (`DataFrame.append` removed) |
| **Total** | **2 / 4** | **2 / 4** |

Same rate with and without the lever. In the successful "on" run the chain is the known one — `system_deps` (R + `rpy2`, via
**conda, i.e. not date-constrained**) → `downgrade_search` (`rpy2` 3.5.11 → 3.0.5) → `edit_code` — and none of the steps that fixed it was made by era resolution.

## 4. What the lever did and why it was weak (all 47 agent installs of the paired run)

| | count |
|---|---|
| installs resolved with `uv --exclude-newer` | 34 (72%) |
| fell back to plain pip (no consistent set for the date) | 9 (19%) |
| not applicable (conda) | 4 (9%) |

- **Most fallbacks are the agent asking for something newer than the repo date**, e.g. `matplotlib>=3.6`, `numpy>=1.23,<2.0`,
  `numba>=0.56` in a 2021 repository, or `h5py==3.6.0` (published Nov 2021, after the repo's July 2021 date). The resolver
  refuses, and the fallback then installs the modern version anyway — **exactly the packages the lever was meant to hold back.**
  The fallback keeps the lever from making installs impossible, but it also hides the lever where the model chooses modern versions.
  A stricter variant (refuse and tell the model "no such release existed by <date>") is a possible follow-up; not built.
- `matrix_paper`: `matplotlib_inline==0.1.4/0.1.5/0.1.6` fell back three times (these releases date from Aug 2022; the repo is Jan 2021),
  and the key old package `cooltools==0.3.2` came from conda, which the lever cannot constrain.
- **The date is the weak point.** The lever uses the repository's *last commit*. For `MaaSSim` (2023-02), `TeachOpenCADD` (2026-10) and
  `SAMPL8` (2026-05) that is years after the notebooks were written (MaaSSim's first commit is 2020-09), so "as of that date"
  allows almost everything. At 2023-02 the resolver gave `osmnx` 1.3.0, and the downgrade search shows 1.2.3 still gave the same error as 1.3.0 (it only changed at 1.1.2), so the date window did not reach back to a working `osmnx`. The notebook's *own* era
  (not recorded in a clone: shallow clones keep one commit) would be a better date; I did not invent one.
- 7 of the 9 environments were built by conda/micromamba (old-Python recipes); packages conda installed are outside the lever (`not_applied`).
- Only 3 of the 9 repos declare pip dependencies (`MaaSSim`, `TeachOpenCADD`, `modulome_mtb`), so for the others the lever acts only on
  the agent's one-at-a-time installs — each constrained by date, but **not jointly re-resolved** with what was installed earlier.
  A joint re-resolution of the whole environment is the "whole environment in one go" ideal; this lever approximates it by capping
  every install at one date, which keeps transitive dependencies from being newer than that date but does not by itself reconcile conflicts.

## 5. Costs and new failure modes

| | six levers (off) | + era_resolve (on) |
|---|---|---|
| Model calls / agent steps | 171 / 171 | 179 / 179 (+4.7%) |
| Tokens | 855k | 786k |
| Wall time (9 notebooks, 2 workers, overlapped runs) | 36.3 min | 45.2 min |
| Stops: no-progress / step cap / fixed | 7 / 1 / 1 | 4 / 3 / 2 |

New failure modes seen: (1) resolver fallback loops — the model re-requests a modern spec, the date constraint refuses it, plain pip
installs it (9 times; `interpretable-generative` 5 times); (2) the agent spends steps on `h5py`/`tables` versions that the date window
cannot provide; (3) the lever's effect is invisible for conda-built packages. The "no solution for this date" failure mode asked for in the
task *does* occur (9 fallbacks), and one case, `opencadd` ("not found in the package registry"), failed identically with plain pip.

## 6. The honest reading (for the Evaluation chapter)

**Even whole-environment era resolution by date does not beat the version cascade on this sample**, and the data say why:
the repository's last-commit date is a poor proxy for the notebook's era; the agent's own modern version requests defeat the cap;
packages that matter most for these notebooks (old compiled bio packages, R) come from conda, which the date mechanism cannot reach;
and the rest of the failures are out of scope. The observed 2/9 vs 1/9 difference is **not** evidence for the lever: the notebook
responsible flips between fixed and not fixed with the lever off as often as with it on (2 of 4 each). A gain of one notebook is not a rate.

What this does and does not show: it shows this implementation (last-commit date, fallback to pip, conda unconstrained) gave no
measurable benefit here. It does **not** show that an era-consistent environment cannot help in general — a better date, stricter
refusal of out-of-window versions, or a joint solve (and conda-side date handling) could behave differently, and the sample is
small (9 notebooks, one model, one seed, single runs apart from h5n1).

## 7. Limits

- 9 notebooks / 13 in-scope; one model; one seed; single runs per cell except h5n1 (4 per configuration); emulated x86_64.
- The paired runs overlapped other work, so wall times are indicative only.
- Not run: the lever on its own against no levers (to separate its effect from the other six). With no effect visible in the combined
  run and h5n1 dominated by noise, I did not spend the ~45 min; it is a reasonable follow-up if wanted.
- The 4 shell-command notebooks and the other 12 sample notebooks were not re-run.
- Rows of the paired runs store the environment-build record with a naming collision (`applied` there is a **count** of constrained declared installs);
  fixed in code for later runs (`declared_installs`) — the h5n1 repeat rows already use the fixed form; `evaluation/compare_runs.py` reads both.
- `era_cutoff_date` is the *last* commit; it can overestimate a notebook's age by years (see §4).

## 8. Reproduce

```bash
SIX=downgrade,conda_fallback,system_deps,interpreter_down,bounded_fallback,code_forward
python evaluation/run_eval.py --failures-csv evaluation/samples/lever_misses.csv --sample 0 --seed 42 \
    --in-scope-confirmed-only --tag my_run --runtime docker --workers 2 --levers "$SIX,era_resolve"   # or "$SIX" for off
python evaluation/compare_runs.py evaluation/results/era_off6.jsonl evaluation/results/era_on7.jsonl
```

Results: `era_off6.jsonl`, `era_on7.jsonl` (paired 9-notebook runs); `h5_off_r1-3.jsonl`, `h5_on_r1-3.jsonl` (h5n1 repeats;
input `evaluation/samples/era_h5n1_only.csv`). New code: `repair_tool/era.py`, `evaluation/compare_runs.py`; tests `tests/test_era.py`.
