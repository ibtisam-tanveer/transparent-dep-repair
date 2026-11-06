"""WHY a notebook failed when the harness actually ran it (TASK_next_steps.md
step 1).

The static taxonomy label says what a failure *was* in the 2023 study; this
says what the last observed failure is now, as a short stable slug, so
analyze_results.py can report "what fraction of notebook failures are even
dependency-repairable vs. need data / compute / network" -- the thesis's
scope finding. It reads only the captured stderr/stdout of the last failing
run_target/verify step; it never guesses beyond what the text shows, and
anything it does not recognise is reported as `code_error:<ExceptionName>`
rather than being forced into a friendlier bucket.

Slugs
  dependency:<kind>        the failure is a dependency problem (diagnose.py's kind:
                           missing_module, import_name, module_attribute_removed,
                           object_attribute_error, ...)
  local_module             ModuleNotFoundError for a name the repo itself provides
  cell_timeout_download    a cell hit the time limit while fetching something (URL /
                           download in the cell or its output)
  cell_timeout             a cell hit the time limit (slow computation or hang)
  notebook_hard_timeout    the whole-notebook watchdog fired
  missing_data_file        FileNotFoundError / "No such file or directory"
  network_error            connection / DNS / HTTP / SSL failure
  gpu_required             CUDA / GPU needed
  credentials              authentication / API key / permission problem
  out_of_memory            MemoryError / killed for memory
  shell_command_failed     a shell command in the notebook (`!cmd`, `%%bash`) failed: an external
                           tool / binary / CLI outside the Python packages (CalledProcessError)
  external_tool_missing    a program the code tries to execute is not installed ("Can't execute `prog`")
  kernel_died              the kernel died mid-run
  code_error:<Exception>   some other runtime error unrelated to dependencies
  unobserved               no failing run was observed (e.g. never executed)
"""

from __future__ import annotations

import re

UNOBSERVED = "unobserved"

_TIMEOUT_RE = re.compile(r"CellTimeoutError|TimeoutExpired|timed out while it was being executed|Cell execution timed out", re.I)
_HARD_TIMEOUT_RE = re.compile(r"NotebookHardTimeout")
_DOWNLOAD_HINT_RE = re.compile(r"https?://|download|urlopen|urlretrieve|wget|requests\.get|read_csv\(\s*['\"]http", re.I)
_DEAD_KERNEL_RE = re.compile(r"DeadKernelError|kernel died|kernel has died", re.I)
_MEMORY_RE = re.compile(r"MemoryError|Cannot allocate memory|out of memory|\bOOM\b", re.I)
_GPU_RE = re.compile(r"CUDA|cuda|no NVIDIA|NVIDIA driver|GPU|torch\.cuda|cuDNN", re.I)
_CRED_RE = re.compile(
    r"NoCredentialsError|AuthenticationError|Unauthorized|HTTP Error 40[13]|status code:? 40[13]|API[ _-]?key|access token|"
    r"invalid[_ ]credentials|PermissionError|Permission denied|LoginRequired", re.I,
)
_NETWORK_RE = re.compile(
    r"URLError|ConnectionError|ConnectionRefused|ConnectionReset|Max retries exceeded|Name or service not known|"
    r"Temporary failure in name resolution|nodename nor servname|SSLError|HTTPError|HTTPSConnectionPool|"
    r"gaierror|RemoteDisconnected|socket\.timeout|ReadTimeout", re.I,
)
_MISSING_FILE_RE = re.compile(
    r"FileNotFoundError|No such file or directory|(file|path|directory)[^\n]{0,80}does not exist|cannot find the (file|path)", re.I
)
_SHELL_FAILED_RE = re.compile(r"CalledProcessError|Command '.*' returned non-zero exit status", re.I)
_TOOL_MISSING_RE = re.compile(r"Can't execute `[^`]+`|command not found|is not installed|executable .* not found", re.I)
_EXC_NAME_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))\b")


def _exception_name(exception_line: str) -> str:
    match = _EXC_NAME_RE.match((exception_line or "").strip())
    return match.group(1).split(".")[-1] if match else "unknown"


def classify_runtime_reason(
    exception_line: str | None,
    stderr: str,
    stdout: str,
    observed_category: str,
    diagnosis_kind: str,
    local_module: bool,
    cell_source: str = "",
) -> str:
    """Slug for the last observed failure; see the module docstring."""
    if local_module:
        return "local_module"
    if exception_line is None and not (stderr or "").strip():
        return UNOBSERVED

    text = f"{exception_line or ''}\n{stderr or ''}"

    # A failing shell command or a missing external program is a system-tool
    # problem even when the static taxonomy filed it under a dependency category.
    if _SHELL_FAILED_RE.search(exception_line or ""):
        return "shell_command_failed"
    if _TOOL_MISSING_RE.search(text) and not re.search(r"No module named", text):
        return "external_tool_missing"

    # A dependency problem is judged first on the *diagnosis* (the same
    # evidence the repair loop acts on), so an A-D failure is never relabelled
    # as environmental just because its traceback mentions a URL.
    if observed_category in ("A", "B", "C", "D"):
        kind = diagnosis_kind if diagnosis_kind and diagnosis_kind != "unknown" else f"other({_exception_name(exception_line or '')})"
        return f"dependency:{kind}"

    if _HARD_TIMEOUT_RE.search(text):
        return "notebook_hard_timeout"
    if _TIMEOUT_RE.search(text):
        # The CellTimeoutError message embeds a preview of the cell's source,
        # so a fetch in the stuck cell shows up in `text`; the cell's own
        # printed output (stdout) is the other place a download announces itself.
        fetching = _DOWNLOAD_HINT_RE.search(text) or _DOWNLOAD_HINT_RE.search(stdout or "") or _DOWNLOAD_HINT_RE.search(cell_source or "")
        return "cell_timeout_download" if fetching else "cell_timeout"
    if _DEAD_KERNEL_RE.search(text):
        return "kernel_died"
    if _MEMORY_RE.search(text):
        return "out_of_memory"
    if _GPU_RE.search(text):
        return "gpu_required"
    if _MISSING_FILE_RE.search(text):
        return "missing_data_file"
    if _NETWORK_RE.search(text):
        return "network_error"
    if _CRED_RE.search(text):
        return "credentials"
    return f"code_error:{_exception_name(exception_line or '')}"


# Reasons that mean "needs data / compute / network / credentials" rather than
# "the code is broken for some other reason" -- used for the scope finding.
ENVIRONMENTAL_REASONS = frozenset({
    "cell_timeout_download", "cell_timeout", "notebook_hard_timeout", "missing_data_file", "network_error",
    "gpu_required", "credentials", "out_of_memory", "kernel_died",
})


def reason_group(reason: str) -> str:
    """Coarse group for the scope finding."""
    if not reason or reason == UNOBSERVED:
        return "unobserved"
    if reason.startswith("dependency:"):
        return "dependency"
    if reason == "local_module":
        return "local_module"
    if reason in ("shell_command_failed", "external_tool_missing"):
        return "external_tool"
    if reason in ENVIRONMENTAL_REASONS:
        return "needs_data_compute_or_network"
    if reason.startswith("code_error:"):
        return "code_error"
    return "other"
