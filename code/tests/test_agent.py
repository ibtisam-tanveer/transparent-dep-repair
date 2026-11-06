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
    @patch("openai.OpenAI")
    def test_a_response_with_no_choices_is_an_llm_error_not_a_crash(self, mock_openai_cls, _venv, _workspace):
        """kiste_smoke_v6: two notebooks ended with an unexplained
        "TypeError: 'NoneType' object is not subscriptable" because a reply
        without `choices` was indexed outside the LLM-error handler."""
        for empty in (None, []):
            response = MagicMock()
            response.choices = empty
            mock_openai_cls.return_value.chat.completions.create.side_effect = [response]
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
            self.assertFalse(result.fixed)
            self.assertEqual(result.stop_reason, "error")
            self.assertIn("no choices", result.error)

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

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
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

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True), \
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

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
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

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
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

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"), max_steps=3)

        self.assertFalse(result.fixed)
        self.assertEqual(len(result.trace), 3)

    @patch("openai.OpenAI")
    def test_model_stopping_without_a_tool_call_ends_the_run_not_fixed(self, mock_openai_cls, _mock_venv, _mock_workspace):
        mock_openai_cls.return_value.chat.completions.create.return_value = _response(
            [], content="I don't know how to fix this."
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
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
    @patch("repair_tool.agent.AGENT_BASE_URL", None)
    def test_kiste_token_is_used_when_openai_key_is_unset(self, mock_openai_cls, _mock_venv, _mock_workspace):
        """LLM_SETUP_kiste.md: KISTE_API_TOKEN works as the sole credential
        for the self-hosted endpoint, which has no OPENAI_API_KEY at all."""
        mock_openai_cls.return_value.chat.completions.create.return_value = _response([], content="give up")
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ["KISTE_API_TOKEN"] = "kiste-fake-for-test"
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertEqual(result.error, "")
        mock_openai_cls.assert_called_once_with(
            api_key="kiste-fake-for-test", base_url=None, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    @patch("openai.OpenAI")
    @patch("repair_tool.agent.AGENT_BASE_URL", None)
    def test_kiste_token_takes_priority_when_both_keys_are_set(self, mock_openai_cls, _mock_venv, _mock_workspace):
        """Regression test for a real bug found switching to kiste: a .env
        that still has OPENAI_API_KEY (kept from the OpenAI path) alongside
        a newly-added KISTE_API_TOKEN sent the OpenAI key to kiste's
        base_url as a bearer token -- kiste's own auth layer correctly
        rejected it with 401. KISTE_API_TOKEN must win whenever both are
        present, since it's the more specific override."""
        mock_openai_cls.return_value.chat.completions.create.return_value = _response([], content="give up")
        with patch.dict(
            os.environ, {"OPENAI_API_KEY": "sk-openai-fake", "KISTE_API_TOKEN": "kiste-fake-for-test"}, clear=True
        ):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertEqual(result.error, "")
        mock_openai_cls.assert_called_once_with(
            api_key="kiste-fake-for-test", base_url=None, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    @patch("openai.OpenAI")
    @patch("repair_tool.agent.AGENT_BASE_URL", "https://kiste.informatik.tu-chemnitz.de/v1")
    def test_base_url_override_is_passed_to_the_client(self, mock_openai_cls, _mock_venv, _mock_workspace):
        mock_openai_cls.return_value.chat.completions.create.return_value = _response([], content="give up")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertEqual(result.error, "")
        mock_openai_cls.assert_called_once_with(
            api_key="sk-fake-for-test", base_url="https://kiste.informatik.tu-chemnitz.de/v1",
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    @patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "Successfully installed seaborn"))
    @patch("repair_tool.agent_tools.pypi.latest_version", return_value="0.13.0")
    @patch("repair_tool.agent_tools.pypi.resolve_package_name", return_value="seaborn")
    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_reasoning_model_output_does_not_break_tool_call_parsing(
        self, mock_openai_cls, mock_run, mock_resolve, mock_latest, mock_apply, _mock_venv, _mock_workspace
    ):
        """LLM_SETUP_kiste.md gotcha 1: a reasoning model (Qwen3 'thinking'
        style) returns a separate reasoning_content field alongside
        content/tool_calls. The agent must keep extracting tool_calls/
        content exactly as before, and must not echo reasoning_content
        back into the conversation it sends the model."""
        mock_run.side_effect = [
            RunResult(ok=False, returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'seaborn'"),
            RunResult(ok=True, returncode=0, stdout="0.13.0\n", stderr=""),
        ]

        def _reasoning_response(tool_calls, content=None):
            resp = _response(tool_calls, content=content)
            resp.choices[0].message.reasoning_content = "thinking about which tool to call next..."
            return resp

        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _reasoning_response([_tool_call("c1", "run_target", {})], content="let's see what's wrong"),
            _reasoning_response([_tool_call("c2", "diagnose_error", {"run_result": {
                "ok": False, "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError: No module named 'seaborn'"
            }})]),
            _reasoning_response([_tool_call("c3", "lookup_package", {"import_name": "seaborn"})]),
            _reasoning_response([_tool_call("c4", "install_package", {"package": "seaborn"})]),
            _reasoning_response([_tool_call("c5", "verify", {})]),
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))

        self.assertTrue(result.fixed)
        tools_called = [step.tool_called for step in result.trace]
        self.assertEqual(tools_called, ["run_target", "diagnose_error", "lookup_package", "install_package", "verify"])

        sent_messages = mock_openai_cls.return_value.chat.completions.create.call_args_list[-1].kwargs["messages"]
        assistant_messages = [m for m in sent_messages if m.get("role") == "assistant"]
        self.assertTrue(assistant_messages)
        for m in assistant_messages:
            self.assertNotIn("reasoning_content", m)

    @patch("openai.OpenAI")
    def test_llm_call_failure_degrades_to_error_not_a_crash(self, mock_openai_cls, _mock_venv, _mock_workspace):
        mock_openai_cls.return_value.chat.completions.create.side_effect = RuntimeError("connection reset")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertFalse(result.fixed)
        self.assertIn("LLM call failed", result.error)
        self.assertFalse(result.too_large_for_context)

    @patch("openai.OpenAI")
    def test_context_length_exceeded_is_flagged_distinctly_not_as_a_generic_failure(
        self, mock_openai_cls, _mock_venv, _mock_workspace
    ):
        """PILOT_DECISIONS.md #2: a notebook too large for the model's
        context window is its own outcome, not a repair failure -- found
        for real during the evaluation pilot smoke test."""
        too_large_exc = RuntimeError("Error code: 400 - context length exceeded")
        too_large_exc.code = "context_length_exceeded"
        mock_openai_cls.return_value.chat.completions.create.side_effect = too_large_exc
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertFalse(result.fixed)
        self.assertTrue(result.too_large_for_context)

    @patch("openai.OpenAI")
    def test_a_different_bad_request_error_is_not_mistaken_for_context_length(
        self, mock_openai_cls, _mock_venv, _mock_workspace
    ):
        other_exc = RuntimeError("Error code: 400 - invalid request")
        other_exc.code = "invalid_request_error"
        mock_openai_cls.return_value.chat.completions.create.side_effect = other_exc
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertFalse(result.too_large_for_context)

    @patch("openai.OpenAI")
    def test_quota_exhausted_is_flagged_distinctly_not_as_a_generic_failure(
        self, mock_openai_cls, _mock_venv, _mock_workspace
    ):
        """Found for real running the pilot smoke test: the API key ran out
        of credits mid-run (OpenAI's 429 'insufficient_quota' error),
        degrading 37 notebooks to a generic failure that would have
        silently counted against effectiveness -- same treatment as
        too_large_for_context, for the same reason."""
        quota_exc = RuntimeError(
            "Error code: 429 - {'error': {'message': 'You have no credits remaining.', "
            "'type': 'insufficient_quota', 'param': None, 'code': 'credit_balance_exhausted'}}"
        )
        quota_exc.type = "insufficient_quota"
        quota_exc.code = "credit_balance_exhausted"
        mock_openai_cls.return_value.chat.completions.create.side_effect = quota_exc
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertFalse(result.fixed)
        self.assertTrue(result.quota_exhausted)
        self.assertFalse(result.too_large_for_context)

    @patch("openai.OpenAI")
    def test_a_different_error_type_is_not_mistaken_for_quota_exhaustion(
        self, mock_openai_cls, _mock_venv, _mock_workspace
    ):
        other_exc = RuntimeError("Error code: 400 - context length exceeded")
        other_exc.type = "invalid_request_error"
        other_exc.code = "context_length_exceeded"
        mock_openai_cls.return_value.chat.completions.create.side_effect = other_exc
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))
        self.assertFalse(result.quota_exhausted)


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
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
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
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
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
    @patch("repair_tool.agent.AGENT_BASE_URL", None)
    def test_openai_client_is_given_a_bounded_timeout(self, mock_openai_cls, _mock_venv, _mock_workspace):
        """Regression test for a real bug found running the evaluation
        pilot: with no client-side timeout, a slow/stuck OpenAI response
        can block a whole evaluation run for minutes with no local symptom
        (no subprocess, no CPU use) to diagnose. The client must be built
        with an explicit timeout so a hang degrades to a normal
        'LLM call failed' error on that one step instead.
        """
        mock_openai_cls.return_value.chat.completions.create.return_value = _response([], content="stop")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))

        mock_openai_cls.assert_called_once_with(
            api_key="sk-fake-for-test", base_url=None, timeout=_REQUEST_TIMEOUT_SECONDS
        )


@patch("repair_tool.agent.get_workspace_copy", return_value=os.path.join(EXAMPLES, "01_missing_package.py"))
@patch("repair_tool.agent.get_venv_python", return_value="/fake/python")
class TestAgentStoppingBehavior(unittest.TestCase):
    """TASK_agent_stopping.md: the harness enforces termination itself
    instead of trusting the model's own judgement -- found necessary
    running kiste's Qwen3.6-35B, which (unlike gpt-4o) doesn't recognise
    when it's stuck and burns the full MAX_STEPS budget re-trying."""

    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_no_progress_stop_fires_before_max_steps_on_a_stuck_notebook(
        self, mock_openai_cls, mock_run, _mock_venv, _mock_workspace
    ):
        """The same error signature (ModuleNotFoundError / seaborn) on every
        run_target call -- a model that just keeps re-checking without
        trying anything new must be stopped well before the 30-step cap."""
        same_failure = RunResult(ok=False, returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'seaborn'")
        mock_run.side_effect = [same_failure] * 10
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call(f"c{i}", "run_target", {})]) for i in range(10)
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"), max_steps=30, no_progress_limit=3)

        self.assertFalse(result.fixed)
        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(len(result.trace), 4, "1 baseline observation + 3 consecutive unchanged ones should stop it")
        self.assertLess(len(result.trace), 30)

    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_a_changing_error_signature_resets_the_counter_and_keeps_going(
        self, mock_openai_cls, mock_run, _mock_venv, _mock_workspace
    ):
        """A genuinely multi-layer fix -- each run_target reveals a
        *different* missing module -- must NOT trip the no-progress stop,
        since each change is real progress (a previous layer was cleared)."""
        mock_run.side_effect = [
            RunResult(ok=False, returncode=1, stdout="", stderr=f"ModuleNotFoundError: No module named 'pkg{i}'")
            for i in range(5)
        ]
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call(f"c{i}", "run_target", {})]) for i in range(5)
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"), max_steps=5, no_progress_limit=3)

        self.assertFalse(result.fixed)
        self.assertEqual(result.stop_reason, "step_cap")
        self.assertEqual(len(result.trace), 5, "should run the full budget since no-progress never actually fires")

    @patch("repair_tool.agent_tools.apply_module.apply", return_value=(False, "still fails"))
    @patch("openai.OpenAI")
    def test_a_repeated_identical_fix_action_is_not_re_executed(self, mock_openai_cls, mock_apply, _mock_venv, _mock_workspace):
        """Looping models re-issue identical calls repeatedly -- the harness
        must not re-run the same install_package call over and over, and
        must count the repeat itself as no-progress."""
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call(f"c{i}", "install_package", {"package": "seaborn"})]) for i in range(4)
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"), max_steps=30, no_progress_limit=3)

        self.assertFalse(result.fixed)
        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(mock_apply.call_count, 1, "the real install should only ever be executed once")
        self.assertEqual(len(result.trace), 4)
        self.assertTrue(result.trace[1].tool_result.get("already_attempted"))
        self.assertTrue(result.trace[2].tool_result.get("already_attempted"))
        self.assertTrue(result.trace[3].tool_result.get("already_attempted"))

    @patch("openai.OpenAI")
    def test_report_unfixable_produces_a_clean_model_gave_up_outcome(self, mock_openai_cls, _mock_venv, _mock_workspace):
        mock_openai_cls.return_value.chat.completions.create.return_value = _response(
            [_tool_call("c1", "report_unfixable", {"reason": "needs a system-level compiler, out of scope"})]
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"))

        self.assertFalse(result.fixed)
        self.assertEqual(result.stop_reason, "model_gave_up")
        self.assertEqual(result.error, "")
        self.assertEqual(len(result.trace), 1)
        self.assertEqual(result.trace[0].tool_called, "report_unfixable")
        self.assertEqual(result.trace[0].tool_result["reason"], "needs a system-level compiler, out of scope")

    @patch("repair_tool.agent_tools.run_project")
    @patch("openai.OpenAI")
    def test_old_tool_results_are_compacted_in_the_outgoing_request(
        self, mock_openai_cls, mock_run, _mock_venv, _mock_workspace
    ):
        """TASK_agent_stopping.md #5: the full message history (not the
        notebook source, which is never resent) grows by two messages every
        step and is resent whole on every call with no server-side state --
        the real contributor to latency/context pressure on a long run."""
        mock_run.side_effect = [
            RunResult(ok=False, returncode=1, stdout="", stderr=f"ModuleNotFoundError: No module named 'pkg{i}'")
            for i in range(6)
        ]
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            _response([_tool_call(f"c{i}", "run_target", {})]) for i in range(6)
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"), max_steps=6, no_progress_limit=10)

        self.assertEqual(len(result.trace), 6)  # the full trace is never compacted

        last_call_messages = mock_openai_cls.return_value.chat.completions.create.call_args_list[-1].kwargs["messages"]
        tool_messages = [m for m in last_call_messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 5)  # 5 prior steps by the time of the 6th call
        compacted = [m for m in tool_messages if "omitted" in m["content"]]
        verbatim = [m for m in tool_messages if "omitted" not in m["content"]]
        self.assertEqual(len(compacted), 1)
        self.assertEqual(len(verbatim), 4)


class TestCompactedMessagesForRequest(unittest.TestCase):
    def test_short_history_is_left_untouched(self):
        from repair_tool.agent import _compacted_messages_for_request

        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}] + [
            {"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"} for i in range(3)
        ]
        self.assertEqual(_compacted_messages_for_request(messages), messages)

    def test_older_tool_results_beyond_the_window_are_replaced(self):
        from repair_tool.agent import _compacted_messages_for_request

        messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"} for i in range(6)]
        compacted = _compacted_messages_for_request(messages)
        self.assertEqual(len(compacted), 6)
        self.assertIn("omitted", compacted[0]["content"])
        self.assertIn("omitted", compacted[1]["content"])
        for i in range(2, 6):
            self.assertEqual(compacted[i]["content"], f"result {i}")

    def test_non_tool_messages_are_never_touched(self):
        from repair_tool.agent import _compacted_messages_for_request

        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}] + [
            {"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"} for i in range(6)
        ]
        compacted = _compacted_messages_for_request(messages)
        self.assertEqual(compacted[0], {"role": "system", "content": "s"})
        self.assertEqual(compacted[1], {"role": "user", "content": "u"})

    def test_original_messages_list_is_never_mutated(self):
        from repair_tool.agent import _compacted_messages_for_request

        messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"} for i in range(6)]
        original_first_content = messages[0]["content"]
        _compacted_messages_for_request(messages)
        self.assertEqual(messages[0]["content"], original_first_content)


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
