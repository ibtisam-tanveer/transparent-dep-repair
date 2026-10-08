"""A single tool-calling agent that replaces loop.py's fixed decision-making
with an LLM choosing which tool to call next -- see
AGENTIC_DIRECTION_AND_FIRST_TASK.md section 4.2, sharpened by
TASK_provenance_and_report.md into a two-axis trust model.

This is a new, parallel entry point. loop.py's deterministic fixed loop is
untouched and still exists as the baseline for comparison (RQ3). The model
only *proposes* which tool to call; a fix counts as done only once the
`verify` tool (re-running the project) confirms it -- the model owns
search, the checker owns authority.

Every tool call is recorded as a TraceStep carrying two independent axes
(TASK_provenance_and_report.md Part A):

- `grounding` -- was this action backed by a deterministic/classical tool
  result (`metadata_grounded`), only the LLM's own reasoning
  (`llm_proposed`), or is it one of the observation tools themselves,
  which are facts rather than proposals (`deterministic`)? Set once, when
  the action is taken, and never changed afterward -- an action's source
  doesn't change based on outcome. (`kg_grounded` is reserved for a future
  knowledge-graph tool; nothing produces it yet.)
- `verification` -- did a `verify` call *after* this action confirm the
  project then ran (`verified`), or has nothing confirmed it yet
  (`unverified`)? `n/a` for the observation tools (nothing to verify).

A `confidence` is derived from the (grounding, verification) pair for every
fix action (install_package/edit_code) -- see `_CONFIDENCE`.

Never raises: a missing key, a network failure, or a malformed tool call
all degrade to AgentResult(fixed=False, error=...), the same never-raise
contract as llm.py.

TASK_repo_scale_agent.md added two more ways to obtain an environment,
alongside the default persistent cache: an explicit `python_exe`/
`workspace_path` (how agent_repo.py runs this same function inside a
repo's one shared venv) and `fresh=True` (a throwaway venv/workspace,
required for dataset evaluation -- see agent_repair's docstring). All
three funnel through the same `_agent_repair_loop`, so repair behaviour
itself never depends on where the environment came from.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field

from . import agent_tools
from .venv_manager import get_fresh_venv_python, get_fresh_workspace_copy, get_venv_python, get_workspace_copy

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

MAX_STEPS = 10
AGENT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# Found during TASK_evaluation.md's pilot: the openai SDK's own default
# per-request timeout is long enough (minutes) that one slow/stuck API
# response can silently stall an entire evaluation run with no local
# symptom to diagnose (no subprocess, no CPU use -- just blocked network
# I/O). A bounded client-side timeout turns that into a normal "LLM call
# failed" error on this one step instead, via the existing except-and-report
# path below.
_REQUEST_TIMEOUT_SECONDS = 120

# Grounding -- the source of an action, fixed at the moment it's taken.
GROUNDING_METADATA = "metadata_grounded"
GROUNDING_KG = "kg_grounded"  # reserved for a future knowledge-graph tool; unused
GROUNDING_LLM = "llm_proposed"
GROUNDING_DETERMINISTIC = "deterministic"

# Verification -- the outcome, which can be upgraded once a later verify
# call confirms this specific action.
VERIFICATION_VERIFIED = "verified"
VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_NA = "n/a"

_OBSERVATION_TOOLS = ("run_target", "diagnose_error", "verify", "lookup_package")
_FIX_TOOLS = ("install_package", "edit_code")

# TASK_provenance_and_report.md Part A's confidence table. Only defined for
# the two axes a fix action can actually have -- deterministic/n/a tools
# have no confidence (see _confidence_for).
_CONFIDENCE = {
    (GROUNDING_METADATA, VERIFICATION_VERIFIED): "high",
    (GROUNDING_METADATA, VERIFICATION_UNVERIFIED): "medium",
    (GROUNDING_LLM, VERIFICATION_VERIFIED): "medium-high",
    (GROUNDING_LLM, VERIFICATION_UNVERIFIED): "low",
}

_PACKAGE_NAME_RE = re.compile(r"[<>=!~\s;\[].*$")

_API_KEY_MISSING_MESSAGE = (
    "OPENAI_API_KEY is not set. Set it in your environment or in a local "
    ".env file (see README.md) to enable agent-based repair."
)

_SYSTEM_PROMPT = """You are a Python dependency-repair agent. You are given a \
failing target file and a set of tools to diagnose and fix it. Call \
run_target first to see the current failure, then diagnose_error to \
classify it. For a missing/removed package, call lookup_package to confirm \
a real installable name before install_package -- never guess a package \
name. For a removed/changed API, use edit_code with an exact, verbatim \
find/replace. After every install_package or edit_code, call verify to \
re-run the project -- a fix is only accepted once verify reports ok=true. \
If verify still fails, try a different tool or a different fix; do not \
repeat the exact same action. Stop calling tools once verify succeeds."""


def _confidence_for(grounding: str, verification: str) -> str:
    return _CONFIDENCE.get((grounding, verification), "")


def _package_base_name(package_spec: str) -> str:
    """'numpy<1.24' -> 'numpy'; 'seaborn' -> 'seaborn'."""
    return _PACKAGE_NAME_RE.sub("", package_spec).strip().lower()


@dataclass
class TraceStep:
    """One tool call the agent made: what it was thinking, which tool, with
    what input, what came back, and the two-axis trust tags."""

    thought: str
    tool_called: str
    tool_input: dict
    tool_result: dict
    grounding: str
    verification: str
    confidence: str = ""


@dataclass
class AgentResult:
    """Outcome of one agent_repair() run -- target, verdict, and the full
    decision trace, the seed of the transparency report.

    `llm_calls`/`total_tokens` are cost-visibility bookkeeping for
    TASK_evaluation.md's harness (a real evaluation run is real money) --
    not trust data, not used by report.py, never affect `fixed`. Populated
    passively from the OpenAI response's own `usage` field when the SDK
    provides one; always 0 for a mocked/offline run.
    """

    target: str
    fixed: bool
    trace: list[TraceStep] = field(default_factory=list)
    final_strategy: str = ""
    error: str = ""
    llm_calls: int = 0
    total_tokens: int = 0


def _assistant_message_entry(message) -> dict:
    entry: dict = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ]
    return entry


def _run_tool_call(dispatch: dict, name: str, args: dict) -> dict:
    fn = dispatch.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return fn(**args)
    except Exception as exc:  # noqa: BLE001 - a bad tool call must not crash the agent
        return {"error": f"tool {name!r} raised: {exc}"}


def agent_repair(
    path: str,
    max_steps: int = MAX_STEPS,
    fresh: bool = False,
    python_exe: str | None = None,
    workspace_path: str | None = None,
) -> AgentResult:
    """Let an LLM agent choose tools to fix `path`, verifying by re-running.

    Three ways to get an environment + working copy to run in, checked in
    this order:

    1. `python_exe` and `workspace_path` given explicitly -- used as-is, no
       venv_manager call at all. This is how agent_repo.py runs this exact
       function against one file of a repository inside that repo's *one
       shared* environment, instead of giving every file its own venv.
    2. `fresh=True` -- a brand-new, throwaway venv and a fresh copy of
       `path`, ignoring any cached one, deleted again before this call
       returns. **Any dataset evaluation must use fresh=True.** The
       default (3) below reuses venv_manager's persistent, hash-cached
       venv/workspace for `path` -- correct for interactive/manual use,
       but a *second* evaluation run against the same target would
       silently find it already fixed from the first run and report
       "nothing to do" instead of repairing from scratch, corrupting
       results across a dataset. See TASK_repo_scale_agent.md Part C.
    3. Default -- venv_manager's persistent, hash-cached venv/workspace for
       `path`, reused across calls.

    `path` itself is never modified in any of the three cases -- only the
    working copy (wherever it came from) is ever touched.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return AgentResult(target=path, fixed=False, error=_API_KEY_MISSING_MESSAGE)

    try:
        import openai
    except ImportError:
        return AgentResult(target=path, fixed=False, error="the 'openai' package is not installed")

    if python_exe is not None and workspace_path is not None:
        return _agent_repair_loop(path, workspace_path, python_exe, openai, api_key, max_steps)

    if fresh:
        tmp_root = tempfile.mkdtemp(prefix="repair_tool_agent_fresh_")
        try:
            fresh_python = get_fresh_venv_python(tmp_root)
            fresh_workspace = get_fresh_workspace_copy(path, tmp_root)
            return _agent_repair_loop(path, fresh_workspace, fresh_python, openai, api_key, max_steps)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    cached_python = get_venv_python(path)
    cached_workspace = get_workspace_copy(path)
    return _agent_repair_loop(path, cached_workspace, cached_python, openai, api_key, max_steps)


def _agent_repair_loop(path: str, workspace_path: str, python_exe: str, openai, api_key: str, max_steps: int) -> AgentResult:
    """The actual ReAct tool-calling loop, independent of where
    `workspace_path`/`python_exe` came from (the three modes agent_repair
    documents). `path` is only used for AgentResult.target's label."""
    dispatch = agent_tools.build_dispatch(workspace_path, python_exe)

    client = openai.OpenAI(api_key=api_key, timeout=_REQUEST_TIMEOUT_SECONDS)
    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"The target file is {workspace_path!r}. Fix it so it runs successfully.",
        },
    ]

    trace: list[TraceStep] = []
    # Packages a lookup_package call has actually confirmed exist, for the
    # whole run -- a confirmed PyPI fact doesn't go stale just because a
    # later, unrelated verify failed, so this is never reset.
    confirmed_packages: set[str] = set()
    # Fix actions (install/edit) applied since the last verify call, still
    # waiting to find out if they made the project pass.
    pending_action_indices: list[int] = []
    final_strategy = ""
    llm_calls = 0
    total_tokens = 0

    while len(trace) < max_steps:
        try:
            response = client.chat.completions.create(
                model=AGENT_MODEL,
                temperature=0,
                tools=agent_tools.TOOL_SPECS,
                tool_choice="auto",
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - any SDK/network failure -> stop, never a crash
            return AgentResult(
                target=path, fixed=False, trace=trace, error=f"LLM call failed: {exc}",
                llm_calls=llm_calls, total_tokens=total_tokens,
            )

        llm_calls += 1
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        if isinstance(tokens, int):
            total_tokens += tokens

        message = response.choices[0].message
        tool_calls = message.tool_calls or []
        if not tool_calls:
            break  # the model stopped calling tools without verify succeeding

        messages.append(_assistant_message_entry(message))
        thought = message.content or ""

        for call in tool_calls:
            if len(trace) >= max_steps:
                break

            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            result = _run_tool_call(dispatch, name, args)

            if name in _OBSERVATION_TOOLS:
                grounding = GROUNDING_DETERMINISTIC
                verification = VERIFICATION_NA
                confidence = ""
                if name == "lookup_package" and result.get("exists"):
                    for candidate in (result.get("import_name"), result.get("resolved_package")):
                        if candidate:
                            confirmed_packages.add(candidate.lower())
            elif name == "install_package":
                base_name = _package_base_name(args.get("package", ""))
                grounding = GROUNDING_METADATA if base_name in confirmed_packages else GROUNDING_LLM
                verification = VERIFICATION_UNVERIFIED
                confidence = _confidence_for(grounding, verification)
            else:  # edit_code -- always the model's own reasoning, no classical tool backs a code rewrite
                grounding = GROUNDING_LLM
                verification = VERIFICATION_UNVERIFIED
                confidence = _confidence_for(grounding, verification)

            step_index = len(trace)
            trace.append(
                TraceStep(
                    thought=thought,
                    tool_called=name,
                    tool_input=args,
                    tool_result=result,
                    grounding=grounding,
                    verification=verification,
                    confidence=confidence,
                )
            )

            if name in _FIX_TOOLS and result.get("ok"):
                pending_action_indices.append(step_index)
                final_strategy = "install" if name == "install_package" else "code_edit"

            # run_target and verify are the same deterministic check (verify
            # just re-runs run_target -- see agent_tools.py); a project that
            # already passes (nothing to fix, or a previously-fixed cached
            # workspace) can be confirmed by either name, not only "verify"
            # literally -- the fact that it runs doesn't depend on which
            # tool name the model happened to use to observe that.
            if name in ("run_target", "verify") and result.get("ok"):
                for idx in pending_action_indices:
                    step = trace[idx]
                    step.verification = VERIFICATION_VERIFIED
                    step.confidence = _confidence_for(step.grounding, VERIFICATION_VERIFIED)
                return AgentResult(
                    target=path, fixed=True, trace=trace, final_strategy=final_strategy,
                    llm_calls=llm_calls, total_tokens=total_tokens,
                )
            if name == "verify":
                pending_action_indices = []

            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})

    return AgentResult(
        target=path, fixed=False, trace=trace, final_strategy=final_strategy,
        llm_calls=llm_calls, total_tokens=total_tokens,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Let an LLM agent choose tools to fix a target file, verifying by re-running."
    )
    parser.add_argument("path", help="path to the target .py or .ipynb file")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument(
        "--report", action="store_true", help="print a human-readable transparency report instead of the raw trace"
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="use a brand-new venv/workspace instead of the cached one (required for dataset evaluation)",
    )
    args = parser.parse_args()

    result = agent_repair(args.path, max_steps=args.max_steps, fresh=args.fresh)

    if result.error:
        print(f"ERROR: {result.error}")
        return 1

    if args.report:
        from .report import build_report

        print(build_report(result))
        return 0 if result.fixed else 1

    print(f"{'FIXED' if result.fixed else 'NOT FIXED'}: {args.path}")
    for i, step in enumerate(result.trace, start=1):
        print(f"  step {i}: {step.tool_called}({step.tool_input})")
        print(f"    grounding: {step.grounding}, verification: {step.verification}, confidence: {step.confidence or 'n/a'}")
        print(f"    result: {step.tool_result}")
    if result.final_strategy:
        print(f"  final_strategy: {result.final_strategy}")

    return 0 if result.fixed else 1


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
