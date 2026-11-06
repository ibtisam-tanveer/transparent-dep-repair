"""Working-directory resolution for repo-local imports
(TASK_prepilot_fixes.md #3).

The problem: a notebook in `notebooks/old/` does `sys.path.append("../src/")`
and `import combat`. Those relative entries resolve against the *working
directory*, and the author ran it from `notebooks/`, not `notebooks/old/` --
so run in its own folder it fails with `No module named 'combat'` even though
`src/combat.py` is right there. That is a where-it-is-run-from problem, not a
missing dependency, and (in kiste_smoke_v5) 23 covid notebooks were thrown
out as "repo-local module" because of it.

What this does, deterministically and only when needed: when a run fails with
`ModuleNotFoundError` for a name the repo itself provides, try the
notebook's ancestor directories (nearest first) as the working directory --
preferring one where the notebook's own relative `sys.path` entries resolve
and lead to that module -- and keep the first that makes the local-import
error go away. The choice and its reason are recorded (`RunResult.cwd` /
`cwd_source`) so the report shows what was actually changed.

Honest limits: it only looks at the notebook's ancestors up to the repo
root, only reads literal `sys.path.append/insert("...")` strings, and it
never invents paths. If nothing makes the local import resolve, the failure
stays what it was and the caller keeps classifying it as a repo-local-module
case with the real reason.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable

from .localmods import find_local_module

MAX_CANDIDATES = 3

_SYS_PATH_RE = re.compile(r"""sys\.path\.(?:append|insert)\(\s*(?:-?\d+\s*,\s*)?[rR]?["']([^"']+)["']""")
_NO_MODULE_RE = re.compile(r"No module named ['\"]([\w.]+)['\"]")

SOURCE_FILE_DIR = "file_dir"
SOURCE_SYS_PATH = "inferred_from_sys_path"
SOURCE_MODULE_DIR = "ancestor_containing_module"


@dataclass
class Resolution:
    cwd: str  # absolute directory that made the local import resolve
    source: str
    module: str


# path -> the resolution that worked / the set of paths already searched.
# Process-level on purpose: one repair session = one process per repo, and
# every later run_target/verify for the same file must reuse the choice.
_RESOLUTIONS: dict[str, Resolution] = {}
_SEARCHED: set[str] = set()


def reset() -> None:
    """Forget every cached resolution (tests, or a fresh repo)."""
    _RESOLUTIONS.clear()
    _SEARCHED.clear()


def save_state(path: str) -> None:
    """Persist the cache. The container runs each action in a fresh process,
    so the remembered working directories must outlive it (a file in the
    container); on the host one process keeps them in memory and never calls this."""
    data = {
        "resolutions": {k: {"cwd": r.cwd, "source": r.source, "module": r.module} for k, r in _RESOLUTIONS.items()},
        "searched": sorted(_SEARCHED),
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def load_state(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for key, r in (data.get("resolutions") or {}).items():
        _RESOLUTIONS[key] = Resolution(cwd=r["cwd"], source=r["source"], module=r["module"])
    _SEARCHED.update(data.get("searched") or [])


def _source_text(path: str) -> str:
    try:
        if path.endswith(".ipynb"):
            with open(path, encoding="utf-8") as f:
                nb = json.load(f)
            return "\n".join(
                "".join(cell.get("source", [])) if isinstance(cell.get("source"), list) else str(cell.get("source", ""))
                for cell in nb.get("cells", []) if cell.get("cell_type") == "code"
            )
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, ValueError):
        return ""


def relative_sys_path_entries(path: str) -> list[str]:
    """Literal relative entries the file adds to sys.path
    (`sys.path.append("../src/")` -> '../src/'). Absolute, home-relative and
    computed entries are ignored: only literals can be checked honestly."""
    entries = []
    for entry in _SYS_PATH_RE.findall(_source_text(path)):
        if entry.startswith(("/", "~", "$")) or os.path.isabs(entry) or entry in (".", ""):
            continue
        entries.append(entry)
    return entries


def missing_local_module(stderr: str, repo_root: str) -> str | None:
    """Top-level name from a ModuleNotFoundError in `stderr` IF the repo
    itself provides a module of that name, else None. The last such error
    wins (it is the one that actually stopped the run)."""
    matches = _NO_MODULE_RE.findall(stderr or "")
    if not matches:
        return None
    module = matches[-1].split(".", 1)[0]
    return module if find_local_module(module, repo_root) else None


def _ancestors(path: str, repo_root: str) -> list[str]:
    """Directories from the file's own folder up to (and including)
    repo_root, nearest first."""
    root = os.path.realpath(repo_root)
    current = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    chain = []
    while True:
        chain.append(current)
        if current == root or os.path.dirname(current) == current:
            break
        current = os.path.dirname(current)
        if not current.startswith(root):
            break
    return chain


def _module_reachable_from(directory: str, module: str) -> bool:
    return os.path.isfile(os.path.join(directory, f"{module}.py")) or os.path.isdir(os.path.join(directory, module))


def candidate_working_dirs(path: str, repo_root: str, module: str) -> list[tuple[str, str]]:
    """(directory, source) pairs to try instead of the file's own folder,
    best first: ancestors whose resolved relative sys.path entries lead to
    `module`, then ancestors that directly contain it."""
    own = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    ancestors = [d for d in _ancestors(path, repo_root) if d != own]
    entries = relative_sys_path_entries(path)

    ranked: list[tuple[str, str]] = []
    if entries:
        for directory in ancestors:
            for entry in entries:
                target = os.path.normpath(os.path.join(directory, entry))
                if os.path.isdir(target) and _module_reachable_from(target, module):
                    ranked.append((directory, SOURCE_SYS_PATH))
                    break
    for directory in ancestors:
        if _module_reachable_from(directory, module) and all(directory != d for d, _ in ranked):
            ranked.append((directory, SOURCE_MODULE_DIR))
    return ranked[:MAX_CANDIDATES]


def run_with_resolution(path: str, repo_root: str, run_once: Callable):
    """Run `path` via `run_once(cwd_or_None) -> RunResult`, re-running from a
    better working directory if (and only if) the failure is an import of a
    repo-local module. The returned RunResult carries `cwd` / `cwd_source`."""
    key = os.path.abspath(path)
    own_dir = os.path.dirname(key)

    cached = _RESOLUTIONS.get(key)
    if cached is not None:
        return _annotate(run_once(cached.cwd), cached.cwd, cached.source)

    result = _annotate(run_once(None), own_dir, SOURCE_FILE_DIR)
    if result.ok or key in _SEARCHED:
        return result

    module = missing_local_module(result.stderr, repo_root)
    if module is None:
        return result

    _SEARCHED.add(key)
    for directory, source in candidate_working_dirs(key, repo_root, module):
        attempt = run_once(directory)
        if not _still_missing(attempt.stderr, module):
            _RESOLUTIONS[key] = Resolution(cwd=directory, source=source, module=module)
            return _annotate(attempt, directory, source)
    return result


def _still_missing(stderr: str, module: str) -> bool:
    return re.search(rf"No module named ['\"]{re.escape(module)}[.'\"]", stderr or "") is not None


def _annotate(result, cwd: str, source: str):
    result.cwd = cwd
    result.cwd_source = source
    return result


def describe(path: str, repo_root: str) -> dict:
    """What was done for `path`, for the result row / report: an empty dict
    if nothing was ever searched, else the working directory (repo-relative),
    why, and whether it recovered a local-import failure."""
    key = os.path.abspath(path)
    resolution = _RESOLUTIONS.get(key)
    if resolution is not None:
        return {
            "working_directory": os.path.relpath(resolution.cwd, os.path.realpath(repo_root)) or ".",
            "working_directory_source": resolution.source,
            "working_directory_recovered": True,
            "working_directory_module": resolution.module,
        }
    if key in _SEARCHED:
        return {
            "working_directory": "", "working_directory_source": "",
            "working_directory_recovered": False, "working_directory_module": "",
        }
    return {}


def describe_exception(exc: BaseException) -> str:
    """'TypeError: msg [file.py:123 in func]' -- the innermost frame, so a
    crash recorded in a result row says WHERE it happened instead of only
    what (kiste_smoke_v6 had two unexplained 'NoneType' crashes)."""
    import traceback

    frames = traceback.extract_tb(exc.__traceback__)
    where = ""
    if frames:
        last = frames[-1]
        where = f" [{os.path.basename(last.filename)}:{last.lineno} in {last.name}]"
    return f"{type(exc).__name__}: {exc}{where}"
