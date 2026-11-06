"""Run and edit `.ipynb` notebooks so they can plug into the same loop as
`.py` scripts: runner.run_project() and apply.apply_code_edit() both
detect a `.ipynb` target and dispatch here. Nothing downstream (diagnose,
repair, llm) needs to know or care that the target was a notebook rather
than a script -- this module's only job is producing a RunResult that
looks like a normal Python error, and editing cell source in place of file
text. See NOTEBOOK_SUPPORT_TASK.md for the full design reasoning.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time

from .proc import kill_process_group, register_group, run_group, unregister_group
from .runner import RunResult
from .venv_manager import ensure_ipykernel, ensure_ipython

# TASK_prepilot_fixes.md #1: nbclient's `timeout` is per CELL and relies on
# nbclient itself staying responsive. This is the safety net on top of it: a
# wall-clock cap on the WHOLE notebook, enforced from outside nbclient by
# killing the kernel's entire process group, so a notebook can never outlive
# its budget regardless of what the kernel or nbclient is doing.
# Which executor runs notebooks: "kernel" (default; a real Jupyter kernel via nbclient)
# or "inproc" (inproc_runner.py: plain process, no ZeroMQ). The linux/amd64 container on
# an Apple-Silicon host sets "inproc" because a ZeroMQ-holding process cannot spawn any
# child there (see inproc_runner.py).
EXECUTOR_ENV = "REPAIR_TOOL_NOTEBOOK_EXECUTOR"
INPROC_RESULT_PREFIX = "INPROC_RESULT:"
_INPROC_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inproc_runner.py")

NOTEBOOK_TOTAL_TIMEOUT_FACTOR = 5
NOTEBOOK_MIN_TOTAL_TIMEOUT = 300
# After the kernel group is killed, how long to wait for nbclient to notice
# and unwind before abandoning its (daemon) thread and returning anyway.
HARD_TIMEOUT_JOIN_GRACE = 15

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _collect_stdout(nb) -> str:
    """Join every stream-stdout output across all cells, in order."""
    chunks = []
    for cell in nb.cells:
        if cell.get("cell_type") != "code":
            continue
        for output in cell.get("outputs", []):
            if output.get("output_type") == "stream" and output.get("name") == "stdout":
                chunks.append(output.get("text", ""))
    return "".join(chunks)


def _first_error_output(nb) -> dict | None:
    """The first code cell's error output, in notebook order, or None if
    every cell ran cleanly. Read from the notebook's own recorded outputs
    rather than from the raised exception -- nbclient's CellExecutionError
    doesn't reliably expose ename/evalue as attributes across versions
    (confirmed against the installed version), but the notebook itself
    always records a structured error output for the cell that failed.
    """
    for cell in nb.cells:
        if cell.get("cell_type") != "code":
            continue
        for output in cell.get("outputs", []):
            if output.get("output_type") == "error":
                return output
    return None


def _format_error(error: dict) -> str:
    """Build a plain-text traceback from a notebook error output, in the
    same unindented 'ExceptionName: message' shape diagnose.py already
    parses for subprocess tracebacks -- ANSI-stripped, since Jupyter
    colorizes ename/evalue/traceback and an ANSI-prefixed line would
    silently fail diagnose.py's classification regex instead of crashing.
    """
    ename = _strip_ansi(str(error.get("ename", "")))
    evalue = _strip_ansi(str(error.get("evalue", "")))
    traceback_lines = [_strip_ansi(line) for line in error.get("traceback", [])]
    header = f"{ename}: {evalue}"
    if traceback_lines:
        return "\n".join(traceback_lines) + "\n" + header
    return header


def _run_notebook_inproc(
    path: str, timeout: int, python_exe: str | None, total_timeout: float | None, cwd: str | None,
) -> RunResult:
    """Run `path` with inproc_runner.py in the TARGET environment's interpreter.
    The whole run is process-group-killed at `total`; the per-cell timeout is
    inside the runner (SIGALRM), the group kill is the backstop for a cell stuck
    in C code that ignores signals."""
    import sys

    executable = python_exe or sys.executable
    if not os.path.isfile(path):
        return RunResult(ok=False, returncode=-1, stdout="", stderr=f"FileNotFoundError: no such file: {path!r}")
    ok, log = ensure_ipython(executable)
    if not ok:
        return RunResult(ok=False, returncode=-1, stdout="", stderr=f"KernelSetupError: could not install IPython: {log[-500:]}")

    work_dir = cwd or os.path.dirname(os.path.abspath(path))
    total = total_timeout if total_timeout is not None else max(timeout * NOTEBOOK_TOTAL_TIMEOUT_FACTOR, NOTEBOOK_MIN_TOTAL_TIMEOUT)
    fd, progress_file = tempfile.mkstemp(suffix=".progress.json")
    os.close(fd)
    try:
        from . import condaenv

        try:
            completed = run_group(
                [executable, _INPROC_RUNNER, os.path.abspath(path), str(int(timeout)), work_dir, progress_file],
                text=True, encoding="utf-8", errors="replace", timeout=total, cwd=work_dir,
                env=condaenv.activation_env(executable),  # None for a venv -> inherit as before
            )
        except subprocess.TimeoutExpired as exc:
            progress, cell = "", ""
            try:
                with open(progress_file, encoding="utf-8") as f:
                    data = json.load(f)
                progress, cell = data.get("progress", ""), data.get("stuck_cell", "")
            except (OSError, ValueError):
                pass
            return RunResult(
                ok=False, returncode=-1, stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(f"NotebookHardTimeout: the notebook exceeded its {total:.0f}s total budget; "
                        "its process and all child processes were killed"),
                progress=progress, stuck_cell=cell,
            )
        except OSError as exc:
            return RunResult(ok=False, returncode=-1, stdout="", stderr=f"OSError: could not run {executable!r}: {exc}")

        for line in reversed(completed.stdout.splitlines()):
            if line.startswith(INPROC_RESULT_PREFIX):
                try:
                    data = json.loads(line[len(INPROC_RESULT_PREFIX):])
                except ValueError:
                    break
                return RunResult(
                    ok=bool(data["ok"]), returncode=0 if data["ok"] else -1, stdout=data.get("stdout", ""),
                    stderr=data.get("stderr", ""), progress=data.get("progress", ""), stuck_cell=data.get("stuck_cell", ""),
                )
        return RunResult(
            ok=False, returncode=-1, stdout=completed.stdout[-2000:],
            stderr=f"RuntimeError: the notebook executor produced no result (exit {completed.returncode}): {completed.stderr[-800:]}",
        )
    finally:
        try:
            os.remove(progress_file)
        except OSError:
            pass


def _progress_and_cell(nb) -> tuple[str, str]:
    """("last started code cell K of N", head of that cell's source). nbclient
    stamps a cell's execution_count when it STARTS running, so the last stamped
    code cell is the one that failed or was still running at the timeout."""
    code = [c for c in nb.cells if c.get("cell_type") == "code"]
    started = [i for i, c in enumerate(code) if c.get("execution_count") is not None]
    if not code or not started:
        return (f"no code cell started (of {len(code)})" if code else "", "")
    k = started[-1]
    source = code[k].get("source", "")
    source = "".join(source) if isinstance(source, list) else str(source)
    return f"last started code cell {k + 1} of {len(code)}", source[:300]


def _kernel_pgid(km) -> int | None:
    """Process-group id of `km`'s kernel, or None if it hasn't started.
    jupyter_client launches kernels with start_new_session=True, so the
    kernel is its own group leader and the group holds every child it spawns."""
    provisioner = getattr(km, "provisioner", None)
    pid = getattr(provisioner, "pid", None) or getattr(getattr(provisioner, "process", None), "pid", None)
    if not pid:
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return pid


def run_notebook(
    path: str, timeout: int = 60, python_exe: str | None = None, total_timeout: float | None = None,
    cwd: str | None = None,
) -> RunResult:
    """Execute `path` (already confirmed to exist by runner.run_project)
    cell by cell, in a kernel launched from `python_exe`'s environment, and
    return a RunResult in the same shape a subprocess run produces.

    Stops at the first failing cell (nbclient's default behaviour -- no
    allow_errors) rather than running every remaining cell only to discard
    the results: cheaper, and avoids side effects (file writes, network
    calls) from cells whose output will never be examined. Never raises:
    a bad notebook file, a kernel-launch failure, or a timeout all come
    back as a failed RunResult.

    `timeout` is nbclient's per-cell limit. `total_timeout` (default: a
    multiple of it) caps the whole run with a process-group kill, so the
    kernel and everything it spawned die at the budget -- see proc.py.
    """
    import sys

    if os.environ.get(EXECUTOR_ENV, "kernel") == "inproc":
        return _run_notebook_inproc(path, timeout, python_exe, total_timeout, cwd)

    import nbformat
    from jupyter_client.kernelspec import KernelSpec
    from jupyter_client.manager import AsyncKernelManager
    from jupyter_core.utils import run_sync
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError

    executable = python_exe or sys.executable

    try:
        nb = nbformat.read(path, as_version=4)
    except Exception as exc:  # noqa: BLE001 - any parse failure -> failed RunResult, never a crash
        return RunResult(ok=False, returncode=-1, stdout="", stderr=f"NotebookReadError: could not parse {path!r}: {exc}")

    # nbclient only touches the cells it RUNS: every other cell keeps the
    # execution_count and outputs saved in the author's notebook (kiste pilot:
    # "the last cell with a count" was just the last cell the AUTHOR ran, so the
    # recorded stop position and stuck cell were wrong, and stale output text leaked
    # into stdout). Start from a clean state so what we report is this run only.
    for cell in nb.cells:
        if cell.get("cell_type") == "code":
            cell["execution_count"] = None
            cell["outputs"] = []

    ok, log = ensure_ipykernel(executable)
    if not ok:
        return RunResult(
            ok=False, returncode=-1, stdout="", stderr=f"KernelSetupError: could not install ipykernel: {log}"
        )

    # KernelManager.format_kernel_cmd() builds the launch command from
    # self.kernel_spec.argv -- a plain `km.kernel_cmd = [...]` attribute is
    # silently ignored (confirmed: not a real trait on this KernelManager,
    # and format_kernel_cmd never reads it), so every notebook was actually
    # executing under this tool's OWN interpreter instead of the target
    # venv's, defeating isolation entirely. Assigning a KernelSpec directly
    # to the private `_kernel_spec` slot bypasses the by-name kernelspec
    # lookup and is the mechanism that's actually honored -- confirmed
    # directly (sys.executable inside the kernel matches `executable`, and
    # a package installed only in the target venv becomes importable).
    #
    # AsyncKernelManager, NOT the sync KernelManager (TASK_prepilot_fixes.md
    # #1, the actual root cause of the "hung forever" notebooks): nbclient
    # polls kernel messages with `kc.iopub_channel.get_msg(timeout=1)`, which
    # on a sync manager's client is a *blocking* zmq poll() executed directly
    # on the asyncio loop. That starves the very task that enforces the
    # per-cell timeout, so a hung cell never timed out (reproduced: a plain
    # `time.sleep(300)` cell with timeout=5 ran to completion). With the
    # async manager the polling yields to the loop and the timeout fires.
    km = AsyncKernelManager()
    km._kernel_spec = KernelSpec(
        argv=[executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        display_name="repair_tool target venv",
        language="python",
    )

    # TASK_fix_repo_execution.md: nbclient's execution cwd defaults to
    # wherever this *process* happens to be running from, not the
    # notebook's own directory -- unlike `jupyter nbconvert --execute`,
    # which sets it to the notebook's folder by default. Without this, a
    # local sibling import (`import helper`) or a relative-path data read
    # fails even when the sibling file is physically present right next to
    # the notebook (Part A's whole-repo copy).
    # `cwd` overrides that when workdir.py found a better directory for it.
    resources = {"metadata": {"path": cwd or os.path.dirname(os.path.abspath(path))}}
    client = NotebookClient(nb, km=km, timeout=timeout, allow_errors=False, resources=resources)

    # nbclient does not take ownership of an externally-provided KernelManager
    # (confirmed: km.has_kernel stays True after execute() otherwise), so
    # both the kernel process AND its ZMQ communication channels (confirmed
    # separately: client.kc's channels stay open too, otherwise, producing
    # "unclosed socket" ResourceWarnings) must be torn down explicitly --
    # always, success or failure -- or every run_notebook() call leaks both.
    total = total_timeout if total_timeout is not None else max(timeout * NOTEBOOK_TOTAL_TIMEOUT_FACTOR, NOTEBOOK_MIN_TOTAL_TIMEOUT)
    outcome: dict = {}

    def _execute() -> None:
        try:
            client.execute()
            outcome["exc"] = None
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread below
            outcome["exc"] = exc

    # Executed in a daemon thread and watched from here, so the whole-notebook
    # deadline holds even if nbclient never returns on its own. Wall-clock
    # (time.time) on purpose: on macOS the monotonic clock pauses during sleep.
    executor = threading.Thread(target=_execute, name="notebook-exec", daemon=True)
    pgid: int | None = None
    hard_timed_out = False
    try:
        executor.start()
        deadline = time.time() + total
        while executor.is_alive():
            executor.join(0.25)
            if pgid is None:
                pgid = _kernel_pgid(km)
                if pgid is not None:
                    register_group(pgid)
            if executor.is_alive() and time.time() >= deadline:
                hard_timed_out = True
                kill_process_group(pgid)
                executor.join(HARD_TIMEOUT_JOIN_GRACE)
                break
    finally:
        try:
            kc = getattr(client, "kc", None)
            if kc is not None:
                kc.stop_channels()
        except Exception:  # noqa: BLE001 - cleanup must never crash the tool either
            pass
        try:
            if km.has_kernel:
                run_sync(km.shutdown_kernel)(now=True)
        except Exception:  # noqa: BLE001
            pass
        # shutdown_kernel kills the kernel itself; the group kill also takes
        # everything the notebook spawned (subprocesses, multiprocessing
        # workers) -- the part that used to be left orphaned.
        if pgid is not None:
            kill_process_group(pgid, grace=1.0)
            unregister_group(pgid)

    progress, stuck_cell = _progress_and_cell(nb)

    if hard_timed_out:
        return RunResult(
            ok=False, returncode=-1, stdout=_collect_stdout(nb),
            stderr=(
                f"NotebookHardTimeout: the notebook exceeded its {total:.0f}s total budget; "
                "its kernel and all child processes were killed"
            ),
            progress=progress, stuck_cell=stuck_cell,
        )

    exc = outcome.get("exc")
    if exc is not None and not isinstance(exc, CellExecutionError):
        # timeout / kernel launch failure / etc.
        return RunResult(ok=False, returncode=-1, stdout=_collect_stdout(nb), stderr=f"{type(exc).__name__}: {exc}",
                         progress=progress, stuck_cell=stuck_cell)
    # (a CellExecutionError is fine: the failing cell's structured error
    # output is read below)

    stdout = _collect_stdout(nb)
    error = _first_error_output(nb)

    if error is None:
        return RunResult(ok=True, returncode=0, stdout=stdout, stderr="")

    return RunResult(ok=False, returncode=-1, stdout=stdout, stderr=_format_error(error),
                     progress=progress, stuck_cell=stuck_cell)


def edit_notebook_cells(edits: list[dict], workspace_path: str) -> tuple[bool, str]:
    """Apply find/replace edits across a notebook's code cells, in place.

    Mirrors apply.apply_code_edit's contract exactly: `workspace_path` must
    be a working copy, never the original input file; a `find` string that
    doesn't appear verbatim in any code cell is a clean failure, not a
    crash; nothing is written back unless every edit applies successfully
    (no partial edits on a later failure).
    """
    import nbformat

    if not edits:
        return False, "no edits provided"

    try:
        nb = nbformat.read(workspace_path, as_version=4)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read notebook: {exc}"

    applied = []
    for i, edit in enumerate(edits, start=1):
        find, replace = edit.get("find", ""), edit.get("replace", "")
        if not find:
            return False, f"edit {i} failed to apply: empty find string"

        matched_cell = None
        for cell in nb.cells:
            if cell.get("cell_type") == "code" and find in cell.get("source", ""):
                matched_cell = cell
                break

        if matched_cell is None:
            return False, f"edit {i} failed to apply: {find!r} not found verbatim in any code cell"

        matched_cell["source"] = matched_cell["source"].replace(find, replace, 1)
        applied.append(f"{find!r} -> {replace!r}")

    try:
        nbformat.write(nb, workspace_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not write notebook: {exc}"

    return True, "; ".join(applied)
