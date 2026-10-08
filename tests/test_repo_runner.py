import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation._repo_runner import count_discovered_files, run_repo_incrementally
from repair_tool.agent import AgentResult
from repair_tool.runner import RunResult


class TestCountDiscoveredFiles(unittest.TestCase):
    def test_counts_real_runnable_files_in_a_real_directory(self):
        tmpdir = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmpdir, "a.py"), "w") as f:
                f.write("print(1)\n")
            with open(os.path.join(tmpdir, "b.py"), "w") as f:
                f.write("print(2)\n")
            with open(os.path.join(tmpdir, "README.md"), "w") as f:
                f.write("not runnable\n")
            self.assertEqual(count_discovered_files(tmpdir), 2)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_an_empty_directory_counts_zero(self):
        tmpdir = tempfile.mkdtemp()
        try:
            self.assertEqual(count_discovered_files(tmpdir), 0)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestRunRepoIncrementally(unittest.TestCase):
    """repo.py's discovery/env-setup and venv_manager's fresh-venv/
    workspace-copy functions are mocked at the same seams agent_repo.py
    itself calls them at -- agent.agent_repair() (the actual repair) and
    runner.run_project (the initial per-file check) are mocked too, same
    offline-testing pattern as test_agent_repo.py."""

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    @patch("evaluation._repo_runner.get_repo_file_workspace_copy", return_value="/fake/workspace/b.py")
    @patch("evaluation._repo_runner.agent_repair")
    @patch("evaluation._repo_runner.run_project")
    @patch("evaluation._repo_runner.get_fresh_venv_python", return_value="/fake/python")
    @patch("evaluation._repo_runner.repo_module.discover_runnable_files", return_value=["a.py", "b.py"])
    @patch("evaluation._repo_runner.repo_module._install_dependencies", return_value=(True, "no deps", ""))
    @patch("evaluation._repo_runner.repo_module.find_dependency_files", return_value=[])
    def test_yields_one_result_per_file_in_discovery_order(
        self, mock_find_deps, mock_install, mock_discover, mock_fresh_venv, mock_run_project, mock_agent_repair, mock_workspace
    ):
        mock_run_project.side_effect = [
            RunResult(ok=True, returncode=0, stdout="", stderr=""),  # a.py already passes
            RunResult(ok=False, returncode=1, stdout="", stderr="boom"),  # b.py fails, needs the agent
        ]
        mock_agent_repair.return_value = AgentResult(target="b.py", fixed=True, trace=[])

        results = list(run_repo_incrementally("/fake/repo", self.tmp_root, 30, budget_seconds=1000))

        self.assertEqual([r.target for r, _ in results], ["a.py", "b.py"])
        self.assertTrue(results[0][0].fixed)
        self.assertEqual(results[0][0].trace, [])  # already passing -- no agent call
        self.assertTrue(results[1][0].fixed)
        mock_agent_repair.assert_called_once()

    @patch("evaluation._repo_runner.get_fresh_venv_python", return_value="/fake/python")
    @patch("evaluation._repo_runner.repo_module.discover_runnable_files", return_value=["a.py", "b.py", "c.py"])
    @patch("evaluation._repo_runner.repo_module._install_dependencies", return_value=(True, "no deps", ""))
    @patch("evaluation._repo_runner.repo_module.find_dependency_files", return_value=[])
    @patch("evaluation._repo_runner.run_project", return_value=RunResult(ok=True, returncode=0, stdout="", stderr=""))
    def test_budget_exhaustion_stops_remaining_files_gracefully_between_files(
        self, mock_run_project, mock_find_deps, mock_install, mock_discover, mock_fresh_venv
    ):
        """A file already IN PROGRESS is never interrupted (there's no
        way to fake 'mid-file' here since each file call is fast and
        mocked) -- this asserts the between-files check: once the budget
        is gone, every remaining file is yielded as cut-off, not attempted
        or silently dropped."""
        # time.monotonic() is called once for `start`, then once per file's
        # between-files check (short-circuited, so never called again once
        # exhausted) -- 0 (start), 0 (file a's check), 5 (file b's check),
        # 50 (file c's check: exhausted).
        call_times = iter([0, 0, 5, 50])
        with patch("evaluation._repo_runner.time.monotonic", side_effect=lambda: next(call_times, 50)):
            results = list(run_repo_incrementally("/fake/repo", self.tmp_root, 30, budget_seconds=10))

        self.assertEqual(len(results), 3)
        self.assertTrue(results[0][0].fixed)  # within budget
        self.assertTrue(results[1][0].fixed)  # within budget
        self.assertFalse(results[2][0].fixed)  # budget exhausted before this one
        self.assertIn("time budget exhausted", results[2][0].error)
        mock_run_project.assert_called()  # called for files 0 and 1, not for the cut-off file 2... (see call count below)
        self.assertEqual(mock_run_project.call_count, 2)

    @patch("evaluation._repo_runner.get_repo_file_workspace_copy", side_effect=RuntimeError("workspace copy boom"))
    @patch("evaluation._repo_runner.get_fresh_venv_python", return_value="/fake/python")
    @patch("evaluation._repo_runner.repo_module.discover_runnable_files", return_value=["a.py"])
    @patch("evaluation._repo_runner.repo_module._install_dependencies", return_value=(True, "no deps", ""))
    @patch("evaluation._repo_runner.repo_module.find_dependency_files", return_value=[])
    @patch("evaluation._repo_runner.run_project", side_effect=RuntimeError("initial check boom"))
    def test_one_bad_file_does_not_abort_the_generator(
        self, mock_run_project, mock_find_deps, mock_install, mock_discover, mock_fresh_venv, mock_workspace
    ):
        """A run_project that raises outright (not just a failing
        RunResult) is treated the same as a genuine failure -- initial
        check failed, so a repair is attempted -- matching agent_repo.py's
        own identical contract. The repair attempt here then also fails
        (get_repo_file_workspace_copy raises), and THAT failure is what
        surfaces, without crashing the generator either way."""
        results = list(run_repo_incrementally("/fake/repo", self.tmp_root, 30, budget_seconds=1000))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0].fixed)
        self.assertIn("workspace copy boom", results[0][0].error)

    @patch("evaluation._repo_runner.run_project", return_value=RunResult(ok=True, returncode=0, stdout="", stderr=""))
    @patch("evaluation._repo_runner.get_fresh_venv_python", return_value="/fake/python")
    @patch("evaluation._repo_runner.repo_module.discover_runnable_files", return_value=["a.py"])
    @patch("evaluation._repo_runner.repo_module._install_dependencies", return_value=(False, "install failed", "requirements.txt"))
    @patch("evaluation._repo_runner.repo_module.find_dependency_files", return_value=["requirements.txt"])
    def test_repo_meta_carries_env_setup_facts_on_every_yield(
        self, mock_find_deps, mock_install, mock_discover, mock_fresh_venv, mock_run_project
    ):
        results = list(run_repo_incrementally("/fake/repo", self.tmp_root, 30, budget_seconds=1000))
        self.assertEqual(len(results), 1)
        _, repo_meta = results[0]
        self.assertFalse(repo_meta["env_setup_ok"])
        self.assertEqual(repo_meta["env_setup_log"], "install failed")
        self.assertEqual(repo_meta["dependency_file_used"], "requirements.txt")
        self.assertEqual(repo_meta["dependency_files_found"], ["requirements.txt"])


if __name__ == "__main__":
    unittest.main()
