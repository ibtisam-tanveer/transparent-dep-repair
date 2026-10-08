# AI-Driven Transparent Repair of Software Dependency Configurations

![tests](https://github.com/ibtisam-tanveer/transparent-dep-repair/actions/workflows/tests.yml/badge.svg)

MSc thesis project (Muhammad Ibtisam Tanveer, supervised by Dr. Sheeba Samuel,
MSc Web Engineering / TUC). See `Vision_Document_v2.docx` for the full research
motivation, related work, and evaluation plan.

The tool's eventual loop:

```
run the project -> read the error -> propose a fix -> apply it -> re-run to verify -> explain every decision
```

This repo is built phase by phase, one link of that loop at a time.

**Scope note (2026-09-20, refined 2026-10-03/04):** the supervisor has
directed the thesis toward whole-**repository** repair (not just single
files), a hybrid classical+LLM strategy, and an **agentic** architecture —
see `NEW_DIRECTION.md` and `AGENTIC_DIRECTION_AND_FIRST_TASK.md` (the
staged agentic build plan). The exact novel contribution and build order
are still being confirmed; built so far: the repo-analysis foundation, a
single LLM agent that chooses tools instead of a fixed loop (see "Agentic
direction" below), a two-axis provenance model + the first transparency
report (see "Provenance & report" below), and that same agent lifted to
**whole repositories** in one shared environment, evaluation-ready via a
`fresh` mode (see "Repo-scale agentic repair" below). Everything in
Phases 1-5 and Notebook Support survives unchanged as the engine every
later layer sits on top of.

## Phase 1 — run a project and capture its error

`repair_tool/runner.py` runs a target `.py` file **as a subprocess** (never
by importing it, so a broken target can never crash this tool) and returns a
structured `RunResult`.

```python
from repair_tool.runner import run_project

result = run_project("broken_examples/02_numpy_float.py")
assert result.ok is False
assert "AttributeError" in result.stderr
```

CLI:

```bash
python -m repair_tool.runner <path-to-target.py> [--timeout SECONDS]
# or, once pip-installed: repair-tool-run <path-to-target.py>
```

Stdlib only (`subprocess`, `sys`, `os`, `dataclasses`, `argparse`) — no
third-party dependencies, Python 3.10+, cross-platform (uses `sys.executable`,
never a hard-coded `python`).

### `broken_examples/`

Eight small scripts, each broken by one real, known dependency problem (see
`broken_examples/MANIFEST.md` for the full table and the intended fix for
each). They are the fixed regression set for every later phase, not just
Phase 1 — a diagnosis phase can be checked against `MANIFEST.md`'s "type of
problem" column, and a repair phase against its "correct fix" column.

Some of them (02, 03, 04, 05, 06) only reach their *interesting* error once
the underlying package is actually installed — otherwise you just see
`ModuleNotFoundError` for the package itself. The `dev` extra (below) installs
those packages **in a throwaway venv for development/testing only**; it is
deliberately separate from `runner.py`'s own zero-dependency footprint.
`seaborn` is intentionally *not* installed, so example 01 keeps demonstrating
a genuinely missing package.

## Phase 2 — diagnose the captured error

`repair_tool/diagnose.py` reads the `stderr` a failed `RunResult` captured
and classifies it into a structured `Diagnosis` (`kind`, `module`,
`package`, `symbol`, `detail`) — pure classification, no fixing, installing,
or LLM calls yet.

```python
from repair_tool.diagnose import diagnose

d = diagnose("AttributeError: module 'numpy' has no attribute 'float'")
assert d.kind == "module_attribute_removed"
assert d.package == "numpy"
assert d.symbol == "float"
```

`diagnose_result(result)` wraps a Phase 1 `RunResult` directly — `kind="none"`
for a successful run, otherwise the same classification as `diagnose(result.stderr)`.

CLI (runs the target via `run_project` first, then diagnoses it):

```bash
python -m repair_tool.diagnose <path-to-target.py> [--timeout SECONDS]
# or: repair-tool-diagnose <path-to-target.py>
```

The five categories, in priority order: `missing_module`, `import_name`,
`module_attribute_removed`, `object_attribute_error`, and a `unknown` catch-all
for anything else (e.g. a `TypeError` from a changed function signature) —
`unknown` is expected and correct for now; a later LLM step is what will
actually handle those.

Classification works off the traceback's real exception line, found by
scanning from the end of `stderr` for the last unindented `ExceptionName:
message` line, rather than just taking the literal last line — some
libraries (NumPy's deprecation notices, for one) print explanatory text
*after* the actual error line, and naively grabbing the last line would
misclassify those as `unknown`.

**A version-dependent classification note:** running `broken_examples/04_sklearn_externals_joblib.py`
against the `dev` extra's scikit-learn produces `ImportError: cannot import
name 'joblib' from 'sklearn.externals'` (kind=`import_name`), not the
`missing_module` result you might expect from the file's name — scikit-learn
still ships an (empty) `sklearn.externals` shim module rather than deleting
it outright. The file's own header comment already anticipates this
("`EXPECTED ERR : ImportError / ModuleNotFoundError`"); the test suite
asserts against what actually happens, not the older assumption.

## Phase 3 — propose and apply a fix, in isolation, then verify

Closes the loop for the **one safe, deterministic case**: a genuinely
missing package. Everything else is detected and reported honestly as "not
handled yet" — deliberately narrow, since fixing removed/changed APIs needs
code understanding and belongs to the LLM step (Phase 5). Still no LLM here:
rule-based plus real PyPI lookups, a clean baseline to compare the LLM
against later (RQ3). See `PHASE3_TASK.md` and `PHASE3_ADDENDUM.md` (design
decisions made before implementation: install timeout, confidence/source
mapping, CI network-test policy).

| Module | Responsibility |
|---|---|
| `repair_tool/pypi.py` | `package_exists`, `latest_version`, `resolve_package_name` — talks to real PyPI via stdlib `urllib`, plus a curated alias map (`sklearn`→`scikit-learn`, `cv2`→`opencv-python`, etc.) |
| `repair_tool/venv_manager.py` | `get_venv_python(target)` — creates/reuses an isolated venv per target, returns its python executable |
| `repair_tool/repair.py` | `Proposal` + `propose(diagnosis)` — rule-based, decides what to do and records why |
| `repair_tool/apply.py` | `apply(proposal, python_exe)` — runs the install inside the venv, 300s timeout, never raises |
| `repair_tool/loop.py` | `Attempt`, `RepairResult`, `repair(path)` — orchestrates run→diagnose→propose→apply→verify, plus the CLI |

```python
from repair_tool.loop import repair

result = repair("broken_examples/01_missing_package.py")
assert result.fixed is True   # installs seaborn in an isolated venv, verifies by re-running
```

CLI:

```bash
python -m repair_tool.loop <path-to-target.py> [--max-attempts N]
# or: repair-tool-fix <path-to-target.py>
```

**Isolation**: `run_project` now takes an optional `python_exe` (defaults to
`sys.executable`, so Phases 1-2 are unaffected); `loop.repair` passes the
target's venv python, so installs never touch the interpreter running this
tool. Venvs live under `.repair_venvs/` (gitignored), keyed by the target's
absolute path, and are reused across runs.

**The verify loop never trusts an install worked** — success is only
"the project actually ran afterward." Capped at `MAX_ATTEMPTS = 5`, and it
stops early if the same diagnosis (kind/module/package/symbol) repeats,
so a stuck state can't loop forever pretending to make progress. A nice
side effect of real isolation: since each venv starts empty, `02`/`04` often
surface as `missing_module` on the *first* run (numpy/sklearn aren't
installed at all yet) before revealing their actual unhandled error
(`module_attribute_removed`/`import_name`) on the second — exactly the
layered-error behavior `broken_examples/MANIFEST.md` describes.

**Transparency fields are seeded now, not retrofitted.** `Proposal` carries
`reason`/`source`/`confidence`, `Attempt` carries `applied`/`verification` —
the first real pieces of the full transparency report (Vision Doc §7),
captured from the start so later phases don't need to reconstruct this data.

## Phase 5 — LLM-based repair of the hard cases (code vs. environment)

**The thesis's core contribution.** Phase 3 only fixes a missing package;
everything else (`module_attribute_removed`, `object_attribute_error`,
`import_name`, `unknown`) used to stop at "not handled yet." Phase 5 makes
those fixable — without picking a side in the unsettled code-vs-environment
debate (Vision Doc ref [4]): for each hard case it asks an LLM for **two**
candidate fixes in one call (a code edit and an environment/version pin),
**applies and verifies each by actually re-running the project**, keeps
whichever one works, and records the other as a rejected alternative. See
`PHASE5_TASK.md` for the full spec and `PHASE5_SUMMARY.md` for what
actually happened building it (including a real LLM JSON-escaping bug found
and fixed, and one example that's honestly "not fixed" for real reasons).

| Module | Responsibility |
|---|---|
| `repair_tool/llm.py` | `request_fix(diagnosis, code, error_text)` — the only place `openai`/`OPENAI_API_KEY` are touched; strict JSON via OpenAI's `response_format=json_object`, defensive parsing, never raises |
| `repair_tool/repair.py` | `propose_hard_case(...)` — asks the LLM once, returns both candidates; `STRATEGY_ORDER` decides which to try first per diagnosis kind |
| `repair_tool/apply.py` | `apply_code_edit(edits, workspace_path)` — find/replace edits on a working copy, never the original; a non-matching `find` is a clean failure, not a crash |
| `repair_tool/loop.py` | applies + verifies each candidate in order, picks the winner, reverts a failed code edit before trying the other, records `strategy_won` + `alternatives` |

```python
from repair_tool.loop import repair

result = repair("broken_examples/02_numpy_float.py")
assert result.fixed is True
assert result.attempts[-1].proposal.strategy_won in ("code", "environment")
assert result.attempts[-1].proposal.alternatives   # the rejected candidate, recorded
```

**Verification is always by re-running** — a candidate only counts if
`RunResult.ok` afterward, never the model's own claim. **The original input
file is never touched**: `repair()` makes one working copy (under
`.repair_venvs/<hash>/workspace/`) the moment it starts, and every run/edit
from then on targets that copy.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
echo 'OPENAI_API_KEY=sk-...' > .env   # gitignored; loaded automatically via python-dotenv
python -m unittest tests.test_runner tests.test_diagnose tests.test_pypi tests.test_repair tests.test_apply tests.test_llm tests.test_loop -v
```

`pip install -e ".[dev]"` also registers `repair-tool-run`, `repair-tool-diagnose`,
and `repair-tool-fix` console scripts — see `pyproject.toml`. `openai` and
`python-dotenv` are now core dependencies (Phase 5), but the import stays
isolated to `llm.py` — the rest of the tool works without either installed.

**Network note**: several test modules include tests that hit real
services — PyPI, and (Phase 5) OpenAI, plus real installs into temporary
venvs. Set `SKIP_NETWORK_TESTS=1` to skip all of those (CI does this by
default); leave it unset locally, with a real `OPENAI_API_KEY`, to run them
for real.

### Manual checks

```bash
python -m repair_tool.runner broken_examples/02_numpy_float.py    # prints the numpy AttributeError, tool stays alive
python -m repair_tool.runner hello.py                               # prints OK

python -m repair_tool.diagnose broken_examples/01_missing_package.py   # missing_module: seaborn
python -m repair_tool.diagnose broken_examples/06_scipy_imread.py       # import_name: imread from scipy.misc
python -m repair_tool.diagnose hello.py                                  # none (ran fine)

python -m repair_tool.loop broken_examples/01_missing_package.py         # FIXED (installs seaborn in a venv)
python -m repair_tool.loop broken_examples/02_numpy_float.py             # FIXED (LLM code_edit: np.float -> float)
python -m repair_tool.loop broken_examples/03_numpy_int_bool.py          # FIXED (LLM code_edit)
python -m repair_tool.loop broken_examples/04_sklearn_externals_joblib.py  # NOT FIXED: not handled yet (import_name)
```

### CI

`.github/workflows/tests.yml` runs all test modules on every push, on
Python 3.10 and 3.12, with `SKIP_NETWORK_TESTS=1` set so CI stays fast and
isn't a source of flakiness from PyPI/OpenAI hiccups or API cost — this
directly backs the vision doc's "Reproducibility" non-functional
requirement: the test suite behaves the same on a clean machine as it does
locally (the offline subset, at least; the network subset is a deliberate
local-only check, see `PHASE3_ADDENDUM.md` #3).

## Notebook Support — run and repair `.ipynb` notebooks

Extends the same loop to Jupyter notebooks, since the primary evaluation
dataset (`dataset/`, the GigaScience corpus) is entirely `.ipynb`. See
`NOTEBOOK_SUPPORT_TASK.md` for the design and `NOTEBOOK_SUPPORT_SUMMARY.md`
for what actually happened building it — including two real, subtle bugs
found and fixed by testing against real execution rather than assuming the
design worked: a `KernelManager.kernel_cmd` attribute that looked correct
but was silently never consulted (every notebook was secretly executing
under this tool's own interpreter, defeating isolation entirely — caught by
the kernel-isolation test the task doc specifically called for), and a ZMQ
socket leak from an externally-provided kernel manager not being fully torn
down.

`repair_tool/notebook.py` is the only new module — `diagnose.py`,
`repair.py`, `llm.py`, `pypi.py`, and `venv_manager.py`'s core logic needed
**zero changes**, exactly as designed: `runner.run_project` and
`apply.apply_code_edit` each detect a `.ipynb` target and dispatch there,
so a notebook produces the same `RunResult` shape a `.py` subprocess run
does, and everything downstream stays format-agnostic.

```python
from repair_tool.loop import repair

result = repair("broken_examples/notebooks/missing_package.ipynb")
assert result.fixed is True   # same loop, same transparency report, a notebook this time
```

Manual checks:

```bash
python -m repair_tool.loop broken_examples/notebooks/missing_package.ipynb   # FIXED (install)
python -m repair_tool.loop broken_examples/notebooks/numpy_float.ipynb        # FIXED (LLM code_edit)
python -m repair_tool.loop broken_examples/notebooks/missing_data_file.ipynb  # NOT FIXED, honestly (out of scope: needs external data)
```

`nbclient`/`nbformat` are core dependencies (the execution *driver*, run
from this tool's own environment); `ipykernel` is installed into each
*target* venv instead (`venv_manager.ensure_ipykernel`), since that's what
actually launches a kernel using that venv's packages — getting this split
right is what makes "installing a fix into the venv" actually affect the
notebook run. Test with `python -m unittest tests.test_notebook -v`.

## Repo Foundation — run and diagnose a whole repository

The first layer of the new repo-level direction (`NEW_DIRECTION.md`,
`REPO_FOUNDATION_TASK.md`). Given a folder, `analyze_repo()` sets up **one
shared isolated environment for the whole repo** (not one per file),
installs whatever dependency declaration it can find, discovers every
`.py`/`.ipynb` file, runs each one in that shared environment, and
classifies every failure — reusing `runner.run_project` (which already
dispatches to `.ipynb` via `notebook.run_notebook`) and `diagnose.py`
completely unchanged.

**This phase only runs and diagnoses — it does not repair anything at repo
scale.** Repo-scale repair, the classical+LLM hybrid, and an agentic
architecture are the next layers, explicitly on hold until the supervisor
confirms the novel contribution and build priority.

```python
from repair_tool.repo import analyze_repo

result = analyze_repo("path/to/some/repo")
print(result.summary)  # e.g. {"total": 12, "ran": 9, "failed": 3, "failures_by_kind": {...}}
```

CLI (accepts a local path **or** a git URL — a URL is cloned into a
throwaway temp directory, analyzed, and the clone is always deleted
afterward, success or failure):

```bash
python -m repair_tool.repo <path-to-repo-or-git-url> [--timeout SECONDS]
# or: repair-tool-analyze-repo <path-to-repo-or-git-url>

python -m repair_tool.repo https://github.com/some/repo.git   # clones, analyzes, cleans up
```

`analyze_repo_url(git_url)` is the same thing from Python. Public repos
only — it shells out to a plain `git clone` with no credential handling of
any kind, so a private repo fails honestly (`env_setup_ok=False`), never a
crash.

**One shared venv needed no changes to `venv_manager.py`** — `get_venv_python()`
just hashes whatever path string it's given, so passing it the repo root
instead of a single file's path already gives one consistent venv reused
across every file in that repo.

**Not every dependency format is pip-installable on its own.**
`requirements.txt` and a package-declaring `pyproject.toml`/`setup.py` are;
`environment.yml` (conda) and `Pipfile` (pipenv) aren't, without tooling
this project doesn't carry — those are recognised and reported ("found but
not installed"), not silently ignored or treated as a hard failure. Many
`pyproject.toml` files are just tool config (ruff/black/pytest settings)
with nothing to install — `_pyproject_declares_a_package()` checks for a
real `[project]`/`[tool.poetry]` section before attempting `pip install .`.

**The pass/fail verdict for a whole repo is a caller-supplied rule, not a
hard-coded definition** — `analyze_repo(path, pass_rule=lambda r: ...)`
sets `result.passed`; without one, `result.passed` stays `None` and the
per-file results + `summary` counts are still fully available. This is
deliberate: what counts as "the repo runs" is a decision the supervisor may
set later, and it must be trivial to change without touching the analysis
itself.

## Agentic direction — first task: agent-callable tools + one tool-calling agent

The first concrete step of `NEW_DIRECTION.md`'s agentic layer, staged per
`AGENTIC_DIRECTION_AND_FIRST_TASK.md` so the "build a multi-agent hybrid
system" goal doesn't get tackled in one risky leap. This task converts the
existing engine into LLM-callable tools and replaces `loop.py`'s *fixed*
decision order (propose → apply → verify) with a single LLM agent that
*chooses* which tool to call next — proving the "agent calls tools, every
action is logged with provenance" pattern before adding a second/third
agent or a classical knowledge-graph tool.

| Module | Responsibility |
|---|---|
| `repair_tool/agent_tools.py` | Wraps the existing engine as six JSON-in/JSON-out tools (`run_target`, `diagnose_error`, `lookup_package`, `install_package`, `edit_code`, `verify`) plus `TOOL_SPECS` (the OpenAI `tools=[...]` schema) and `build_dispatch()` (binds one session's workspace/venv). No new repair logic — every tool delegates to the unchanged Phase 1-5 function it wraps. |
| `repair_tool/agent.py` | `agent_repair(path) -> AgentResult` — a ReAct-style tool-calling loop: the model sees the situation, calls a tool, sees the result, calls the next, until `verify` passes or `MAX_STEPS` (10) is hit. Records an ordered, provenance-tagged `TraceStep` list — the seed of the transparency report. |

```python
from repair_tool.agent import agent_repair

result = agent_repair("broken_examples/01_missing_package.py")
assert result.fixed is True
assert result.trace[0].tool_called == "run_target"
```

CLI:

```bash
python -m repair_tool.agent <path-to-target.py> [--max-steps N]
# or: repair-tool-agent <path-to-target.py>
```

**The model owns search, the checker owns authority** (Schwarz paper
principle, see `AGENTIC_DIRECTION_AND_FIRST_TASK.md` §2): `install_package`
and `edit_code` steps are tagged `llm_unverified` the moment they're
applied — a proposal, not yet a fact. Only once the *next* `verify` call
reports `ok=true` does that specific action get upgraded: to
`metadata_verified + execution_verified` if a `lookup_package` call
confirmed the package first, or plain `execution_verified` for a code edit
confirmed only by re-running. An action applied but *not* the one
immediately preceding a passing `verify` (e.g. installing numpy before
realizing a separate code edit was also needed) stays `llm_unverified`
permanently — a deliberately strict reading of "verified": it's tied to the
specific action a passing re-run actually followed, not to anything that
later turned out to matter.

**Verified for real against both existing hard cases**: `agent_repair`
fixes `01_missing_package.py` in one diagnose/install/verify round, and
`02_numpy_float.py` in two rounds — the agent installs numpy first (fixing
the `ModuleNotFoundError`), `verify` then reveals the *real* error
(`AttributeError: module 'numpy' has no attribute 'float'`), and the agent
diagnoses again and reaches for `edit_code` instead of repeating the same
action — exactly the layered-error behavior `loop.py`'s fixed pipeline
handles with two different code paths (Phase 3's install, then Phase 5's
hard-case branch); the agent reaches the same outcome by *choosing* both
steps itself.

**`loop.py` is untouched and still the baseline.** `agent.py` is a new,
parallel entry point — not a replacement — so the deterministic fixed loop
remains for comparison (RQ3: does choosing tools beat a fixed pipeline?).

See `AGENT_FIRST_TASK_SUMMARY.md` for the full build notes, test strategy,
and design decisions.

## Provenance & report — two independent trust axes, and the first transparency report

Running the agent above for real exposed a real weakness: a single
`provenance` tag collapses two different questions into one. Per
`TASK_provenance_and_report.md`, every `TraceStep` now carries two
independent fields instead:

- **`grounding`** — was this action backed by a deterministic/classical
  tool result (`metadata_grounded`, e.g. an install whose package a
  `lookup_package` call confirmed exists), or only the LLM's own reasoning
  (`llm_proposed`, e.g. a code rewrite)? Observation tools themselves
  (`run_target`, `diagnose_error`, `lookup_package`, `verify`) are
  `deterministic` — facts, not proposals. Set once, when the action is
  taken, and **never changes afterward** — an action's source doesn't
  depend on whether it later turns out to matter. (`kg_grounded` is
  reserved for a future knowledge-graph tool; nothing produces it yet.)
- **`verification`** — did a later check confirm the project then ran
  (`verified`), or has nothing confirmed it yet (`unverified`)? `n/a` for
  observation tools.

A `confidence` (`high`/`medium`/`medium-high`/`low`) is derived from the
pair for every fix action. The motivating case: installing `numpy`
(confirmed on PyPI first) now reads honestly as `metadata_grounded` +
`unverified` the moment the *next* check reveals a second, unrelated
failure (`np.float`'s `AttributeError`) — well-founded, just not (yet)
sufficient — instead of the old, misleading `llm_unverified`, which read
exactly like an ungrounded guess.

```python
from repair_tool.agent import agent_repair
from repair_tool.report import build_report

result = agent_repair("broken_examples/02_numpy_float.py")
print(build_report(result))
```

```
Repair report — broken_examples/02_numpy_float.py
Outcome: FIXED

Step 1  install numpy
        grounding:    metadata_grounded ('numpy' confirmed by a classical lookup)
        verification: unverified (no passing re-run has confirmed this yet)
        confidence:   medium

Step 2  edit code: x = np.float(3.14) -> x = float(3.14)
        grounding:    llm_proposed (no classical tool backed this)
        verification: verified (the project ran successfully afterward)
        confidence:   medium-high

Summary: 2 fix actions (1 grounded, 1 model-proposed). Project now runs.
         Accepted fix confidence: medium-high — a model proposal confirmed
         only by re-running; a reviewer may wish to check it.
```

CLI: `python -m repair_tool.agent <path> --report` prints this instead of
the raw trace. `repair_tool/report.py`'s `build_report()` adds no new trust
logic — it only formats what's already on the trace, and it **never hides
a necessary-but-insufficient or failed action**: both appear with honest
tags, not just the step that ultimately worked. Plain text only — no
UI/HTML in this task; see `PROVENANCE_REPORT_SUMMARY.md` for the full
design notes, including a real bug this task's own verification found (a
`run_target` call confirming an already-passing project wasn't being
treated the same as a `verify` call doing the identical check).

## Repo-scale agentic repair — the single-file agent, lifted to a whole repository

`repair_tool/agent_repo.py`'s `agent_repair_repo(repo_path, fresh=...)`
reuses `repo.py`'s discovery/dependency-install logic and `agent.py`'s
per-file `agent_repair()` entirely unchanged, running every file in **one
shared environment** — a package the agent installs while fixing one file
is then genuinely present for the next, exactly like a real repository.

```python
from repair_tool.agent_repo import agent_repair_repo
from repair_tool.report import build_repo_report

result = agent_repair_repo("path/to/some/repo")
print(build_repo_report(result))   # "N of M files now run", per-file detail, honest about what's still broken
```

CLI: `python -m repair_tool.agent_repo <repo> [--fresh] [--report]`, or
`repair-tool-agent-repo`.

**The shared environment is real, not simulated** — verified against a
tiny fixture repo (`tests/fixtures/agent_repo/`) where a second file
deliberately needs the exact same package as the first: fixing file A
installs it for real, and file B then turns out already passing, with an
**empty trace** (`AgentResult(fixed=True, trace=[])` is how "needed no
repair" is told apart from "the agent fixed it" throughout). A third file
in the same fixture showed this going even further than planned: it needed
`numpy`, which turned out to already be present once `seaborn` (file A's
fix) pulled it in as its own transitive dependency — so the agent correctly
skipped straight to the code edit it still needed, with no install step at
all. See `REPO_AGENT_SUMMARY.md`.

**Evaluation-ready, deliberately**: both `agent.agent_repair()` and
`agent_repair_repo()` take a `fresh: bool = False` option. The default
reuses `venv_manager`'s persistent, hash-cached venv/workspace — correct
for interactive use, but a *second* run against a target a prior run
already fixed would silently see it already passing and report "nothing to
do," which would quietly corrupt a dataset evaluation's results. **Any
dataset evaluation must pass `fresh=True`** — a brand-new, throwaway venv
and fresh file copies every time, deleted again once that run ends.

## Evaluation — running the tool on real data and measuring it

The pivot from building to measuring, per `TASK_evaluation.md`. Uses the
**2023 GigaScience rerun** (Zenodo record 8226725, 27,271 notebooks across
5,240 repositories) — not the 2021 run `DATASET_SUMMARY.md` describes,
which is now superseded.

| File | Purpose |
|---|---|
| `DEPENDENCY_FAILURE_TAXONOMY.md` | The A-E dependency-failure-cause taxonomy (missing dependency / moved import / removed API / version conflict / not-a-dependency-failure) + confirmed/candidate/excluded tiers, used to label the failure set and report results *by cause*, not by raw symptom |
| `dataset/taxonomy.py` | Classifies one `executions.reason` string into (category, tier) — a dataset-specific classifier, not a reuse of `diagnose.py` (see `dataset/NOTES_2023.md` for why) |
| `dataset/extract_failures_2023.py` | Queries the 2023 `db.sqlite`, classifies every real execution-exception row, writes `dataset/failures_2023.csv` |
| `dataset/NOTES_2023.md` | Findings: the nested two-database zip structure, the query population, and a real labelling blind spot (category B is structurally unobservable from this db's bare-exception-class-name logging) |
| `evaluation/run_eval.py` | The harness: samples repositories from the labelled set, clones each, and records one JSONL row per labelled notebook as it's produced. Resumable (checkpointed per repo), cost-logged (`llm_calls`/`total_tokens` per notebook), sample-able (`--sample`, `--limit-minutes`, `--max-llm-calls`), cleans up every clone |
| `evaluation/_repo_runner.py` | `run_repo_incrementally()` — reuses `repo.py`'s discovery/dependency-install and `agent.agent_repair()` per file (the same primitives `agent_repo.py` itself calls), but *yields* one result per file as it finishes, with a time budget checked between files |
| `evaluation/_repo_worker.py` | The subprocess entry point `run_eval.py` runs each repo in, streaming each file's result to disk immediately (flushed) as `_repo_runner` yields it |
| `evaluation/analyze_results.py` | Turns result rows into the headline numbers: effectiveness overall and per category (on both a confirmed-only and a confirmed+candidate denominator), grounded-vs-proposed + confidence distribution, cap-limited vs. genuine failures, and the cross-file "fixed for free" count |

```bash
python dataset/extract_failures_2023.py
python evaluation/run_eval.py --sample 20 --tag pilot --per-file-budget-minutes 10
python evaluation/analyze_results.py evaluation/results/pilot.jsonl
```

**Two real evaluation-readiness bugs found by smoke-testing the harness,
not by writing it:**

1. With no client-side timeout, a slow/stuck OpenAI response could block
   an entire run for minutes with no local symptom to diagnose (no
   subprocess, no CPU use — just blocked network I/O). `agent.py`'s OpenAI
   client now has an explicit 120s timeout, permanently, not just for
   evaluation.
2. A fixed per-repo timeout is unfair across repo sizes, and — worse — the
   original design called `agent_repair_repo()` as one all-or-nothing
   block, so a timeout discarded *every* result for that repo, including
   notebooks already fixed before the cut-off. A real smoke test found an
   11-notebook repository that ran 43 minutes before being stopped.
   **Fixed with a redesign**: each repo's time budget now scales with its
   real discovered file count, and results stream to disk per file as
   they finish, so a cutoff keeps whatever already succeeded. Re-run for
   real after the fix, the same repo went from "0/11 usable" to "4/11
   genuinely attempted + 7/11 honestly marked not-reached" — real,
   non-wasted signal either way.

See `EVALUATION_PILOT_SUMMARY.md` for the full writeup — the harness is
validated by two 3-repo smoke tests, but the actual ~20-50 repo pilot
`TASK_evaluation.md` asks for hasn't been run yet, so there are no
reportable effectiveness numbers in it yet.

## Roadmap (out of scope so far, tracked here for context)

- **Multiple agents** (diagnosis/repair/verification split) and **a
  classical knowledge-graph tool** (PyEGo/ReadPyE, a stretch goal) — the
  remaining agentic steps per `AGENTIC_DIRECTION_AND_FIRST_TASK.md`, now
  that both the single-file and repo-scale agent exist.
- **The classical+LLM hybrid** beyond what's already there (PyPI facts +
  execution checks as the symbolic half already makes the current agent
  hybrid; a published KG tool is the optional next step, not a dependency).
- **Phase 4** (deferred, not dropped — see `PHASE5_TASK.md`'s "Build order"
  note) — verify each fix against authoritative package metadata beyond
  existence/name, once there's more to harden.
- **The "correct fix, missing import" gap** (found via `06_scipy_imread.py`
  during Phase 5 hardening) — a code fix that introduces a new import can't
  succeed, since nothing installs it; see `PROJECT_STATUS.md`'s flagged
  section. Expected to matter more at repo scale, not less.
- **Dataset evaluation, the failure taxonomy, and the 2023 GigaScience
  rerun** — all paused behind the re-scope; see `PROJECT_STATUS.md`.
- **Later** — assemble the full transparency report (`alternatives` is now
  real for hard cases; richer fields like a full package-metadata
  provenance trail come with Phase 4) that is this thesis's core
  contribution over prior repair tools (Vision Doc, Section 7).

## Project layout

```
repair_tool/runner.py         Phase 1 deliverable — run_project, RunResult
repair_tool/diagnose.py       Phase 2 deliverable — diagnose, diagnose_result, Diagnosis
repair_tool/pypi.py           Phase 3 — PyPI lookups + curated alias map
repair_tool/venv_manager.py   Phase 3 — isolated venv + workspace-copy creation/reuse per target
repair_tool/repair.py         Phase 3 (propose) + Phase 5 (propose_hard_case, STRATEGY_ORDER)
repair_tool/apply.py          Phase 3 (installs) + Phase 5 (apply_code_edit) inside a venv/workspace
repair_tool/llm.py            Phase 5 deliverable — the only module that imports openai
repair_tool/loop.py           Phase 3+5 deliverable — Attempt, RepairResult, repair(), CLI
hello.py                      trivial script used to test the success path
broken_examples/              fixed regression set (8 scripts + MANIFEST.md)
tests/test_runner.py          Phase 1 unittest suite
tests/test_diagnose.py        Phase 2 unittest suite
tests/test_pypi.py            Phase 3 — pypi.py (offline + network-guarded)
tests/test_repair.py          Phase 3 + 5 — propose()/propose_hard_case() (offline + network-guarded)
tests/test_apply.py           Phase 3 + 5 — apply()/apply_code_edit() (offline, mocked subprocess)
tests/test_llm.py             Phase 5 — llm.py (offline mocked + network-guarded real call)
tests/test_loop.py            Phase 3 + 5 — repair() loop logic (offline, mocked) + real end-to-end integration tests
repair_tool/notebook.py       Notebook Support deliverable — run_notebook, edit_notebook_cells
broken_examples/notebooks/    small .ipynb fixtures mirroring the .py set
tests/test_notebook.py        Notebook Support — dispatch, error extraction, kernel isolation, end-to-end
repair_tool/repo.py            Repo Foundation deliverable — analyze_repo, analyze_repo_url, RepoResult, FileResult, CLI
tests/fixtures/sample_repo/                small offline-testable repo fixture (no deps, one good/bad file, a notebook)
tests/fixtures/sample_repo_with_deps/      repo fixture with a real requirements.txt (network-guarded)
tests/test_repo.py            Repo Foundation — discovery, dependency detection/install, analyze_repo (offline + guarded)
tests/fixtures/hangs.py       infinite loop, used only to test the timeout path
repair_tool/agent_tools.py    Agentic first task — the engine as LLM-callable tools, TOOL_SPECS, build_dispatch
repair_tool/agent.py          Agentic first task — agent_repair(), AgentResult, TraceStep, CLI
tests/test_agent_tools.py     Agentic first task — each tool wrapper + dispatch binding (offline)
tests/test_agent.py           Agentic first task + provenance/report task — agent_repair()'s tool-calling loop and two-axis trust tags (offline, mocked LLM) + real end-to-end
repair_tool/report.py         Provenance/report task + repo-scale task — build_report(), build_repo_report() (plain text)
tests/test_report.py          Provenance/report task + repo-scale task — build_report()/build_repo_report() (offline) + real end-to-end
repair_tool/agent_repo.py     Repo-scale task — agent_repair_repo(), RepoAgentResult, CLI
tests/fixtures/agent_repo/    Repo-scale task — a tiny real fixture repo (passing/missing-package/shared-dep/removed-API files)
tests/test_agent_repo.py      Repo-scale task — orchestration, shared-env, error-isolation, fresh mode (offline + real end-to-end)
tests/test_venv_manager.py    Repo-scale task — venv_dir_for, get_fresh_venv_python/workspace_copy, get_repo_file_workspace_copy
dataset/taxonomy.py           Evaluation task — classify() a GigaScience executions.reason into the A-E taxonomy + tier
dataset/extract_failures_2023.py  Evaluation task — queries the 2023 db, writes dataset/failures_2023.csv
dataset/NOTES_2023.md         Evaluation task — the two-database-zip finding, query population, category-B blind spot
tests/test_taxonomy.py        Evaluation task — classify() against real observed db reason values
evaluation/run_eval.py        Evaluation task — the harness: sample/clone/record, resumable, file-count-scaled time budget
evaluation/_repo_runner.py    Evaluation task — run_repo_incrementally(): per-file streaming driver, reused repo.py/agent.py primitives
evaluation/_repo_worker.py    Evaluation task — subprocess entry point that streams each file's result to disk as it finishes
evaluation/analyze_results.py Evaluation task — turns result rows into the thesis's headline numbers
tests/test_run_eval.py        Evaluation task — harness resumability, sampling, budget-scaling, partial-row preservation (offline)
tests/test_repo_runner.py     Evaluation task — the incremental per-file driver: ordering, budget cutoff, error isolation (offline)
tests/test_analyze_results.py Evaluation task — effectiveness/grounding/cap-hit/cross-file number-crunching (offline)
pyproject.toml                packaging + core deps (openai, python-dotenv, nbclient, nbformat) + dev-only extras (numpy, pandas, ipykernel, ...)
.github/workflows/tests.yml   CI: runs all test suites on every push (network tests skipped)
dataset/                      independent data track — see DATASET_SUMMARY.md (2021, superseded) and NOTES_2023.md (current)
NEW_DIRECTION.md              the repo-level/hybrid/agentic scope change — read this first for anything repo-level
AGENTIC_DIRECTION_AND_FIRST_TASK.md  the staged agentic build plan — read this first for anything agent-related
TASK_provenance_and_report.md the two-axis trust model + first transparency report spec
TASK_repo_scale_agent.md      the repo-scale agent + evaluation-ready (fresh mode) spec
DEPENDENCY_FAILURE_TAXONOMY.md  the A-E dependency-failure-cause taxonomy + confidence tiers
TASK_evaluation.md            the evaluation harness + pilot-run spec
```

`runner.py`/`diagnose.py` moved into the `repair_tool/` package as Phase 3's
Step 0 (a pure restructure, committed on its own before any new feature) —
once a third module (`pypi.py`) needed a home, that was the point to
introduce it, exactly as this README used to say it would be.
