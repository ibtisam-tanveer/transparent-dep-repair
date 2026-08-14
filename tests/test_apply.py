import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.apply import apply
from repair_tool.repair import Proposal


class TestApply(unittest.TestCase):
    def test_non_install_proposal_is_a_noop(self):
        with patch("subprocess.run") as mock_run:
            ok, log = apply(Proposal(kind="none", reason="not handled"), python_exe="/fake/python")
        mock_run.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("none", log)

    @patch("subprocess.run")
    def test_successful_install(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Successfully installed seaborn\n", stderr="")
        proposal = Proposal(kind="install", package="seaborn", command="pip install seaborn")
        ok, log = apply(proposal, python_exe="/fake/python")
        self.assertTrue(ok)
        self.assertIn("Successfully installed", log)
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["/fake/python", "-m", "pip", "install", "seaborn"])
        self.assertEqual(kwargs["timeout"], 300)

    @patch("subprocess.run")
    def test_failed_install(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ERROR: No matching distribution\n")
        proposal = Proposal(kind="install", package="nonexistent-xyz", command="pip install nonexistent-xyz")
        ok, log = apply(proposal, python_exe="/fake/python")
        self.assertFalse(ok)
        self.assertIn("No matching distribution", log)

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=5))
    def test_install_timeout_is_captured_not_raised(self, _mock_run):
        proposal = Proposal(kind="install", package="seaborn", command="pip install seaborn")
        ok, log = apply(proposal, python_exe="/fake/python", timeout=5)
        self.assertFalse(ok)
        self.assertIn("timed out after 5s", log)


if __name__ == "__main__":
    unittest.main()
