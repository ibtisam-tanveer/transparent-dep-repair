"""Rule-based proposal: decide what (if anything) to do about a Diagnosis,
and record why. No LLM, no network beyond what pypi.resolve_package_name
needs, no side effects — apply.py is what actually does something.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import pypi
from .diagnose import Diagnosis

# Confidence/source per resolution path — see PHASE3_ADDENDUM.md #2.
_CURATED_SOURCE = "curated_alias"
_CURATED_CONFIDENCE = "high"
_PYPI_MATCH_SOURCE = "pypi_name_match"
_PYPI_MATCH_CONFIDENCE = "medium"


@dataclass
class Proposal:
    """What repair.propose() thinks should happen next, and why."""

    kind: str  # "install" or "none"
    package: str = ""
    import_name: str = ""
    command: str = ""
    reason: str = ""
    source: str = ""
    confidence: str = "low"  # "high" / "medium" / "low"


def propose(diagnosis: Diagnosis) -> Proposal:
    """Decide what to do about `diagnosis`. Only missing_module (resolvable
    to a real PyPI package) gets an install proposal; everything else is
    reported honestly as not handled yet — deliberately narrow scope, see
    PHASE3_TASK.md's "Scope" section.
    """
    if diagnosis.kind != "missing_module":
        return Proposal(
            kind="none",
            reason=(
                f"kind={diagnosis.kind!r} requires code-level understanding, "
                "not just an install; deferred to a later (LLM) phase"
            ),
            confidence="low",
        )

    import_name = diagnosis.package or diagnosis.module
    resolved = pypi.resolve_package_name(import_name)

    if resolved is None:
        return Proposal(
            kind="none",
            import_name=import_name,
            reason=f"could not resolve {import_name!r} to an installable PyPI package",
            confidence="low",
        )

    if import_name in pypi.ALIASES:
        source, confidence = _CURATED_SOURCE, _CURATED_CONFIDENCE
        reason = f"{import_name!r} is a known alias for PyPI package {resolved!r}"
    else:
        source, confidence = _PYPI_MATCH_SOURCE, _PYPI_MATCH_CONFIDENCE
        reason = f"{import_name!r} exists on PyPI as {resolved!r}; installing it directly"

    return Proposal(
        kind="install",
        package=resolved,
        import_name=import_name,
        command=f"pip install {resolved}",
        reason=reason,
        source=source,
        confidence=confidence,
    )
