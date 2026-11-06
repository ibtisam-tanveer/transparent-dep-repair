"""Expose the existing repair engine as a documented, LLM-callable tool
interface -- see AGENTIC_DIRECTION_AND_FIRST_TASK.md section 4.1.

This module adds no new repair logic. Each function below wraps one
existing module's function unchanged and gives it a JSON-serialisable
calling convention (plain dicts in, plain dicts out) that an LLM
tool-calling API can use. `TOOL_SPECS` is the OpenAI `tools=[...]` schema;
`build_dispatch()` binds one repair session's workspace/venv and returns
`{tool_name: callable(**kwargs) -> dict}` for agent.py's tool-calling loop.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Callable

from . import apply as apply_module
from . import condafallback, era, levers, pyladder, versions
from . import diagnose as diagnose_module
from . import pypi
from .localmods import find_local_module
from .repair import Proposal
from .runner import RunResult, run_project


def run_target(path: str, python_exe: str | None = None, timeout: int = 60, repo_root: str | None = None) -> dict:
    """Run the target (.py or .ipynb) and report pass/fail + captured output.

    Wraps runner.run_project unchanged (which itself dispatches .ipynb
    targets to notebook.run_notebook) -- see runner.py.
    """
    if repo_root:
        return asdict(run_project(path, timeout=timeout, python_exe=python_exe, repo_root=repo_root))
    return asdict(run_project(path, timeout=timeout, python_exe=python_exe))


def diagnose_error(run_result: dict) -> dict:
    """Classify the error captured by run_target/verify into a structured
    diagnosis (kind/module/package/symbol). Wraps diagnose.diagnose_result
    unchanged; `run_result` is exactly a run_target/verify result dict.
    """
    rr = RunResult(
        ok=bool(run_result.get("ok", False)),
        returncode=int(run_result.get("returncode", -1)),
        stdout=str(run_result.get("stdout", "")),
        stderr=str(run_result.get("stderr", "")),
    )
    return asdict(diagnose_module.diagnose_result(rr))


def lookup_package(import_name: str) -> dict:
    """Classical/symbolic fact lookup: resolve an import name to an
    installable PyPI package, confirmed to really exist, with its latest
    version. Wraps pypi.resolve_package_name/latest_version unchanged --
    never guess-install a name this hasn't confirmed.
    """
    resolved = pypi.resolve_package_name(import_name)
    return {
        "import_name": import_name,
        "resolved_package": resolved,
        "exists": resolved is not None,
        "latest_version": pypi.latest_version(resolved) if resolved else None,
    }


_HAS_VERSION_CONSTRAINT_RE = re.compile(r"[<>=!~\s;\[]")


def install_package(
    package: str, python_exe: str, era_cutoff_date: str | None = None, repo_root: str | None = None,
    target_path: str | None = None,
) -> dict:
    """Install a package (e.g. 'seaborn' or 'numpy<1.24') into the target's
    isolated venv. Wraps apply.apply unchanged (install path only).

    TASK_version_decay_diagnosis.md Part B: when the model gives a bare
    package name (no version constraint of its own) and `era_cutoff_date`
    is available (the repo's last-commit date, bound in by build_dispatch),
    prefer the latest version that actually existed as of that date over
    today's latest -- defaulting to latest is the main driver of the
    "install at latest, trigger a new removed-API error" cascade found
    diagnosing kiste_smoke_v3's genuine category-A failures. Falls back to
    the model's own choice unchanged if no era-appropriate version can be
    resolved (network issue, or the name never existed on PyPI under any
    version before the cutoff) -- the existing reactive "pin older after a
    new error" behaviour remains available either way, since this never
    touches a package name the model already constrained itself.

    TASK_prepilot_fixes.md #2: when `repo_root` is given and the repo ships
    its own module of that name (`src/combat.py`), the install is REFUSED --
    installing an unrelated PyPI package of the same name would shadow the
    local one (observed: 22 such `combat` installs in one run). The failure
    is a path / working-directory problem, not a missing dependency, and the
    result says so, so the agent doesn't retry it.
    """
    if repo_root:
        local = find_local_module(package, repo_root, require_python_content=True, from_file=target_path)
        if local:
            return {
                "ok": False, "refused": True, "refusal_reason": "repo_local_module",
                "local_module_path": local, "resolved_package": "", "version_source": "refused",
                "log": (
                    f"Refused: {package!r} is this repository's own module ({local}), not a missing PyPI "
                    "dependency -- installing the PyPI package of the same name would shadow it. The import fails "
                    "because of where the code is run from / its sys.path, not because something is uninstalled. "
                    "Do not retry this install."
                ),
            }
    resolved_package = package
    version_source = "unconstrained"
    if era_cutoff_date and not _HAS_VERSION_CONSTRAINT_RE.search(package):
        era_version = pypi.latest_version_as_of(package, era_cutoff_date)
        if era_version:
            resolved_package = f"{package}=={era_version}"
            version_source = "era_capped"

    active = levers.active()
    extras: dict = {}

    # TASK_improve_fix_rate.md #3 (lever system_deps): a package that needs a system
    # component (rpy2 -> R) is provisioned through conda instead of a pip build that
    # cannot work. If conda cannot be used, the reason is recorded and pip is still tried.
    if levers.SYSTEM_DEPS in active:
        provisioned = condafallback.provision_system_dependency(python_exe, resolved_package)
        if provisioned is not None:
            # `lever` is set ONLY when the lever actually produced the install, so a fix is never
            # credited to a lever that was merely attempted and failed.
            extras.update(system_dependency=_conda_summary(provisioned))
            if provisioned["ok"]:
                # conda has no "exclude newer than" mechanism: recorded, not hidden (era.py docstring).
                if levers.ERA_RESOLVE in active:
                    extras["era_resolution"] = {"lever_on": True, "applied": False, "reason": "conda_install",
                                                "mechanism": "conda"}
                return {"ok": True, "log": provisioned["log"], "resolved_package": resolved_package,
                        "version_source": version_source, "mechanism": "conda", "lever": levers.SYSTEM_DEPS, **extras}

    if levers.ERA_RESOLVE in active:
        ok, log, era_meta = apply_module.apply_ex(Proposal(kind="install", package=resolved_package), python_exe, era_date=era_cutoff_date)
    else:  # the unchanged baseline path
        ok, log = apply_module.apply(Proposal(kind="install", package=resolved_package), python_exe)
        era_meta = {}
    result = {"ok": ok, "log": log, "resolved_package": resolved_package, "version_source": version_source, **extras}
    if era_meta:
        result["era_resolution"] = era_meta

    # TASK_improve_fix_rate.md #2 (lever conda_fallback): pip could not BUILD the package.
    if not ok and levers.CONDA_FALLBACK in active and condafallback.looks_like_build_failure(log):
        conda = condafallback.conda_install_with_unpinned_retry(python_exe, resolved_package)
        result.update(conda_fallback=_conda_summary(conda))
        if conda["ok"]:
            result.update(ok=True, mechanism="conda", log=conda["log"], lever=levers.CONDA_FALLBACK)
            if levers.ERA_RESOLVE in active:
                result["era_resolution"] = {"lever_on": True, "applied": False, "reason": "conda_install", "mechanism": "conda"}
    return result


def _conda_summary(conda: dict) -> dict:
    """The compact, report-friendly form of a condafallback result."""
    out = {k: conda.get(k) for k in ("ok", "specs", "tool", "pinned") if k in conda}
    if conda.get("unavailable"):
        out["unavailable"] = conda["unavailable"]
    out["log_tail"] = (conda.get("log") or "")[-300:]
    return out


def edit_code(edits: list[dict], workspace_path: str) -> dict:
    """Apply exact find/replace edits to the working copy (never the
    original file). Wraps apply.apply_code_edit unchanged -- a .ipynb
    target is dispatched to notebook.edit_notebook_cells automatically."""
    ok, log = apply_module.apply_code_edit(edits, workspace_path)
    return {"ok": ok, "log": log}


def verify(path: str, python_exe: str | None = None, timeout: int = 60, repo_root: str | None = None) -> dict:
    """The deterministic checker: re-run the target and report whether it
    now passes. A fix counts as done only when this returns ok=true --
    nothing is accepted on the model's word alone. Re-runs via run_target."""
    return run_target(path, python_exe=python_exe, timeout=timeout, repo_root=repo_root)


def report_unfixable(reason: str) -> dict:
    """TASK_agent_stopping.md #3: an explicit, clean "give up" signal for a
    well-behaved model, alongside (not instead of) agent.py's own
    no-progress guard. Pure signal -- nothing to execute, just echoes the
    reason back for the trace."""
    return {"ok": True, "reason": reason}


TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_target",
            "description": (
                "Run the target project (the file being repaired) and report "
                "whether it passes, plus captured stdout/stderr. Call this "
                "first to see the current failure."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose_error",
            "description": (
                "Classify a run_target/verify result into a structured "
                "diagnosis (kind, module, package, symbol). Use this on the "
                "result of a failed run before deciding what to do."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_result": {
                        "type": "object",
                        "description": "the exact dict returned by run_target or verify",
                    }
                },
                "required": ["run_result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_package",
            "description": (
                "Classical/symbolic fact lookup: confirm an import name "
                "resolves to a real, installable PyPI package, and its "
                "latest version. Always call this before install_package -- "
                "never guess a package name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "import_name": {
                        "type": "string",
                        "description": "the name used in the import statement, e.g. 'seaborn'",
                    }
                },
                "required": ["import_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_package",
            "description": "Install a package (e.g. 'seaborn' or 'numpy<1.24') into the target's isolated venv.",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "pip-installable package spec, e.g. 'seaborn' or 'numpy<1.24'",
                    }
                },
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_code",
            "description": (
                "Apply one or more exact find/replace edits to the target's "
                "working copy. Each 'find' string must match the source "
                "verbatim, character for character -- it is applied by "
                "exact-text replacement, not by understanding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string"},
                                "replace": {"type": "string"},
                            },
                            "required": ["find", "replace"],
                        },
                    }
                },
                "required": ["edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify",
            "description": (
                "The deterministic checker: re-run the target after a change "
                "and report whether it now passes. A fix is only 'done' once "
                "this returns ok=true -- call it after every install_package "
                "or edit_code."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_unfixable",
            "description": (
                "Call this instead of repeating a previous action when the "
                "same error persists after your attempts and you have no "
                "new approach left to try. Ends the run cleanly as "
                "'not fixed' instead of looping on the same action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "one brief sentence on why this can't be fixed"}
                },
                "required": ["reason"],
            },
        },
    },
]


# Tools that exist ONLY when their lever is on (TASK_improve_fix_rate.md): with the
# lever off the model is not even told they exist, so the baseline is untouched.
LEVER_TOOL_SPECS: dict[str, dict] = {
    levers.DOWNGRADE: {
        "type": "function",
        "function": {
            "name": "downgrade_search",
            "description": (
                "When a package IS installed but the project fails with a removed/changed-API error from it "
                "(e.g. \"cannot import name X from 'pkg.mod'\", \"module 'pkg' has no attribute X\"), search "
                "progressively OLDER releases of that package: each is installed and the project re-run, and the "
                "search stops when it passes, when the error changes to something else, or after a bounded number "
                "of tries. Use this instead of guessing one version bound yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {"package": {"type": "string", "description": "PyPI name of the installed package, e.g. 'osmnx'"}},
                "required": ["package"],
            },
        },
    },
    levers.INTERPRETER_DOWN: {
        "type": "function",
        "function": {
            "name": "lower_python",
            "description": (
                "Step the Python interpreter DOWN one minor version and rebuild the environment (the repo's declared "
                "dependencies and your earlier installs are reinstalled). Use when the error suggests the interpreter "
                "is too new for an old package the project needs. Call verify afterwards."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "one brief sentence on why the interpreter is too new"}},
                "required": ["reason"],
            },
        },
    },
}


def tool_specs(active=None) -> list[dict]:
    """TOOL_SPECS plus the tools of the active levers (default: levers.active())."""
    active = levers.active() if active is None else active
    return TOOL_SPECS + [spec for name, spec in LEVER_TOOL_SPECS.items() if name in active]


def build_dispatch(
    workspace_path: str, python_exe: str, era_cutoff_date: str | None = None, repo_root: str | None = None,
) -> dict[str, Callable[..., dict]]:
    """Bind one repair session's workspace copy and venv, returning
    {tool_name: callable(**kwargs) -> dict} for agent.py's tool-calling loop.

    `era_cutoff_date` (the repo's last-commit date, see repo.repo_last_commit_date)
    is bound into install_package only -- TASK_version_decay_diagnosis.md
    Part B. None (single-file mode with no repo context) means "no
    era-capping", preserving the prior latest-version behaviour exactly.

    `repo_root` (the whole-repo working copy, when repairing inside a repo)
    enables the repo-local name-collision guard in install_package and the
    working-directory resolution in run_target/verify (TASK_prepilot_fixes.md
    #2 and #3). None (single-file mode) leaves both off.

    The interpreter is held in `state` because lever `interpreter_down` can replace
    it mid-run; every tool reads it from there. `state["installed"]` is what the
    agent installed so far (replayed onto a lower interpreter); `state["last_run"]`
    is the latest failing run (the baseline for a downgrade search).
    """
    # Remember refused installs so a repeated attempt is answered instantly
    # with the same verdict instead of re-checking (and the agent is told it
    # already tried this).
    refused: dict[str, dict] = {}
    state: dict = {"python_exe": python_exe, "installed": [], "last_run": None, "era_refusals": 0}

    def _with_hint(result: dict, text: str) -> dict:
        if levers.INTERPRETER_DOWN in levers.active() and not result.get("ok"):
            hint = pyladder.python_too_new_hint(text)
            if hint:
                result = dict(result, hint=hint)
        return result

    def _record_run(result: dict) -> dict:
        state["last_run"] = None if result.get("ok") else result
        return _with_hint(result, str(result.get("stderr", "")))

    def _install(package: str) -> dict:
        key = package.strip().lower()
        if key in refused:
            return dict(refused[key], log="Already refused: " + refused[key]["log"])
        gate = era.strict_gate(package, era_cutoff_date, state)  # lever era_strict (None when off / acceptable)
        if gate is not None:
            return gate
        result = install_package(
            package, state["python_exe"], era_cutoff_date=era_cutoff_date, repo_root=repo_root, target_path=workspace_path,
        )
        if result.get("refused"):
            refused[key] = result
        elif result.get("ok"):
            state["installed"].append(result.get("resolved_package") or package)
        return _with_hint(result, str(result.get("log", "")))

    def _downgrade(package: str) -> dict:
        python = state["python_exe"]

        def install_fn(spec: str) -> tuple[bool, str]:
            return apply_module.apply(Proposal(kind="install", package=spec), python, era_date=era_cutoff_date)

        result = versions.downgrade_search(
            package, python, install_fn,
            run_fn=lambda: run_target(workspace_path, python_exe=python, repo_root=repo_root),
            initial_failure=state["last_run"],
        )
        if result.get("final_version") and result["final_version"] != result.get("original_version"):
            base = condafallback.base_name(package)
            state["installed"] = [s for s in state["installed"] if condafallback.base_name(s) != base]
            state["installed"].append(f"{package}=={result['final_version']}")
        if result.get("last_run") is not None:
            state["last_run"] = None if result["last_run"].get("ok") else result["last_run"]
        return result

    def _lower(reason: str) -> dict:
        result = pyladder.lower_python(state["python_exe"], repo_root, list(state["installed"]), reason, era_date=era_cutoff_date)
        if result.get("ok") and result.get("python_exe"):
            state["python_exe"] = result["python_exe"]
            state["last_run"] = None
        return result

    dispatch = {
        "run_target": lambda **_: _record_run(run_target(workspace_path, python_exe=state["python_exe"], repo_root=repo_root)),
        "diagnose_error": lambda **kw: diagnose_error(kw["run_result"]),
        "lookup_package": lambda **kw: lookup_package(kw["import_name"]),
        "install_package": lambda **kw: _install(kw["package"]),
        "edit_code": lambda **kw: edit_code(kw["edits"], workspace_path),
        "verify": lambda **_: _record_run(verify(workspace_path, python_exe=state["python_exe"], repo_root=repo_root)),
        "report_unfixable": lambda **kw: report_unfixable(kw["reason"]),
    }
    active = levers.active()
    if levers.DOWNGRADE in active:
        dispatch["downgrade_search"] = lambda **kw: _downgrade(kw["package"])
    if levers.INTERPRETER_DOWN in active:
        dispatch["lower_python"] = lambda **kw: _lower(kw.get("reason", ""))
    return dispatch
