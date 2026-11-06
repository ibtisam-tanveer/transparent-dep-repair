"""Container runtime: run the repair ACTIONS inside a Linux container
(TASK_containerize.md Parts A-C; `--runtime docker`).

What runs where
  host       the agent loop, every LLM call (kiste needs the university VPN
             and a container's NAT'd network cannot be trusted to carry it),
             PyPI lookups, diagnosis of captured output, result rows.
  container  everything that executes or installs: building the environment
             (interpreter provisioning, declared dependencies, postBuild),
             running the target script/notebook, installing packages,
             editing the target file.

Each action is one `docker exec ... repair_tool.container_rpc`; see that
module. The container is created per repo and removed afterwards (`fresh`),
the repo is COPIED in (not bind-mounted: a macOS bind mount is slow and lets
a notebook write into the host checkout), and the edited target is copied
back after each file so the host copy mirrors what the container ran.

The platform is a configurable default (`linux/amd64`, so old x86_64 wheels
resolve on an arm64 host), recorded on every row -- it is part of the
reproduced environment. On Apple Silicon amd64 runs under emulation:
correct, slower, and not a faithful proxy for a native x86_64 machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict
from typing import Callable

from . import agent_tools, condafallback, era, levers, pyladder
from .proc import run_group
from .runner import RunResult

DEFAULT_PLATFORM = "linux/amd64"
IMAGE_REPO = "repair-tool-runtime"
IMAGE_VERSION = "1"
REMOTE_REPO = "/work/repo"
REMOTE_ENVS = "/work/envs"
REMOTE_CODE = "/work/rt"
RESULT_PREFIX = "RPC_RESULT:"
# Measured under Rosetta (see venv_manager.ensure_ipykernel): newer kernels never answer.
KERNEL_STACK_PIN = "ipykernel==6.16.2"
# Notebook executor inside the container (see inproc_runner.py for why not a kernel).
NOTEBOOK_EXECUTOR = "inproc"

IMAGE_BUILD_TIMEOUT = 3600
SETUP_TIMEOUT = 7200
_DOCKERFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker")
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def docker_binary() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    fallback = os.path.expanduser("~/.docker/bin/docker")
    return fallback if os.path.isfile(fallback) else None


def docker_available() -> tuple[bool, str]:
    """(usable, why-not): the CLI exists AND the daemon answers."""
    binary = docker_binary()
    if binary is None:
        return False, "docker CLI not found"
    try:
        completed = subprocess.run([binary, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"docker did not answer: {exc}"
    if completed.returncode != 0:
        return False, "the docker daemon is not running (start Docker Desktop)"
    return True, ""


def image_tag(platform: str) -> str:
    return f"{IMAGE_REPO}:{IMAGE_VERSION}-{platform.replace('/', '-')}"


def remove_container(name: str) -> None:
    """Best-effort `docker rm -f` (the harness calls this after a worker is
    killed, so a container never outlives its repo run)."""
    binary = docker_binary()
    if not binary or not name:
        return
    try:
        subprocess.run([binary, "rm", "-f", name], capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        pass


class ContainerError(RuntimeError):
    pass


class DockerRuntime:
    name = "docker"

    def __init__(
        self, platform: str = DEFAULT_PLATFORM, timeout_scale: float = 1.0, container_name: str | None = None,
        image: str | None = None,
    ):
        self.platform = platform
        self.timeout_scale = max(timeout_scale, 1.0)
        self.container_name = container_name or f"repair-tool-{os.getpid()}-{int(time.time())}"
        self.image = image or image_tag(platform)
        self.image_id = ""
        self.python_exe = ""
        self.started = False
        self.host_root = ""
        self._docker = docker_binary()
        self._refused: dict[str, dict] = {}

    # --- lifecycle -------------------------------------------------------

    def ensure_image(self) -> None:
        """Build the runtime image once; later runs reuse the cache."""
        if self._docker is None:
            raise ContainerError("docker CLI not found")
        inspect = subprocess.run([self._docker, "image", "inspect", "--format", "{{.Id}}", self.image],
                                 capture_output=True, text=True, timeout=60)
        if inspect.returncode != 0:
            built = run_group(
                [self._docker, "build", "--platform", self.platform, "-t", self.image, _DOCKERFILE_DIR],
                text=True, encoding="utf-8", errors="replace", timeout=IMAGE_BUILD_TIMEOUT,
            )
            if built.returncode != 0:
                raise ContainerError(f"image build failed: {(built.stdout + built.stderr)[-1500:]}")
            inspect = subprocess.run([self._docker, "image", "inspect", "--format", "{{.Id}}", self.image],
                                     capture_output=True, text=True, timeout=60)
        self.image_id = inspect.stdout.strip()

    def start(self, repo_copy_root: str) -> None:
        """Create the container, put the repair_tool code and a copy of the
        repo (without .git) inside it."""
        self.ensure_image()
        self.host_root = os.path.realpath(repo_copy_root)
        run = subprocess.run(
            [self._docker, "run", "-d", "--init", "--platform", self.platform, "--name", self.container_name,
             self.image, "sleep", "infinity"],
            capture_output=True, text=True, timeout=300,
        )
        if run.returncode != 0:
            raise ContainerError(f"could not start the container: {(run.stdout + run.stderr)[-800:]}")
        self.started = True
        self._exec_ok(["mkdir", "-p", REMOTE_REPO, REMOTE_ENVS, REMOTE_CODE, "/work/state"])
        self._tar_into(_PACKAGE_DIR, f"{REMOTE_CODE}/repair_tool", excludes=["__pycache__", "*.pyc"])
        self._tar_into(repo_copy_root, REMOTE_REPO, excludes=[".git"])

    def close(self) -> None:
        if self.started:
            remove_container(self.container_name)
            self.started = False

    # --- low-level -------------------------------------------------------

    def _exec_cmd(self, args: list[str], env: dict | None = None, interactive: bool = False) -> list[str]:
        cmd = [self._docker, "exec"]
        if interactive:
            cmd.append("-i")
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        return cmd + [self.container_name] + args

    def _exec_ok(self, args: list[str], timeout: int = 120) -> None:
        completed = run_group(self._exec_cmd(args), text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if completed.returncode != 0:
            raise ContainerError(f"{' '.join(args)[:80]} failed: {(completed.stdout + completed.stderr)[-500:]}")

    def _tar_into(self, src_dir: str, dest: str, excludes: list[str]) -> None:
        """Stream `src_dir` into `dest` inside the container (dest created)."""
        self._exec_ok(["mkdir", "-p", dest])
        tar_cmd = ["tar", "--no-xattrs", "-C", src_dir] + [f"--exclude={e}" for e in excludes] + ["-cf", "-", "."]
        producer = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=dict(os.environ, COPYFILE_DISABLE="1"))
        consumer = subprocess.Popen(self._exec_cmd(["tar", "-x", "-C", dest], interactive=True), stdin=producer.stdout,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        producer.stdout.close()
        try:
            _out, err = consumer.communicate(timeout=1800)
            producer.wait(timeout=60)
        except subprocess.TimeoutExpired:
            consumer.kill()
            producer.kill()
            raise ContainerError(f"copying {src_dir} into the container timed out")
        if consumer.returncode != 0:
            raise ContainerError(f"copying into the container failed: {err.decode('utf-8', 'replace')[-500:]}")

    def reap(self) -> None:
        """Best-effort: kill stray kernels / action servers left by an action
        that overran. Safe because actions run one at a time per container."""
        try:
            run_group(
                self._exec_cmd(["sh", "-c", "pkill -9 -f '[i]pykernel_launcher'; pkill -9 -f '[c]ontainer_rpc'; true"]),
                text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    def call(self, action: str, payload: dict, timeout: int) -> dict:
        """One action in the container. Never raises: a failure comes back as
        {'rpc_error': ...} so a stalled/emulation-hung action is a recorded
        timeout, not a hang or a crash."""
        request = json.dumps({"action": action, "payload": payload})
        try:
            completed = run_group(
                self._exec_cmd(["python", "-m", "repair_tool.container_rpc"],
                               env={"PYTHONPATH": REMOTE_CODE, "REPAIR_TOOL_IPYKERNEL_SPEC": KERNEL_STACK_PIN,
                                    "REPAIR_TOOL_NOTEBOOK_EXECUTOR": NOTEBOOK_EXECUTOR,
                                    levers.ENV: os.environ.get(levers.ENV, "")},
                               interactive=True),
                text=True, encoding="utf-8", errors="replace", timeout=timeout, input=request,
            )
        except subprocess.TimeoutExpired:
            self.reap()
            return {"rpc_error": f"TimeoutExpired: '{action}' exceeded {timeout}s in the container and was killed"}
        except OSError as exc:
            return {"rpc_error": f"OSError: could not run docker: {exc}"}
        for line in reversed(completed.stdout.splitlines()):
            if line.startswith(RESULT_PREFIX):
                try:
                    return json.loads(line[len(RESULT_PREFIX):])
                except ValueError:
                    break
        return {"rpc_error": f"no result from the container (exit {completed.returncode}): "
                             f"{(completed.stdout + completed.stderr)[-600:]}"}

    # --- the actions -----------------------------------------------------

    def rel_path(self, path: str) -> str:
        """Repo-relative form of `path`. The agent loop knows the file by its
        absolute HOST path (the host copy of the repo); the container knows it
        by the path under /work/repo -- both reduce to the same relative path."""
        if os.path.isabs(path) and self.host_root:
            real = os.path.realpath(path)
            if real == self.host_root or real.startswith(self.host_root + os.sep):
                return os.path.relpath(real, self.host_root)
        return path

    def remote_path(self, rel_path: str) -> str:
        rel_path = self.rel_path(rel_path)
        return f"{REMOTE_REPO}/{rel_path.replace(os.sep, '/')}"

    def setup(self, era_cutoff_date: str | None, repair_interpreter: bool, run_postbuild: bool) -> dict:
        result = self.call("setup", {
            "repo_root": REMOTE_REPO, "tmp_root": REMOTE_ENVS, "era_cutoff_date": era_cutoff_date,
            "repair_interpreter": repair_interpreter, "run_postbuild": run_postbuild,
        }, timeout=SETUP_TIMEOUT)
        if "rpc_error" in result:
            raise ContainerError(result["rpc_error"])
        self.python_exe = result["python_exe"]
        return result

    def _run_timeouts(self, timeout: int) -> tuple[int, int]:
        inner = max(int(timeout * self.timeout_scale), 1)
        # run_notebook's own whole-notebook watchdog is max(5 x cell, 300); the
        # outer limit only has to exceed it, so the in-container kill fires first.
        outer = max(inner * 5, 300) + 240
        return inner, outer

    def run_project(self, rel_path: str, timeout: int, python_exe: str | None = None) -> RunResult:
        inner, outer = self._run_timeouts(timeout)
        result = self.call("run_project", {
            "path": self.remote_path(rel_path), "timeout": inner,
            "python_exe": python_exe or self.python_exe, "repo_root": REMOTE_REPO,
        }, timeout=outer)
        if "rpc_error" in result:
            return RunResult(ok=False, returncode=-1, stdout="", stderr=f"ContainerError: {result['rpc_error']}")
        return RunResult(
            ok=result["ok"], returncode=result["returncode"], stdout=result["stdout"], stderr=result["stderr"],
            cwd=result.get("cwd", ""), cwd_source=result.get("cwd_source", ""),
            progress=result.get("progress", ""), stuck_cell=result.get("stuck_cell", ""),
        )

    def workdir_describe(self, rel_path: str) -> dict:
        result = self.call("workdir_describe", {"path": self.remote_path(rel_path), "repo_root": REMOTE_REPO}, timeout=120)
        return {} if "rpc_error" in result else result

    def pull(self, rel_path: str, host_path: str) -> None:
        """Copy the (possibly edited) file back so the host copy mirrors it."""
        try:
            subprocess.run([self._docker, "cp", f"{self.container_name}:{self.remote_path(rel_path)}", host_path],
                           capture_output=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def build_dispatch(
        self, rel_path: str, python_exe: str, era_cutoff_date: str | None,
    ) -> dict[str, Callable[..., dict]]:
        """The agent's tools, with every executing tool routed into the container.

        Mirrors agent_tools.build_dispatch: the interpreter lives in `state` because lever
        `interpreter_down` can replace it mid-run; `installed` is what the agent installed
        (replayed onto a lower interpreter); `last_run` is the latest failing run (the
        baseline for a downgrade search)."""
        remote = self.remote_path(rel_path)
        state: dict = {"python_exe": python_exe, "installed": [], "last_run": None, "era_refusals": 0}

        def _hint(result: dict, text: str) -> dict:
            if levers.INTERPRETER_DOWN in levers.active() and not result.get("ok"):
                hint = pyladder.python_too_new_hint(text)
                if hint:
                    result = dict(result, hint=hint)
            return result

        def _run(**_) -> dict:
            result = asdict(self.run_project(rel_path, timeout=60, python_exe=state["python_exe"]))
            state["last_run"] = None if result.get("ok") else result
            return _hint(result, str(result.get("stderr", "")))

        def _install(**kw) -> dict:
            package = kw["package"]
            key = package.strip().lower()
            if key in self._refused:
                return dict(self._refused[key], log="Already refused: " + self._refused[key]["log"])
            gate = era.strict_gate(package, era_cutoff_date, state)  # lever era_strict: checked on the host, no container call
            if gate is not None:
                return gate
            inner, outer = self._run_timeouts(300)
            result = self.call("install_package", {
                "package": package, "python_exe": state["python_exe"], "era_cutoff_date": era_cutoff_date,
                "repo_root": REMOTE_REPO, "target_path": remote,
            }, timeout=outer + (900 if levers.active() else 0))  # a conda retry can add minutes
            if "rpc_error" in result:
                return {"ok": False, "log": f"ContainerError: {result['rpc_error']}", "resolved_package": package,
                        "version_source": "unconstrained"}
            if result.get("refused"):
                self._refused[key] = result
            elif result.get("ok"):
                state["installed"].append(result.get("resolved_package") or package)
            return _hint(result, str(result.get("log", "")))

        def _edit(**kw) -> dict:
            result = self.call("edit_code", {"edits": kw["edits"], "workspace_path": remote}, timeout=120)
            if "rpc_error" in result:
                return {"ok": False, "log": f"ContainerError: {result['rpc_error']}"}
            return result

        def _downgrade(**kw) -> dict:
            package = kw["package"]
            inner, _outer = self._run_timeouts(60)
            budget = 1200
            result = self.call("downgrade_search", {
                "package": package, "python_exe": state["python_exe"], "path": remote, "timeout": inner,
                "repo_root": REMOTE_REPO, "initial_failure": state["last_run"], "time_budget": budget,
                "era_cutoff_date": era_cutoff_date,
            }, timeout=budget + 1200)
            if "rpc_error" in result:
                return {"ok": False, "lever": levers.DOWNGRADE, "message": f"ContainerError: {result['rpc_error']}"}
            if result.get("final_version") and result["final_version"] != result.get("original_version"):
                base = condafallback.base_name(package)
                state["installed"] = [s for s in state["installed"] if condafallback.base_name(s) != base]
                state["installed"].append(f"{package}=={result['final_version']}")
            if result.get("last_run") is not None:
                state["last_run"] = None if result["last_run"].get("ok") else result["last_run"]
            return result

        def _lower(**kw) -> dict:
            result = self.call("lower_python", {
                "python_exe": state["python_exe"], "repo_root": REMOTE_REPO,
                "installed_specs": list(state["installed"]), "reason": kw.get("reason", ""),
                "era_cutoff_date": era_cutoff_date,
            }, timeout=3600)
            if "rpc_error" in result:
                return {"ok": False, "lever": levers.INTERPRETER_DOWN, "message": f"ContainerError: {result['rpc_error']}"}
            if result.get("ok") and result.get("python_exe"):
                state["python_exe"] = result["python_exe"]
                state["last_run"] = None
            return result

        dispatch = {
            "run_target": _run,
            "diagnose_error": lambda **kw: agent_tools.diagnose_error(kw["run_result"]),
            "lookup_package": lambda **kw: agent_tools.lookup_package(kw["import_name"]),
            "install_package": _install,
            "edit_code": _edit,
            "verify": _run,
            "report_unfixable": lambda **kw: agent_tools.report_unfixable(kw["reason"]),
        }
        active = levers.active()
        if levers.DOWNGRADE in active:
            dispatch["downgrade_search"] = _downgrade
        if levers.INTERPRETER_DOWN in active:
            dispatch["lower_python"] = _lower
        return dispatch

    # --- provenance ------------------------------------------------------

    def describe(self) -> dict:
        return {
            "runtime": "docker", "container_platform": self.platform,
            "container_image": self.image, "container_image_id": self.image_id,
            "container_timeout_scale": self.timeout_scale, "container_kernel_pin": KERNEL_STACK_PIN,
            "container_notebook_executor": NOTEBOOK_EXECUTOR,
        }


HOST_DESCRIPTION = {
    "runtime": "host", "container_platform": "", "container_image": "", "container_image_id": "",
    "container_timeout_scale": 1.0, "container_kernel_pin": "", "container_notebook_executor": "kernel",
}
