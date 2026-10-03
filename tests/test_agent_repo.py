import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.agent import AgentResult
from repair_tool.agent_repo import RepoAgentResult, agent_repair_repo
from repair_tool.runner import RunResult

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_REPO = os.path.join(ROOT, "tests", "fixtures", "agent_repo")


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


def _ok(stdout: str = "") -> RunResult:
    return RunResult(ok=True, returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> RunResult:
    return RunResult(ok=False, returncode=1, stdout="", stderr=stderr)


class TestAgentRepairRepoOffline(unittest.TestCase):
    """The LLM, pypi, and pip-install calls are all mocked -- exercises the
    repo agent's own orchestration: reusing repo.py's discovery/env-setup,
    running the per-file agent only on files that actually fail, the
    shared-environment behaviour (a later file benefiting from an earlier
    file's install), and not letting one bad file abort the others.
    """

    def setUp(self):
        self.tmp_venv_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_venv_dir, ignore_errors=True)

    @patch("repair_tool.agent_tools.apply_module.apply_code_edit", return_value=(True, "applied"))
    @patch("repair_tool.agent_tools.apply_module.apply")
    @patch("repair_tool.agent_tools.pypi.latest_version")
    @patch("repair_tool.agent_tools.pypi.resolve_package_name")
    @patch("repair_tool.agent_tools.run_project")
    @patch("repair_tool.agent_repo.run_project")
    @patch("repair_tool.agent_repo.venv_dir_for")
    @patch("repair_tool.agent_repo.get_venv_python", return_value="/fake/shared/python")
    @patch("openai.OpenAI")
    def test_fixes_what_it_can_shares_the_environment_and_leaves_already_passing_files_alone(
        self,
        mock_openai_cls,
        mock_get_venv,
        mock_venv_dir_for,
        mock_repo_run,
        mock_agent_run,
        mock_resolve,
        mock_latest,
        mock_apply,
        mock_apply_edit,
    ):
        mock_venv_dir_for.return_value = self.tmp_venv_dir

        # The per-file agent's own run_target/verify calls (agent_tools.run_project).
        mock_agent_run.side_effect = [
            _fail("ModuleNotFoundError: No module named 'seaborn'"),  # a: run_target
            _ok("0.13.2\n"),  # a: verify (installing seaborn was enough)
            _fail("ModuleNotFoundError: No module named 'numpy'"),  # c: run_target
            _fail("AttributeError: module 'numpy' has no attribute 'float'"),  # c: verify after install
            _ok("3.14\n"),  # c: verify after edit_code
        ]
        mock_resolve.side_effect = ["seaborn", "numpy"]
        mock_latest.side_effect = ["0.13.2", "2.0.0"]
        mock_apply.side_effect = [(True, "Successfully installed seaborn"), (True, "Successfully installed numpy")]

        # agent_repair_repo's own initial per-file check, one per discovered
        # file, in sorted order: a (fails), b (now passes -- shared env),
        # c (fails), good (already passing).
        mock_repo_run.side_effect = [
            _fail("ModuleNotFoundError: No module named 'seaborn'"),
            _ok("0.13.2\n"),
            _fail("ModuleNotFoundError: No module named 'numpy'"),
            _ok("already passing\n"),
        ]

        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            # --- file a_missing_seaborn.py ---
            _response([_tool_call("a1", "run_target", {})]),
            _response([_tool_call("a2", "diagnose_error", {"run_result": {
                "ok": False, "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError: No module named 'seaborn'"
            }})]),
            _response([_tool_call("a3", "lookup_package", {"import_name": "seaborn"})]),
            _response([_tool_call("a4", "install_package", {"package": "seaborn"})]),
            _response([_tool_call("a5", "verify", {})]),
            # --- file c_numpy_float.py ---
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
            result = agent_repair_repo(FIXTURE_REPO)

        self.assertIsInstance(result, RepoAgentResult)
        self.assertEqual(result.error, "")
        self.assertEqual(result.repo_path, FIXTURE_REPO)

        targets = [f.target for f in result.files]
        self.assertEqual(targets, ["a_missing_seaborn.py", "b_also_needs_seaborn.py", "c_numpy_float.py", "good.py"])

        file_a, file_b, file_c, file_good = result.files

        # a: genuinely repaired by the per-file agent.
        self.assertTrue(file_a.fixed)
        self.assertTrue(file_a.trace)
        install_step = next(s for s in file_a.trace if s.tool_called == "install_package")
        self.assertEqual(install_step.grounding, "metadata_grounded")
        self.assertEqual(install_step.verification, "verified")

        # b: never needed the agent at all -- it just benefited from a's install.
        self.assertTrue(file_b.fixed)
        self.assertEqual(file_b.trace, [])

        # c: two-round repair, same shape as the single-file motivating case.
        self.assertTrue(file_c.fixed)
        c_install = next(s for s in file_c.trace if s.tool_called == "install_package")
        self.assertEqual(c_install.grounding, "metadata_grounded")
        self.assertEqual(c_install.verification, "unverified")  # necessary but not sufficient on its own
        c_edit = next(s for s in file_c.trace if s.tool_called == "edit_code")
        self.assertEqual(c_edit.verification, "verified")

        # good.py: already passing, no agent call needed.
        self.assertTrue(file_good.fixed)
        self.assertEqual(file_good.trace, [])

        self.assertEqual(result.summary, {"total": 4, "already_passing": 2, "fixed": 2, "still_failing": 0})

        # Every per-file agent call and every tool call shared the SAME
        # python_exe -- proof this is one environment, not one per file.
        for call_args in mock_apply.call_args_list:
            self.assertEqual(call_args.args[1], "/fake/shared/python")

    @patch("repair_tool.agent_repo.agent_repair")
    @patch("repair_tool.agent_repo.run_project")
    @patch("repair_tool.agent_repo.venv_dir_for")
    @patch("repair_tool.agent_repo.get_venv_python", return_value="/fake/shared/python")
    def test_one_bad_file_does_not_abort_the_repo_run(
        self, mock_get_venv, mock_venv_dir_for, mock_repo_run, mock_agent_repair
    ):
        mock_venv_dir_for.return_value = self.tmp_venv_dir
        mock_repo_run.side_effect = [
            _fail("ModuleNotFoundError: No module named 'seaborn'"),
            _fail("ModuleNotFoundError: No module named 'seaborn'"),
            _fail("ModuleNotFoundError: No module named 'numpy'"),
            _ok(),
        ]
        mock_agent_repair.side_effect = RuntimeError("boom -- something unexpected went wrong processing this file")

        result = agent_repair_repo(FIXTURE_REPO)

        self.assertEqual(result.error, "")
        self.assertEqual(len(result.files), 4)
        for file_result in result.files[:3]:
            if file_result.target != "good.py":
                self.assertFalse(file_result.fixed)
                self.assertIn("boom", file_result.error)
        self.assertTrue(result.files[3].fixed)  # good.py never even reaches agent_repair

    def test_not_a_directory_returns_a_clean_error_not_a_crash(self):
        result = agent_repair_repo("/no/such/repo/path")
        self.assertIsInstance(result, RepoAgentResult)
        self.assertNotEqual(result.error, "")
        self.assertFalse(result.env_setup_ok)
        self.assertEqual(result.files, [])

    @patch("repair_tool.agent_repo.get_fresh_venv_python", return_value="/tmp/freshrepo/bin/python")
    @patch("repair_tool.agent_repo.tempfile.mkdtemp", return_value="/tmp/freshrepo")
    @patch("repair_tool.agent_repo.shutil.rmtree")
    @patch("repair_tool.agent_repo.get_venv_python")
    @patch("repair_tool.agent_repo.run_project", return_value=_ok())
    def test_fresh_uses_a_throwaway_venv_for_the_whole_repo_not_the_cache(
        self, mock_repo_run, mock_get_venv, mock_rmtree, mock_mkdtemp, mock_fresh_venv
    ):
        result = agent_repair_repo(FIXTURE_REPO, fresh=True)

        mock_get_venv.assert_not_called()
        mock_fresh_venv.assert_called_once_with("/tmp/freshrepo")
        mock_rmtree.assert_called_once_with("/tmp/freshrepo", ignore_errors=True)
        self.assertEqual(result.summary["total"], 4)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
@unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
class TestAgentRepairRepoNetwork(unittest.TestCase):
    def test_required_behaviour_real_repo_agent_fixes_both_files_in_one_shared_env(self):
        from repair_tool.report import build_repo_report

        result = agent_repair_repo(FIXTURE_REPO, fresh=True)

        self.assertEqual(result.error, "", msg=result.error)
        by_target = {f.target: f for f in result.files}

        self.assertTrue(by_target["a_missing_seaborn.py"].fixed)
        self.assertTrue(by_target["b_also_needs_seaborn.py"].fixed)
        # Proof of the shared environment: b was never touched by the agent,
        # it just started passing once a's real pip install made seaborn
        # importable in the one venv both files share.
        self.assertEqual(by_target["b_also_needs_seaborn.py"].trace, [])
        self.assertTrue(by_target["c_numpy_float.py"].fixed)
        self.assertTrue(by_target["good.py"].fixed)

        self.assertEqual(result.summary["still_failing"], 0)

        report = build_repo_report(result)
        self.assertIn("4 of 4 files now run", report)
        self.assertIn("a_missing_seaborn.py", report)
        self.assertIn("grounding:", report)


if __name__ == "__main__":
    unittest.main()
