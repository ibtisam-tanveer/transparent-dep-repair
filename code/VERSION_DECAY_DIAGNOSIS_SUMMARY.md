# Version Decay Diagnosis — why genuine category-A notebooks still aren't fixed

Status: **diagnosis complete, targeted fix (Part B) implemented and verified for real,
decision gate reached.** See `TASK_version_decay_diagnosis.md` for the full task spec.

## Background

The sibling-file fix (`TASK_fix_repo_execution.md`) worked — local-module false
positives are gone. But genuine category-A (missing-dependency) notebooks in real
repos still weren't being fixed, while the toy `broken_examples` cases were. This
needed a real diagnosis before the number means anything.

## Part A — the real failure pattern

All 10 genuinely-attempted (`no_progress`, `local_module_import = False`) notebooks
from the `kiste_smoke_v3` run come from **one repo**, `PBrockmann/ipython_ferretmagic`
(the other two sampled repos either lost everything to a clone timeout or hit the
per-repo hard timeout — reported separately, not folded into this bucket count).

Bucket assignment for all 10:

| Bucket | Count |
|---|---|
| 1. Latest-vs-era version cascade | 10 |
| 2. Declared dependencies ignored | 10 (contributing cause) |
| 3. Python-version mismatch | 10 (**root cause**) |
| 4. Build/compile failure | confirmed present (see below) |
| 5. Other | 0 |

**Why all three apply together, with bucket 3 underneath:** the repo's own
`binder/environment.yml` — not discoverable by the old root-only dependency search,
which is exactly bucket 2 — pins `python=3.7` and a fully 2019-era stack
(`numpy=1.17.3`, `pandas=0.25.3`, `ferretmagic==20181001`, etc.). The evaluation
venvs run Python 3.14: a 7-major-version gap. Installing reactively at *latest* (the
old default) triggers cascading removed-API errors — bucket 1 — which is the direct,
visible symptom of bucket 3.

### Transcript 1 — `ferretBasics.ipynb` (full LLM trace)

1. `run_target` → `ModuleNotFoundError: No module named 'ferretmagic'`
2. `lookup_package` → confirms `ferretmagic` on PyPI
3. `install_package("ferretmagic")` → latest (20220825) installs successfully
4. `verify` → **new** error, *inside ferretmagic's own installed code*:
   `ModuleNotFoundError: No module named 'IPython.utils.py3compat'`
5. `diagnose_error` → correctly classifies it as `missing_module`
6. `edit_code` on the installed package's source → **fails** (the tool can only
   patch the main target file, never a third-party installed file — a real tool-scope
   limit, not a bug)
7. Pivots: `install_package("IPython<8.0")` → succeeds
8. `verify` → **new** error one layer deeper: `AttributeError` — a different
   IPython-internals break
9. Runs out of budget mid-fix

Genuine, sensible, multi-layer troubleshooting throughout — not a bug, not a loop.

### Transcript 2 — `test.ipynb` (deterministic replay, no LLM needed)

1. `pyferret` missing → installed *latest* PyPI `pyferret`
2. New error: `AttributeError: module 'pyferret' has no attribute 'start'` — the
   PyPI package has drifted from the 2019 conda-channel build the notebook expects,
   possibly not even the same software lineage under that name

Confirms the cascade independently of the agent's own reasoning.

### Direct confirmation of bucket 4 (build failure), no agent needed

Manually installing the declared `numpy==1.17.3` pin **fails outright** on Python
3.14: no compatible wheel exists, and the 2019-era source build fails against the
modern toolchain. This is hard proof bucket 3 is the true, deep blocker — even a
*correctly identified, correctly pinned* dependency can be uninstallable without an
actual old Python interpreter.

## Part B — the fix, implemented and verified

1. **Declared dependencies installed first, at pinned versions.**
   `find_dependency_files` now searches the whole repo tree (was root-only — exactly
   why `binder/environment.yml` was invisible). `_install_dependencies` now attempts
   *every* installable recipe found (a package-declaring `setup.py`/`pyproject.toml`
   *and* the pinned third-party stack from `requirements.txt`/`environment.yml`),
   not just the first match — they answer different questions and aren't mutually
   exclusive. Added `environment.yml` support: extracts its `pip:` sub-list plus
   pip-expressible `name=version` conda pins, installing each individually so one
   conda-only pin failing (e.g. `pyferret`) doesn't block the others.

2. **Real bug found and fixed while testing this**: the environment.yml's own
   `pip=19.3.1` pin was being installed, which downgrades the *running* pip to a
   2019 version whose internals import `distutils` — removed in Python 3.12 —
   silently breaking every install attempted after it in the same batch. `pip` (and
   `python`, the interpreter itself) are now always excluded from the extracted
   specs.

3. **Era-appropriate versions for undeclared installs.** Added
   `pypi.latest_version_as_of(name, cutoff_date)`, verified for real:
   `numpy` as of `2019-10-01` → `1.17.2`, closely matching the repo's own `1.17.3`
   pin. Wired into `install_package` via the repo's last-commit date
   (`git log -1 --format=%ad --date=short`), computed once per repo and bound in
   through `build_dispatch`. Falls back to latest when no era version resolves, and
   never overrides a version the model already specified itself (e.g. a reactive
   `IPython<8.0`).

4. 326 offline tests pass; `fresh=True`, the no-progress stop, context compaction,
   and all prior behaviour stay intact.

## Part C — re-test result

Direct re-run of `ferretBasics.ipynb` with Part B in place: **measurable progress,
still not fixed.**

The agent's first observed error now jumps straight to `pyferret` missing —
`ferretmagic` itself no longer needs a reactive install at all, since it's already
present at the era-correct pinned version (`20181001`) from the declared-deps-first
step. That is real, confirmed signal that Part B works as intended.

It still dead-ends at the same wall: IPython itself isn't pinned anywhere in the
declared recipe (it arrives transitively via `jupyter`/`notebook`, at a too-modern
version), and `edit_code`'s inability to patch a third-party installed file — not a
Part B concern — blocks the remaining fix either way.

This was verified directly against the real repo rather than re-running the full
3-repo smoke set, since that would very likely reproduce 0 fixed again — the root
blocker (Python 3.7 vs 3.14) is unchanged and outside what Part B targets. The full
3-repo re-comparison can still be run on request.

## Decision gate

Part A confirms the cascade is **deep and rooted in a genuine Python-version
mismatch** that pip-level fixes cannot fully resolve (`numpy==1.17.3` has no install
path at all on Python 3.14). Per the task's own framing, this is the strategic
decision point, not a "build more" moment.

**Recommendation:** ship the honest result. Part B is real, measured, and worth
keeping — it reduces wasted reactive work and adds version provenance
(`resolved_package`/`version_source`) to the transparency report — but it should not
be expected to move the fixed-count off zero for this repo family specifically. A
true version-inference tool (PyEGo/SnifferDog-style) or running actual period-correct
Python interpreters is the heavier remedy: legitimate future work, not something to
build now.

## For the thesis

Two honest, citable points:

1. **Isolate the environment, not the filesystem** — running notebooks in their repo
   is the established reproducibility methodology (Pimentel et al., MSR 2019; the
   GigaScience study this thesis's dataset comes from); a per-file stripped workspace
   under-measures success.
2. A `ModuleNotFoundError` conflates several distinct causes — a missing PyPI
   package, a repo-local module, and (the dominant pattern found here) **version
   decay**: an old notebook needing old, mutually-compatible versions, where naive
   latest-version installation triggers cascading API breakages. Installing declared
   dependencies first and preferring era-appropriate versions is the established
   mitigation; true version inference is the heavier remedy and a natural
   future-work / extension point.
