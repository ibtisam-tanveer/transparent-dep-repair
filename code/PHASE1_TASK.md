# Task: Phase 1 — Run a Python project and capture its error

## Project context (read first)

We are building a tool that **automatically repairs broken Python dependency
configurations** (thesis: "AI-Driven Transparent Repair of Software Dependency
Configurations"). Many old scripts and notebooks no longer run because a
dependency is missing, or an API was removed or changed in a newer version.

The tool works as a loop: **run the project → read the error → propose a fix →
apply it → re-run to verify → explain every decision.** This task implements only
the **first step of that loop**: running a target file and capturing the result.

Do **not** implement diagnosis, fixing, installing, or any LLM calls in this task.
Those are later phases. Keep the scope tight.

## Goal

Provide a reusable function that runs a target Python file **as a separate
process** and returns a structured result describing whether it succeeded and
what error it produced. Broken target files must never crash our tool — their
failure must be captured and returned cleanly.

## Why a subprocess (design constraint)

Do NOT run the target by importing it. Import would let the target's exception
crash our own program and would not capture all failure modes. Run it as a
separate process with the `subprocess` module so the target can fail however it
likes while our tool calmly reads the outcome. This also prepares Phase 3, where
the same process will run inside an isolated virtual environment.

## Deliverables

Create a file `runner.py` containing:

1. A dataclass `RunResult` with exactly these fields:
   - `ok: bool` — True if the target exited without error (return code 0)
   - `returncode: int` — the process exit code
   - `stdout: str` — captured standard output
   - `stderr: str` — captured standard error (tracebacks live here)

2. A function `run_project(path: str, timeout: int = 60) -> RunResult` that:
   - runs the file at `path` using the current interpreter (`sys.executable`)
   - captures stdout and stderr as text (not bytes)
   - enforces the timeout; a timed-out run must be returned as a failed
     `RunResult` (not raised as an exception to the caller)
   - if `path` does not exist, returns a failed `RunResult` whose `stderr`
     explains that the file was not found (do not raise)

3. A command-line entry point so the file can be run directly:
   ```
   python runner.py <path-to-target.py>
   ```
   It should print a clear "OK" or "FAILED" summary and, on failure, the captured
   error text.

## Required behaviour / interface

```python
from runner import run_project

result = run_project("broken_examples/02_numpy_float.py")
assert result.ok is False
assert "AttributeError" in result.stderr
```

## Constraints

- Python 3.10+ only. Use the standard library only (`subprocess`, `sys`,
  `os`, `dataclasses`). No third-party packages.
- Cross-platform: rely on `sys.executable`, not a hard-coded "python".
- No side effects: this task must not install anything or modify the target.
- Keep it small and readable; add short docstrings and comments.

## Test set

A folder `broken_examples/` contains eight deliberately broken `.py` files
(e.g. `01_missing_package.py`, `02_numpy_float.py`, ...). Each has a comment at
the top stating what is wrong. Use these to test. Also test at least one file
that runs successfully (create a trivial `hello.py` that prints a line) to
confirm the success path returns `ok=True`.

## Definition of done

- Running `python runner.py <file>` on each of the eight broken examples prints a
  FAILED summary and shows the captured error — and our tool itself never crashes.
- Running it on a working file prints an OK summary and shows any stdout.
- A non-existent path returns a failed `RunResult` with an explanatory message,
  not an exception.
- A script that hangs (infinite loop) is reported as failed via the timeout,
  not left hanging.
- `from runner import run_project` works, and the assertions in the "Required
  behaviour" section pass.

## Out of scope (do NOT do in this task)

- No error classification / diagnosis (Phase 2).
- No fixing, pip installs, or virtual environments (Phase 3).
- No LLM or API calls (Phase 5).
- No `.ipynb` notebook handling yet — only plain `.py` files.

## Suggested manual check

```bash
# should print the numpy AttributeError, tool stays alive:
python runner.py broken_examples/02_numpy_float.py

# should print OK:
echo "print('hello')" > hello.py
python runner.py hello.py
```
