# Phase 1 Summary — Run a Python project and capture its error

Status: **done**, pushed to `github.com/ibtisam-tanveer/transparent-dep-repair` (commit `4197885`, branch `main`).

## What was asked (`PHASE1_TASK.md`)

Implement the first link of the tool's loop — *run → read error → propose fix
→ apply → re-verify → explain* — and nothing more. Concretely: a
`runner.py` that runs a target `.py` file as a subprocess and returns a
structured result describing success/failure, without diagnosing, fixing,
installing, or calling any LLM.

## What was built

| File | Purpose |
|---|---|
| `runner.py` | The deliverable: `RunResult` dataclass + `run_project(path, timeout=60)` + a CLI (`python runner.py <path> [--timeout N]`) |
| `broken_examples/` | The 8 provided broken scripts + `MANIFEST.md`, used as the fixed regression set |
| `hello.py` | Trivial success-path fixture (`print("hello")`) |
| `tests/test_runner.py` | 12-case `unittest` suite |
| `tests/fixtures/hangs.py` | Infinite-loop fixture, used only to exercise the timeout path |
| `pyproject.toml` | Packaging metadata + `dev` extra (numpy/pandas/scikit-learn/scipy/imageio/pyyaml) + `repair-tool-run` console script |
| `.github/workflows/tests.yml` | CI — runs the test suite on every push, Python 3.10 and 3.12 |
| `README.md` | Setup, usage, and a roadmap for later phases |

## Design decisions and why

- **Subprocess, not import.** The spec required this explicitly: importing a
  broken target would let its exception propagate into our own process. Used
  `subprocess.run([sys.executable, path], ...)` — `sys.executable` instead of
  a hard-coded `"python"` keeps it cross-platform and guarantees the same
  interpreter runs the target.
- **Never raises.** Three failure modes are normalized into a *returned*
  `RunResult` instead of a raised exception: non-existent path (checked with
  `os.path.isfile` before spawning anything), a real crash (non-zero return
  code, stderr captured as-is), and a timeout (`subprocess.TimeoutExpired`
  caught, partial output preserved, explanatory note appended to `stderr`).
  Both synthetic-failure cases use a `returncode=-1` sentinel since no real
  process exit code exists for them.
- **Text decoding robustness.** `text=True, encoding="utf-8", errors="replace"`
  so a target that writes non-UTF-8 bytes to stdout/stderr can't crash the
  runner on decode.
- **Stdlib only, by design** (`subprocess`, `sys`, `os`, `dataclasses`,
  `argparse`) — matches the spec's constraint and keeps `runner.py` itself
  dependency-free regardless of what's later needed for diagnosis/repair.

## How each definition-of-done item was verified

- All 8 `broken_examples/*.py` → `FAILED` printed, real traceback shown, tool
  stays alive (checked with a plain stdlib interpreter, no venv, to rule out
  the runner secretly depending on something).
- `hello.py` → `OK` printed with its stdout.
- Non-existent path → failed `RunResult`, explanatory `stderr`, no exception.
- `tests/fixtures/hangs.py` with `--timeout 2` → killed and reported failed
  in ~2s, not left hanging.
- `from runner import run_project` + the exact assertions from the spec's
  "Required behaviour" section → pass (`test_required_behaviour_assertion_from_spec`
  in the test suite).

## A test-environment subtlety worth knowing about

Per `broken_examples/MANIFEST.md`, examples 02–06 only reach their
*interesting* error (e.g. `AttributeError: module 'numpy' has no attribute
'float'`) once the real package is installed — otherwise you just see
`ModuleNotFoundError` for the package itself. The `dev` extra in
`pyproject.toml` installs numpy/pandas/scikit-learn/scipy/imageio/pyyaml
into `.venv` for exactly this reason. `seaborn` is deliberately **not**
installed, so example 01 keeps demonstrating a genuinely missing package.
This install is for testing only and has no bearing on `runner.py`'s own
zero-dependency footprint.

## Added beyond the literal spec (agreed with you before building)

- Automated test suite (`tests/test_runner.py`) instead of only manual
  spot-checks — covers all 8 broken examples individually plus success,
  missing-file, and timeout paths.
- Packaging (`pyproject.toml`) and CI (`.github/workflows/tests.yml`), added
  as a "solid foundation" pass after Phase 1 was already verified working.
  Deliberately kept light: `runner.py` stays a single module at the repo
  root (not moved into a package) so the `from runner import run_project` /
  `python runner.py <path>` interface the spec requires stays intact: the
  natural point to introduce a `repair_tool/` package is Phase 2, when
  `diagnose.py` needs a home next to it.
- A UI was considered and explicitly deferred — there's no diagnosis or
  transparency-report data yet for one to visualize (see `README.md`
  roadmap and the Vision Document's transparency-report design in
  Section 7).

## Explicitly out of scope (per the spec, unchanged)

No error classification, no fixing, no pip installs, no virtual-environment
creation, no LLM calls, no `.ipynb` handling. All deferred to later phases.

## How to run / verify

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m unittest tests.test_runner -v
python runner.py broken_examples/02_numpy_float.py   # FAILED + AttributeError
python runner.py hello.py                              # OK
```

## Note (Phase 3)

`runner.py` later moved to `repair_tool/runner.py` as part of Phase 3's
Step 0 restructure — see `PHASE3_SUMMARY.md` once written. The interface
described above (`run_project`, `RunResult`) is unchanged; only its import
path is.
