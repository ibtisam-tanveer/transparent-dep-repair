"""In-container action server (TASK_containerize.md Part B).

The agent loop and every LLM call stay on the HOST. Only the *actions* run in
the container, one `docker exec ... python -m repair_tool.container_rpc` per
action: a JSON request on stdin, one `RPC_RESULT:<json>` line on stdout. Each
action is the SAME function the host path calls (run_project, install_package,
the environment build ...) -- nothing is reimplemented here, so interpreter
repair, declared-deps-first, era-capped installs, working-directory
resolution, the async-kernel / process-group / watchdog hang fixes and
provenance all apply inside the container exactly as they do on the host.

Actions (payload -> result):
  setup            build the repo's environment (baseline venv, interpreter
                   repair, declared dependencies, postBuild)
  run_project      run a script/notebook (with working-directory resolution)
  install_package  install one package into the target environment
  edit_code        apply find/replace edits to the target file
  workdir_describe what working-directory resolution did for a file
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict

RESULT_PREFIX = "RPC_RESULT:"
STATE_DIR = "/work/state"
WORKDIR_STATE = os.path.join(STATE_DIR, "workdir.json")


def _setup(p: dict) -> dict:
    from . import era
    from . import repo as repo_module
    from .interpreter import build_repair_environment
    from .venv_manager import get_fresh_venv_python

    repo_root, tmp_root = p["repo_root"], p["tmp_root"]
    started = time.time()
    python_exe = get_fresh_venv_python(tmp_root)
    interpreter_env = build_repair_environment(
        repo_root, os.path.join(tmp_root, "interp"), python_exe,
        era_cutoff_date=p.get("era_cutoff_date"), enabled=bool(p.get("repair_interpreter", True)),
    )
    python_exe = interpreter_env.python_exe

    files = repo_module.find_dependency_files(repo_root)
    era_date, era_metas = p.get("era_cutoff_date"), []
    env_ok, env_log, used = repo_module._install_dependencies(
        python_exe, repo_root, files, repo_module._INSTALL_TIMEOUT, era_date=era_date, era_meta_out=era_metas)

    if p.get("run_postbuild", True):
        postbuild = repo_module.run_postbuild(repo_root, python_exe, os.path.join(tmp_root, "postbuild_home"))
    else:
        postbuild = {"found": repo_module.find_postbuild(repo_root) is not None, "ran": False, "ok": False,
                     "path": "", "returncode": None, "log_tail": "disabled (--no-postbuild)"}
    return {
        "python_exe": python_exe, "interpreter_meta": interpreter_env.meta,
        "env_setup_ok": env_ok, "env_setup_log": env_log[-4000:], "dependency_file_used": used,
        "dependency_files_found": files, "postbuild": postbuild, "setup_seconds": round(time.time() - started, 1),
        "era_resolution_setup": era.setup_record(era_date, era_metas, interpreter_env.meta),
    }


def _run_project(p: dict) -> dict:
    from . import workdir
    from .runner import run_project

    workdir.load_state(WORKDIR_STATE)
    result = run_project(p["path"], timeout=int(p["timeout"]), python_exe=p["python_exe"], repo_root=p.get("repo_root"))
    workdir.save_state(WORKDIR_STATE)
    return asdict(result)


def _install_package(p: dict) -> dict:
    from .agent_tools import install_package

    return install_package(
        p["package"], p["python_exe"], era_cutoff_date=p.get("era_cutoff_date"), repo_root=p.get("repo_root"),
        target_path=p.get("target_path"),
    )


def _edit_code(p: dict) -> dict:
    from .agent_tools import edit_code

    return edit_code(p["edits"], p["workspace_path"])


def _workdir_describe(p: dict) -> dict:
    from . import workdir

    workdir.load_state(WORKDIR_STATE)
    return workdir.describe(p["path"], p["repo_root"])


def _downgrade_search(p: dict) -> dict:
    """Lever 1 inside the container: install/run are the container's own functions."""
    from . import apply, workdir
    from .repair import Proposal
    from .runner import run_project
    from .versions import downgrade_search

    python = p["python_exe"]

    def install_fn(spec: str):
        return apply.apply(Proposal(kind="install", package=spec), python, era_date=p.get("era_cutoff_date"))

    def run_fn() -> dict:
        workdir.load_state(WORKDIR_STATE)
        result = asdict(run_project(p["path"], timeout=int(p["timeout"]), python_exe=python, repo_root=p.get("repo_root")))
        workdir.save_state(WORKDIR_STATE)
        return result

    return downgrade_search(
        p["package"], python, install_fn, run_fn, initial_failure=p.get("initial_failure"),
        max_tries=int(p.get("max_tries", 6)), time_budget_seconds=float(p.get("time_budget", 1200)),
    )


def _lower_python(p: dict) -> dict:
    """Lever 4 inside the container."""
    from .pyladder import lower_python

    return lower_python(p["python_exe"], p.get("repo_root"), p.get("installed_specs") or [], p.get("reason", ""),
                        era_date=p.get("era_cutoff_date"))


ACTIONS = {
    "downgrade_search": _downgrade_search,
    "lower_python": _lower_python,
    "setup": _setup,
    "run_project": _run_project,
    "install_package": _install_package,
    "edit_code": _edit_code,
    "workdir_describe": _workdir_describe,
}


def main() -> int:
    try:
        request = json.load(sys.stdin)
        handler = ACTIONS[request["action"]]
        result = handler(request.get("payload") or {})
    except Exception as exc:  # noqa: BLE001 - an action failure is a result, never a crash of the server
        from .workdir import describe_exception

        result = {"rpc_error": describe_exception(exc)}
    sys.stdout.write("\n" + RESULT_PREFIX + json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
