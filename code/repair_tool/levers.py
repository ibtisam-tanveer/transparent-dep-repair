"""Individually toggleable repair levers (TASK_improve_fix_rate.md).

Every lever is a measured experiment, so each can be switched on or off on its
own and the default is OFF (the baseline behaviour every earlier result was
produced with). The active set travels as a comma list in one environment
variable so it reaches worker subprocesses and the in-container action server
without any extra plumbing.

  downgrade         iterative version downgrade after a removed/changed-API error
  conda_fallback    retry a pip build failure through conda/micromamba
  system_deps       provision a system component (e.g. R for rpy2) through conda
  interpreter_down  step the Python interpreter down when it is too new
  bounded_fallback  before the no-progress stop, nudge the agent toward untried levers
  code_forward      prefer an edit_code migration to the new API for removed-API errors
  era_resolve       resolve every pip install as of the repo's date (uv --exclude-newer): era-consistent versions
  era_date          date that era from the notebook's true era (paper date / the file's git history), not the repo's last commit
  era_strict        refuse an agent install that no release by the era date can satisfy (no silent fallback to modern pip)
"""

from __future__ import annotations

import os

ENV = "REPAIR_TOOL_LEVERS"

DOWNGRADE = "downgrade"
CONDA_FALLBACK = "conda_fallback"
SYSTEM_DEPS = "system_deps"
INTERPRETER_DOWN = "interpreter_down"
BOUNDED_FALLBACK = "bounded_fallback"
CODE_FORWARD = "code_forward"
ERA_RESOLVE = "era_resolve"
ERA_DATE = "era_date"
ERA_STRICT = "era_strict"

# The six levers of TASK_improve_fix_rate.md (the set every LEVERS_RESULTS.md number was measured with).
SIX_LEVERS = (DOWNGRADE, CONDA_FALLBACK, SYSTEM_DEPS, INTERPRETER_DOWN, BOUNDED_FALLBACK, CODE_FORWARD)
ALL_LEVERS = SIX_LEVERS + (ERA_RESOLVE, ERA_DATE, ERA_STRICT)


def parse(spec: str | None) -> frozenset[str]:
    """'all' | 'none' | '' | 'downgrade,conda_fallback' -> a validated set.
    An unknown name is an error, not silently ignored: a typo would otherwise
    quietly run a different experiment than the one written in the log."""
    if spec is None:
        return frozenset()
    text = spec.strip().lower()
    if text in ("", "none", "off"):
        return frozenset()
    if text == "all":
        return frozenset(ALL_LEVERS)
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [n for n in names if n not in ALL_LEVERS]
    if unknown:
        raise ValueError(f"unknown lever(s) {unknown}; valid: {', '.join(ALL_LEVERS)}, 'all', 'none'")
    return frozenset(names)


def to_env_value(levers) -> str:
    return ",".join(name for name in ALL_LEVERS if name in set(levers))


def active() -> frozenset[str]:
    try:
        return parse(os.environ.get(ENV, ""))
    except ValueError:
        return frozenset()


def enabled(name: str) -> bool:
    return name in active()
