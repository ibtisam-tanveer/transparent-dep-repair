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

## Phase 1 — run a project and capture its error (current)

`runner.py` runs a target `.py` file **as a subprocess** (never by importing
it, so a broken target can never crash this tool) and returns a structured
`RunResult`.

```python
from runner import run_project

result = run_project("broken_examples/02_numpy_float.py")
assert result.ok is False
assert "AttributeError" in result.stderr
```

CLI:

```bash
python runner.py <path-to-target.py> [--timeout SECONDS]
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

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # dev/test only, not needed to use runner.py itself
python -m unittest tests.test_runner -v
```

`pip install -e ".[dev]"` also registers a `repair-tool-run` console script
(equivalent to `python runner.py`) — see `pyproject.toml`.

### Manual checks

```bash
python runner.py broken_examples/02_numpy_float.py   # prints the numpy AttributeError, tool stays alive
python runner.py hello.py                              # prints OK
```

### CI

`.github/workflows/tests.yml` runs the test suite on every push, on Python
3.10 and 3.12 — the same check you'd run locally, just automated. This
directly backs the vision doc's "Reproducibility" non-functional requirement:
the test suite behaves the same on a clean machine as it does locally.

## Roadmap (out of scope for Phase 1, tracked here for context)

- **Phase 2** — classify/diagnose the captured error (missing package vs.
  removed/changed API, per `broken_examples/MANIFEST.md`).
- **Phase 3** — propose and apply a fix inside an isolated virtual
  environment (no installs or mutation happen in Phase 1).
- **Phase 4** (implied by the vision doc) — verify each fix against
  authoritative package metadata, not just re-running the project.
- **Phase 5** — LLM-driven repair proposals, retrieval of package info.
- **Later** — assemble the transparency report (reason, source/provenance,
  alternatives rejected, verification result, confidence) that is this
  thesis's core contribution over prior repair tools (Vision Doc, Section 7).

## Project layout

```
runner.py                     Phase 1 deliverable
hello.py                      trivial script used to test the success path
broken_examples/              fixed regression set (8 scripts + MANIFEST.md)
tests/test_runner.py          unittest suite (success, failure, timeout, missing file)
tests/fixtures/hangs.py       infinite loop, used only to test the timeout path
pyproject.toml                packaging + dev-only extras (numpy, pandas, ...)
.github/workflows/tests.yml   CI: runs the test suite on every push
```

`runner.py` stays a single module at the repo root on purpose — that's the
`from runner import run_project` / `python runner.py <path>` interface the
Phase 1 spec requires. When Phase 2 needs a home for `diagnose.py`, that's
the natural point to introduce a package folder; no need to restructure
twice on a guess.
