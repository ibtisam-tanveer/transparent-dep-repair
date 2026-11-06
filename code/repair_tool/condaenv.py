"""Run a conda environment's Python WITH its activation (TASK_improve_fix_rate.md #3).

Found measuring lever 3: conda provisioned R and rpy2 for h5n1-cambodia successfully,
and the notebook then failed with `ValueError: openrlib.R_HOME cannot be None` --
because the environment's python was started directly, without conda's activation, so
the variables its activate.d scripts set (R_HOME, and e.g. FER_* for pyferret) were
missing. A package that needs a system component is only usable once its environment
is activated, so lever 3 is incomplete without this.

`activation_env(python_exe)` returns the full environment `micromamba run -p <prefix>`
would give a process (cached on disk, refreshed when packages change), falling back to
the few variables that matter most if micromamba cannot be run. None for a plain venv
(nothing to activate), so non-conda runs are untouched.
"""

from __future__ import annotations

import json
import os
import subprocess

from . import condafallback

CACHE_NAME = ".repair_activation_env.json"
_PROBE = "import json, os; print('ACTENV:' + json.dumps(dict(os.environ)))"


def _manual_env(prefix: str) -> dict:
    """The minimum: the environment's bin first on PATH, CONDA_PREFIX, and R_HOME when R is present."""
    env = dict(os.environ)
    env["PATH"] = os.path.join(prefix, "bin") + os.pathsep + env.get("PATH", "")
    env["CONDA_PREFIX"] = prefix
    r_home = os.path.join(prefix, "lib", "R")
    if os.path.isdir(r_home):
        env["R_HOME"] = r_home
    return env


def _read_cache(prefix: str) -> dict | None:
    cache = os.path.join(prefix, CACHE_NAME)
    meta = os.path.join(prefix, "conda-meta")
    try:
        if os.path.getmtime(cache) < os.path.getmtime(meta):
            return None  # packages changed since the snapshot
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def activation_env(python_exe: str, timeout: int = 120) -> dict | None:
    """Activated environment for `python_exe` if it lives in a conda environment, else None."""
    from . import interpreter  # lazy: interpreter imports agent -> agent_tools -> condafallback

    prefix = condafallback.conda_prefix(python_exe)
    if prefix is None:
        return None
    cached = _read_cache(prefix)
    if cached is not None:
        return cached
    tool = interpreter.find_conda_tool()
    if tool is None:
        return _manual_env(prefix)
    try:
        completed = subprocess.run(
            [tool, "run", "-p", prefix, "python", "-c", _PROBE],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            env=interpreter._mamba_env(tool),
        )
    except (subprocess.TimeoutExpired, OSError):
        return _manual_env(prefix)
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("ACTENV:"):
            try:
                env = json.loads(line[len("ACTENV:"):])
            except ValueError:
                break
            try:
                with open(os.path.join(prefix, CACHE_NAME), "w", encoding="utf-8") as f:
                    json.dump(env, f)
            except OSError:
                pass
            return env
    return _manual_env(prefix)
