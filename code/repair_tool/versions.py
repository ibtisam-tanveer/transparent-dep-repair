"""Lever 1 -- iterative version downgrade (TASK_improve_fix_rate.md #1).

Observed miss (MaaSSim): the agent installed `osmnx<2.0`, the notebook still
failed with `cannot import name 'get_nearest_node' from 'osmnx.distance'`, and
the run stopped -- the function was removed several releases before 2.0, so a
single guess was never going to be old enough.

This walks BACKWARDS through the package's release history and re-runs the
project after each candidate, instead of stopping after one guess:

- candidates are the latest release of each OLDER major.minor series, thinned
  geometrically (1, 2, 3, 5, 8, 13 ... series back) so a handful of tries covers
  years of history rather than the last few patches;
- every candidate is installed and verified by actually re-running the target;
- it stops when the project passes, when the failure changes to something that
  is no longer this package's API (a new layer -- the version stays), or when the
  try / time budget is spent, in which case the original version is restored;
- every version tried and what happened is recorded in the result, so the
  report can say exactly which version was reached and why.

The install and run steps are injected so the same code runs on the host and
inside the container, and so it is testable without a network.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Callable

from packaging.version import InvalidVersion, Version

from . import diagnose as diagnose_module
from . import pypi

DEFAULT_MAX_TRIES = 6
DEFAULT_TIME_BUDGET_SECONDS = 1200
_API_KINDS = ("module_attribute_removed", "import_name", "object_attribute_error")

LEVER = "downgrade"


def installed_version(python_exe: str, package: str, timeout: int = 60) -> str | None:
    """Version of `package` in `python_exe`'s environment (`pip show`), or None."""
    try:
        completed = subprocess.run(
            [python_exe, "-m", "pip", "show", package], capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    match = re.search(r"^Version:\s*(\S+)", completed.stdout, re.M)
    return match.group(1) if match else None


def probe_schedule(history: list[dict], current_version: str, max_tries: int = DEFAULT_MAX_TRIES) -> list[dict]:
    """The candidates to try, oldest-ward, from a newest-first release history:
    the latest release of each series OLDER than `current_version`'s, taken at
    positions 1, 2, 3, 5, 8, 13 ... (1-based) of that list, at most `max_tries`."""
    try:
        current = Version(current_version)
    except InvalidVersion:
        return []
    current_series = (current.major, current.minor)

    older_series: list[dict] = []
    seen: set[str] = set()
    for release in history:  # newest first, so the first hit per series is its latest patch
        try:
            v = Version(release["version"])
        except InvalidVersion:
            continue
        if (v.major, v.minor) >= current_series:
            continue
        if release["series"] in seen:
            continue
        seen.add(release["series"])
        older_series.append(release)

    positions, a, b = [], 1, 2
    while a <= len(older_series) and len(positions) < max_tries:
        positions.append(a)
        a, b = b, a + b  # 1, 2, 3, 5, 8, 13 ...
    return [older_series[p - 1] for p in positions]


def _signature(run_result: dict) -> tuple:
    diag = diagnose_module.diagnose(str(run_result.get("stderr", "")))
    return (diag.kind, diag.module, diag.package, diag.symbol)


def _error_line(run_result: dict) -> str:
    line = diagnose_module._find_exception_line(str(run_result.get("stderr", "")))
    return (line or "")[:200]


def _still_this_packages_api(sig: tuple, package: str) -> bool:
    kind, module, _pkg, _symbol = sig
    if kind not in _API_KINDS:
        return False
    top = (module or "").split(".", 1)[0].replace("-", "_").lower()
    return top == package.replace("-", "_").lower()


def downgrade_search(
    package: str,
    python_exe: str,
    install_fn: Callable[[str], tuple[bool, str]],
    run_fn: Callable[[], dict],
    initial_failure: dict | None = None,
    max_tries: int = DEFAULT_MAX_TRIES,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    history_fn: Callable[[str], list[dict]] | None = None,
    version_fn: Callable[[str, str], str | None] | None = None,
) -> dict:
    """Search older releases of `package`. Never raises. See the module docstring.

    `install_fn("pkg==1.2.3") -> (ok, log)`; `run_fn() -> run_target-style dict`.
    `initial_failure` is the failing run being repaired (re-run once if omitted)."""
    started = time.monotonic()
    history = (history_fn or pypi.release_history)(package)
    original = (version_fn or installed_version)(python_exe, package)
    result: dict = {
        "lever": LEVER, "package": package, "original_version": original, "tried": [], "ok": False,
        "final_version": original,
    }
    if original is None:
        result.update(stopped_because="package_not_installed",
                      message=f"{package!r} is not installed, so there is nothing to downgrade -- install it first.")
        return result
    if not history:
        result.update(stopped_because="no_release_history",
                      message=f"could not read the release history of {package!r} from PyPI")
        return result

    candidates = probe_schedule(history, original, max_tries)
    if not candidates:
        result.update(stopped_because="no_older_series",
                      message=f"{package} {original} is already the oldest release series available.")
        return result

    baseline = initial_failure if initial_failure is not None else run_fn()
    if baseline.get("ok"):
        result.update(ok=True, stopped_because="already_passing", message="the project already passes")
        return result
    base_sig = _signature(baseline)
    result["initial_error"] = _error_line(baseline)

    for release in candidates:
        if time.monotonic() - started > time_budget_seconds:
            result["stopped_because"] = "time_budget"
            break
        version = release["version"]
        ok, log = install_fn(f"{package}=={version}")
        step = {"version": version, "series": release["series"], "released": release["date"], "install_ok": ok}
        if not ok:
            step["outcome"] = "install_failed"
            step["error"] = log.strip().splitlines()[-1][:200] if log.strip() else ""
            result["tried"].append(step)
            continue

        run = run_fn()
        if run.get("ok"):
            step["outcome"] = "passes"
            result["tried"].append(step)
            result.update(ok=True, final_version=version, stopped_because="passes",
                          message=f"the project passes with {package}=={version}")
            result["last_run"] = run
            return result

        sig = _signature(run)
        step["error"] = _error_line(run)
        if sig == base_sig:
            step["outcome"] = "same_error"
        elif _still_this_packages_api(sig, package):
            step["outcome"] = "still_this_packages_api"
        else:
            step["outcome"] = "error_changed"
            result["tried"].append(step)
            result.update(final_version=version, stopped_because="error_changed", last_run=run,
                          message=(f"with {package}=={version} the original error is gone and a different one "
                                   f"appeared ({step['error']}); keeping this version -- continue from the new error."))
            return result
        result["tried"].append(step)
    else:
        result.setdefault("stopped_because", "candidates_exhausted")

    # Nothing helped: put the original version back so the environment is as it was.
    restored, _ = install_fn(f"{package}=={original}")
    result["restored_original"] = bool(restored)
    result["final_version"] = original
    result["message"] = (
        f"tried {len(result['tried'])} older release series of {package} (down to "
        f"{result['tried'][-1]['version'] if result['tried'] else 'none'}); none removed the error. "
        "The version change alone does not fix this -- consider migrating the code to the new API with edit_code, "
        "or a different interpreter."
    )
    return result
