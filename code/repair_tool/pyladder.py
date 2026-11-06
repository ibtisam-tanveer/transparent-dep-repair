"""Lever 4 -- interpreter downgrade on version errors (TASK_improve_fix_rate.md #4).

Observed miss (interpretable-generative-deep-learning): the Python version is
a guess from the repo's last-commit date (3.9 here) and the notebook needs an old
`scvi` that only runs on an older interpreter. Trusting the guess alone left it
stuck. When the failure says the interpreter is too NEW for something the project
needs, step the interpreter DOWN one minor version and rebuild:

  1. provision the next lower CPython (micromamba in the container -- it has 3.6/3.7
     on linux-64; uv otherwise), refusing a result that is not actually lower;
  2. reinstall the repo's declared dependencies on it (same code as the first build);
  3. replay the packages the agent had installed so far, so progress is not lost;
  4. hand back the new interpreter path -- the caller switches to it.

`python_too_new_hint` is the deterministic detector (pip's "requires a different
Python", a pinned old release with no build for this Python, and stdlib names removed
in recent CPython). It only produces a HINT for the agent; the agent chooses.
"""

from __future__ import annotations

import os
import re

from . import apply as apply_module
from .repair import Proposal

LEVER = "interpreter_down"
LADDER = ("3.14", "3.13", "3.12", "3.11", "3.10", "3.9", "3.8", "3.7", "3.6")
FLOOR = "3.6"

# Text that says "this Python is too new for what the project needs".
_TOO_NEW_RE = re.compile(
    r"requires a different Python|Requires-Python|not in '[<>=!~, .0-9*]+'|"
    r"No matching distribution found for|Could not find a version that satisfies the requirement|"
    r"cannot import name '(?:Mapping|MutableMapping|Iterable|Callable|Sequence|Set)' from 'collections'|"
    r"module 'collections' has no attribute '(?:Mapping|MutableMapping|Iterable|Callable|Sequence)'|"
    r"No module named 'distutils|No module named 'imp'|module 'asyncio' has no attribute 'coroutine'|"
    r"module 'inspect' has no attribute 'getargspec'|module 'time' has no attribute 'clock'|"
    r"module 'pkgutil' has no attribute 'ImpImporter'|use_2to3|"
    r"module 'collections' has no attribute|ImportError: cannot import name '.*' from 'typing'",
    re.I,
)


def python_too_new_hint(text: str) -> str | None:
    """A one-line hint if `text` (stderr or an install log) looks like 'the Python is too new'."""
    if text and _TOO_NEW_RE.search(text):
        return ("This error pattern often means the Python interpreter is too NEW for something the project "
                "needs (a removed standard-library name, or a pinned old package with no build for this Python). "
                "If version changes do not help, call lower_python to step the interpreter down.")
    return None


def _lazy():
    """interpreter/repo import agent (via agent_tools) -- import them only when used."""
    from . import interpreter, repo

    return interpreter, repo


def next_lower(current: str | None) -> str | None:
    """The next lower minor version on the ladder below `current`, or None at the floor."""
    if not current:
        return None
    try:
        cur = tuple(int(x) for x in current.split(".")[:2])
    except ValueError:
        return None
    for candidate in LADDER:
        if tuple(int(x) for x in candidate.split(".")) < cur:
            return candidate
    return None


def _env_dir_for(python_exe: str, target: str) -> str:
    prefix = os.path.dirname(os.path.dirname(os.path.abspath(python_exe)))
    return os.path.join(os.path.dirname(prefix), f"agent_py{target.replace('.', '')}")


def lower_python(
    python_exe: str, repo_root: str | None, installed_specs: list[str] | None = None, reason: str = "",
    target: str | None = None, era_date: str | None = None,
) -> dict:
    """Rebuild the environment on the next lower Python. Never raises.
    Returns {ok, lever, from_version, to_version, python_exe, mechanism, ...}."""
    interpreter, repo = _lazy()
    current = interpreter._interpreter_minor(python_exe)
    wanted = target or next_lower(current)
    result: dict = {"lever": LEVER, "ok": False, "from_version": current, "to_version": None}
    if wanted is None:
        result["message"] = f"already at the oldest supported interpreter ({current}); cannot go lower"
        return result

    choice = interpreter.PythonChoice(
        version=wanted, source="agent-requested", grounding="llm_proposed",
        detail=(reason or "interpreter too new for the project's packages")[:200],
    )
    env_dir = _env_dir_for(python_exe, wanted)
    prov = interpreter.provision_interpreter(choice, env_dir, repo_root or os.path.dirname(env_dir))
    if not prov.ok:
        result["message"] = f"could not provision Python {wanted}: {prov.unprovisionable_reason or prov.note}"
        result["log_tail"] = (prov.log or "")[-400:]
        return result

    # A "nearest available" substitution may land on the same or a newer interpreter -- that is not a downgrade.
    try:
        if tuple(int(x) for x in prov.actual.split(".")) >= tuple(int(x) for x in (current or "99.0").split(".")):
            result["message"] = (f"the nearest available Python was {prov.actual}, which is not lower than {current}; "
                                 "no lower interpreter is reachable on this platform")
            result["to_version"] = prov.actual
            return result
    except (ValueError, AttributeError):
        pass

    new_python = prov.python_exe
    declared_ok, declared_log, used = True, "", ""
    if repo_root:
        files = repo.find_dependency_files(repo_root)
        declared_ok, declared_log, used = repo._install_dependencies(
            new_python, repo_root, files, repo._INSTALL_TIMEOUT, era_date=era_date)

    replayed, failed = [], []
    for spec in installed_specs or []:
        ok, _log = apply_module.apply(Proposal(kind="install", package=spec), new_python, era_date=era_date)
        (replayed if ok else failed).append(spec)

    result.update(
        ok=True, to_version=prov.actual, python_exe=new_python, mechanism=prov.mechanism,
        declared_dependencies_used=used, declared_dependencies_ok=declared_ok,
        replayed_installs=replayed, replay_failed=failed,
        message=(f"rebuilt the environment on Python {prov.actual} (was {current}); reinstalled the repo's declared "
                 f"dependencies and replayed {len(replayed)} of your earlier installs"
                 + (f" ({len(failed)} failed: {failed})" if failed else "")
                 + ". Call verify to re-run the project."),
    )
    return result
