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

    log = completed.stdout + completed.stderr
    return completed.returncode == 0, log
