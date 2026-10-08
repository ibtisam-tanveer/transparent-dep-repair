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

from dataclasses import asdict
from typing import Callable

from . import apply as apply_module
from . import diagnose as diagnose_module
from . import pypi
from .repair import Proposal
from .runner import RunResult, run_project


def run_target(path: str, python_exe: str | None = None, timeout: int = 60) -> dict:
    """Run the target (.py or .ipynb) and report pass/fail + captured output.

    Wraps runner.run_project unchanged (which itself dispatches .ipynb
    targets to notebook.run_notebook) -- see runner.py.
    """
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


def install_package(package: str, python_exe: str) -> dict:
    """Install a package (e.g. 'seaborn' or 'numpy<1.24') into the target's
    isolated venv. Wraps apply.apply unchanged (install path only)."""
    ok, log = apply_module.apply(Proposal(kind="install", package=package), python_exe)
    return {"ok": ok, "log": log}


def edit_code(edits: list[dict], workspace_path: str) -> dict:
    """Apply exact find/replace edits to the working copy (never the
    original file). Wraps apply.apply_code_edit unchanged -- a .ipynb
    target is dispatched to notebook.edit_notebook_cells automatically."""
    ok, log = apply_module.apply_code_edit(edits, workspace_path)
    return {"ok": ok, "log": log}


def verify(path: str, python_exe: str | None = None, timeout: int = 60) -> dict:
    """The deterministic checker: re-run the target and report whether it
    now passes. A fix counts as done only when this returns ok=true --
    nothing is accepted on the model's word alone. Re-runs via run_target."""
    return run_target(path, python_exe=python_exe, timeout=timeout)


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
]


def build_dispatch(workspace_path: str, python_exe: str) -> dict[str, Callable[..., dict]]:
    """Bind one repair session's workspace copy and venv, returning
    {tool_name: callable(**kwargs) -> dict} for agent.py's tool-calling loop.
    """
    return {
        "run_target": lambda **_: run_target(workspace_path, python_exe=python_exe),
        "diagnose_error": lambda **kw: diagnose_error(kw["run_result"]),
        "lookup_package": lambda **kw: lookup_package(kw["import_name"]),
        "install_package": lambda **kw: install_package(kw["package"], python_exe),
        "edit_code": lambda **kw: edit_code(kw["edits"], workspace_path),
        "verify": lambda **_: verify(workspace_path, python_exe=python_exe),
    }
