"""Rule-based proposal: decide what (if anything) to do about a Diagnosis,
and record why. No LLM, no network beyond what pypi.resolve_package_name
needs, no side effects — apply.py is what actually does something.

Phase 5 extends this for the "hard cases" (removed/changed APIs) that
Phase 3 can't fix: propose_hard_case() asks an LLM (via llm.py) for two
independent candidate fixes in one call. This function is still a "just
decide, don't do" step — actually applying and verifying each candidate,
picking the winner, and recording the loser as an alternative all happen
in loop.py, exactly like Phase 3's separation of propose() from apply().
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import llm, pypi
from .diagnose import Diagnosis

# Confidence/source per resolution path — see PHASE3_ADDENDUM.md #2.
_CURATED_SOURCE = "curated_alias"
_CURATED_CONFIDENCE = "high"
_PYPI_MATCH_SOURCE = "pypi_name_match"
_PYPI_MATCH_CONFIDENCE = "medium"

# Which strategy to try first, per diagnosis kind (PHASE5_TASK.md's
# STRATEGY_ORDER constant). Kinds not listed use DEFAULT_STRATEGY_ORDER.
# The other strategy is always tried too if the first doesn't verify.
STRATEGY_ORDER: dict[str, tuple[str, str]] = {
    "module_attribute_removed": ("code", "environment"),
    "object_attribute_error": ("code", "environment"),
}
DEFAULT_STRATEGY_ORDER: tuple[str, str] = ("environment", "code")

# Diagnosis kinds Phase 5 routes to the LLM rather than the rule-based path.
HARD_CASE_KINDS = frozenset(
    {"module_attribute_removed", "object_attribute_error", "import_name", "unknown"}
)


def strategy_order_for(kind: str) -> tuple[str, str]:
    return STRATEGY_ORDER.get(kind, DEFAULT_STRATEGY_ORDER)


@dataclass
class Proposal:
    """What repair.propose()/propose_hard_case() thinks should happen next,
    and why. Phase 5 fields (alternatives, strategy_won, edits) are only
    populated for LLM-resolved hard cases; Phase 3's missing_module path
    leaves them at their defaults, unchanged."""

    kind: str  # "install", "code_edit", or "none"
    package: str = ""
    import_name: str = ""
    command: str = ""
    reason: str = ""
    source: str = ""
    confidence: str = "low"  # "high" / "medium" / "low"
    edits: list[dict] = field(default_factory=list)  # code_edit only
    alternatives: str = ""  # rejected strategy + why, once resolved (Phase 5)
    strategy_won: str = ""  # "code" or "environment" (Phase 5)
    model: str = ""  # exact model identifier that produced this fix (Phase 5)


@dataclass
class HardCaseProposal:
    """Both candidates for a hard case, from a single LLM call. Pure aside
    from the network call itself — no files touched, nothing installed.
    loop.py applies and verifies each candidate to pick a winner."""

    understanding: str = ""
    code_fix: llm.FixCandidate | None = None
    env_fix: llm.FixCandidate | None = None
    order: tuple[str, str] = DEFAULT_STRATEGY_ORDER
    model: str = ""  # which model was asked, whether or not it produced a usable answer
    error: str = ""  # set if the LLM call itself failed; no candidates usable


def propose_hard_case(diagnosis: Diagnosis, code: str, error_text: str) -> HardCaseProposal:
    """Ask the LLM once for both candidate fixes. Never raises — a missing
    key, network failure, or malformed response all come back as `.error`.
    """
    suggestion = llm.request_fix(diagnosis, code, error_text)
    if suggestion.error:
        return HardCaseProposal(error=suggestion.error, model=suggestion.model)

    return HardCaseProposal(
        understanding=suggestion.understanding,
        code_fix=suggestion.code_fix,
        env_fix=suggestion.env_fix,
        order=strategy_order_for(diagnosis.kind),
        model=suggestion.model,
    )


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
