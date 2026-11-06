"""Process-group-aware subprocess helpers (TASK_prepilot_fixes.md #1).

Why this exists: `subprocess.run(..., timeout=N)` only kills the *direct*
child on timeout; anything that child spawned is left running. And
jupyter_client launches every kernel with `start_new_session=True`, so a
kernel is never in its parent's process group and a group-level kill of the
worker would miss it. Both let a notebook's processes outlive their budget.
(The hang itself -- a kernel alive ~90 minutes past the limit -- had a
separate root cause, fixed in notebook.py: a hung cell never timed out
because a sync KernelManager starved nbclient's timeout task. These helpers
are the defence in depth behind that fix.)

The fixes here are deliberately small and independent:
- `run_group` runs a command in its own process group and, on timeout (or an
  interrupt), kills the WHOLE group (SIGTERM, then SIGKILL) before draining.
- `kill_process_group` is the shared SIGTERM-then-SIGKILL primitive.
- a small file-based registry lets the harness find and reap any kernel
  process group a worker started, even if the worker itself was SIGKILLed
  and never got to clean up.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

KILL_GRACE_SECONDS = 3.0
# How long to wait for pipes to close after a group kill before giving up on
# them (a process that escaped the group via setsid could still hold them).
DRAIN_AFTER_KILL_SECONDS = 5.0

# Path of the file the worker appends kernel process-group ids to; set by the
# harness in the worker's environment (see evaluation/run_eval.py).
REGISTRY_ENV = "REPAIR_TOOL_KERNEL_REGISTRY"


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def _reap_if_child(pgid: int) -> None:
    """If the group leader is our own child it stays a zombie -- which still
    counts as a live group member -- until waited on."""
    try:
        os.waitpid(pgid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def kill_process_group(pgid: int | None, grace: float = KILL_GRACE_SECONDS) -> bool:
    """SIGTERM the whole group, wait up to `grace` seconds for it to empty,
    then SIGKILL whatever is left. Returns True if a signal was sent.

    Never signals our own group or pid <= 1, and never raises."""
    if pgid is None or pgid <= 1:
        return False
    try:
        if pgid == os.getpgrp():
            return False
    except OSError:
        return False

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        _reap_if_child(pgid)
        return False
    except OSError:
        return False

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        _reap_if_child(pgid)
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    # SIGKILL can't be ignored, but the kernel still needs a moment to tear
    # the group down; reap so a zombie leader doesn't look "alive".
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        _reap_if_child(pgid)
        if not _group_alive(pgid):
            break
        time.sleep(0.05)
    return True


def run_group(cmd, timeout: float | None = None, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run(cmd, capture_output=True, timeout=...)`, but the child
    gets its own process group and a timeout (or Ctrl-C) kills that whole
    group, so no grandchild survives and no pipe is left open to block the
    caller. Raises `subprocess.TimeoutExpired` exactly like `subprocess.run`
    (carrying whatever output was captured) and `OSError` if it can't start.
    """
    kwargs.pop("capture_output", None)
    stdin_data = kwargs.pop("input", None)
    if stdin_data is not None:
        kwargs["stdin"] = subprocess.PIPE
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate(input=stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=DRAIN_AFTER_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            # Something escaped the group and still holds a pipe; don't wait.
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from None
    except BaseException:
        kill_process_group(proc.pid)
        proc.wait()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


# --- kernel process-group registry ------------------------------------

def register_group(pgid: int) -> None:
    """Record `pgid` in the registry file (a no-op when no registry is set)."""
    _append(f"+{pgid}")


def unregister_group(pgid: int) -> None:
    _append(f"-{pgid}")


def _append(line: str) -> None:
    path = os.environ.get(REGISTRY_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # a missing registry only loses the belt-and-braces reap


def reap_registered(path: str | None) -> int:
    """Kill every process group registered in `path` and not unregistered.
    Returns how many groups were still alive and got killed. Never raises."""
    if not path or not os.path.isfile(path):
        return 0
    live: set[int] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if len(raw) < 2 or raw[0] not in "+-":
                    continue
                try:
                    pgid = int(raw[1:])
                except ValueError:
                    continue
                if raw[0] == "+":
                    live.add(pgid)
                else:
                    live.discard(pgid)
    except OSError:
        return 0
    killed = 0
    for pgid in live:
        if _group_alive(pgid) and kill_process_group(pgid):
            killed += 1
    return killed
