"""Carry out a Proposal by running the install inside the target's isolated
venv. Never touches the interpreter running this tool.
"""

from __future__ import annotations

import subprocess

from .repair import Proposal

# See PHASE3_ADDENDUM.md #1: generous enough for a slow/compiled wheel,
# short enough that MAX_ATTEMPTS can't turn one stalled install into an
# effectively-infinite run.
DEFAULT_TIMEOUT = 300


def apply(proposal: Proposal, python_exe: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Run `proposal`'s install inside the venv at `python_exe`.

    Never raises: a timeout or a failed install both come back as
    (False, <log explaining what happened>), same contract as run_project.
    """
    if proposal.kind != "install":
        return False, f"nothing to apply for proposal kind={proposal.kind!r}"

    try:
        completed = subprocess.run(
            [python_exe, "-m", "pip", "install", proposal.package],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"install of {proposal.package!r} timed out after {timeout}s"
    except OSError as exc:
        return False, f"could not run {python_exe!r}: {exc}"

    log = completed.stdout + completed.stderr
    return completed.returncode == 0, log


def installed_packages(python_exe: str, timeout: int = 60) -> set[str]:
    """Exact 'name==version' set currently installed in python_exe's venv.

    Never raises: any failure (timeout, pip error) comes back as an empty
    set rather than propagating — a best-effort snapshot, not a hard fact.
    """
    try:
        completed = subprocess.run(
            [python_exe, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        # OSError covers a missing/invalid python_exe (FileNotFoundError etc.)
        # -- same "never raise" contract as run_project/apply.
        return set()
    if completed.returncode != 0:
        return set()
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def restore_packages(python_exe: str, before: set[str], timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Undo whatever a losing environment-fix candidate did to the venv, by
    diffing the current package set against `before` (captured prior to
    that attempt): a genuinely new package (including transitive
    dependencies the candidate pulled in) is uninstalled; a package that
    already existed but changed *version* (e.g. a downgrade like
    `PyYAML<5.1` overwriting an already-installed PyYAML 6.x) is
    reinstalled back to its exact prior pinned version rather than removed
    outright -- removing it entirely would leave the venv missing a package
    that was legitimately present before this attempt, which is worse than
    not restoring at all.

    Keeps two candidates genuinely independent: see PHASE5_IMPROVEMENTS_TASK.md
    #1 — without this, a losing env-fix's leftover state could make a later
    candidate pass or fail for reasons that have nothing to do with its own
    merits, corrupting the strategy_won measurement. Never raises.
    """
    after = installed_packages(python_exe, timeout=60)
    if after == before:
        return True, "no changes to restore"

    before_by_name = {pkg.split("==", 1)[0]: pkg for pkg in before if pkg}
    after_by_name = {pkg.split("==", 1)[0]: pkg for pkg in after if pkg}

    to_uninstall = sorted(name for name in after_by_name if name not in before_by_name)
    to_reinstall = sorted(
        before_by_name[name] for name in before_by_name if after_by_name.get(name) != before_by_name[name]
    )

    log_parts = []
    ok = True

    if to_uninstall:
        success, log = _run_pip(python_exe, ["uninstall", "-y", *to_uninstall], timeout)
        ok = ok and success
        log_parts.append(f"removed {to_uninstall}: {log}")

    if to_reinstall:
        success, log = _run_pip(python_exe, ["install", *to_reinstall], timeout)
        ok = ok and success
        log_parts.append(f"restored {to_reinstall}: {log}")

    return ok, "; ".join(log_parts) or "no packages to restore"


def _run_pip(python_exe: str, pip_args: list[str], timeout: int) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [python_exe, "-m", "pip", *pip_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    return completed.returncode == 0, completed.stdout + completed.stderr


def apply_code_edit(edits: list[dict], workspace_path: str) -> tuple[bool, str]:
    """Apply find/replace edits to the file at `workspace_path`, in place.

    `workspace_path` must be a working copy (see venv_manager.get_workspace_copy),
    never the user's original input file. Never raises: an edit whose `find`
    string doesn't appear verbatim in the source (e.g. different whitespace)
    is a verification failure, not a crash — per PHASE5_TASK.md.

    A `.ipynb` target dispatches to notebook.edit_notebook_cells (searches
    across code cells instead of one file's text) -- see
    NOTEBOOK_SUPPORT_TASK.md. Everything below this is the unchanged
    plain-text path for `.py` targets.
    """
    if workspace_path.endswith(".ipynb"):
        from .notebook import edit_notebook_cells

        return edit_notebook_cells(edits, workspace_path)

    if not edits:
        return False, "no edits provided"

    try:
        with open(workspace_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return False, f"could not read workspace file: {exc}"

    applied = []
    for i, edit in enumerate(edits, start=1):
        find, replace = edit.get("find", ""), edit.get("replace", "")
        if not find or find not in text:
            return False, f"edit {i} failed to apply: {find!r} not found verbatim in source"
        text = text.replace(find, replace, 1)
        applied.append(f"{find!r} -> {replace!r}")

    try:
        with open(workspace_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as exc:
        return False, f"could not write workspace file: {exc}"

    return True, "; ".join(applied)
