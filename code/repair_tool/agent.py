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

from . import agent_tools, levers
from . import diagnose as diagnose_module
from .venv_manager import get_fresh_venv_python, get_fresh_workspace_copy, get_venv_python, get_workspace_copy

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

MAX_STEPS = 10
AGENT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# LLM_SETUP_kiste.md: lets the backend point at a self-hosted OpenAI-compatible
# server (e.g. the TU Chemnitz "kiste" endpoint) instead of api.openai.com.
# None (the default, unset) means "use the SDK's own default" -- passing
# base_url=None to openai.OpenAI() is a no-op, same as omitting it.
AGENT_BASE_URL = os.environ.get("OPENAI_BASE_URL") or None
# Found during TASK_evaluation.md's pilot: the openai SDK's own default
# per-request timeout is long enough (minutes) that one slow/stuck API
# response can silently stall an entire evaluation run with no local
# symptom to diagnose (no subprocess, no CPU use -- just blocked network
# I/O). A bounded client-side timeout turns that into a normal "LLM call
# failed" error on this one step instead, via the existing except-and-report
# path below.
_REQUEST_TIMEOUT_SECONDS = 120

# TASK_agent_stopping.md: found running kiste's Qwen3.6-35B -- unlike
# gpt-4o, this model doesn't recognise when it's stuck and will spend its
# entire MAX_STEPS budget (~10-12 minutes on kiste's hardware) re-trying
# without converging. The harness enforces termination instead of trusting
# the model's own judgement: if the observed failure signature (from
# diagnose.py, the same classifier the model's own diagnose_error tool
# uses) hasn't changed for this many consecutive re-run attempts, stop.
# MAX_STEPS stays as the hard backstop; this is meant to fire first on
# genuinely stuck cases while leaving real multi-layer fixes their full
# budget (a signature *change* resets the counter).
DEFAULT_NO_PROGRESS_LIMIT = 3

# How many of the most recent tool-result messages are sent to the model
# verbatim; anything older is compacted (see _compacted_messages_for_request).
# TASK_agent_stopping.md #5: with no server-side conversation state, the
# *entire* message history is resent on every single call -- which grows by
# two messages every step, not the notebook source (never resent). This is
# the real, measured contributor to both per-call latency and context-window
# pressure on a long run.
_CONTEXT_COMPACTION_KEEP_RECENT = 4

# Why which stop condition ended a run matters, for the results chapter:
STOP_FIXED = "fixed"
STOP_NO_PROGRESS = "no_progress"
STOP_STEP_CAP = "step_cap"
STOP_MODEL_GAVE_UP = "model_gave_up"
STOP_MODEL_STOPPED = "model_stopped"
STOP_ERROR = "error"

_STOP_TOOLS = ("report_unfixable",)

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
# TASK_improve_fix_rate.md: tools that CHANGE the environment but are not plain installs.
# downgrade_search re-runs the project itself, so a pass is a deterministic verification.
_LEVER_FIX_TOOLS = ("downgrade_search", "lower_python")
# TASK_improve_fix_rate.md #5: how many times the harness may nudge the agent toward an
# untried lever before the no-progress stop is allowed to fire. Bounded on purpose: a
# genuinely stuck run must still stop.
MAX_FALLBACK_NUDGES = 3
_API_ERROR_KINDS = ("module_attribute_removed", "import_name", "object_attribute_error")

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
    "Neither OPENAI_API_KEY nor KISTE_API_TOKEN is set. Set one in your "
    "environment or in a local .env file (see README.md) to enable "
    "agent-based repair."
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
repeat the exact same action. If the same error persists after your \
attempts and you have no new approach, call report_unfixable with a brief \
reason instead of repeating a previous action. Stop calling tools once \
verify succeeds."""


_LEVER_PROMPT_ADDENDA = {
    levers.DOWNGRADE: (
        "When a package is installed but the project fails with a removed/changed-API error from it "
        "(\"cannot import name\", \"has no attribute\"), call downgrade_search on that package instead of "
        "guessing one version bound -- it tries progressively older releases and re-runs the project."
    ),
    levers.INTERPRETER_DOWN: (
        "If an install or import suggests the Python interpreter is too new for an old package the project needs "
        "(see the `hint` field on results), call lower_python to step the interpreter down, then verify."
    ),
    levers.CODE_FORWARD: (
        "For a removed/changed API in a package that IS installed, also consider edit_code to migrate the "
        "notebook's call to the package's current API (exact, verbatim find/replace) -- not only an older version."
    ),
    levers.CONDA_FALLBACK: (
        "install_package automatically retries through conda when pip cannot build a package; read the "
        "`conda_fallback` field of its result."
    ),
    levers.SYSTEM_DEPS: (
        "install_package automatically provisions known system components (for example R for rpy2) through "
        "conda; read the `system_dependency` field of its result."
    ),
}


def system_prompt(active=None) -> str:
    """The base prompt plus one short paragraph per ACTIVE lever; with no lever on
    this is exactly the baseline prompt."""
    active = levers.active() if active is None else active
    extras = [text for name, text in _LEVER_PROMPT_ADDENDA.items() if name in active]
    return _SYSTEM_PROMPT if not extras else _SYSTEM_PROMPT + " " + " ".join(extras)


def _fallback_nudge(trace, last_diag, active, already_nudged: set) -> tuple[str, tuple] | None:
    """Lever 5 (bounded_fallback): the best UNTRIED lever to suggest before the
    no-progress stop fires, as (message, key), or None if there is none.

    "Stuck" is different from "has not tried the other lever yet": a lever counts as
    tried once its tool was called (or it was already suggested). Deterministic -- it
    reads the trace and the last diagnosis, nothing else."""
    called = {step.tool_called for step in trace}
    kind = getattr(last_diag, "kind", "")
    module = (getattr(last_diag, "module", "") or "").split(".", 1)[0]
    package = getattr(last_diag, "package", "") or module

    if kind in _API_ERROR_KINDS and package:
        if levers.DOWNGRADE in active and "downgrade_search" not in called and ("downgrade", package) not in already_nudged:
            return (
                f"Before giving up: the same removed/changed-API error from '{package}' keeps recurring and you have not "
                f"tried searching older releases. Call downgrade_search with package='{package}'.",
                ("downgrade", package),
            )
        if levers.CODE_FORWARD in active and "edit_code" not in called and ("code_forward", package) not in already_nudged:
            return (
                f"Before giving up: the API '{package}' exposes changed. Use edit_code to migrate the notebook to the "
                "package's current API (exact verbatim find/replace), then verify.",
                ("code_forward", package),
            )
    if levers.INTERPRETER_DOWN in active and "lower_python" not in called and ("interpreter_down", 0) not in already_nudged:
        return (
            "Before giving up: you have not tried a different Python interpreter. If an old package the project needs "
            "cannot install or import here, call lower_python to step the interpreter down, then verify.",
            ("interpreter_down", 0),
        )
    if levers.CODE_FORWARD in active and kind in _API_ERROR_KINDS and "edit_code" not in called \
            and ("code_forward", "any") not in already_nudged:
        return ("Before giving up, try edit_code to adapt the notebook to the installed package's API.", ("code_forward", "any"))
    return None


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
    # PILOT_DECISIONS.md #2: a notebook whose own code exceeds the model's
    # context window is a distinct outcome, not a repair failure -- the
    # agent never got a chance to try. Set only when the OpenAI API itself
    # reports this (exc.code == "context_length_exceeded"), not guessed
    # from the error text.
    too_large_for_context: bool = False
    # Found for real running the pilot smoke test: the API key ran out of
    # credits mid-run (OpenAI's documented "insufficient_quota" error type),
    # degrading 37 notebooks to a generic "LLM call failed" non-fix that
    # would have silently counted against effectiveness. Same contract as
    # too_large_for_context -- never guessed from the error text, only set
    # when the SDK's own body says so.
    quota_exhausted: bool = False
    # TASK_agent_stopping.md: which of the STOP_* conditions actually ended
    # this run -- "" only for the few early-return paths that predate this
    # (missing key / openai not installed), which already have .error set
    # and are unambiguous without it.
    stop_reason: str = ""
    # TASK_prepilot_fixes.md #3: set by the repo runners when a repo-local
    # import failure was investigated -- the working directory used, why, and
    # whether it recovered the run (see workdir.describe). Empty otherwise.
    working_directory: dict = field(default_factory=dict)
    # TASK_improve_fix_rate.md #5: the nudges the harness gave before the no-progress stop
    # ([{lever, after_step, message}]) -- empty when the lever is off or never needed.
    fallback_nudges: list = field(default_factory=list)


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


def _compacted_messages_for_request(messages: list[dict]) -> list[dict]:
    """TASK_agent_stopping.md #5: build the outgoing request's message list,
    keeping the _CONTEXT_COMPACTION_KEEP_RECENT most recent tool results
    verbatim (the model needs its current state to decide the next step)
    and replacing any older tool result's content with a short marker.
    `messages` itself (the full-fidelity log) is never touched -- only the
    copy actually sent to the API is compacted, so nothing is lost from the
    trace or from a later call to this same function.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= _CONTEXT_COMPACTION_KEEP_RECENT:
        return messages

    keep_from = tool_indices[-_CONTEXT_COMPACTION_KEEP_RECENT]
    compacted = []
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and i < keep_from:
            compacted.append({**m, "content": "(older tool result omitted to save context -- superseded by later steps)"})
        else:
            compacted.append(m)
    return compacted


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
    no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT,
    era_cutoff_date: str | None = None,
    repo_root: str | None = None,
    tool_runtime=None,
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
    # LLM_SETUP_kiste.md: KISTE_API_TOKEN checked first -- found for real
    # switching backends: a .env with BOTH vars set (OPENAI_API_KEY kept
    # around from the OpenAI path, KISTE_API_TOKEN added for kiste) sent the
    # OpenAI key to kiste's base_url as a bearer token, which kiste's own
    # auth layer correctly rejected with 401. Whichever token is the most
    # specific override present wins; OPENAI_API_KEY is the fallback for
    # when only the default OpenAI path is configured.
    api_key = os.environ.get("KISTE_API_TOKEN") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return AgentResult(target=path, fixed=False, error=_API_KEY_MISSING_MESSAGE)

    try:
        import openai
    except ImportError:
        return AgentResult(target=path, fixed=False, error="the 'openai' package is not installed")

    if python_exe is not None and workspace_path is not None:
        return _agent_repair_loop(
            path, workspace_path, python_exe, openai, api_key, max_steps, no_progress_limit, era_cutoff_date, repo_root, tool_runtime
        )

    if fresh:
        tmp_root = tempfile.mkdtemp(prefix="repair_tool_agent_fresh_")
        try:
            fresh_python = get_fresh_venv_python(tmp_root)
            fresh_workspace = get_fresh_workspace_copy(path, tmp_root)
            return _agent_repair_loop(
                path, fresh_workspace, fresh_python, openai, api_key, max_steps, no_progress_limit, era_cutoff_date, repo_root, tool_runtime
            )
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    cached_python = get_venv_python(path)
    cached_workspace = get_workspace_copy(path)
    return _agent_repair_loop(
        path, cached_workspace, cached_python, openai, api_key, max_steps, no_progress_limit, era_cutoff_date, repo_root, tool_runtime
    )


def _agent_repair_loop(
    path: str, workspace_path: str, python_exe: str, openai, api_key: str, max_steps: int,
    no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT,
    era_cutoff_date: str | None = None,
    repo_root: str | None = None,
    tool_runtime=None,
) -> AgentResult:
    """The actual ReAct tool-calling loop, independent of where
    `workspace_path`/`python_exe` came from (the three modes agent_repair
    documents). `path` is only used for AgentResult.target's label."""
    if tool_runtime is not None:
        # --runtime docker: the executing tools run in the container; the
        # loop itself (and every LLM call) stays here on the host.
        dispatch = tool_runtime.build_dispatch(workspace_path, python_exe, era_cutoff_date)
    else:
        dispatch = agent_tools.build_dispatch(
            workspace_path, python_exe, era_cutoff_date=era_cutoff_date, repo_root=repo_root
        )

    client = openai.OpenAI(api_key=api_key, base_url=AGENT_BASE_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt()},
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

    # TASK_agent_stopping.md #1/#2: the harness's own termination guard,
    # not reliant on the model noticing it's stuck. `last_failure_signature`
    # is the (kind, module, package, symbol) diagnose.py gives the most
    # recent *failing* run_target/verify result; unchanged across
    # `no_progress_limit` consecutive observations (or a fix action repeated
    # verbatim) stops the run as STOP_NO_PROGRESS. A signature *change*
    # means a layer was actually cleared -- real progress -- and resets the
    # counter, preserving the full budget for genuinely multi-layer fixes.
    no_progress_count = 0
    last_failure_signature: tuple | None = None
    attempted_fix_actions: set[tuple[str, str]] = set()
    # TASK_improve_fix_rate.md #5/#6: bounded nudges toward untried levers before the stop.
    nudges: list[dict] = []
    nudged_keys: set = set()
    last_diag = None
    active_levers = levers.active()

    def _R(**kw):
        return AgentResult(fallback_nudges=list(nudges), **kw)

    while len(trace) < max_steps:
        pending_nudge: str | None = None
        try:
            response = client.chat.completions.create(
                model=AGENT_MODEL,
                temperature=0,
                tools=agent_tools.tool_specs(),
                tool_choice="auto",
                messages=_compacted_messages_for_request(messages),
            )
        except Exception as exc:  # noqa: BLE001 - any SDK/network failure -> stop, never a crash
            return _R(
                target=path, fixed=False, trace=trace, error=f"LLM call failed: {exc}",
                llm_calls=llm_calls, total_tokens=total_tokens, stop_reason=STOP_ERROR,
                too_large_for_context=getattr(exc, "code", None) == "context_length_exceeded",
                quota_exhausted=getattr(exc, "type", None) == "insufficient_quota",
            )

        llm_calls += 1
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        if isinstance(tokens, int):
            total_tokens += tokens

        # A reply with no `choices` (an error payload served with a 200, an
        # overloaded backend) used to raise "TypeError: 'NoneType' object is
        # not subscriptable" HERE, outside the handler above, and the run
        # was recorded as an unexplained crash. Treat it as the LLM/SDK
        # error it is. (kiste_smoke_v6: 2 covid notebooks ended this way.)
        choices = getattr(response, "choices", None)
        if not choices:
            return _R(
                target=path, fixed=False, trace=trace, error="LLM call failed: the response contained no choices",
                llm_calls=llm_calls, total_tokens=total_tokens, stop_reason=STOP_ERROR,
            )
        message = choices[0].message
        tool_calls = message.tool_calls or []
        if not tool_calls:
            # the model stopped calling tools without verify succeeding
            return _R(
                target=path, fixed=False, trace=trace, final_strategy=final_strategy,
                llm_calls=llm_calls, total_tokens=total_tokens, stop_reason=STOP_MODEL_STOPPED,
            )

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

            # TASK_agent_stopping.md #3: a clean, explicit give-up signal --
            # checked before dispatch since there's nothing to execute.
            if name in _STOP_TOOLS:
                trace.append(
                    TraceStep(
                        thought=thought, tool_called=name, tool_input=args,
                        tool_result={"ok": True, "reason": args.get("reason", "")},
                        grounding=GROUNDING_LLM, verification=VERIFICATION_NA,
                    )
                )
                return _R(
                    target=path, fixed=False, trace=trace, final_strategy=final_strategy,
                    llm_calls=llm_calls, total_tokens=total_tokens, stop_reason=STOP_MODEL_GAVE_UP,
                )

            # TASK_agent_stopping.md #2: don't re-execute a fix action
            # already tried verbatim in this run -- looping models re-issue
            # identical calls repeatedly; this is the harness's own guard
            # against that, not reliant on the model noticing by itself.
            action_key = (name, json.dumps(args, sort_keys=True)) if name in _FIX_TOOLS else None
            if action_key is not None and action_key in attempted_fix_actions:
                result = {
                    "ok": False, "already_attempted": True,
                    "message": "This exact action was already tried in this run and did not fix it -- try something different.",
                }
                no_progress_count += 1
            else:
                result = _run_tool_call(dispatch, name, args)
                if action_key is not None:
                    attempted_fix_actions.add(action_key)

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
            elif name == "downgrade_search":
                # the candidate versions come from PyPI's own release history (a classical tool), and a
                # passing result is the deterministic checker's -- so this is metadata-grounded
                grounding = GROUNDING_METADATA
                verification = VERIFICATION_UNVERIFIED
                confidence = _confidence_for(grounding, verification)
            elif name == "lower_python":  # the model chose to change the interpreter
                grounding = GROUNDING_LLM
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

            if name == "downgrade_search":
                if result.get("ok"):
                    # the search re-ran the project itself (the same deterministic check as verify) and it passed
                    for idx in pending_action_indices + [step_index]:
                        step = trace[idx]
                        step.verification = VERIFICATION_VERIFIED
                        step.confidence = _confidence_for(step.grounding, VERIFICATION_VERIFIED)
                    return _R(
                        target=path, fixed=True, trace=trace, final_strategy="downgrade",
                        llm_calls=llm_calls, total_tokens=total_tokens, stop_reason=STOP_FIXED,
                    )
                if result.get("final_version") and result.get("final_version") != result.get("original_version"):
                    pending_action_indices.append(step_index)  # the environment changed; the next verify decides
                    final_strategy = "install"
            if name == "lower_python" and result.get("ok"):
                pending_action_indices.append(step_index)
                final_strategy = "interpreter"

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
                return _R(
                    target=path, fixed=True, trace=trace, final_strategy=final_strategy,
                    llm_calls=llm_calls, total_tokens=total_tokens, stop_reason=STOP_FIXED,
                )
            if name == "verify":
                pending_action_indices = []

            if name in ("run_target", "verify") and not result.get("ok"):
                diag = diagnose_module.diagnose(str(result.get("stderr", "")))
                last_diag = diag
                signature = (diag.kind, diag.module, diag.package, diag.symbol)
                if signature == last_failure_signature:
                    no_progress_count += 1
                else:
                    no_progress_count = 0
                    last_failure_signature = signature

            if no_progress_count >= no_progress_limit and levers.BOUNDED_FALLBACK in active_levers \
                    and len(nudges) < MAX_FALLBACK_NUDGES:
                # TASK_improve_fix_rate.md #5: "stuck" is not the same as "has not tried the other lever yet".
                nudge = _fallback_nudge(trace, last_diag, active_levers, nudged_keys)
                if nudge is not None:
                    text, key = nudge
                    nudged_keys.add(key)
                    nudges.append({"lever": key[0], "after_step": len(trace), "message": text})
                    pending_nudge = text
                    no_progress_count = 0  # fresh patience -- but only a bounded number of times (MAX_FALLBACK_NUDGES)

            if no_progress_count >= no_progress_limit:
                return _R(
                    target=path, fixed=False, trace=trace, final_strategy=final_strategy,
                    llm_calls=llm_calls, total_tokens=total_tokens, stop_reason=STOP_NO_PROGRESS,
                )

            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})

        if pending_nudge:
            messages.append({"role": "user", "content": pending_nudge})

    return _R(
        target=path, fixed=False, trace=trace, final_strategy=final_strategy,
        llm_calls=llm_calls, total_tokens=total_tokens, stop_reason=STOP_STEP_CAP,
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
    parser.add_argument(
        "--no-progress-limit", type=int, default=DEFAULT_NO_PROGRESS_LIMIT,
        help="stop early once the observed failure is unchanged this many consecutive times "
             "(a model that doesn't recognise it's stuck would otherwise burn the full step budget)",
    )
    args = parser.parse_args()

    result = agent_repair(
        args.path, max_steps=args.max_steps, fresh=args.fresh, no_progress_limit=args.no_progress_limit
    )

    if result.error:
        print(f"ERROR: {result.error}")
        return 1

    if args.report:
        from .report import build_report

        print(build_report(result))
        return 0 if result.fixed else 1

    print(f"{'FIXED' if result.fixed else 'NOT FIXED'}: {args.path} (stop_reason: {result.stop_reason})")
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
