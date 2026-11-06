# Pilot summary — containerized evaluation on a broad sample

Task: `TASK_next_steps.md` (record why notebooks fail → container runtime → broad sample → run and report).
Model: `Qwen3.6-35B-A3B-MLX-8bit` on kiste. Machine: Apple-Silicon Mac. Container: `linux/amd64` under Rosetta.
**Final runs: `pilot_docker_v4` (container) and `pilot_host_v3` (native).** Earlier runs are superseded (§8).

## 0. In plain words

- I took **26 failing notebooks from 26 different research projects** and asked the tool to repair them.
  25 could be tried (one project would not download).
- The tool's job is to fix **broken software setup** (a missing package, a wrong version). It cannot invent
  missing data or fix someone's code.
- **Result: the tool fixed 1 notebook.** Of the 25, only **9–13** were really a package problem
  (depending on how you count, §3); it fixed 1 of those.
- **Most of the 25 notebooks cannot be fixed by repairing packages at all:** 9 need data files that were never
  published (hard-coded paths like `C:\Users\...` or `/home/bikash/...`), 4 run shell commands needing special
  programs, 3 contain code errors. That is itself a finding: *roughly two thirds of failing notebooks are
  outside what any dependency-repair tool can do.*
- The 8 notebooks the tool failed on **are real misses** (old packages that no longer install or work
  together). They count against the tool; §4 lists them.
- Running the same 26 notebooks natively on the Mac gave nearly the same picture. **No difference caused by the
  container's architecture showed up on this sample** — but the sample did not really test that (§6).

## 1. Headline numbers

| | **Container (`linux/amd64`)** `pilot_docker_v4` | **Native host** `pilot_host_v3` |
|---|---|---|
| Notebooks sampled | 26 | 26 |
| Could be tried (cloned and found) | 25 | 25 |
| **Fixed** | **1** | **0** |
| In scope, genuinely attempted (as run) | 9 → **1/9 (11%)** | 11 → **0/11 (0%)** |
| In scope, attempted (corrected, §3) | 13 → **1/13 (7.7%)** | 11 → **0/11** |
| Out of scope (reason recorded) | 16 as run / 12 corrected | 14 |
| Wall time (26 repos, 2 workers) | 51.9 min | 56.6 min |
| LLM calls / tokens | 299 / 1.18 M | 319 / 3.04 M |

Honest reading: **1 fix out of 25 notebooks overall (4%), 1 out of 9–13 real package problems (8–11%).**
With this few cases (9–13) no rate is statistically meaningful (the exact 95% interval on 1/9 is about 0.3–48%).
For context, classical tools reach ~33–37% on *curated* sets; this sample is random and messy.

**The one fix:** `modulome_mtb` — installed `pymodulon==0.2.1`; chosen from package-registry metadata
(`metadata_grounded`), **verified** by re-running the notebook, **high** confidence, 4 agent steps.

## 2. What was built (the task's four steps)

1. **Why notebooks fail** — every result row records the observed error, a reason code, where the notebook
   stopped and the stuck cell (`runtime_observed_*`); `analyze_results.py` reports it by reason and as a scope finding.
2. **Container runtime** — `--runtime host|docker`, `--platform linux/amd64`, `--container-scale`. The host keeps
   the agent loop and all LLM calls; the container builds the environment and runs every executing action
   (`repair_tool/runtime.py`, `container_rpc.py`, `docker/`; see `docker/README.md`). micromamba supplies old
   Python (exact minor first, uv second); the repo is *copied* in and the edited file copied back; time limits
   are scaled for emulation; the earlier hang fixes work through the container boundary.
3. **Sample** — `evaluation/samples/pilot_sample.{csv,json}` (seed 42, reproducible).
4. **Runs** — below.

**The sample:** 26 notebooks, 26 distinct repositories, one each, verified to still exist at the repo's current
HEAD (2 of 28 candidate repos were skipped as stale). 18 category A (confirmed), 4 C and 4 D. **Deviation from
the task text:** it asked for confirmed A/C/D, but confirmed C and D do not exist in the dataset (0 rows), so C
and D are drawn from the *candidate* tier — the same adjustment already recorded in `PILOT_DECISIONS.md`.

## 3. What fails and why (the scope finding)

Reasons recorded for the 25 reachable notebooks (the reason codes do not depend on any labelling quirk):

| Why it failed | Container | Host | Can a package-repair tool fix it? |
|---|---|---|---|
| **Fixed** | 1 (4%) | 0 | — |
| **Real package/version problem, not fixed** | 8 (32%) | 7 (28%) | **Yes — this is the tool's job** |
| Missing data / network / time limit | 9 (36%) | 12 (48%) | No (host: 10 data + 2 slow cells) |
| Shell command or external program failed (`dx`, `hmmtop`, Miniconda installer, `mkdir`) | 4 (16%) | 4 (16%) | No — not Python packages |
| Other code error (`TypeError`, `NameError`, `KeyError`) | 3 (12%) | 2 (8%) | No — a coding bug |

→ **About 36% (container) / 28% (host) of failures are package problems at all.** The rest are missing data,
outside tools or code bugs. (If the 4 shell-command cases were counted as dependency failures, as the 2023 labels
do for `CalledProcessError`, the package-related share is 52% / 44%.)

**Why the as-run and corrected denominators differ.** The study's taxonomy files `CalledProcessError` under
category D (in scope) but `subprocess.CalledProcessError` under E (out of scope). My in-process executor wrote
the qualified name, a kernel writes the bare one — so the same 4 failing shell notebooks counted as in-scope on
the host and out-of-scope in the container. I fixed the spelling afterwards (test added); I did **not** re-run.
"Corrected" simply moves those 4 rows to in-scope, which the classifier confirms is what the bare spelling gives.

### Per category (container, corrected)

| Category | Sampled | In-scope attempted | Fixed | Out of scope | Not found |
|---|---|---|---|---|---|
| A (confirmed) | 18 | 9 | 1 | 9 | 0 |
| C (candidate) | 4 | 0 | 0 | 3 | 1 |
| D (candidate) | 4 | 4 | 0 | 0 | 0 |

C's three reachable notebooks all ended out of scope (code error, key error, missing data), so **no C
effectiveness can be stated**. D's four are all shell-command failures — in scope by the study's label but not
fixable by installing packages. **No A-vs-C-vs-D effectiveness comparison is possible from this pilot.**

### Accepted-fix grounding and confidence
One accepted fix: `metadata_grounded`, `verified`, confidence `high`. Across all install actions the agent took
(almost all unverified because nothing passed): container 40 `metadata_grounded` + 50 `llm_proposed`; host 54 +
54. Most versions were era-capped (the newest release as of the repo's last commit).

## 4. The 8 package problems the tool did NOT fix (container run)

| Notebook | What it needed | Fixable in principle? |
|---|---|---|
| SAMPL8 | `openeye` — a **paid, licensed** toolkit | No (licence); the one fair "can't" |
| MaaSSim | an **older `osmnx`** — the notebook uses a function newer versions removed | **Yes.** The tool tried `osmnx<2.0`; not old enough |
| interpretable-generative | an **old `scvi`**, which needs older Python | **Yes, in principle.** Python guessed as 3.9; gave up after 6 steps |
| h5n1-cambodia | `rpy2`, which needs the **R language** on the system | Probably — needs a system-level install (conda) |
| grp_paper | `abutils` (old compiled bio package) | Maybe — install kept failing |
| matrix_paper | `cooltools` (old compiled bio package) | Maybe |
| TeachOpenCADD | `opencadd` after 25 steps | Unclear from the stored data |
| ProteinUnfolding2D | `'NoneType' ... 'norm2temp'` | Probably a code/data problem, not a package |

**Patterns (the thesis's limits / future work):** (1) version pins are not old enough (`<2.0` where a 2020 release
was needed); (2) the Python version is a heuristic — newest at the last commit — and was 3.14 twice and 3.9 for the
`scvi` case; (3) the tool stops when the same error repeats (all 8 ended this way); (4) it cannot install
system-level tools such as R unless the repo has an `environment.yml`.

## 5. Interpreter repair

No repository **declared** a Python version, so all 25 choices were era-inferred (ungrounded). Container:
conda/micromamba in 20 cases, baseline in 5; host: uv in 21, baseline in 4. Versions requested were identical in
both runs (3.8 ×6, 3.9 ×5, 3.11 ×5, 3.14 ×4, 3.10 ×2, 3.13 ×2, 3.12 ×1). Fixed after provisioning: 1 of 7 (container).
Limitation: era-inference gives modern Pythons to recently-updated repos regardless of the notebook's real era.

## 6. Container vs host — the measured comparison

26 notebooks compared one by one. **Same outcome bucket in 20 of 26; same bucket and reason in 17.**

The 6 notebooks whose bucket differs:

| Notebook | Container | Host | Why |
|---|---|---|---|
| SYSC4906, pairtools, TheHumanPangenome, Rt_IFO0880 | out of scope | in scope (shell command failed) | **Labelling artefact** (§3) — identical failures |
| modulome_mtb | **fixed** | cell timeout | **Different time limits**: container 180 s per cell (60 s × scale 3), host 60 s. Cannot be called an architecture effect |
| interpretable-generative | package problem | cell timeout | same time-limit difference |

Another 3 notebooks (MaaSSim, matrix_paper, bde_model_methods) land in the same bucket with a slightly different
error text — model/environment variation, not a pattern.

**So after correcting the labelling artefact, buckets agree on 24 of 26, and the other 2 are explained by the
unequal cell limit.** *On this sample I observed no effect attributable to the container's CPU architecture* —
**and I cannot claim there is none**: the container's real advantage (old x86-only wheels such as
`numpy==1.17.3`, `pyferret`) needs a repo that *pins* them. In this sample no repo declared a Python version, the
tool chose modern versions, and both runs got the same versions. The earlier ferretmagic spike
(`INTERPRETER_REPAIR_SUMMARY.md`) is the case that does show the effect. A proper architecture test needs a
sample of repos with old pins.

**Methodological finding (a real result of the container work):** on an Apple-Silicon Mac, Docker's Rosetta
emulation cannot start **any child process from inside a process holding a ZeroMQ context** — reproduced with a
bare `zmq.Context()` + `subprocess.run(["echo"])` for every pyzmq from 23 to 27 (a plain process or a Python
thread is fine). A Jupyter kernel always holds one, so there every `!cmd`, `%%bash`, `subprocess` and even
matplotlib's first import hang until the time limit. The first container pilots recorded these as "timeouts" that
never happen on a real machine (19 of 25 notebooks!). The fix was to run notebooks in the container with a small
in-process executor instead of a kernel (`repair_tool/inproc_runner.py`; tested, including real-container
tests). Anyone running Jupyter-based tooling in `linux/amd64` containers on Apple Silicon will meet this.

## 7. Time, cost, emulation overhead

- **Wall time** (26 repos, 2 workers): container 51.9 min, host 56.6 min — no visible emulation penalty at this
  scale, because model latency and installs dominate.
- **Measured overhead** (same machine, microbenchmarks): CPU-bound Python ~1.8× slower in the container;
  `venv + pip install numpy pandas` ~4.5× slower (32 s vs 7 s). Container environment build: median 26 s, max 308 s.
- **Model cost:** kiste is a university server — no per-token price. Container 299 calls / 1.18 M tokens; host
  319 calls / 3.04 M tokens (the host used ~2.6× the tokens; I did not investigate why).
- **Agent effort:** median 11 steps per attempted notebook; all non-fixed notebooks stopped on "no progress".

## 8. Validity: what went wrong along the way (and what is superseded)

Each item was a bug in my own tooling, found by checking results against ground truth:

| Run | Status | Why |
|---|---|---|
| `pilot_docker_v1`, `v2` | **superseded** | Jupyter-kernel executor under Rosetta → 19 false "timeouts" (§6). v2's recorded stop positions were also wrong (stale cell counts saved in the notebooks; fixed + tested). v2's wall time includes hours of laptop sleep |
| `pilot_docker_v3` | **superseded** | in-process executor lacked `enable_gui`, so `%matplotlib inline` failed in 3 notebooks (fixed + tested); also a too-loose "this module belongs to the repo" rule (see the next paragraph) |
| `pilot_host_v1` | **superseded** | stale-count instrumentation; older classifier |
| **`pilot_docker_v4`, `pilot_host_v3`** | **final** | final code; full suite 504 tests green before the runs |

Other defects fixed during the pilot: a host/container path-mapping bug (caught by the first real smoke run);
transient `git clone` failures (now retried); a loose repo-module match that treated an unrelated
`.../T018/utils/helpers/nglview.py` as the module for `import nglview` and blocked the install (now "importable
from this notebook" only; tests added); a test-isolation flaw; `analyze_results.py` import path. Under
Rosetta a Jupyter kernel with `ipykernel >= 6.29` also never answers its first message (6.20.2 and 6.16.2 do), so the
optional kernel path pins `ipykernel==6.16.2`.

## 9. Honest limits

- 26 notebooks / 26 repos / one model / one seed / one Mac: a pilot, not the thesis evaluation. 9–13 attempted
  package problems cannot support a rate.
- **Final-run caveats not re-run:** (a) the 4 shell notebooks' scope label (§3) — corrected in the tables, not in the
  result files; (b) host cell limit 60 s vs container 180 s (§6). Both only affect the comparison, not the
  tool's own results.
- The "out of scope" decision comes from the last failing run's error text; a fix that would have needed more
  time than the cell limit allows is invisible. No long-limit sensitivity run was done.
- The Python version is era-inferred for all 25 (ungrounded); the 2023 study's recorded version was not used
  (the real tool would not have it).
- `NTF2Analysis` failed to clone in every run (1 of 26 slots lost).
- Nothing is committed to git.

## 10. Files and how to reproduce

Results: `evaluation/results/pilot_docker_v4.jsonl`, `pilot_host_v3.jsonl` (+ `.meta.json`); earlier runs kept but superseded.
Sample: `evaluation/samples/pilot_sample.{csv,json}`; selector `evaluation/select_sample.py`.
Container: `docker/Dockerfile`, `docker/README.md`, `repair_tool/runtime.py`, `container_rpc.py`, `inproc_runner.py`.
Analysis: `evaluation/analyze_results.py`, `evaluation/runtime_reasons.py`.

```bash
python evaluation/run_eval.py --failures-csv evaluation/samples/pilot_sample.csv --sample 0 --seed 42 \
    --in-scope-confirmed-only --tag my_pilot --runtime docker --workers 2     # or --runtime host
python evaluation/analyze_results.py evaluation/results/my_pilot.jsonl
```
