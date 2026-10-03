"""Render an AgentResult's decision trace into a human-readable transparency
report -- the first concrete version of Vision Doc section 7's report, per
TASK_provenance_and_report.md Part B.

Plain text/markdown only -- no UI, no HTML in this task; richer rendering
comes later. This module adds no new trust logic: it only describes what
agent.py already recorded on each TraceStep (grounding, verification,
confidence).
"""

from __future__ import annotations

from .agent import GROUNDING_LLM, GROUNDING_METADATA, AgentResult, TraceStep

_FIX_TOOLS = ("install_package", "edit_code")


def _describe_action(step: TraceStep) -> str:
    if step.tool_called == "install_package":
        return f"install {step.tool_input.get('package', '?')}"
    if step.tool_called == "edit_code":
        edits = step.tool_input.get("edits") or []
        if edits:
            first = edits[0]
            more = f" (+{len(edits) - 1} more)" if len(edits) > 1 else ""
            return f"edit code: {first.get('find', '?')} -> {first.get('replace', '?')}{more}"
        return "edit code"
    return step.tool_called


def _grounding_note(step: TraceStep) -> str:
    if step.grounding == GROUNDING_METADATA:
        package = step.tool_input.get("package", "")
        base = package.split("<")[0].split(">")[0].split("=")[0].split("!")[0].strip() or package
        return f"metadata_grounded ({base!r} confirmed by a classical lookup)" if base else "metadata_grounded"
    if step.grounding == GROUNDING_LLM:
        return "llm_proposed (no classical tool backed this)"
    return step.grounding


def _verification_note(step: TraceStep) -> str:
    if step.verification == "verified":
        return "verified (the project ran successfully afterward)"
    if step.verification == "unverified":
        if step.tool_result.get("ok") is False:
            return "unverified (this action itself failed to apply)"
        return "unverified (no passing re-run has confirmed this yet)"
    return step.verification


def build_report(result: AgentResult) -> str:
    """Render `result`'s trace as a plain-text transparency report: every
    fix action taken, in order, with its reason, grounding, verification,
    and derived confidence -- including actions that didn't finish the job
    (a necessary-but-insufficient install) or failed outright (a bad edit).
    """
    lines = [f"Repair report — {result.target}", f"Outcome: {'FIXED' if result.fixed else 'NOT FIXED'}"]

    if result.error:
        lines.append(f"Error: {result.error}")
        return "\n".join(lines)

    fix_steps = [step for step in result.trace if step.tool_called in _FIX_TOOLS]

    if not fix_steps:
        lines.append("")
        lines.append("No fix action was taken.")
        return "\n".join(lines)

    lines.append("")
    for i, step in enumerate(fix_steps, start=1):
        lines.append(f"Step {i}  {_describe_action(step)}")
        lines.append(f"        reason:       {step.thought or '(no reason recorded)'}")
        lines.append(f"        grounding:    {_grounding_note(step)}")
        lines.append(f"        verification: {_verification_note(step)}")
        lines.append(f"        confidence:   {step.confidence or 'n/a'}")
        lines.append("")

    grounded = sum(1 for step in fix_steps if step.grounding == GROUNDING_METADATA)
    proposed = sum(1 for step in fix_steps if step.grounding == GROUNDING_LLM)
    accepted = next((step for step in reversed(fix_steps) if step.verification == "verified"), None)

    plural = "s" if len(fix_steps) != 1 else ""
    summary = f"Summary: {len(fix_steps)} fix action{plural} ({grounded} grounded, {proposed} model-proposed)."

    if result.fixed and accepted is not None:
        summary += " Project now runs."
        lines.append(summary)
        note = ""
        if accepted.grounding == GROUNDING_LLM:
            note = " — a model proposal confirmed only by re-running; a reviewer may wish to check it."
        lines.append(f"         Accepted fix confidence: {accepted.confidence}{note}")
    else:
        summary += " Project still does not run."
        lines.append(summary)

    return "\n".join(lines)
