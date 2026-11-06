"""Repo-local module detection, shared by the install guard
(agent_tools.install_package), the working-directory resolver (workdir.py)
and the evaluation's runtime classification (run_eval._looks_like_local_module).

A `ModuleNotFoundError: No module named 'combat'` is not always a missing
PyPI dependency: the repo may ship its own `src/combat.py`. Installing the
unrelated PyPI package of the same name would shadow it (observed: 22 such
installs in one run), so this answers "is this name one of the repo's own
files?" from the repo tree itself.
"""

from __future__ import annotations

import os
import re

_SKIP_DIRS = {"__pycache__", "node_modules"}


def _normalise(name: str) -> str:
    """Import-name form of a package name ('my-pkg' / 'My.Pkg' -> 'my_pkg')."""
    return re.sub(r"[-.]+", "_", name.strip()).lower()


def _top_level(name: str) -> str:
    """'combat.pycombat' / 'combat==0.3.0' / 'combat>=1' -> 'combat'."""
    name = re.split(r"[<>=!~;\[\s]", name.strip(), maxsplit=1)[0]
    return name.split(".", 1)[0]


def _dir_has_python(path: str) -> bool:
    try:
        return any(entry.endswith(".py") for entry in os.listdir(path))
    except OSError:
        return False


def importable_dirs(from_file: str, repo_root: str) -> set[str] | None:
    """Directories a module could be imported FROM when running `from_file`:
    its own folder and every ancestor up to the repo root (the working directory /
    sys.path[0] candidates), plus every directory its own literal relative
    `sys.path.append("../src/")` entries lead to from each of those. None if
    `from_file` is not inside `repo_root` (then nothing can be said and callers
    fall back to the broad whole-repo match)."""
    from . import workdir  # local import: workdir imports this module at load time

    root = os.path.realpath(repo_root)
    current = os.path.realpath(os.path.dirname(os.path.abspath(from_file)))
    if current != root and not current.startswith(root + os.sep):
        return None
    ancestors = []
    while True:
        ancestors.append(current)
        if current == root or os.path.dirname(current) == current:
            break
        current = os.path.dirname(current)
    dirs = set(ancestors)
    for entry in workdir.relative_sys_path_entries(from_file):
        for base in ancestors:
            dirs.add(os.path.normpath(os.path.join(base, entry)))
    return dirs


def find_local_module(
    name: str, repo_root: str, require_python_content: bool = False, from_file: str | None = None,
) -> str | None:
    """Repo-relative path of a file/directory that `import <name>` could
    resolve to inside `repo_root` -- `<name>.py` or a `<name>/` directory
    anywhere in the tree -- or None.

    Searched anywhere (not just next to the failing file), because a
    notebook's `sys.path` tweaks can reach any directory. `name` may be a
    dotted module ('a.b'), a requirement string ('a==1.0') or a PyPI project
    name with '-' (treated as '_'); only the top-level component is matched.

    `from_file`: only count a match that is importable FROM that file (see
    importable_dirs). Without it a file named like a famous PyPI package anywhere in
    the repo (e.g. `.../T018/utils/helpers/nglview.py`) would make an unrelated
    notebook's `import nglview` look repo-local and block installing the real package.

    `require_python_content=True` is the strict form used before *refusing*
    an install: a directory only counts if it holds Python (an `__init__.py`
    or any `.py`), so an unrelated data folder that happens to share a
    package's name can never block a legitimate install.
    """
    if not name or not repo_root or not os.path.isdir(repo_root):
        return None
    top = _normalise(_top_level(name))
    if not top:
        return None
    reachable = importable_dirs(from_file, repo_root) if from_file else None
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS)
        if reachable is not None and os.path.realpath(dirpath) not in reachable:
            continue
        for filename in sorted(filenames):
            if filename.endswith(".py") and _normalise(filename[:-3]) == top:
                return os.path.relpath(os.path.join(dirpath, filename), repo_root)
        for dirname in dirnames:
            if _normalise(dirname) == top:
                candidate = os.path.join(dirpath, dirname)
                if not require_python_content or _dir_has_python(candidate):
                    return os.path.relpath(candidate, repo_root)
    return None


def is_local_module(
    name: str, repo_root: str, require_python_content: bool = False, from_file: str | None = None,
) -> bool:
    return find_local_module(name, repo_root, require_python_content, from_file) is not None
