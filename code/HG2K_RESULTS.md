# HG2.9K secondary evaluation — the tool on 400 seeded gists

Task: `TASK_hg2k_evaluation.md`. Dataset: the hard subset of Gistable (HG2.9K, 2,891 single-file gists that still fail with
an ImportError after Gistable's naive resolution). Run: Docker `linux/amd64`, kiste `Qwen3.6-35B-A3B-MLX-8bit`, seed 42,
the six levers, **a seeded random sample of 400 of the 2,891 gists**, one machine.
Nothing is committed or pushed. This file is the draft of the secondary-evaluation section of Chapter 6.

## 0. In plain words

- On a seeded sample of 400 gists, the tool makes **77 gists run to the end (19.2 %)**. Counted the way DockerizeMe and
  PyEGo count ("no longer fails with an ImportError", other later errors allowed), it resolves **132 (33.0 %)**
  (95 % interval 28.6–37.8 %).
- That puts it **at the level of DockerizeMe (30.9 %) and clearly below PyEGo (46.1 %), ReadPyE (47.2 %, as measured by
  PLLM) and PLLM (about 50 % per run, 54.8 % over 10 runs)**. It is a mid-table result (above DockerizeMe and pipreqs, below the three newer
  tools), not a record, and the report says so. The thesis never claimed a record (Chapter 1).
- The biggest single reason is the language version. **36 % of the sample (144 of 400) is Python-2 code** (does not parse
  under Python 3). The tool targets Python 3: on those it resolves 13.9 %, on the Python-3 part 43.8 %. The comparison
  tools run an interpreter ladder that includes 2.7.
- Many of the remaining failures are not package problems in the pip sense: the missing module belongs to an application
  that embeds Python (Sublime Text, IDA, Maya, Blender, Pythonista, PyQt4, PyGTK) or is a Python-2 standard-library name.
- Cost: about 11.8 model calls and 38 000 tokens per gist on average (15.1 M tokens for the 400); about 128 s per gist.
- Things to verify before this goes into the thesis are listed in §10.

## 1. Dataset and subset

| Item | Value |
|---|---|
| Dataset | HG2.9K: `hard-gists/` of the DockerizeMe artifact, <https://github.com/dockerizeme/dockerizeme> |
| Source commit | `408f3fa3d36542d8fc1236ba1cac804de6f14b0c` (shallow clone, kept outside the repository in `../datasets/dockerizeme`) |
| Gists | **2,891** (one `snippet.py` each); matches the count in DockerizeMe, PyEGo and PLLM |
| Fingerprint | SHA-256 over gist ids and per-file hashes: `f83860a8b577ae4c324715b3af6828e63acbdf56bffc94fe2ab89d350cab2d06` |
| Prepared list | `evaluation/samples/hg2k_all.csv` (+ `hg2k_all.meta.json`), built by `evaluation/hg2k_prepare.py` |
| Python 2 flag | static: the file does not parse under the runtime image's Python 3.11.17. **1,017 of 2,891 (35.2 %)**; in the sample 144 of 400 (36.0 %) |
| Sample | **400 gists** = the first 400 of the 2,891 ids after `random.Random(42).shuffle(sorted ids)` |

The gists were treated as untrusted input: the dataset sits in its own directory outside the repository, is mounted read-only
for the parse, is copied (never modified) into each run, and every gist is executed only inside a Docker container.

The sample is **prefix-stable**: a sample of N is the first N of any larger sample with the same seed. The 400 can therefore be
extended to more gists later without changing them.

## 2. How a gist becomes a target

Each gist is one single-file "repository" for the existing harness: `repository = hg2k/<gist id>`,
`clone_url = local:hard-gists/<gist id>`, `notebook_path = snippet.py`. The harness copies it and starts one Docker container
per gist. The agent loop and the model calls run on the host; the actions run in the container. Changes made to the harness:
the `local:` source (`--source-root`), and a `repo_seconds` field on every row. Both are covered by `tests/test_hg2k.py`.

## 3. Run configuration

| Setting | Value |
|---|---|
| Levers on | `downgrade, conda_fallback, system_deps, interpreter_down, bounded_fallback, code_forward` (the six-lever configuration of the main evaluation) |
| Levers off | `era_resolve`, `era_date`, `era_strict` (true-era). **Off because a gist carries no commit date or git history to anchor an era** |
| Runtime | Docker, `linux/amd64`, 3 workers, one container per gist |
| Model | `Qwen3.6-35B-A3B-MLX-8bit` through the kiste OpenAI-compatible endpoint |
| Budget | 10 minutes per gist × container scale 3.0; the tool's own run of the target limited to 60 s; at most 30 agent steps; stop after 3 steps without progress |
| Sample | 400, seed 42, tag `hg2k_main` |
| Execution | three segments (2026-11-15 15:12 UTC, 2026-11-16 14:47, 2026-11-17 11:22) resumed from the checkpoint; at each pause the up-to-three gists in flight were discarded and run again from scratch; apart from those and §4, no gist was run twice |

**Reproduction / KG re-run.** Same command, a new tag, the KG-enabled tool:

```bash
SIX="downgrade,conda_fallback,system_deps,interpreter_down,bounded_fallback,code_forward"
../.venv/bin/python evaluation/run_eval.py --failures-csv evaluation/samples/hg2k_all.csv \
  --source-root ../datasets/dockerizeme --sample 400 --seed 42 --tag hg2k_main \
  --runtime docker --workers 3 --levers "$SIX"
```

Use a different `--tag` for another tool. For more gists, raise `--sample` (prefix-stable) and, to extend this run, keep the
tag. Analysis: `python evaluation/analyze_hg2k.py evaluation/results/hg2k_main.jsonl evaluation/results/hg2k_retry.jsonl`
(the later file overrides the earlier row of the same gist).

## 4. What was run, and what was not

398 of the 400 gists were attempted by the tool; **2 were not run** and are counted as failures in the denominator, not
dropped.

- Two gists stopped with a transient LLM response ("no choices"). They were re-run once under the tag `hg2k_retry`; the
  retry replaces the failed row.
- Two gists stopped with `Prompt too long` (about 364 000 and 447 000 tokens against the server's context window): their
  output flooded the model's context. This is deterministic, so they were not retried. They are `not_run`.
- No gist was missing from the dataset and no container failed to start.

## 5. Definition of success, and where it differs from the other tools

Three definitions are computed on the **same run**, always with the whole sample (400) as denominator, with Wilson 95 %
intervals.

| Definition | Meaning |
|---|---|
| **strict** | the harness's `fixed`: the gist runs to exit code 0 within the time limit |
| **import-resolved** (DockerizeMe / PyEGo style) | the gist no longer fails with an import problem: later runtime errors, and a timeout, count as resolved; a syntax error never counts (the imports were never reached) |
| **PLLM-style** | import-resolved and no `AttributeError` |

Differences from the published tools (these matter for §8):

1. **Interpreter.** DockerizeMe ran a clean Python 2.7.14; PyEGo infers 2.7 for 528 of its 1,334 successes; PLLM infers the version with the model and validates it against the neighbouring
   versions (arXiv:2501.16191v2, Sections III-G and IV-C; its handling of Python 2.7 is stated only in the threats-to-validity section, which is not cited here). This tool floors its interpreter ladder at 3.6 and uses 3.11 by default, so Python-2 code is largely out of reach
   except by editing the code.
2. **Code edits.** The comparison tools never edit the gist. This tool may (`edit_code`). Every result is therefore also
   reported "environment only" (no edit). Of the 77 strict fixes, 26 involved an edit.
3. **Stricter on `AttributeError`.** A final error that the tool itself labels a dependency failure (for example a removed
   library attribute) counts as *unresolved* under both lenient definitions, which is why they give the same number. The
   published tools only look at `ImportError`. The literal "no ImportError/ModuleNotFoundError left, syntax errors never
   count" rate on the same run is **144 of 400 (36.0 %, 95 % interval 31.4–40.8 %)**; this is the closest to DockerizeMe's
   and PyEGo's wording and is given as a sensitivity figure.
4. **Timeouts.** A gist that is killed after 60 s but passed its imports is counted as resolved (5 gists), as the other
   tools' "an error other than ImportError" would.
5. **SyntaxError.** PLLM counts a final `SyntaxError` as failure; DockerizeMe's wording would not. Here it always fails, so that
   Python-2 files cannot inflate the lenient rates.

## 6. Results

### 6.1 Rates

| Criterion | Rate | of which with no edit to the gist's code |
|---|---|---|
| strict (exit 0) | **19.2 %** (77/400; 95 % CI 15.7–23.4 %) | 51 (12.8 %) |
| import-resolved | **33.0 %** (132/400; 95 % CI 28.6–37.8 %) | 72 (18.0 %) |
| PLLM-style | 33.0 % (132/400; 95 % CI 28.6–37.8 %) | 72 (18.0 %) |
| literal ImportError-only (sensitivity, §5.3) | 36.0 % (144/400; 95 % CI 31.4–40.8 %) | not computed |

### 6.2 Outcome buckets (400 gists)

| Bucket | Gists | Share |
|---|---|---|
| ran as is (no action needed) | 7 | 1.8 % |
| fixed, environment only | 44 | 11.0 % |
| fixed, with a code edit | 26 | 6.5 % |
| **fixed in total** | **77** | **19.2 %** |
| unfixed: an import is still failing | 145 | 36.2 % |
| unfixed: syntax error | 99 | 24.8 % |
| unfixed: other runtime error | 70 | 17.5 % |
| unfixed: runs, but exceeds the time limit | 5 | 1.2 % |
| unfixed: final state not observed | 2 | 0.5 % |
| not run (context overflow) | 2 | 0.5 % |

The "ran as is" 7 are gists that already run in the tool's Python 3.11 container: HG2.9K was built against Python 2.7 and a
naive resolver.

### 6.3 Python 2 versus Python 3 (static flag)

| Stratum | Gists | strict | import-resolved | LLM calls |
|---|---|---|---|---|
| parses as Python 3 | 256 | 27.3 % (70/256; CI 22.2–33.1 %) | 43.8 % (112/256; CI 37.8–49.9 %) | 3,018 |
| Python-2 syntax | 144 | 4.9 % (7/144; CI 2.4–9.7 %) | 13.9 % (20/144; CI 9.2–20.5 %) | 1,705 |

Python-2 gists are not entirely unreachable: all 7 strict fixes among them needed `edit_code` (the agent rewrote `print` statements
and similar) and some also `lower_python`. The agent fixes one statement per step, so a file with many Python-2 constructs
stalls (90 of the 144 end on a syntax error). The flag is static and approximate (a file that parses may still be Python 2 in
behaviour).

### 6.4 What the unfixed gists ended on

| Final exception | Gists |
|---|---|
| `ModuleNotFoundError` | 113 |
| `SyntaxError` | 88 |
| unclassified code error | 20 |
| `ImportError` | 20 |
| `NameError` | 17 |
| `AttributeError` | 11 |
| `FileNotFoundError` | 10 |
| `TabError` / `IndentationError` | 8 / 3 |
| `HTTPError` | 6 |
| `IndexError` | 5 |
| time limit | 5 |
| other (11 single cases) | 11 |
| not observed | 2 |

Among the 114 gists that still end on a missing module, 83 modules are distinct. The most frequent are `PyQt4` (5),
`sublime_plugin` (4), `urllib2`, `PySide`, `tensorflow`, `objc`, `idaapi`, `maya`, `bpy` (3 each), and `pygtk`, `photos`,
`sublime`, `idc`, `Foundation`, `machine`, `sets`, `StringIO`, `gi` (2 each). By my reading of the names, most belong to an
application that embeds Python (Sublime Text, IDA, Maya, Blender, Pythonista, macOS PyObjC), to an old GUI toolkit, or are
Python-2 standard-library names. This classification is by name only and was not checked gist by gist.

### 6.5 Provenance and confidence of the fixes (70 gists fixed by the tool, "ran as is" excluded)

| | |
|---|---|
| Lowest confidence over each gist's fix actions | high 26, medium-high 2, medium 24, low 17, none 1 |
| Grounding of the 162 individual fix actions | registry-grounded 106 (65 %), model-proposed 56 (35 %) |
| Gists whose every action was registry-grounded | 41 of 70 (59 %) |
| Tools used | `install_package` 67, `edit_code` 26, `downgrade_search` 4, `lower_python` 2 |
| Levers that produced a fix | `downgrade` 4, `interpreter_down` 2 |

The lowest-confidence class (17) is almost entirely gists that needed a code edit: an edit is model-proposed and, here, was
accepted only because the re-run succeeded.

### 6.6 Cost

| | Total | Per gist (mean / median / max) |
|---|---|---|
| Model calls | 4,723 | 11.8 / 10 / 30 |
| Tokens | 15.1 M | 37.7 k / 27.6 k / 340 k |
| Seconds (container + repair) | 51,343 (14.3 h of worker time) | 128 / 51 / 1,987 |

A gist the tool fixes costs 7.9 calls on average; one it gives up on costs 12.9. 315 of 400 stopped because three consecutive
steps made no progress, 70 because the gist ran, 2 at the step cap.

## 7. Scope: what HG2.9K does and does not test

HG2.9K tests the **core dependency-repair capability on single-file gists**. It does not exercise repository scale (requirement
files, `setup.py`, build steps) or notebooks, and it has no era to anchor on, so the era levers cannot apply. The GigaScience
notebooks remain the primary evaluation for those.

The scope profile is also different. These gists were curated because they fail on an import, so in this dataset almost
every failure is in principle in scope; yet the tool ends on a non-import cause for 24.8 % (syntax) plus 17.5 % (other
runtime) of them once the imports are resolved. In the GigaScience pilot, by contrast, roughly two thirds of the
examined failures were not package problems at all. The "two thirds out of scope" finding is therefore a property of
scientific notebooks, not of dependency repair in general. That contrast is itself a result.

## 8. Comparison with prior work

All prior numbers are on the same 2,891 gists and come from the papers; none was re-run here.

| Tool | Fixed | Rate | Source and criterion |
|---|---|---|---|
| DockerizeMe (own paper) | 892 / 2,891 | 30.85 % | Horton & Parnin, ICSE 2019, Sec. VII-B1: "an additional 892 gists (31 %) executed without experiencing Python's ImportError"; clean Python 2.7.14; installation failures ignored |
| DockerizeMe (PyEGo's re-run) | 888 / 2,891 | 30.72 % | Ye et al., ICSE 2022, Table 4; Python 2.7; 888 not 892 because some packages left PyPI |
| pipreqs (PyEGo's run) | 297 / 2,891 | 10.27 % | Ye et al., ICSE 2022, Table 4 |
| PyEGo (own paper) | 1,334 / 2,891 | 46.14 % | Ye et al., ICSE 2022, Sec. 5.2 / Table 4; "no longer experiences ImportErrors"; Python 2.7 for 528, 3.9 for 765, 3.8 for 41 of the 1,334 |
| PyEGo (PLLM's re-run) | 1,302 / 2,891 | 45.04 % | Bartlett et al., arXiv:2501.16191v2, Table III |
| ReadPyE (as measured by PLLM) | 1,365 / 2,891 | 47.22 % | same table; KG0, Python modules only. **ReadPyE's own figure: `TODO verify`** (Cheng et al., TSE 2024 was not accessible) |
| PLLM, 10 runs aggregated | 1,583 / 2,891 | 54.76 % | same table; Gemma2-9B with RAG; "executed PLLM 10 times, aggregating the data"; success = no ImportError / ModuleNotFoundError / AttributeError / SyntaxError |
| PLLM, one run | about 1,440 / 2,891 | about 49.8 % | Bartlett et al., Sec. V-A, "~1,440 fixes" (approximate, as stated by the authors) |
| **This tool**, import-resolved | 132 / 400 | **33.0 %** (CI 28.6–37.8 %) | this run, random sample |
| **This tool**, literal ImportError-only | 144 / 400 | 36.0 % (CI 31.4–40.8 %) | this run, sensitivity |
| **This tool**, strict (exit 0) | 77 / 400 | **19.2 %** (CI 15.7–23.4 %) | this run |
| **This tool**, import-resolved, no code edit | 72 / 400 | 18.0 % | this run |

**Reading.**

- On the DockerizeMe/PyEGo definition the tool is **level with DockerizeMe** (33.0 % against 30.9 %; the published figure lies
  inside this tool's interval, so the difference is not demonstrated) and **13 to 22 points below PyEGo, ReadPyE and PLLM**. It does not beat the state of the art on this
  dataset and no such claim should be made.
- The Python-version difference explains a large part of the gap, and is an indicative, not a controlled, comparison:
  PyEGo's successes under Python 3.8/3.9 are (765 + 41) / 2,891 = **27.9 %** of all gists, and this tool's import-resolved
  successes on the gists that parse as Python 3 are 112 / 400 = **28.0 %** of the sample. On the Python-3 part the two are close;
  the rest of PyEGo's lead comes from Python-2 gists (528 successes under 2.7), where the tool resolves 13.9 %. This is
  an observation from published and measured numbers of different kinds, and it should be presented as such.
- The language model is a 35B open-weights model, the tool has a 10-minute budget per gist and runs one agent loop per
  gist, with no corpus of earlier fixes. The baselines were built and tuned with this dataset in view (DockerizeMe created
  it). These are hypotheses for the gap, not measured causes.

**Comparability caveats (state all of them in the chapter).**

1. The baselines ran on all 2,891 gists, the tool on a seeded random 400. A sample proportion has sampling error (the
   intervals above); a different 400 would give a different figure.
2. "Executable" is defined differently: DockerizeMe emits a Dockerfile and was judged in a clean Python 2.7.14 image; PyEGo
   and PLLM judge in an environment they build; this tool builds a virtual environment inside a Python 3.11 container and may
   edit the code. The lenient rates are the closest match, the strict rate the most demanding.
3. The tools were run at different times, with different package-index state. Packages have since been removed from PyPI
   (DockerizeMe lost 4 gists to this between its paper and PyEGo's re-run), which lowers any later run.
4. PLLM's best figure is the union of ten stochastic runs; a single run gives about 1,440. This tool's figure is one run
   with one seed.
5. The tool has non-deterministic parts (the model, the network); one run on one machine does not show run-to-run spread.
   The GigaScience experiments showed such flipping for single notebooks.

## 9. Honest reading

- **The result is modest and should be presented as modest.** About one in five gists runs to the end; one in three has its
  imports resolved. This is on par with the oldest of the four baselines and below the three newer ones.
- **It is not a failure of the transparency idea.** The thesis's claim is about what the repair report says, not about a
  record success rate (Chapter 1). Here 65 % of the fix actions are registry-grounded and the report labels the 35 %
  that were the model's proposals; that, not the rate, is the point of this tool.
- **The ceiling of this design on this dataset is visible.** A third of the gists are Python 2, and about 25 % end on a
  syntax error that a one-statement-at-a-time edit loop cannot clear. A deterministic Python-2-to-3 step (such as running
  `2to3` as a bounded action, labelled model-free and unverified until the re-run) is the obvious next lever. It is not built
  and should be called future work, not implied.
- **Many failures are legitimately out of scope.** Modules that exist only inside Sublime Text, IDA, Maya, Blender or
  Pythonista cannot be installed from PyPI by any tool; the lenient rates of the baselines treat some of these as successes
  only because a later error hides them.
- **The KG experiment has a clean baseline.** This same 400, seed and command give the "ours" line for "ours vs PyEGo vs
  ours+KG".

## 10. For the author: what to verify, and what is still `TODO verify`

Numbers checked against the papers during this task: DockerizeMe 892 (31 %) (Sec. VII-B1); PyEGo 1,334 (46.14 %), pipreqs
297, DockerizeMe 888 (Table 4 and Sec. 5.2); PLLM 1,583 / ReadPyE 1,365 / PyEGo 1,302 (Table III), the 10-run aggregation
and the "~1,440" single run.

Still open:

1. **ReadPyE's own HG2.9K figure.** `TODO verify` against Cheng, Hu and Ma, TSE 50(2), 2024 (DOI 10.1109/TSE.2023.3346474).
   The IEEE page was not retrievable; only PLLM's measurement of ReadPyE is given, and labelled as such.
2. **PLLM's venue.** The paper (arXiv:2501.16191v2) does not state one; the existing `references.bib` entry already carries a
   `TODO verify` for the workshop claim.
3. **PLLM Sec. VI** (threats to validity; "unresolvable due to module deprecation", "x86 Docker Linux only") has not been read
   in full by me; do not cite it before reading the section. (2026-11-17: an earlier version of §5 gave PLLM's interpreter range as
   2.7 to 3.7; that rested on this section and has been replaced by what Sections III-G and IV-C support.)
4. Optional cross-check: the PyEGo hard-gist results repository (<https://github.com/PyEGo/exp-gist>) and PLLM's replication package
   (Figshare DOI 10.6084/m9.figshare.29204693) were not opened.
5. The reading of the missing-module names (§6.4) is by name only.
6. The Python-2 flag is static (parse under 3.11) and approximate.
7. `.github/workflows/tests.yml` lists test modules explicitly and does not yet include `tests.test_hg2k`; add it if the
   author wants CI to run the new tests.

## 11. Files

| File | Content |
|---|---|
| `evaluation/hg2k_prepare.py` | builds the list from the dataset directory (parse in the runtime image) |
| `evaluation/samples/hg2k_all.csv`, `hg2k_all.meta.json` | the 2,891-gist list and its fingerprint |
| `evaluation/run_eval.py` | `local:` sources and `--source-root`; `repo_seconds` per row |
| `evaluation/analyze_hg2k.py` | success definitions, buckets, strata, provenance, cost, comparison table |
| `tests/test_hg2k.py` | 17 offline tests |
| `evaluation/results/hg2k_main.*`, `hg2k_retry.*` | raw rows, checkpoint, metadata of the run (gitignored); `hg2k_main.hg2k_summary.json` is the analysis output |
