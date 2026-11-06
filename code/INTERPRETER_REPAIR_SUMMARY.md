# Interpreter repair — results summary

Task: `TASK_interpreter_repair.md`. The tool now detects the Python version a repo
declares, provisions it, repairs residual issues on top, and records where the
choice came from. This file is the honest "before" baseline for the container work
(`TASK_containerize.md`).

> **ERRATUM (added after `kiste_smoke_v6`).** Every run below (v2–v5) executed
> notebooks with a kernel manager that silently disabled the per-cell timeout, so
> a slow or hung cell was never stopped (this is also what left a Jupyter kernel
> alive for ~90 minutes and caused the 2700 s worker timeouts). The single fix
> credited to interpreter repair, `WNTRAC_NPI_Index.ipynb`, almost certainly
> depended on that: its first data cell downloads a large Google mobility CSV, and
> with the 60 s cell limit actually enforced it times out and is correctly
> classified out-of-scope (v6). **Treat the v5 "1/17" as a measurement artifact,
> not a repair result.** v6 is the first run with enforced timeouts; compare
> against it, not against v2–v5. See `PREPILOT_FIXES_SUMMARY.md`.

**Setup of every run below:** same 3 repos / 51 labelled notebooks, model
`Qwen3.6-35B-A3B-MLX-8bit` on kiste, seed 42, 2-minute per-file budget, no-progress
limit 3, harness interpreter Python 3.14 on an Apple-Silicon (arm64) Mac.

## 1. Headline

| | v2 | v3 | **v5 (interpreter repair on)** |
|---|---|---|---|
| What changed | stop conditions only | + whole-repo copy, local-module classification | + declared-deps-first, era-capped installs, **interpreter repair**, 900 s clone timeout |
| Fixed | 0 | 0 | **1** |
| Genuine fail (in scope, attempted) | 15 | 10 | 16 |
| Repo-local module (out of scope) | 0 | 0 | 23 |
| Out of scope at run time (category E) | 25 | 4 | 10 |
| Budget exhausted | 11 | 0 | 1 |
| LLM backend unreachable | 0 | 0 | 0 (27 in the first v5 pass, re-run) |
| Interpreter unprovisionable | 0 | 0 | 0 |
| Not discovered (clone failed / hard timeout) | 0 | 37 | 0 |
| **In-scope denominator** | 15 | 10 | **17** |
| **Effectiveness** | 0/15 | 0/10 | **1/17 (5.9%)** |
| LLM calls | 244 | 90 | 410 |

v5 is a merge of two files: wntrac and ferretmagic rows from `kiste_smoke_v5.jsonl`,
and covid rows from the re-run `kiste_smoke_v5_covid.jsonl` (merged file:
`kiste_smoke_v5_merged.jsonl`). The covid rows in the first v5 pass were all
"Connection error / Request timed out" with zero model calls (kiste unreachable);
`analyze_results.py` now reports those as their own excluded bucket instead of
counting them as failures, and covid was simply re-run once kiste was back.

Per repo (v5):

| Repo | Fixed | Genuine fail | Local module | Out of scope (E) | Budget |
|---|---|---|---|---|---|
| IBM/wntrac (6) | **1** | 0 | 0 | 4 | 1 |
| PBrockmann/ipython_ferretmagic (10) | 0 | 10 | 0 | 0 | 0 |
| e-stan/covid_19_analysis (35) | 0 | 6 | 23 | 6 | 0 |

**Read this carefully:** 1/17 is the number that matters, and it is a very small
numerator. The v2→v5 change is not statistically meaningful at n=1, and the three
runs differ in more than interpreter repair (see §3).

## 2. The one fix

`WNTRAC_NPI_Index.ipynb` (IBM/wntrac): fixed in 4 agent steps, `metadata_grounded`,
confidence high. The repo declares Python 3.6 in `code/environment.yml`; uv has no
3.6, so the nearest available 3.8 was provisioned (recorded with provenance:
requested 3.6, actual 3.8, source `environment.yml`, mechanism `uv`).

## 3. Ablation — did interpreter repair cause that fix?

**Not cleanly established. Evidence is consistent but not a controlled result.**

With interpreter repair off (Python 3.14), this notebook was never fixed:

| Run | Outcome for `WNTRAC_NPI_Index.ipynb` |
|---|---|
| v2 (repair off) | attempted, 5 steps, `no_progress`, reclassified out-of-scope (E) |
| v3 (repair off) | never attempted — whole worker hit the 2700 s hard limit |
| ablation, full wntrac repo, `--no-interpreter-repair` | never attempted — same 2700 s hard limit |
| ablation, this notebook alone, `--no-interpreter-repair` | **hung** (a Jupyter kernel stayed alive for ~90 minutes); I killed it. No result row. |
| v5 (repair on, Python 3.8) | **fixed**, 4 steps |

So: three of four repair-off runs never attempted the notebook, one attempted it and
did not fix it, and the controlled single-notebook ablation did not finish. That is
suggestive, not proof. Between v2 and v5 several other things changed too
(declared-deps-first installs, era-capped versions, whole-repo copy), so the fix
cannot be credited to interpreter repair alone from these runs. A clean ablation
needs the hang fixed first (see §6).

## 4. Interpreter provenance (v5, in-scope attempted notebooks, n=17)

- Version declared in the repo's own recipe (grounded): 11
- Version era-inferred from the last-commit date (ungrounded): 6
- A different interpreter than the 3.14 default was provisioned: 17
- Exact version unreachable, nearest available used: 11
- Fixed after interpreter provisioning: 1/17

The 11 substitutions are mostly ferretmagic (declared 3.7, got 3.8): uv has no
CPython 3.7, and on macOS arm64 neither does conda-forge. Substituting a newer
minor is recorded, not hidden.

## 5. ferretBasics.ipynb — before / after

Repo declares `python=3.7`, `numpy=1.17.3`, `pandas=0.25.3`, `pyferret=7.5.0`,
`ferret_datasets=7.4` in `binder/environment.yml`.

| Interpreter | What happened |
|---|---|
| **3.14 (before repair)** | the agent `pip install`ed `pyferret` from PyPI — an unrelated package, install "succeeded" — then failed on `IPython.utils.py3compat` (removed upstream). `edit_code` cannot edit third-party files. Stopped `no_progress`. |
| **3.8 (after repair)** | `pyferret` install fails: it is conda-only and has no osx-arm64 build. The declared `numpy==1.17.3` has only an x86_64 cp38 wheel, and the interpreter is arm64, so it falls back to a source build that fails (`No module named 'distutils.msvccompiler'`). Stopped `no_progress` at 7 steps. **Not fixed.** |

Interpreter repair moved the failure from "wrong interpreter" to "no build exists for
this machine". That is the platform wall, not a repair-quality problem.

## 6. Findings that need follow-up (not fixed here)

1. **Name-collision installs.** In the covid re-run the agent installed an unrelated
   PyPI package `combat==0.3.0` 22 times. The repo has its own `src/combat.py`
   (the notebooks do `sys.path.append("../src/")`). `install_package` should refuse
   or flag a name that matches a repo-local module.
2. **23 covid notebooks excluded as "repo-local module".** The exclusion is defensible
   (not a PyPI dependency), but the real cause is the working directory: notebooks in
   `notebooks/old/` expect to be run from `notebooks/`. This is a path problem the
   tool could in principle repair; right now it is simply out of scope.
3. **Harness hang.** A notebook run left a Jupyter kernel alive ~90 minutes past the
   worker's intended 45-minute limit, so the process timeout did not take effect.
   Kernel cleanup / process-group kill is needed before any long run or clean ablation.
4. **`postBuild` is ignored.** repo2docker runs `binder/postBuild`; we do not.
5. **Per-repo budget counts every notebook in the repo,** not only the labelled ones,
   so a one-notebook run still gets a ~45-minute limit.

## 7. Setup (conda / uv)

- **uv** is a normal dependency in `pyproject.toml` (with `pyyaml` and `packaging`),
  so installing the project into the venv provides it. It fetches CPython builds on
  demand. Oldest it can provide is 3.8.
- **micromamba** is used for `environment.yml` recipes. It is downloaded to
  `code/.tools/bin/micromamba` (from <https://micro.mamba.pm>), git-ignored via
  `.tools/` in both `.gitignore` files. Override with the `REPAIR_TOOL_MICROMAMBA`
  environment variable. The mamba root prefix is `code/.tools/mamba_root`
  (set automatically when the binary lives in `.tools/`).
- Dispatch: `environment.yml` → micromamba; otherwise (or if conda is unavailable or
  the solve fails) → `uv venv --python X.Y`. `fresh=True` is untouched.
- Hard limits: uv 600 s, conda 900 s per provisioning attempt.
- Python 2 and anything below 3.6 is reported as `interpreter_unprovisionable`.

## 8. Platform finding (feeds the container work)

Everything in §5 and the ferretmagic row of §1 is blocked by CPU architecture, not by
repair logic: on an arm64 host, old x86_64-only wheels and conda builds do not exist.
A manual `linux/amd64` container spike (see the containerization report) lifted that
wall: Python 3.7.12, `numpy 1.17.3`, `pandas 0.25.3` and `pyferret` install and
import. It did not yet get the notebook to pass (`pyferret.start()` hangs inside a
Jupyter kernel under Docker's Rosetta layer; cause not isolated).

## 9. Honest limits

- n=51 notebooks over 3 repos; results are a smoke test, not the thesis evaluation.
- The expected realistic ceiling was ~33–37%; we are far below it on this set, mainly
  because of platform blockers and out-of-scope notebooks, not because the denominator
  was inflated.
- Timings are not comparable between runs (other jobs shared the CPU).
- One model, one seed, one machine.
