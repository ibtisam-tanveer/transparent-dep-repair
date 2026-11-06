"""Levers 2 and 3 -- use conda where pip cannot (TASK_improve_fix_rate.md #2, #3).

Lever 2, conda fallback for pip build failures. Observed misses: `abutils`
(grp_paper) and `cooltools` (matrix_paper) -- old compiled scientific packages
whose pip install tries to build from source and fails. conda-forge / bioconda
carry prebuilt binaries of many such packages that pip does not.

Lever 3, system components via conda. Observed miss: `rpy2` (h5n1-cambodia)
needs the R language installed on the system; pip cannot provide that, conda can
(`r-base`). A small CURATED table of such pip-package -> conda-packages pairs is
used; it is a heuristic and says so (`system_dependency_specs`).

Honest limit shared by both: they only work when the target environment is a
CONDA environment (it has `conda-meta/`). A plain venv cannot be extended with
conda packages, and that is reported as the reason, not hidden.

Every attempt is recorded (`lever`, `mechanism`, the specs, a log tail) so a fix
can be attributed to the mechanism that produced it.
"""

from __future__ import annotations

import os
import re
import subprocess

CONDA_TIMEOUT = 900
DEFAULT_CHANNELS = ("conda-forge", "bioconda")

# A pip failure that is about COMPILING, not about the package being absent.
_BUILD_FAILURE_RE = re.compile(
    r"Failed building wheel|Could not build wheels?|Failed to build|ERROR: Failed to build|"
    r"subprocess-exited-with-error|error: command '.*' failed|command 'gcc' failed|"
    r"setup\.py (?:egg_info|bdist_wheel|install) .*failed|Microsoft Visual C\+\+|"
    r"fatal error: .*: No such file or directory|unable to execute 'gcc'|"
    r"Building wheel for .* \(.*\): finished with status 'error'",
    re.I,
)

# pip package (lower-case) -> conda packages that provide it AND the system component it needs.
# Curated and small on purpose; extend with evidence, not guesses.
SYSTEM_DEPENDENCIES: dict[str, list[str]] = {
    "rpy2": ["r-base", "rpy2"],            # needs the R language
    "pygraphviz": ["graphviz", "pygraphviz"],
    "mpi4py": ["openmpi", "mpi4py"],
    "gdal": ["gdal"],
    "fiona": ["gdal", "fiona"],
    "rasterio": ["gdal", "rasterio"],
    "pycairo": ["cairo", "pycairo"],
}


def looks_like_build_failure(log: str) -> bool:
    return bool(_BUILD_FAILURE_RE.search(log or ""))


def conda_prefix(python_exe: str) -> str | None:
    """The conda environment prefix `python_exe` belongs to, or None for a venv."""
    prefix = os.path.dirname(os.path.dirname(os.path.abspath(python_exe)))
    return prefix if os.path.isdir(os.path.join(prefix, "conda-meta")) else None


def pip_spec_to_conda(spec: str) -> str:
    """'pkg==1.2.3' -> 'pkg=1.2.3' (conda's exact-version syntax); other specs unchanged."""
    return re.sub(r"^([A-Za-z0-9_.\-]+)==([^\s,;]+)$", r"\1=\2", spec.strip())


def base_name(spec: str) -> str:
    return re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def system_dependency_specs(package_spec: str) -> list[str] | None:
    """Conda packages to provision for `package_spec`, or None if it is not in the table."""
    return SYSTEM_DEPENDENCIES.get(base_name(package_spec))


def conda_install(
    python_exe: str, specs: list[str], channels: tuple[str, ...] = DEFAULT_CHANNELS, timeout: int = CONDA_TIMEOUT,
) -> dict:
    """`micromamba install` of `specs` into the environment `python_exe` lives in.
    Returns {ok, mechanism, specs, tool, log, unavailable?}; never raises."""
    from . import interpreter  # lazy: interpreter imports agent, which imports agent_tools, which imports this module

    prefix = conda_prefix(python_exe)
    if prefix is None:
        return {"ok": False, "mechanism": "conda", "specs": specs, "log": "",
                "unavailable": "the target environment is a plain venv, not a conda environment, "
                               "so conda packages cannot be installed into it"}
    tool = interpreter.find_conda_tool()
    if tool is None:
        return {"ok": False, "mechanism": "conda", "specs": specs, "log": "",
                "unavailable": "no micromamba/mamba/conda is available"}
    cmd = [tool, "install", "-y", "-p", prefix]
    for channel in channels:
        cmd += ["-c", channel]
    cmd += specs
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            env=interpreter._mamba_env(tool),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "mechanism": "conda", "specs": specs, "tool": tool, "log": f"timed out after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "mechanism": "conda", "specs": specs, "tool": tool, "log": f"could not run {tool!r}: {exc}"}
    return {"ok": completed.returncode == 0, "mechanism": "conda", "specs": specs, "tool": os.path.basename(tool),
            "log": (completed.stdout + completed.stderr)[-1500:]}


def conda_install_with_unpinned_retry(python_exe: str, package_spec: str, timeout: int = CONDA_TIMEOUT) -> dict:
    """Try the exact (pip-translated) spec; if conda has no such build, try the bare
    name and let the solver choose for this Python. The result says which was used."""
    first = conda_install(python_exe, [pip_spec_to_conda(package_spec)], timeout=timeout)
    if first["ok"] or first.get("unavailable"):
        first["pinned"] = True
        return first
    name = base_name(package_spec)
    second = conda_install(python_exe, [name], timeout=timeout)
    second["pinned"] = False
    second["first_attempt_log"] = first["log"][-400:]
    return second


def provision_system_dependency(python_exe: str, package_spec: str, timeout: int = CONDA_TIMEOUT) -> dict | None:
    """Lever 3: install the conda packages (system component + the Python package built
    against it) listed for `package_spec`. None if the package is not in the table.
    An exact pin is tried first, then the bare names."""
    specs = system_dependency_specs(package_spec)
    if specs is None:
        return None
    name = base_name(package_spec)
    pinned = [pip_spec_to_conda(package_spec) if base_name(s) == name else s for s in specs]
    first = conda_install(python_exe, pinned, timeout=timeout)
    if first["ok"] or first.get("unavailable") or pinned == specs:
        first["pinned"] = pinned != specs
        return first
    second = conda_install(python_exe, specs, timeout=timeout)
    second["pinned"] = False
    second["first_attempt_log"] = first["log"][-400:]
    return second
