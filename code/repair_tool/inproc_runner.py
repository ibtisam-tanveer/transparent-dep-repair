"""Self-contained notebook executor WITHOUT a ZeroMQ kernel.

Why: under Docker Desktop's Rosetta emulation (linux/amd64 on Apple Silicon) a
process that holds a ZeroMQ context cannot create ANY child process -- fork,
posix_spawn, subprocess, os.system all hang forever (reproduced with a bare
`zmq.Context()` + `subprocess.run(["echo"])` for every pyzmq from 23 to 27; a
plain process and a Python thread are fine). A Jupyter kernel always holds a
ZeroMQ context, so in that container every `!cmd`, `%%bash`, `subprocess`, and
even matplotlib's first import (font discovery) hangs until the cell timeout,
and a pilot run records them as "timeouts" that never happened on any real
machine. This script runs the cells in ONE IPython shell in a plain process, so
none of that applies.

It is run BY THE TARGET ENVIRONMENT'S interpreter (`python inproc_runner.py ...`),
so it must depend on nothing but the standard library and IPython, and it parses
the .ipynb with `json` (no nbformat). It is deliberately small:

  python inproc_runner.py NOTEBOOK CELL_TIMEOUT_SECONDS CWD PROGRESS_FILE

- cells run in order in a shared namespace; stops at the first error (like nbclient);
- a per-cell timeout via SIGALRM -> `CellTimeout` (a BaseException, so a user's
  `except Exception:` cannot swallow it); a C-level hang that ignores SIGALRM is
  handled by the caller killing this process group;
- before each cell starts it writes "last started code cell K of N" and the head
  of the cell's source to PROGRESS_FILE, so even a hard-killed run reports where it was;
- prints one line `INPROC_RESULT:<json>` with ok / stdout / stderr / progress / stuck_cell.

Differences from a kernel (the honest list): no comm/widget protocol, no
interactive `input()`, display data is printed as text, and a cell that hangs in C
code cannot be interrupted from inside -- the caller's process-group kill covers it.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import time
import traceback

RESULT_PREFIX = "INPROC_RESULT:"
MAX_OUTPUT_CHARS = 200_000


class CellTimeout(BaseException):
    """BaseException on purpose: user code's `except Exception` must not eat it."""


class _Tee(io.TextIOBase):
    """Collects what a cell writes while still passing it through."""

    def __init__(self, sink: list[str], passthrough):
        self.sink = sink
        self.passthrough = passthrough

    def write(self, text):
        self.sink.append(text)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def _source(cell: dict) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else str(src)


def _write_progress(path: str, progress: str, cell_head: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"progress": progress, "stuck_cell": cell_head}, f)
    except OSError:
        pass


def _emit(result: dict) -> None:
    sys.__stdout__.write("\n" + RESULT_PREFIX + json.dumps(result) + "\n")
    sys.__stdout__.flush()


def _format_error(exc: BaseException) -> str:
    """Traceback text whose LAST line is `Name: message` with the BARE class name, exactly
    as a kernel reports it (`ename: evalue`). traceback.format_exception qualifies
    non-builtin classes (`subprocess.CalledProcessError`), which the taxonomy
    classifier then files differently from the kernel's `CalledProcessError`: that made
    the same failing shell command count as in-scope on the host and out-of-scope in the
    container."""
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    body = "".join(lines).rstrip("\n").splitlines()
    message = str(exc)
    last = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    # replace the final (possibly multi-line) exception message block with the bare form
    tail = f"{type(exc).__module__}.{type(exc).__name__}" if type(exc).__module__ not in ("builtins", "__main__") else type(exc).__name__
    for i in range(len(body) - 1, -1, -1):
        if body[i].startswith(tail) or body[i].startswith(type(exc).__name__):
            return "\n".join(body[:i] + [last])
    return "\n".join(body)


def main(argv: list[str]) -> int:
    notebook, cell_timeout, cwd, progress_file = argv[1], int(argv[2]), argv[3], argv[4]

    try:
        with open(notebook, encoding="utf-8") as f:
            nb = json.load(f)
        cells = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]
    except Exception as exc:  # noqa: BLE001 - any parse failure is a failed result, never a crash
        _emit({"ok": False, "stdout": "", "stderr": f"NotebookReadError: could not parse {notebook!r}: {exc}",
               "progress": "", "stuck_cell": ""})
        return 0

    os.chdir(cwd)
    if "" not in sys.path:
        sys.path.insert(0, "")  # a kernel puts the working directory first; local imports rely on it
    os.environ.setdefault("MPLBACKEND", "agg")  # headless: no GUI backend, no inline machinery without a kernel

    try:
        from IPython.core.interactiveshell import InteractiveShell
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "stdout": "", "stderr": f"ImportError: IPython is not available in this environment: {exc}",
               "progress": "", "stuck_cell": ""})
        return 0

    class HeadlessShell(InteractiveShell):
        """The base InteractiveShell has no GUI integration and raises NotImplementedError
        ("Implement enable_gui in a subclass") on `%matplotlib inline|notebook|widget`, which
        a kernel's shell implements. There is no GUI here, so activating one is a no-op: the
        matplotlib backend itself (set by the magic, or Agg) is all that matters."""

        def enable_gui(self, gui=None):
            return None

    shell = HeadlessShell.instance()
    # IPython prints a (coloured) traceback to stdout when a cell fails; a kernel sends
    # it as a separate error message instead. We build the error text ourselves from
    # the exception, so keep the traceback out of the captured stdout.
    shell._showtraceback = lambda etype, evalue, stb: None
    out_chunks: list[str] = []
    err_chunks: list[str] = []

    def on_alarm(_signum, _frame):
        raise CellTimeout()

    signal.signal(signal.SIGALRM, on_alarm)

    started = 0
    for index, cell in enumerate(cells, start=1):
        source = _source(cell)
        if not source.strip():
            continue
        started = index
        progress = f"last started code cell {index} of {len(cells)}"
        head = source[:300]
        _write_progress(progress_file, progress, head)

        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(out_chunks, old_out), _Tee(err_chunks, old_err)
        signal.setitimer(signal.ITIMER_REAL, cell_timeout)
        try:
            result = shell.run_cell(source, store_history=False)
            failure = result.error_before_exec or result.error_in_exec
        except CellTimeout as exc:  # raised outside run_cell's own handler
            failure = exc
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            sys.stdout, sys.stderr = old_out, old_err

        if failure is not None:
            if isinstance(failure, CellTimeout):
                stderr = (f"CellTimeoutError: A cell timed out while it was being executed, after {cell_timeout} seconds.\n"
                          f"The message was: Cell execution timed out.\nHere is a preview of the cell contents:\n"
                          f"-------------------\n{head}\n-------------------")
            else:
                stderr = _format_error(failure)
            _emit({"ok": False, "stdout": "".join(out_chunks)[-MAX_OUTPUT_CHARS:], "stderr": stderr,
                   "progress": progress, "stuck_cell": head})
            return 0

    _emit({"ok": True, "stdout": "".join(out_chunks)[-MAX_OUTPUT_CHARS:], "stderr": "",
           "progress": f"completed {started} of {len(cells)} code cells", "stuck_cell": ""})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
