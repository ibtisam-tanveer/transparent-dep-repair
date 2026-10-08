import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.agent import _REQUEST_TIMEOUT_SECONDS, AgentResult, agent_repair
from repair_tool.runner import RunResult

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "broken_examples")


def _tool_call(call_id: str, name: str, arguments: dict):
    call = MagicMock()
    call.id = call_id
    call.function.name = name
    call.function.arguments = json.dumps(arguments)
    return call


def _response(tool_calls=None, content=None):
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls or []
    response = MagicMock()
    response.choices = [MagicMock(message=message)]
    return response


@patch("repair_tool.agent.get_workspace_copy", return_value=os.path.join(EXAMPLES, "01_missing_package.py"))
@patch("repair_tool.agent.get_venv_python", return_value="/fake/python")
class TestAgentRepairOffline(unittest.TestCase):
    """The LLM is fully mocked (scripted tool-call sequences) -- exercises
    only the agent's own loop: dispatching tools, building the trace, and
    tagging the two-axis trust model (grounding/verification/confidence).
    Real network calls (pypi, pip) are mocked at the same seam
    agent_tools.py calls them.
    """

    @patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "Successfully installed seaborn"))
    @patch("repair_tool.agent_tools.pypi.latest_version", return_value="0.13.0")
    @patch("repair_tool.agent_tools.pypi.resolve_package_name", return_value="seaborn")
    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_fixes_a_missing_package_by_choosing_tools_in_order(
        self, mock_openai_cls, mock_run, mock_resolve, mock_latest, mock_apply, _mock_venv, _mock_workspace
    ):
        mock_run.side_effect = [
            RunResult(ok=False, returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'seaborn'"),
            RunResult(ok=True, returncode=0, stdout="0.13.0\n", stderr=""),
        ]
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call("c1", "run_target", {})], content="let's see what's wrong"),
            _response([_tool_call("c2", "diagnose_error", {"run_result": {
                "ok": False, "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError: No module named 'seaborn'"
            }})]),
            _response([_tool_call("c3", "lookup_package", {"import_name": "seaborn"})]),
            _response([_tool_call("c4", "install_package", {"package": "seaborn"})]),
            _response([_tool_call("c5", "verify", {})]),
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))

        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.fixed)
        self.assertEqual(result.final_strategy, "install")
        self.assertEqual(result.error, "")

        tools_called = [step.tool_called for step in result.trace]
        self.assertEqual(tools_called, ["run_target", "diagnose_error", "lookup_package", "install_package", "verify"])

        for i in (0, 1, 4):  # run_target, diagnose_error, verify -- observation tools
            self.assertEqual(result.trace[i].grounding, "deterministic")
            self.assertEqual(result.trace[i].verification, "n/a")
            self.assertEqual(result.trace[i].confidence, "")

        lookup_step = result.trace[2]
        self.assertEqual(lookup_step.grounding, "deterministic")

        install_step = result.trace[3]
        self.assertEqual(install_step.grounding, "metadata_grounded")
        self.assertEqual(install_step.verification, "verified")
        self.assertEqual(install_step.confidence, "high")
        self.assertEqual(result.trace[0].thought, "let's see what's wrong")

    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_fixes_a_removed_api_via_code_edit_without_a_lookup(
        self, mock_openai_cls, mock_run, _mock_venv, _mock_workspace
    ):
        mock_run.side_effect = [
            RunResult(ok=False, returncode=1, stdout="", stderr="AttributeError: module 'numpy' has no attribute 'float'"),
            RunResult(ok=True, returncode=0, stdout="3.14\n", stderr=""),
        ]
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call("c1", "run_target", {})]),
            _response([_tool_call("c2", "diagnose_error", {"run_result": {
                "ok": False, "returncode": 1, "stdout": "",
                "stderr": "AttributeError: module 'numpy' has no attribute 'float'",
            }})]),
            _response([_tool_call("c3", "edit_code", {"edits": [{"find": "np.float", "replace": "float"}]})]),
            _response([_tool_call("c4", "verify", {})]),
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}), \
                patch("repair_tool.agent_tools.apply_module.apply_code_edit", return_value=(True, "applied")):
            result = agent_repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        self.assertTrue(result.fixed)
        self.assertEqual(result.final_strategy, "code_edit")
        edit_step = result.trace[2]
        self.assertEqual(edit_step.tool_called, "edit_code")
        self.assertEqual(edit_step.grounding, "llm_proposed")
        self.assertEqual(edit_step.verification, "verified")
        self.assertEqual(edit_step.confidence, "medium-high")

    @patch("repair_tool.agent_tools.apply_module.apply_code_edit", return_value=(True, "applied"))
    @patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "Successfully installed numpy"))
    @patch("repair_tool.agent_tools.pypi.latest_version", return_value="2.0.0")
    @patch("repair_tool.agent_tools.pypi.resolve_package_name", return_value="numpy")
    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_numpy_style_case_a_grounded_install_that_doesnt_finish_the_job(
        self, mock_openai_cls, mock_run, mock_resolve, mock_latest, mock_apply, mock_edit, _mock_venv, _mock_workspace
    ):
        """Reproduces the exact motivating scenario from
        TASK_provenance_and_report.md: a well-founded install (confirmed by
        lookup_package) that a following verify doesn't confirm -- because
        it reveals a second, different failure -- must read
        metadata_grounded + unverified, never llm_unverified. The edit that
        actually finishes the job is llm_proposed + verified.
        """
        mock_run.side_effect = [
            RunResult(ok=False, returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'numpy'"),
            RunResult(
                ok=False, returncode=1, stdout="",
                stderr="AttributeError: module 'numpy' has no attribute 'float'",
            ),
            RunResult(ok=True, returncode=0, stdout="3.14\n", stderr=""),
        ]
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call("c1", "run_target", {})]),
            _response([_tool_call("c2", "diagnose_error", {"run_result": {
                "ok": False, "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError: No module named 'numpy'"
            }})]),
            _response([_tool_call("c3", "lookup_package", {"import_name": "numpy"})]),
            _response([_tool_call("c4", "install_package", {"package": "numpy"})]),
            _response([_tool_call("c5", "verify", {})]),
            _response([_tool_call("c6", "diagnose_error", {"run_result": {
                "ok": False, "returncode": 1, "stdout": "",
                "stderr": "AttributeError: module 'numpy' has no attribute 'float'",
            }})]),
            _response([_tool_call("c7", "edit_code", {"edits": [{"find": "np.float", "replace": "float"}]})]),
            _response([_tool_call("c8", "verify", {})]),
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = agent_repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        self.assertTrue(result.fixed)

        install_step = next(s for s in result.trace if s.tool_called == "install_package")
        self.assertEqual(install_step.grounding, "metadata_grounded")
        self.assertEqual(install_step.verification, "unverified")
        self.assertEqual(install_step.confidence, "medium")

        edit_step = next(s for s in result.trace if s.tool_called == "edit_code")
        self.assertEqual(edit_step.grounding, "llm_proposed")
        self.assertEqual(edit_step.verification, "verified")
        self.assertEqual(edit_step.confidence, "medium-high")

    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_an_already_passing_target_is_confirmed_by_run_target_alone(
        self, mock_openai_cls, mock_run, _mock_venv, _mock_workspace
    ):
        """Regression test for a real bug: run_target and verify both just
        re-run the project (see agent_tools.verify), so a target that
        already passes -- nothing to fix, or a previously-fixed cached
        workspace from an earlier run reusing the same venv -- must be
        confirmed by a plain run_target call, not only by an explicit
        'verify' call. Found by running agent_repair twice in a row against
        the same real file: the second run's workspace was already fixed,
        the model sensibly called only run_target, saw ok=true, and stopped
        -- which used to report fixed=False even though nothing was broken.
        """
        mock_run.return_value = RunResult(ok=True, returncode=0, stdout="3.14\n", stderr="")
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call("c1", "run_target", {})]),
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = agent_repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        self.assertTrue(result.fixed)
        self.assertEqual(len(result.trace), 1)
        self.assertEqual(result.trace[0].tool_called, "run_target")
        self.assertEqual(result.final_strategy, "")

    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_respects_max_steps_when_verify_never_succeeds(self, mock_openai_cls, mock_run, _mock_venv, _mock_workspace):
        mock_run.return_value = RunResult(ok=False, returncode=1, stdout="", stderr="still broken")
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call(f"c{i}", "run_target", {})]) for i in range(10)
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"), max_steps=3)

        self.assertFalse(result.fixed)
        self.assertEqual(len(result.trace), 3)

    @patch("openai.OpenAI")
    def test_model_stopping_without_a_tool_call_ends_the_run_not_fixed(self, mock_openai_cls, _mock_venv, _mock_workspace):
        mock_openai_cls.return_value.chat.completions.create.return_value = _response(
            [], content="I don't know how to fix this."
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertFalse(result.fixed)
        self.assertEqual(result.trace, [])
        self.assertEqual(result.error, "")

    def test_missing_api_key_gives_a_clear_error_not_a_crash(self, _mock_venv, _mock_workspace):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertFalse(result.fixed)
        self.assertIn("OPENAI_API_KEY", result.error)
        self.assertEqual(result.trace, [])

    @patch("openai.OpenAI")
    def test_llm_call_failure_degrades_to_error_not_a_crash(self, mock_openai_cls, _mock_venv, _mock_workspace):
        mock_openai_cls.return_value.chat.completions.create.side_effect = RuntimeError("connection reset")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertFalse(result.fixed)
        self.assertIn("LLM call failed", result.error)


class TestAgentRepairEnvironmentModes(unittest.TestCase):
    """TASK_repo_scale_agent.md Part A/C: agent_repair must be reusable
    with an explicitly-provided python_exe/workspace_path (how agent_repo.py
    runs it inside a repo's one shared environment) and with fresh=True (a
    throwaway venv/workspace, for evaluation) -- neither path should touch
    venv_manager's persistent, hash-cached store.
    """

    @patch("repair_tool.agent.get_workspace_copy")
    @patch("repair_tool.agent.get_venv_python")
    @patch("openai.OpenAI")
    def test_explicit_python_exe_and_workspace_path_skip_venv_manager_entirely(
        self, mock_openai_cls, mock_get_venv, mock_get_workspace
    ):
        mock_openai_cls.return_value.chat.completions.create.return_value = _response(
            [_tool_call("c1", "run_target", {})]
        )
        with patch("repair_tool.agent_tools.run_project") as mock_run:
            mock_run.return_value = RunResult(ok=True, returncode=0, stdout="ok\n", stderr="")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
                result = agent_repair(
                    "some_file.py",
                    python_exe="/shared/repo/venv/python",
                    workspace_path="/shared/repo/venv/workspace/some_file.py",
                )

        self.assertTrue(result.fixed)
        self.assertEqual(result.target, "some_file.py")
        mock_get_venv.assert_not_called()
        mock_get_workspace.assert_not_called()
        mock_run.assert_called_once_with(
            "/shared/repo/venv/workspace/some_file.py", timeout=60, python_exe="/shared/repo/venv/python"
        )

    @patch("repair_tool.agent.get_fresh_workspace_copy", return_value="/tmp/fresh123/workspace/x.py")
    @patch("repair_tool.agent.get_fresh_venv_python", return_value="/tmp/fresh123/bin/python")
    @patch("repair_tool.agent.tempfile.mkdtemp", return_value="/tmp/fresh123")
    @patch("repair_tool.agent.shutil.rmtree")
    @patch("repair_tool.agent.get_workspace_copy")
    @patch("repair_tool.agent.get_venv_python")
    @patch("openai.OpenAI")
    def test_fresh_uses_a_throwaway_venv_and_cleans_up_not_the_persistent_cache(
        self, mock_openai_cls, mock_get_venv, mock_get_workspace, mock_rmtree, mock_mkdtemp, mock_fresh_venv, mock_fresh_workspace
    ):
        mock_openai_cls.return_value.chat.completions.create.return_value = _response(
            [_tool_call("c1", "run_target", {})]
        )
        with patch("repair_tool.agent_tools.run_project") as mock_run:
            mock_run.return_value = RunResult(ok=True, returncode=0, stdout="ok\n", stderr="")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
                result = agent_repair("x.py", fresh=True)

        self.assertTrue(result.fixed)
        mock_get_venv.assert_not_called()
        mock_get_workspace.assert_not_called()
        mock_fresh_venv.assert_called_once_with("/tmp/fresh123")
        mock_fresh_workspace.assert_called_once_with("x.py", "/tmp/fresh123")
        mock_rmtree.assert_called_once_with("/tmp/fresh123", ignore_errors=True)

    @patch("repair_tool.agent.get_workspace_copy", return_value=os.path.join(EXAMPLES, "01_missing_package.py"))
    @patch("repair_tool.agent.get_venv_python", return_value="/fake/python")
    @patch("openai.OpenAI")
    def test_openai_client_is_given_a_bounded_timeout(self, mock_openai_cls, _mock_venv, _mock_workspace):
        """Regression test for a real bug found running the evaluation
        pilot: with no client-side timeout, a slow/stuck OpenAI response
        can block a whole evaluation run for minutes with no local symptom
        (no subprocess, no CPU use) to diagnose. The client must be built
        with an explicit timeout so a hang degrades to a normal
        'LLM call failed' error on that one step instead.
        """
        mock_openai_cls.return_value.chat.completions.create.return_value = _response([], content="stop")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))

        mock_openai_cls.assert_called_once_with(api_key="sk-fake-for-test", timeout=_REQUEST_TIMEOUT_SECONDS)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
@unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
class TestAgentRepairNetwork(unittest.TestCase):
    def test_required_behaviour_real_agent_fixes_numpy_float(self):
        result = agent_repair(os.path.join(EXAMPLES, "02_numpy_float.py"))
        self.assertEqual(result.error, "", msg=result.error)
        self.assertTrue(result.fixed)
        self.assertTrue(len(result.trace) > 0)

    def test_required_behaviour_fresh_ignores_a_cached_fixed_workspace(self):
        """TASK_repo_scale_agent.md Part C: fix a file once (caching the
        fixed workspace), then run again with fresh=True and confirm it
        starts from the broken original -- the agent must do real work
        again (install/edit a fresh copy), not just see an already-passing
        run_target and stop, which is exactly what the default (cached)
        mode would do on a second call to the same target.
        """
        target = os.path.join(EXAMPLES, "01_missing_package.py")

        cached = agent_repair(target)
        self.assertTrue(cached.fixed, msg=cached.error)

        fresh = agent_repair(target, fresh=True)
        self.assertEqual(fresh.error, "", msg=fresh.error)
        self.assertTrue(fresh.fixed)
        fix_tools_used = [step.tool_called for step in fresh.trace if step.tool_called in ("install_package", "edit_code")]
        self.assertTrue(fix_tools_used, "fresh=True should have had to actually fix the file again, not find it already passing")


if __name__ == "__main__":
    unittest.main()
