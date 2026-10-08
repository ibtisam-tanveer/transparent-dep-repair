"""Run and edit `.ipynb` notebooks so they can plug into the same loop as
`.py` scripts: runner.run_project() and apply.apply_code_edit() both
detect a `.ipynb` target and dispatch here. Nothing downstream (diagnose,
repair, llm) needs to know or care that the target was a notebook rather
than a script -- this module's only job is producing a RunResult that
looks like a normal Python error, and editing cell source in place of file
text. See NOTEBOOK_SUPPORT_TASK.md for the full design reasoning.
"""

from __future__ import annotations

import re

from .runner import RunResult
from .venv_manager import ensure_ipykernel

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


def run_notebook(path: str, timeout: int = 60, python_exe: str | None = None) -> RunResult:
    """Execute `path` (already confirmed to exist by runner.run_project)
    cell by cell, in a kernel launched from `python_exe`'s environment, and
    return a RunResult in the same shape a subprocess run produces.

    Stops at the first failing cell (nbclient's default behaviour -- no
    allow_errors) rather than running every remaining cell only to discard
    the results: cheaper, and avoids side effects (file writes, network
    calls) from cells whose output will never be examined. Never raises:
    a bad notebook file, a kernel-launch failure, or a timeout all come
    back as a failed RunResult.
    """
    import sys

    import nbformat
    from jupyter_client.kernelspec import KernelSpec
    from jupyter_client.manager import KernelManager
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError

    executable = python_exe or sys.executable

    try:
        nb = nbformat.read(path, as_version=4)
    except Exception as exc:  # noqa: BLE001 - any parse failure -> failed RunResult, never a crash
        return RunResult(ok=False, returncode=-1, stdout="", stderr=f"NotebookReadError: could not parse {path!r}: {exc}")

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
    km = KernelManager()
    km._kernel_spec = KernelSpec(
        argv=[executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        display_name="repair_tool target venv",
        language="python",
    )

    client = NotebookClient(nb, km=km, timeout=timeout, allow_errors=False)

    # nbclient does not take ownership of an externally-provided KernelManager
    # (confirmed: km.has_kernel stays True after execute() otherwise), so
    # both the kernel process AND its ZMQ communication channels (confirmed
    # separately: client.kc's channels stay open too, otherwise, producing
    # "unclosed socket" ResourceWarnings) must be torn down explicitly --
    # always, success or failure -- or every run_notebook() call leaks both.
    try:
        try:
            client.execute()
        except CellExecutionError:
            pass  # the failing cell's structured error output is read below
        except Exception as exc:  # noqa: BLE001 - timeout / kernel launch failure / etc.
            stdout = _collect_stdout(nb)
            return RunResult(ok=False, returncode=-1, stdout=stdout, stderr=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            kc = getattr(client, "kc", None)
            if kc is not None:
                kc.stop_channels()
        except Exception:  # noqa: BLE001 - cleanup must never crash the tool either
            pass
        try:
            if km.has_kernel:
                km.shutdown_kernel(now=True)
        except Exception:  # noqa: BLE001
            pass

    stdout = _collect_stdout(nb)
    error = _first_error_output(nb)

    if error is None:
        return RunResult(ok=True, returncode=0, stdout=stdout, stderr="")

    return RunResult(ok=False, returncode=-1, stdout=stdout, stderr=_format_error(error))


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
