"""Create/reuse an isolated virtual environment per target, and return its
python executable. Installs must never touch the interpreter running this
tool — everything apply.py does happens inside one of these venvs.
"""

from __future__ import annotations

import hashlib
import os
import sys
import venv

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


def _venv_dir_for(target_path: str) -> str:
    key = hashlib.sha1(os.path.abspath(target_path).encode()).hexdigest()[:12]
    return os.path.join(VENV_ROOT, key)


def _python_executable(venv_dir: str) -> str:
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")
