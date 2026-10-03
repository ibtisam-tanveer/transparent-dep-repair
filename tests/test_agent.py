import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.agent import AgentResult, agent_repair
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
    only the agent's own loop: dispatching tools, building the trace,
    tagging provenance, and stopping on verify or MAX_STEPS. Real network
    calls (pypi, pip) are mocked at the same seam agent_tools.py calls them.
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

        install_step = result.trace[3]
        self.assertEqual(install_step.provenance, "metadata_verified + execution_verified")
        self.assertEqual(result.trace[0].provenance, "execution_verified")
        self.assertEqual(result.trace[2].provenance, "metadata_verified")
        self.assertEqual(result.trace[4].provenance, "execution_verified")
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
        self.assertEqual(edit_step.provenance, "execution_verified")

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


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
@unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
class TestAgentRepairNetwork(unittest.TestCase):
    def test_required_behaviour_real_agent_fixes_numpy_float(self):
        result = agent_repair(os.path.join(EXAMPLES, "02_numpy_float.py"))
        self.assertEqual(result.error, "", msg=result.error)
        self.assertTrue(result.fixed)
        self.assertTrue(len(result.trace) > 0)


if __name__ == "__main__":
    unittest.main()
