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


def venv_dir_for(target_path: str) -> str:
    """Public read-only access to the venv directory `get_venv_python`
    would (re)use for `target_path`, without creating anything. Lets a
    caller that already knows a venv exists (e.g. agent_repo.py, right
    after get_venv_python(repo_path)) locate its workspace/ subtree
    directly -- see get_repo_file_workspace_copy.
    """
    return _venv_dir_for(target_path)


def get_fresh_venv_python(tmp_root: str) -> str:
    """Create a brand-new isolated venv at `tmp_root` -- NOT the
    persistent, hash-cached store under VENV_ROOT -- and return its python
    executable. For evaluation runs (see agent.agent_repair's `fresh`
    option): the persistent cache is correct for interactive/manual use,
    but reusing it across repeated evaluation runs of the same target
    would silently make an already-fixed target look like it needed no
    repair. The caller owns `tmp_root`'s lifecycle (create before, delete
    after) -- this function only ever creates the venv inside it.
    """
    venv.EnvBuilder(with_pip=True, clear=True).create(tmp_root)
    return _python_executable(tmp_root)


def get_fresh_workspace_copy(target_path: str, tmp_root: str) -> str:
    """Like get_workspace_copy, but under a caller-owned `tmp_root`
    (see get_fresh_venv_python) instead of the persistent per-target
    store -- always a fresh copy of the real, unmodified original."""
    workspace_dir = os.path.join(tmp_root, "workspace")
    os.makedirs(workspace_dir, exist_ok=True)
    copy_path = os.path.join(workspace_dir, os.path.basename(target_path))
    shutil.copyfile(target_path, copy_path)
    return copy_path


def get_repo_file_workspace_copy(venv_dir: str, repo_path: str, rel_file_path: str) -> str:
    """Like get_workspace_copy, but scoped to a whole repo's ONE shared
    venv directory (`venv_dir`, e.g. from venv_dir_for(repo_path) or a
    fresh tmp_root) instead of giving every file in the repo its own venv
    -- every file's working copy lives under the same venv's workspace/
    subtree, preserving the file's path relative to the repo so files with
    the same basename in different subdirectories don't collide.
    """
    copy_path = os.path.join(venv_dir, "workspace", rel_file_path)
    os.makedirs(os.path.dirname(copy_path), exist_ok=True)
    if not os.path.isfile(copy_path):
        shutil.copyfile(os.path.join(repo_path, rel_file_path), copy_path)
    return copy_path


def _venv_dir_for(target_path: str) -> str:
    key = hashlib.sha1(os.path.abspath(target_path).encode()).hexdigest()[:12]
    return os.path.join(VENV_ROOT, key)


def _python_executable(venv_dir: str) -> str:
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")
