"""Lever 7 -- era-consistent resolution (TASK_era_consistent_resolution.md).

The six earlier levers fix packages ONE AT A TIME, and a 2019-era notebook then fails on a
second and third version error because each newly installed package drags in the newest
transitive dependencies (LEVERS_RESULTS.md section 5, "the version cascade"). This lever resolves
every pip install *as of the repository's date*: `uv pip install --exclude-newer <date>` ignores
any distribution released after that date, so the resolver picks a mutually consistent set of
era-appropriate versions -- including the transitive ones -- in one go.

The date is the repo's last-commit date (repo.repo_last_commit_date), the same one era-capping
uses. Where it applies and where it does not (recorded in every result):

  applies   pip installs: the declared requirements/setup/environment.yml pip entries, the
            agent's reactive installs, the downgrade search, the replay after a lower Python
  not       conda/micromamba installs (conda_fallback, system_deps): conda has no clean
            "exclude newer than" mechanism, so those stay unconstrained
  not       a repo with no git date: nothing to resolve against -- reported as `no_repo_date`
  not       a conda *recipe* environment (environment.yml built by micromamba): its own pins stand

If the resolver finds no consistent set for the date (`No solution found`, a package that did
not exist yet), the install falls back to plain pip and the result says so (`fallback`).
"""

from __future__ import annotations

import os
import re
import subprocess

from . import levers

LEVER = levers.ERA_RESOLVE

# uv could not find a consistent set / a release in the window -- NOT a build failure (that is a
# property of the package, and plain pip would fail the same way).
_RESOLUTION_FAILURE_RE = re.compile(
    r"No solution found|no version of .* (?:matching|satisf)|Because .* was not found in the package registry|"
    r"No matching distribution|not found in the package registry|your requirements are unsatisfiable|"
    r"not available in the (?:package registry|configured)", re.I,
)


def enabled() -> bool:
    return levers.enabled(LEVER)


def stamp(date: str) -> str:
    """'2019-05-17' -> an RFC 3339 end-of-day timestamp, so a release made on the commit day counts."""
    return f"{date.strip()[:10]}T23:59:59Z"


def _find_uv() -> str | None:
    from .interpreter import find_uv

    return find_uv()


def decision(era_date: str | None) -> dict:
    """Whether the lever will constrain a pip install now, and if not why not."""
    if not enabled():
        return {"lever_on": False}
    if not era_date:
        return {"lever_on": True, "applied": False, "reason": "no_repo_date"}
    if not _find_uv():
        return {"lever_on": True, "applied": False, "reason": "uv_unavailable", "date": era_date[:10]}
    return {"lever_on": True, "applied": True, "date": era_date[:10]}


def pip_install(
    python_exe: str, args: list[str], era_date: str | None, timeout: int, allow_fallback: bool = True,
) -> tuple[bool, str, dict]:
    """`pip install <args>` into `python_exe`'s environment -- era-constrained when the lever is on.

    Returns (ok, log, meta). meta is {} with the lever off (nothing to record); otherwise
    {lever_on, applied, date, mechanism, fallback?, reason?, era_failure_tail?}. Never raises."""
    meta = decision(era_date)
    if not meta.get("applied"):
        ok, log = _pip(python_exe, args, timeout)
        if meta:
            meta["mechanism"] = "pip"
        return ok, log, meta

    uv = _find_uv()
    cmd = [uv, "pip", "install", "--python", python_exe, "--exclude-newer", stamp(era_date),
           "--link-mode", "copy", "--no-progress", *args]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        ok, log = completed.returncode == 0, completed.stdout + completed.stderr
    except subprocess.TimeoutExpired:
        return False, f"uv pip install timed out after {timeout}s", dict(meta, mechanism="uv --exclude-newer", timed_out=True)
    except OSError as exc:
        ok, log = False, f"could not run uv: {exc}"
    meta["mechanism"] = "uv --exclude-newer"
    if ok:
        return True, log, meta

    if _RESOLUTION_FAILURE_RE.search(log) and not allow_fallback:
        # lever era_strict: no silent fall back to an unconstrained install -- that would put the modern
        # version back in. The refusal is the result; the agent is told why (see strict_message).
        meta.update(fallback=False, strict_refused=True, era_failure_tail=log[-300:])
        return False, strict_resolver_message(log, era_date) , meta
    if _RESOLUTION_FAILURE_RE.search(log) or log.startswith("could not run uv"):
        # No consistent set as of that date: say so, and fall back to the unconstrained install so
        # the lever can never make an install impossible that plain pip could do.
        plain_ok, plain_log = _pip(python_exe, args, timeout)
        meta.update(fallback=True, mechanism="pip (fallback)", era_failure_tail=log[-300:], fallback_ok=plain_ok)
        return plain_ok, plain_log, meta
    return False, log, meta


def _pip(python_exe: str, args: list[str], timeout: int) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [python_exe, "-m", "pip", "install", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"pip install timed out after {timeout}s"
    return completed.returncode == 0, completed.stdout + completed.stderr


def summarize(metas: list[dict]) -> dict:
    """Roll per-install metas up into the few numbers a result row needs."""
    metas = [m for m in metas if m]
    return {
        "installs": len(metas),
        "applied": sum(1 for m in metas if m.get("applied") and not m.get("fallback")),
        "fallback": sum(1 for m in metas if m.get("fallback")),
        "not_applied": sum(1 for m in metas if not m.get("applied")),
        "reasons": sorted({m["reason"] for m in metas if m.get("reason")}),
    }


def setup_record(era_date: str | None, metas: list[dict], interpreter_meta: dict) -> dict:
    """What lever era_resolve did while the environment was built ({} with the lever off)."""
    if not enabled():
        return {}
    record = dict(decision(era_date), declared_installs=summarize(metas))
    if interpreter_meta.get("python_mechanism") == "conda":
        record["note"] = "interpreter built by conda/micromamba; declared pins that conda itself installed are not date-constrained"
    return record


# --- lever era_strict (TASK_true_era_dating.md, part B) -------------------------------------------------

MAX_ERA_REFUSALS = 6  # per notebook: bounded, so a stubborn model cannot loop on refusals forever


def strict_enabled() -> bool:
    return levers.enabled(levers.ERA_STRICT)


def strict_resolver_message(log: str, era_date: str | None) -> str:
    return (f"Refused under the era date {str(era_date)[:10]}: the resolver found no consistent set of releases that "
            f"existed by that date for this install, so it was NOT installed (and not installed unconstrained either). "
            f"Choose versions that existed by {str(era_date)[:10]}.\n--- resolver message ---\n{log[-600:]}")


_RELEASES: dict[str, list] = {}


def _releases(name: str) -> list:
    if name not in _RELEASES:
        from . import pypi

        _RELEASES[name] = pypi.release_history(name)
    return _RELEASES[name]


def out_of_window(package_spec: str, era_date: str | None) -> dict | None:
    """None if the spec is acceptable (or cannot be judged); otherwise a refusal result.

    A spec is out of window when NO release satisfying it was uploaded on or before `era_date`, e.g.
    `matplotlib>=3.6` for a 2021 notebook. Unjudgeable specs (URLs, unparsable, PyPI unreachable or
    unknown name) are allowed through: the lever refuses only what it can show to be impossible."""
    if not era_date:
        return None
    try:
        from packaging.requirements import InvalidRequirement, Requirement

        req = Requirement(package_spec.strip())
    except Exception:  # noqa: BLE001 - not a plain requirement string (URL, path, flags): do not judge it
        return None
    if req.url:
        return None
    releases = _releases(req.name)
    if not releases:
        return None
    cutoff = era_date[:10]
    in_window = [r for r in releases if r["date"] and r["date"] <= cutoff]
    satisfying = [r for r in in_window if req.specifier.contains(r["version"], prereleases=False)]
    if satisfying:
        return None
    newest = in_window[0] if in_window else None
    if newest:
        why = (f"no release of {req.name} satisfying {str(req.specifier) or 'any version'} existed by {cutoff}; "
               f"the newest as of then was {newest['version']} (released {newest['date']}) -- choose a version "
               f"available by that date")
    else:
        why = (f"no release of {req.name} existed at all by {cutoff} (its first release is later); this package "
               f"cannot be installed for that era -- it is probably not what this notebook needs")
    return {
        "ok": False, "refused": True, "refusal_reason": "era_window", "resolved_package": "", "version_source": "refused",
        "era_date": cutoff, "newest_as_of_era": newest["version"] if newest else None,
        "log": "Refused (era window): " + why + ".",
    }


def strict_gate(package_spec: str, era_date: str | None, state: dict) -> dict | None:
    """The agent-facing check, run before an install is attempted. Counts refusals in `state` and stops
    offering advice after MAX_ERA_REFUSALS (still refusing: the modern version is never silently installed)."""
    if not strict_enabled():
        return None
    refusal = out_of_window(package_spec, era_date)
    if refusal is None:
        return None
    state["era_refusals"] = state.get("era_refusals", 0) + 1
    if state["era_refusals"] > MAX_ERA_REFUSALS:
        state["era_refusal_limit_reached"] = True
        refusal = dict(refusal, log=(f"Refused (era window; refusal limit of {MAX_ERA_REFUSALS} reached for this notebook): "
                                     "no more era-window installs will be attempted for specs outside the window. "
                                     "Use report_unfixable if no era-appropriate version can fix the problem. ") + refusal["log"])
    refusal["era_refusal_count"] = state["era_refusals"]
    return refusal
