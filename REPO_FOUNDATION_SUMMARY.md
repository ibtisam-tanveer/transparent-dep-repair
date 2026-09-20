# Repo Foundation Summary — run and diagnose a whole repository

Status: **done**. See `NEW_DIRECTION.md` (the scope pivot from single-file
to whole-repo, hybrid classical+LLM, agentic — most of it still pending
supervisor confirmation) and `REPO_FOUNDATION_TASK.md` (this task's spec,
deliberately the one thing safe to build regardless of how the pending
items resolve).

## What was asked

Given a local repo folder: find its dependency declarations, set up **one
shared** isolated environment for the whole repo, discover every runnable
`.py`/`.ipynb` file, run each in that shared environment, and collect
per-file results + diagnoses + a repo-level summary. **No repair at repo
scale in this task** — run and diagnose only. The task's own design
guidance explicitly deferred git-URL cloning as "a thin wrapper added
later" — added afterward, in this same pass, once asked whether the tool
could clone a repo itself rather than requiring a pre-existing local clone.

## What was built

| File | Purpose |
|---|---|
| `repair_tool/repo.py` | `analyze_repo(repo_path, timeout, pass_rule) -> RepoResult`, `analyze_repo_url(git_url, ...)`, plus `discover_runnable_files`, `find_dependency_files`, and a CLI that auto-detects a URL vs. a local path |
| `tests/fixtures/sample_repo/` | Offline-testable fixture: no deps, one file that runs, one that fails on a missing import, a notebook, a `tests/` file that must be excluded from discovery |
| `tests/fixtures/sample_repo_with_deps/` | A `requirements.txt` (`seaborn`) + a file that needs it, for the network-guarded real-install test |
| `tests/test_repo.py` | 28 tests: discovery/exclusion rules, dependency-file detection and priority, `pyproject.toml` package-vs-tool-config detection, `analyze_repo`'s control flow (offline, mocked venv), URL-vs-path detection, `analyze_repo_url`'s clone/analyze/cleanup cycle (offline, cloning from a local git repo), and three real end-to-end runs (two installs + one real GitHub URL) |

### `analyze_repo_url` — cloning added as a follow-up, same design discipline

`git clone --depth 1 <url> <tmpdir>`, then `analyze_repo(tmpdir, ...)`, then
`shutil.rmtree(tmpdir)` in a `finally` block — always, success or failure,
so repeated calls don't accumulate clones on disk (confirmed by a dedicated
test with a controlled temp path). The returned `RepoResult.repo_path` is
set back to the original URL once analysis finishes, since the temp path
it was actually analyzed under no longer exists by the time the caller
sees the result. Public repos only, deliberately: no credential handling
of any kind, so a private repo fails exactly like a bad URL would
(`env_setup_ok=False`, a message in `env_setup_log`), never a crash. Tests
exercise the real clone mechanism offline by cloning from a local git repo
(git clones a local path exactly the same way it clones a remote one) —
one real network test against a tiny, stable public repo
(`github.com/octocat/Hello-World`) confirms the actual remote path too.

`runner.run_project`, `notebook.run_notebook` (via `run_project`'s existing
dispatch), `diagnose.py`, and `venv_manager.py` — all reused completely
unchanged, exactly as the task asked.

## Design decisions and why

- **"One shared venv" needed zero changes to `venv_manager.py`.**
  `get_venv_python(target_path)` only ever hashes whatever path string it's
  given to decide which venv to (re)use — it has no per-file assumption
  baked in. Calling it with the *repo root* instead of an individual file's
  path already produces one venv shared across every file in that repo,
  reused across `analyze_repo()` calls the same way it's reused across
  `repair()` calls today. No new venv-management code needed at all.
- **Not every dependency format is pip-installable, so the "which one gets
  used" logic is format-aware, not uniform.** `requirements.txt` → `pip
  install -r`. A `pyproject.toml`/`setup.py` that actually declares a
  package (checked via a plain substring search for `[project]` or
  `[tool.poetry]` — good enough to avoid a TOML-parsing dependency for a
  yes/no check) → `pip install .`. `environment.yml` (conda) and `Pipfile`
  (pipenv) are recognised but not attempted, since this project carries
  neither conda nor pipenv tooling — reported honestly ("found but not
  installed") rather than either silently skipped or treated as a hard
  failure. Priority order matches the task doc exactly; only the first
  *installable* candidate found is used, and which one is recorded.
- **The pass/fail verdict is a parameter, not a hard-coded rule.** The task
  is explicit that this is a decision the supervisor may set later, so
  `analyze_repo` takes an optional `pass_rule(RepoResult) -> bool`.
  Supplying one sets `result.passed`; omitting it leaves `result.passed =
  None` while `result.summary` (counts) and every per-file result stay
  fully available regardless — nothing about the analysis itself depends
  on there being a pass/fail definition at all.
- **One bad file can't abort the scan, defensively, twice over.**
  `run_project`/`run_notebook` already never raise by their own contracts,
  but `analyze_repo` still wraps each file's processing in its own
  try/except, so even an unexpected exception (e.g. from `diagnose_result`
  on some edge-case input) degrades to that one file's own failed result
  rather than losing every other file's data.

## A fixture-design lesson worth recording

The first version of the offline fixture repo included a `setup.py` whose
only purpose was to test that `discover_runnable_files` never *runs* it as
a script. It also, correctly, got picked up by `find_dependency_files` and
fed to `pip install .` — which genuinely executes `setup.py`'s content
during the build, immediately raising the deliberately-broken content and
failing the whole environment setup. Not a bug in `repo.py` — `setup.py` is
legitimately both "a possible entrypoint to exclude" and "a possible
dependency source to install from," and a fixture testing the first concern
can't casually also exist for the second. Fixed by removing `setup.py` from
the offline fixture entirely and testing "excluded from discovery" and
"used as an installable dependency source" as separate, purpose-built
fixtures (the latter guarded, using a real minimal package definition).

## How each definition-of-done item was verified

- `analyze_repo(path)` sets up one shared environment, runs every
  discovered file in it, returns per-file results + diagnoses + a summary
  — verified against both fixtures, offline (mocked venv) and for real
  (real venv, real `pip install`).
- The pass/fail rule is configurable, not hard-coded — verified with two
  different rules producing different verdicts on the *same* underlying
  result, and confirmed the per-file data doesn't depend on a rule being
  supplied at all.
- Single-file behaviour and all 118 prior tests are unchanged — confirmed
  unmodified; full suite is now 146 tests.
- One bad file is recorded, not fatal — `bad_missing_import.py` fails
  correctly (`missing_module`) while `good.py` and the notebook still run
  and are reported.
- The repo's original files are never modified — verified by comparing
  every file's mtime before and after `analyze_repo()` runs.
- Both pip-installable dependency formats verified for real:
  `requirements.txt` (a real `seaborn` install) and `pyproject.toml` with a
  genuine `[project]` section (built and installed for real, confirmed the
  file needing it then imports successfully) — plus confirmed a
  tool-config-only `pyproject.toml` is correctly skipped in favor of the
  next candidate.
- `analyze_repo_url` clones, analyzes, and always cleans up — verified with
  a controlled temp path that a normal run, a failing clone, and a timed-out
  clone all result in the temp directory being gone afterward; verified for
  real against a live public GitHub URL.
- Full suite: **146 tests** (118 prior + 28 new for Repo Foundation total —
  25 offline, 3 guarded), passing both offline (127 run, 19 skipped) and
  fully online (~143s, including three real network operations for this
  task alone — two installs and one real GitHub clone — on top of
  everything from prior phases).

## Explicitly out of scope (per the task, unchanged)

Repo-scale repair. The classical (graph-based) + LLM hybrid strategy.
Agentic orchestration. Dataset evaluation, the failure taxonomy, Phase 4.
The exact novel contribution and build priority — all paused pending the
supervisor's confirmation, per `NEW_DIRECTION.md`.

## How to run / verify

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
SKIP_NETWORK_TESTS=1 python -m unittest tests.test_repo -v
# unset SKIP_NETWORK_TESTS to also run the two real-install tests

python -m repair_tool.repo tests/fixtures/sample_repo            # 2/3 ran (no deps installed)
python -m repair_tool.repo tests/fixtures/sample_repo_with_deps  # 1/1 ran (seaborn installed for real)
```
