"""Render an AgentResult's decision trace into a human-readable transparency
report -- the first concrete version of Vision Doc section 7's report, per
TASK_provenance_and_report.md Part B. build_repo_report (added by
TASK_repo_scale_agent.md Part B) does the same for a whole repository's
RepoAgentResult, reusing build_report for each file's detail.

Plain text/markdown only -- no UI, no HTML in this task; richer rendering
comes later. This module adds no new trust logic: it only describes what
agent.py/agent_repo.py already recorded.
"""

from __future__ import annotations

from .agent import GROUNDING_LLM, GROUNDING_METADATA, AgentResult, TraceStep
from .agent_repo import RepoAgentResult

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


def _file_outcome_line(file_result: AgentResult) -> str:
    if file_result.error:
        return f"{file_result.target}: ERROR ({file_result.error})"
    if file_result.fixed and not file_result.trace:
        return f"{file_result.target}: already passing (no repair needed)"
    if file_result.fixed:
        fix_steps = [step for step in file_result.trace if step.tool_called in _FIX_TOOLS]
        accepted = next((step for step in reversed(fix_steps) if step.verification == "verified"), None)
        confidence = accepted.confidence if accepted else "n/a"
        return f"{file_result.target}: FIXED (confidence: {confidence})"
    return f"{file_result.target}: NOT FIXED (the agent found no verified fix)"


def build_repo_report(result: RepoAgentResult) -> str:
    """Render `result` as a plain-text, repository-level transparency
    report: the overall outcome, every file with its outcome and accepted
    fix's confidence, the per-file fix detail (reusing build_report), and
    an honest summary -- still-broken files are named, never hidden.
    """
    lines = [f"Repository repair report — {result.repo_path}"]

    if result.error:
        lines.append(f"Error: {result.error}")
        return "\n".join(lines)

    s = result.summary
    total = s.get("total", 0)
    already_passing = s.get("already_passing", 0)
    fixed = s.get("fixed", 0)
    still_failing = s.get("still_failing", 0)
    now_running = already_passing + fixed

    lines.append(
        f"Outcome: {now_running} of {total} files now run "
        f"({already_passing} already passing, {fixed} fixed by the agent, {still_failing} still failing)"
    )
    if result.dependency_file_used:
        lines.append(f"Dependencies installed from: {result.dependency_file_used}")
    elif result.dependency_files_found:
        lines.append(f"Dependency files found but not installed: {result.dependency_files_found}")
    lines.append("")

    for file_result in result.files:
        lines.append(_file_outcome_line(file_result))
    lines.append("")

    repaired_files = [f for f in result.files if f.trace]
    if repaired_files:
        lines.append("Per-file detail:")
        lines.append("")
        for file_result in repaired_files:
            lines.append(build_report(file_result))
            lines.append("")

    grounded_total = sum(
        1 for f in result.files for step in f.trace if step.tool_called in _FIX_TOOLS and step.grounding == GROUNDING_METADATA
    )
    proposed_total = sum(
        1 for f in result.files for step in f.trace if step.tool_called in _FIX_TOOLS and step.grounding == GROUNDING_LLM
    )
    lines.append(
        f"Summary: {grounded_total} grounded fix action(s), {proposed_total} model-proposed, "
        f"across {fixed} fixed file(s); {still_failing} file(s) remain broken."
    )

    return "\n".join(lines)
