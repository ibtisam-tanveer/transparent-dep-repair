"""TASK_containerize.md: the container runtime. Offline tests mock Docker (the
lifecycle, the host/container split, the timeout handling); the real-container
tests at the bottom run only when a Docker daemon is available."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import runtime as rt_module
from repair_tool.runner import RunResult
from repair_tool.runtime import REMOTE_REPO, RESULT_PREFIX, DockerRuntime, image_tag


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _rpc_ok(payload: dict):
    return _completed(stdout="some pip noise\n" + RESULT_PREFIX + json.dumps(payload) + "\n")


class TestDockerRuntimeOffline(unittest.TestCase):
    def setUp(self):
        self.rt = DockerRuntime(platform="linux/amd64", timeout_scale=3.0, container_name="c1")
        self.rt._docker = "/usr/bin/docker"

    def test_the_image_tag_encodes_the_platform(self):
        self.assertEqual(image_tag("linux/amd64"), "repair-tool-runtime:1-linux-amd64")
        self.assertNotEqual(image_tag("linux/amd64"), image_tag("linux/arm64"))

    def test_a_call_sends_json_on_stdin_with_pythonpath_and_parses_the_result_line(self):
        with patch("repair_tool.runtime.run_group", return_value=_rpc_ok({"ok": True})) as run:
            result = self.rt.call("run_project", {"path": "/work/repo/a.py"}, timeout=50)
        self.assertEqual(result, {"ok": True})
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["/usr/bin/docker", "exec", "-i"])
        self.assertIn("PYTHONPATH=/work/rt", cmd)
        self.assertIn("REPAIR_TOOL_IPYKERNEL_SPEC=ipykernel==6.16.2", cmd)
        self.assertEqual(cmd[-3:], ["python", "-m", "repair_tool.container_rpc"])
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(sent, {"action": "run_project", "payload": {"path": "/work/repo/a.py"}})
        self.assertEqual(run.call_args.kwargs["timeout"], 50)

    def test_a_missing_result_line_is_an_rpc_error_not_a_crash(self):
        with patch("repair_tool.runtime.run_group", return_value=_completed(stdout="garbage", stderr="boom", returncode=1)):
            result = self.rt.call("setup", {}, timeout=5)
        self.assertIn("no result from the container", result["rpc_error"])

    def test_a_timeout_is_recorded_and_strays_are_reaped(self):
        calls = []

        def fake_run_group(cmd, **kwargs):
            calls.append(cmd)
            if "pkill" in " ".join(cmd):
                return _completed()
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

        with patch("repair_tool.runtime.run_group", side_effect=fake_run_group):
            result = self.rt.call("run_project", {}, timeout=7)
        self.assertIn("TimeoutExpired", result["rpc_error"])
        self.assertIn("exceeded 7s", result["rpc_error"])
        self.assertTrue(any("pkill" in " ".join(c) for c in calls), "stray kernels must be reaped after a timeout")

    def test_run_project_scales_the_cell_timeout_for_emulation_and_maps_the_path(self):
        with patch.object(self.rt, "call", return_value={
            "ok": True, "returncode": 0, "stdout": "x", "stderr": "", "cwd": "/work/repo/notebooks", "cwd_source": "inferred_from_sys_path",
        }) as call:
            result = self.rt.run_project(os.path.join("notebooks", "old", "a.ipynb"), timeout=60, python_exe="/work/envs/bin/python")
        payload = call.call_args.args[1]
        self.assertEqual(payload["path"], f"{REMOTE_REPO}/notebooks/old/a.ipynb")
        self.assertEqual(payload["timeout"], 180)  # 60 s x 3.0 emulation scale
        self.assertEqual(payload["python_exe"], "/work/envs/bin/python")
        self.assertEqual(payload["repo_root"], REMOTE_REPO)
        self.assertGreater(call.call_args.kwargs["timeout"], 180)  # the outer limit exceeds the in-container watchdog
        self.assertTrue(result.ok)
        self.assertEqual(result.cwd_source, "inferred_from_sys_path")

    def test_the_agents_absolute_host_path_maps_to_the_same_remote_path_as_the_relative_one(self):
        """Regression (found by the first real docker smoke run): the agent
        loop passes the HOST path of the file; it must not be appended to
        /work/repo verbatim."""
        with tempfile.TemporaryDirectory() as host:
            os.makedirs(os.path.join(host, "notebooks"))
            self.rt.host_root = os.path.realpath(host)
            absolute = os.path.join(host, "notebooks", "a b.ipynb")
            self.assertEqual(self.rt.remote_path(absolute), f"{REMOTE_REPO}/notebooks/a b.ipynb")
            self.assertEqual(self.rt.remote_path(os.path.join("notebooks", "a b.ipynb")), f"{REMOTE_REPO}/notebooks/a b.ipynb")
            dispatch = self.rt.build_dispatch(absolute, "/p", None)
            with patch.object(self.rt, "call", return_value={"ok": True, "returncode": 0, "stdout": "", "stderr": ""}) as call:
                dispatch["run_target"]()
                dispatch["edit_code"](edits=[])
            self.assertEqual(call.call_args_list[0].args[1]["path"], f"{REMOTE_REPO}/notebooks/a b.ipynb")
            self.assertEqual(call.call_args_list[1].args[1]["workspace_path"], f"{REMOTE_REPO}/notebooks/a b.ipynb")

    def test_a_container_failure_becomes_a_failed_run_result(self):
        with patch.object(self.rt, "call", return_value={"rpc_error": "TimeoutExpired: x"}):
            result = self.rt.run_project("a.ipynb", timeout=60, python_exe="/p")
        self.assertFalse(result.ok)
        self.assertIn("ContainerError", result.stderr)

    def test_the_executing_tools_run_in_the_container_and_the_thinking_tools_on_the_host(self):
        dispatch = self.rt.build_dispatch("notebooks/a.ipynb", "/work/envs/bin/python", "2019-05-01")
        with patch.object(self.rt, "call", return_value={"ok": True, "log": "done"}) as call:
            dispatch["install_package"](package="plotly")
            dispatch["edit_code"](edits=[{"find": "a", "replace": "b"}])
        actions = [c.args[0] for c in call.call_args_list]
        self.assertEqual(actions, ["install_package", "edit_code"])
        self.assertEqual(call.call_args_list[0].args[1]["era_cutoff_date"], "2019-05-01")
        self.assertEqual(call.call_args_list[1].args[1]["workspace_path"], f"{REMOTE_REPO}/notebooks/a.ipynb")

        # lookup_package / diagnose_error never touch the container
        with patch.object(self.rt, "call") as call, patch("repair_tool.agent_tools.pypi.resolve_package_name", return_value="x"), \
             patch("repair_tool.agent_tools.pypi.latest_version", return_value="1.0"):
            self.assertTrue(dispatch["lookup_package"](import_name="x")["exists"])
            dispatch["diagnose_error"](run_result={"ok": False, "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError: No module named 'x'"})
        call.assert_not_called()

    def test_a_refused_install_is_answered_from_the_cache_the_second_time(self):
        dispatch = self.rt.build_dispatch("a.ipynb", "/p", None)
        refused = {"ok": False, "refused": True, "log": "Refused: ..."}
        with patch.object(self.rt, "call", return_value=refused) as call:
            first = dispatch["install_package"](package="combat")
            second = dispatch["install_package"](package="Combat")
        self.assertEqual(call.call_count, 1)
        self.assertTrue(first["refused"] and second["refused"])
        self.assertIn("Already refused", second["log"])

    def test_start_creates_a_platform_pinned_container_and_copies_without_git(self):
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return _completed(stdout="id\n")

        with patch.object(self.rt, "ensure_image"), patch("repair_tool.runtime.subprocess.run", side_effect=fake_run), \
             patch.object(self.rt, "_exec_ok") as exec_ok, patch.object(self.rt, "_tar_into") as tar_into:
            self.rt.start("/host/repo_copy")
        run_cmd = commands[0]
        self.assertEqual(run_cmd[:3], ["/usr/bin/docker", "run", "-d"])
        self.assertIn("--init", run_cmd)
        self.assertEqual(run_cmd[run_cmd.index("--platform") + 1], "linux/amd64")
        self.assertEqual(run_cmd[run_cmd.index("--name") + 1], "c1")
        self.assertEqual(run_cmd[-2:], ["sleep", "infinity"])
        repo_copy = [c for c in tar_into.call_args_list if c.args[0] == "/host/repo_copy"]
        self.assertEqual(len(repo_copy), 1)
        self.assertIn(".git", repo_copy[0].kwargs["excludes"])
        self.assertEqual(repo_copy[0].args[1], REMOTE_REPO)
        self.assertTrue(self.rt.started)

    def test_close_removes_the_container_once(self):
        self.rt.started = True
        with patch("repair_tool.runtime.remove_container") as remove:
            self.rt.close()
            self.rt.close()
        remove.assert_called_once_with("c1")

    def test_describe_records_the_container_facts(self):
        self.rt.image_id = "sha256:abc"
        self.assertEqual(
            self.rt.describe(),
            {"runtime": "docker", "container_platform": "linux/amd64", "container_image": "repair-tool-runtime:1-linux-amd64",
             "container_image_id": "sha256:abc", "container_timeout_scale": 3.0, "container_kernel_pin": "ipykernel==6.16.2",
             "container_notebook_executor": "inproc"},
        )

    def test_the_platform_is_a_configurable_default_not_a_constant(self):
        other = DockerRuntime(platform="linux/arm64")
        self.assertEqual(other.platform, "linux/arm64")
        self.assertEqual(other.image, image_tag("linux/arm64"))
        self.assertEqual(rt_module.DEFAULT_PLATFORM, "linux/amd64")


class TestContainerRpcActions(unittest.TestCase):
    """The action server runs the SAME functions as the host path."""

    def test_setup_runs_the_same_sequence_as_the_host_runner(self):
        from repair_tool import container_rpc

        order = []

        with patch("repair_tool.venv_manager.get_fresh_venv_python", side_effect=lambda t: order.append("venv") or "/base/python"), \
             patch("repair_tool.interpreter.build_repair_environment") as env, \
             patch("repair_tool.repo.find_dependency_files", return_value=["requirements.txt"]), \
             patch("repair_tool.repo._install_dependencies", side_effect=lambda *a, **k: order.append("deps") or (True, "log", "requirements.txt")), \
             patch("repair_tool.repo.run_postbuild", side_effect=lambda *a, **k: order.append("postbuild") or {"found": False, "ran": False, "ok": False, "path": "", "returncode": None, "log_tail": ""}):
            env.side_effect = lambda *a, **k: order.append("interp") or MagicMock(python_exe="/interp/python", meta={"python_version_actual": "3.7"})
            result = container_rpc._setup({"repo_root": "/work/repo", "tmp_root": "/work/envs", "era_cutoff_date": "2019-01-01",
                                           "repair_interpreter": True, "run_postbuild": True})
        self.assertEqual(order, ["venv", "interp", "deps", "postbuild"])
        self.assertEqual(result["python_exe"], "/interp/python")  # the repaired interpreter, not the baseline
        self.assertEqual(result["interpreter_meta"]["python_version_actual"], "3.7")
        self.assertEqual(result["dependency_file_used"], "requirements.txt")

    def test_run_project_loads_and_saves_the_workdir_cache(self):
        from repair_tool import container_rpc

        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "workdir.json")
            with patch.object(container_rpc, "WORKDIR_STATE", state), \
                 patch("repair_tool.runner.run_project", return_value=RunResult(ok=True, returncode=0, stdout="o", stderr="")):
                result = container_rpc._run_project({"path": "/work/repo/a.py", "timeout": 5, "python_exe": "/p", "repo_root": "/work/repo"})
            self.assertTrue(result["ok"])
            self.assertTrue(os.path.isfile(state))

    def test_an_action_error_is_returned_not_raised(self):
        from repair_tool import container_rpc

        out = []
        with patch("sys.stdin", MagicMock(read=lambda *a: json.dumps({"action": "run_project", "payload": {}}))), \
             patch("sys.stdout", MagicMock(write=out.append, flush=lambda: None)):
            container_rpc.main()
        text = "".join(out)
        self.assertIn(RESULT_PREFIX, text)
        self.assertIn("rpc_error", text)


class TestRepoRunnerDockerBranch(unittest.TestCase):
    """The host loop around a (fake) container: only labelled notebooks, files
    synced back, container always closed, container failure reported."""

    def _fake_rt(self, run_ok=True, start_error=None):
        fake = MagicMock()
        fake.describe.return_value = {"runtime": "docker", "container_platform": "linux/amd64"}
        fake.setup.return_value = {
            "python_exe": "/work/envs/bin/python", "interpreter_meta": {"python_version_actual": "3.8"},
            "env_setup_ok": True, "env_setup_log": "", "dependency_file_used": "requirements.txt",
            "dependency_files_found": [], "setup_seconds": 1.0,
            "postbuild": {"found": False, "ran": False, "ok": False, "path": "", "returncode": None, "log_tail": ""},
        }
        fake.run_project.return_value = RunResult(ok=run_ok, returncode=0 if run_ok else 1, stdout="", stderr="")
        fake.workdir_describe.return_value = {}
        if start_error:
            from repair_tool.runtime import ContainerError
            fake.start.side_effect = ContainerError(start_error)
        return fake

    def _run(self, fake, only_paths=None, files=("a.ipynb", "b.ipynb")):
        from evaluation._repo_runner import run_repo_incrementally

        tmp_root = tempfile.mkdtemp()
        try:
            with patch("evaluation._repo_runner.DockerRuntime", return_value=fake), \
                 patch("evaluation._repo_runner.get_fresh_repo_copy", return_value="/host/copy"), \
                 patch("evaluation._repo_runner.repo_module.repo_last_commit_date", return_value="2019-05-01"), \
                 patch("evaluation._repo_runner.repo_module.discover_runnable_files", return_value=list(files)), \
                 patch("evaluation._repo_runner.agent_repair") as agent:
                from repair_tool.agent import AgentResult
                agent.side_effect = lambda rel, **kw: AgentResult(target=rel, fixed=False)
                out = list(run_repo_incrementally("/repo", tmp_root, 5, 1000, runtime="docker", only_paths=only_paths))
                return out, agent
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    def test_passing_files_are_counted_and_the_container_is_closed(self):
        fake = self._fake_rt(run_ok=True)
        out, agent = self._run(fake)
        self.assertEqual([r.target for r, _ in out], ["a.ipynb", "b.ipynb"])
        self.assertTrue(all(r.fixed for r, _ in out))
        agent.assert_not_called()
        fake.close.assert_called_once()

    def test_a_failing_file_goes_to_the_agent_with_the_container_as_its_tool_runtime_and_is_synced_back(self):
        fake = self._fake_rt(run_ok=False)
        out, agent = self._run(fake, only_paths={"b.ipynb"})
        self.assertEqual([r.target for r, _ in out], ["b.ipynb"])
        self.assertIs(agent.call_args.kwargs["tool_runtime"], fake)
        self.assertEqual(agent.call_args.kwargs["python_exe"], "/work/envs/bin/python")
        fake.pull.assert_called_once()
        self.assertEqual(out[0][1]["runtime"], "docker")  # provenance on the row meta
        self.assertEqual(out[0][1]["python_version_actual"], "3.8")  # in-container interpreter resolution
        fake.close.assert_called_once()

    def test_an_unavailable_container_is_reported_on_every_file_and_still_closed(self):
        fake = self._fake_rt(start_error="image build failed")
        out, agent = self._run(fake)
        self.assertEqual(len(out), 2)
        for result, meta in out:
            self.assertFalse(result.fixed)
            self.assertTrue(result.error.startswith("container unavailable"))
            self.assertIn("image build failed", meta["container_error"])
        agent.assert_not_called()
        fake.close.assert_called_once()


class TestKernelPin(unittest.TestCase):
    """Under Rosetta, ipykernel >= 6.29 never answers; the container pins the kernel."""

    def _completed(self, rc=0, out=""):
        return subprocess.CompletedProcess([], rc, out, "")

    def test_without_the_pin_an_existing_ipykernel_is_left_alone(self):
        from repair_tool.venv_manager import ensure_ipykernel

        with patch.dict(os.environ, {}, clear=True), patch("repair_tool.venv_manager.subprocess.run", return_value=self._completed()) as run:
            ok, log = ensure_ipykernel("/p/python")
        self.assertTrue(ok)
        run.assert_called_once()  # only the import check

    def test_a_newer_ipykernel_than_the_pin_is_replaced(self):
        from repair_tool.venv_manager import ensure_ipykernel

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[-1].startswith("import ipykernel;print"):
                return self._completed(out="7.4.0\n")
            return self._completed()

        with patch.dict(os.environ, {"REPAIR_TOOL_IPYKERNEL_SPEC": "ipykernel==6.16.2"}, clear=True), \
             patch("repair_tool.venv_manager.subprocess.run", side_effect=fake_run):
            ok, _ = ensure_ipykernel("/p/python")
        self.assertTrue(ok)
        self.assertTrue(any(c[1:4] == ["-m", "pip", "install"] and c[-1] == "ipykernel==6.16.2" for c in calls))

    def test_an_older_ipykernel_than_the_pin_is_kept(self):
        from repair_tool.venv_manager import ensure_ipykernel

        def fake_run(cmd, **kw):
            return self._completed(out="5.1.3\n") if cmd[-1].startswith("import ipykernel;print") else self._completed()

        with patch.dict(os.environ, {"REPAIR_TOOL_IPYKERNEL_SPEC": "ipykernel==6.16.2"}, clear=True), \
             patch("repair_tool.venv_manager.subprocess.run", side_effect=fake_run) as run:
            ok, log = ensure_ipykernel("/p/python")
        self.assertEqual(log, "ipykernel already present")
        self.assertFalse(any(c.args[0][1:4] == ["-m", "pip", "install"] for c in run.call_args_list))


class TestPreferCondaForExactPython(unittest.TestCase):
    """Inside the container micromamba supplies old CPython before uv's nearest-newer fallback."""

    def test_the_flag_defaults_off_and_reads_the_environment(self):
        from repair_tool.interpreter import prefer_conda_for_exact_python

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(prefer_conda_for_exact_python())
        with patch.dict(os.environ, {"REPAIR_TOOL_PREFER_CONDA": "1"}, clear=True):
            self.assertTrue(prefer_conda_for_exact_python())

    def test_exact_python_from_conda_beats_uv_when_the_flag_is_on(self):
        from repair_tool import interpreter
        from repair_tool.interpreter import PythonChoice, provision_interpreter

        with tempfile.TemporaryDirectory() as tmp:
            env_dir = os.path.join(tmp, "interp")
            choice = PythonChoice(version="3.7", source="requirements.txt", grounding="metadata_grounded")

            def fake_conda(tool, spec, prefix, timeout):
                os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
                open(os.path.join(prefix, "bin", "python"), "w").close()
                return True, "ok"

            with patch.dict(os.environ, {"REPAIR_TOOL_PREFER_CONDA": "1"}, clear=True), \
                 patch.object(interpreter, "find_conda_tool", return_value="/usr/bin/micromamba"), \
                 patch.object(interpreter, "_try_conda", side_effect=fake_conda) as conda, \
                 patch.object(interpreter, "_interpreter_minor", return_value="3.7"), \
                 patch.object(interpreter, "_try_uv") as uv:
                prov = provision_interpreter(choice, env_dir, tmp)
            self.assertTrue(prov.ok)
            self.assertEqual((prov.mechanism, prov.actual), ("conda", "3.7"))
            conda.assert_called_once()
            uv.assert_not_called()

    def test_uv_is_still_the_fallback_when_conda_cannot_supply_it(self):
        from repair_tool import interpreter
        from repair_tool.interpreter import PythonChoice, provision_interpreter

        with tempfile.TemporaryDirectory() as tmp:
            env_dir = os.path.join(tmp, "interp")
            choice = PythonChoice(version="3.8", source="requirements.txt", grounding="metadata_grounded")

            def fake_uv(uv, minor, prefix, timeout):
                os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
                open(os.path.join(prefix, "bin", "python"), "w").close()
                return True, "ok"

            with patch.dict(os.environ, {"REPAIR_TOOL_PREFER_CONDA": "1"}, clear=True), \
                 patch.object(interpreter, "find_conda_tool", return_value="/usr/bin/micromamba"), \
                 patch.object(interpreter, "_try_conda", return_value=(False, "no such version")), \
                 patch.object(interpreter, "find_uv", return_value="/usr/bin/uv"), \
                 patch.object(interpreter, "_try_uv", side_effect=fake_uv), \
                 patch.object(interpreter, "_interpreter_minor", return_value="3.8"):
                prov = provision_interpreter(choice, env_dir, tmp)
            self.assertTrue(prov.ok)
            self.assertEqual(prov.mechanism, "uv")


# --- real container (needs a Docker daemon; skipped otherwise) -----------

def _real_docker() -> bool:
    if os.environ.get("SKIP_DOCKER_TESTS"):
        return False
    return rt_module.docker_available()[0]


@unittest.skipUnless(_real_docker(), "no Docker daemon (or SKIP_DOCKER_TESTS set)")
class TestRealContainer(unittest.TestCase):
    """One real linux/amd64 container shared by the tests (starting it is slow)."""

    @classmethod
    def setUpClass(cls):
        import nbformat
        from nbformat.v4 import new_code_cell, new_notebook

        cls.repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.repo, "src"))
        with open(os.path.join(cls.repo, "src", "helper.py"), "w") as f:
            f.write("VALUE = 7\n")
        with open(os.path.join(cls.repo, "ok.py"), "w") as f:
            f.write("import platform\nprint('ARCH', platform.machine())\n")
        nbformat.write(new_notebook(cells=[
            new_code_cell("!echo from_shell_escape"),
            new_code_cell("%%bash\necho from_bash_magic"),
            new_code_cell("import subprocess; print(subprocess.run(['echo','from_subprocess'], capture_output=True, text=True).stdout)"),
            new_code_cell("import seaborn"),
        ]), os.path.join(cls.repo, "spawn.ipynb"))
        os.makedirs(os.path.join(cls.repo, "notebooks", "old"))
        nbformat.write(new_notebook(cells=[new_code_cell("import time; time.sleep(300)")]),
                       os.path.join(cls.repo, "hang.ipynb"))
        nbformat.write(new_notebook(cells=[new_code_cell(
            'import sys\nsys.path.append("../src/")\nimport helper\nprint("VALUE", helper.VALUE)')]),
            os.path.join(cls.repo, "notebooks", "old", "wd.ipynb"))
        cls.rt = DockerRuntime(timeout_scale=1.0)
        cls.rt.start(cls.repo)
        cls.setup = cls.rt.setup(None, False, False)

    @classmethod
    def tearDownClass(cls):
        cls.rt.close()
        shutil.rmtree(cls.repo, ignore_errors=True)

    def test_the_container_really_is_linux_amd64(self):
        result = self.rt.run_project("ok.py", timeout=120)
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("ARCH x86_64", result.stdout)

    def test_the_environment_is_built_inside_the_container(self):
        self.assertTrue(self.setup["python_exe"].startswith("/work/envs"))
        self.assertTrue(self.setup["env_setup_ok"])

    def test_a_hanging_cell_is_killed_inside_the_container_and_recorded_as_a_timeout(self):
        import time

        started = time.time()
        result = self.rt.run_project("hang.ipynb", timeout=10)
        self.assertFalse(result.ok)
        self.assertIn("Timeout", result.stderr)
        self.assertLess(time.time() - started, 240)
        # no kernel left running in the container
        leftover = subprocess.run(
            [self.rt._docker, "exec", self.rt.container_name, "sh", "-c", "pgrep -f '[i]pykernel_launcher' | wc -l"],
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(leftover, "0")

    def test_notebooks_that_spawn_processes_run_in_the_container(self):
        """The Rosetta defect this executor exists for: in a Jupyter kernel, `!cmd`,
        `%%bash`, subprocess and matplotlib's first import all hang there."""
        import time

        self.rt.call("run_project", {"path": f"{REMOTE_REPO}/ok.py", "timeout": 60, "python_exe": self.rt.python_exe,
                                     "repo_root": REMOTE_REPO}, timeout=120)  # warm the env
        started = time.time()
        # seaborn is not installed in this env -> the last cell fails FAST with a real error,
        # which proves the first three (spawning) cells passed and nothing hung.
        result = self.rt.run_project("spawn.ipynb", timeout=60)
        self.assertLess(time.time() - started, 120)
        for expected in ("from_shell_escape", "from_bash_magic", "from_subprocess"):
            self.assertIn(expected, result.stdout)
        self.assertIn("ModuleNotFoundError", result.stderr)
        self.assertEqual(result.progress, "last started code cell 4 of 4")

    def test_working_directory_resolution_works_through_the_container(self):
        result = self.rt.run_project(os.path.join("notebooks", "old", "wd.ipynb"), timeout=60)
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("VALUE 7", result.stdout)


if __name__ == "__main__":
    unittest.main()
