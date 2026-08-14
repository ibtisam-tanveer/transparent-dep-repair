import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import venv_manager
from repair_tool.diagnose import Diagnosis
from repair_tool.loop import repair
from repair_tool.repair import Proposal
from repair_tool.runner import RunResult

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "broken_examples")
HELLO = os.path.join(ROOT, "hello.py")


def _ok(stdout=""):
    return RunResult(ok=True, returncode=0, stdout=stdout, stderr="")


def _fail(stderr):
    return RunResult(ok=False, returncode=1, stdout="", stderr=stderr)


@patch("repair_tool.loop.get_venv_python", return_value="/fake/python")
class TestRepairLoopOffline(unittest.TestCase):
    """All PyPI/venv/subprocess interaction mocked out: exercises the loop's
    control flow only (progress tracking, stopping conditions, iteration cap).
    """

    @patch("repair_tool.loop.run_project")
    def test_already_working_project_needs_no_attempts(self, mock_run, _mock_venv):
        mock_run.return_value = _ok("hello\n")
        result = repair(HELLO)
        self.assertTrue(result.fixed)
        self.assertEqual(result.attempts, [])

    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.propose")
    @patch("repair_tool.loop.run_project")
    def test_install_then_verified_success(self, mock_run, mock_propose, mock_apply, _mock_venv):
        mock_run.side_effect = [
            _fail("ModuleNotFoundError: No module named 'seaborn'"),
            _ok(""),
        ]
        mock_propose.return_value = Proposal(
            kind="install", package="seaborn", reason="test", source="pypi_name_match", confidence="medium"
        )
        mock_apply.return_value = (True, "Successfully installed seaborn")

        result = repair(os.path.join(EXAMPLES, "01_missing_package.py"))

        self.assertTrue(result.fixed)
        self.assertEqual(len(result.attempts), 1)
        self.assertTrue(result.attempts[0].applied)
        self.assertEqual(result.attempts[0].verification, "verified: project now runs")

    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.run_project")
    def test_unhandled_kind_stops_honestly_no_install_attempted(self, mock_run, mock_apply, _mock_venv):
        mock_run.return_value = _fail("AttributeError: module 'numpy' has no attribute 'float'")

        result = repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        mock_apply.assert_not_called()
        self.assertFalse(result.fixed)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].proposal.kind, "none")
        self.assertFalse(result.attempts[0].applied)
        self.assertEqual(result.attempts[0].verification, "not handled yet")

    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.propose")
    @patch("repair_tool.loop.run_project")
    def test_stops_when_no_progress(self, mock_run, mock_propose, mock_apply, _mock_venv):
        # Identical failure every time, even though "apply" claims success -> stall.
        mock_run.return_value = _fail("ModuleNotFoundError: No module named 'seaborn'")
        mock_propose.return_value = Proposal(kind="install", package="seaborn", confidence="medium")
        mock_apply.return_value = (True, "Successfully installed seaborn")

        result = repair(os.path.join(EXAMPLES, "01_missing_package.py"))

        self.assertFalse(result.fixed)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.attempts[-1].verification, "stopped: no progress")

    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.propose")
    @patch("repair_tool.loop.diagnose_result")
    @patch("repair_tool.loop.run_project")
    def test_caps_at_max_attempts_when_genuinely_making_progress(
        self, mock_run, mock_diagnose, mock_propose, mock_apply, _mock_venv
    ):
        # A different missing package "found" each time -> real progress
        # (signature never repeats), so only max_attempts should cap it.
        mock_run.return_value = _fail("ModuleNotFoundError: No module named 'whatever'")
        mock_diagnose.side_effect = [
            Diagnosis(kind="missing_module", module=f"pkg{i}", package=f"pkg{i}") for i in range(1, 11)
        ]
        mock_propose.return_value = Proposal(kind="install", package="pkg", confidence="medium")
        mock_apply.return_value = (True, "installed")

        result = repair(os.path.join(EXAMPLES, "01_missing_package.py"), max_attempts=3)

        self.assertFalse(result.fixed)
        self.assertEqual(len(result.attempts), 3)
        self.assertTrue(all(a.applied for a in result.attempts))

    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.propose")
    @patch("repair_tool.loop.run_project")
    def test_install_failure_stops_the_loop(self, mock_run, mock_propose, mock_apply, _mock_venv):
        mock_run.return_value = _fail("ModuleNotFoundError: No module named 'seaborn'")
        mock_propose.return_value = Proposal(kind="install", package="seaborn", confidence="medium")
        mock_apply.return_value = (False, "ERROR: No matching distribution found for seaborn")

        result = repair(os.path.join(EXAMPLES, "01_missing_package.py"))

        self.assertFalse(result.fixed)
        self.assertEqual(len(result.attempts), 1)
        self.assertFalse(result.attempts[0].applied)
        self.assertIn("install failed", result.attempts[0].verification)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
class TestRepairIntegration(unittest.TestCase):
    """Hits real PyPI, creates a real (temporary) venv, does a real install.
    Confirms PHASE3_TASK.md's end-to-end required behaviour and that the
    interpreter running the tests is left untouched.
    """

    def setUp(self):
        self.tmp_venv_root = tempfile.mkdtemp(prefix="repair_tool_test_venvs_")
        self._orig_venv_root = venv_manager.VENV_ROOT
        venv_manager.VENV_ROOT = self.tmp_venv_root

    def tearDown(self):
        venv_manager.VENV_ROOT = self._orig_venv_root
        shutil.rmtree(self.tmp_venv_root, ignore_errors=True)

    def test_repair_fixes_missing_seaborn_end_to_end(self):
        target = os.path.join(EXAMPLES, "01_missing_package.py")

        result = repair(target)

        self.assertTrue(result.fixed)
        self.assertTrue(any(a.applied and a.proposal.package == "seaborn" for a in result.attempts))

        # seaborn was deliberately never installed in the dev venv (see
        # README.md) precisely so this proves the install stayed isolated.
        with self.assertRaises(ImportError):
            importlib.import_module("seaborn")


if __name__ == "__main__":
    unittest.main()
