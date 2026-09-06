"""Create/reuse an isolated virtual environment per target, and return its
python executable. Installs must never touch the interpreter running this
tool — everything apply.py does happens inside one of these venvs.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import venv

# See NOTEBOOK_SUPPORT_TASK.md's "where the execution libraries live":
# nbclient/nbformat are the driver (repair_tool's own dependency, see
# pyproject.toml); only ipykernel needs to live inside each target venv,
# since that's what actually launches a kernel using that venv's packages.
_IPYKERNEL_INSTALL_TIMEOUT = 120

# Overridable (tests point this at a temp dir instead of polluting the repo).
VENV_ROOT = os.path.join(os.getcwd(), ".repair_venvs")


def get_venv_python(target_path: str) -> str:
    """Return the python executable for `target_path`'s isolated venv,
    creating the venv first if it doesn't exist yet. Reused on later calls
    for the same target (same venv directory, derived from its absolute path).
    """
    venv_dir = _venv_dir_for(target_path)
    python_exe = _python_executable(venv_dir)

    if not os.path.isfile(python_exe):
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)

    return python_exe


def get_workspace_copy(target_path: str) -> str:
    """Return the path to target_path's persistent working copy, creating it
    from the original the first time. Phase 5's code edits (and every run
    after the first) operate on this copy — the original input file passed
    to repair() is read exactly once, right here, and never touched again.
    """
    workspace_dir = os.path.join(_venv_dir_for(target_path), "workspace")
    os.makedirs(workspace_dir, exist_ok=True)
    copy_path = os.path.join(workspace_dir, os.path.basename(target_path))
    if not os.path.isfile(copy_path):
        shutil.copyfile(target_path, copy_path)
    return copy_path


def ensure_ipykernel(python_exe: str) -> tuple[bool, str]:
    """Make sure `python_exe`'s environment can launch a Jupyter kernel.

    Checks first (cheap) so a repeat call on an already-equipped venv is a
    no-op; installs otherwise. Never raises: any failure comes back as
    (False, <log>), same contract as apply.apply().
    """
    try:
        check = subprocess.run(
            [python_exe, "-c", "import ipykernel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"could not check for ipykernel: {exc}"
    if check.returncode == 0:
        return True, "ipykernel already present"

    try:
        completed = subprocess.run(
            [python_exe, "-m", "pip", "install", "ipykernel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_IPYKERNEL_INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"ipykernel install timed out after {_IPYKERNEL_INSTALL_TIMEOUT}s"

    log = completed.stdout + completed.stderr
    return completed.returncode == 0, log


def _venv_dir_for(target_path: str) -> str:
    key = hashlib.sha1(os.path.abspath(target_path).encode()).hexdigest()[:12]
    return os.path.join(VENV_ROOT, key)


def _python_executable(venv_dir: str) -> str:
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")
