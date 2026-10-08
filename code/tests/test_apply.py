import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.apply import apply, apply_code_edit, installed_packages, restore_packages
from repair_tool.repair import Proposal

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))


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

    def test_missing_python_exe_does_not_crash(self):
        # A genuinely nonexistent interpreter path -- real subprocess call,
        # no mocking, to prove the OSError path actually works end to end.
        proposal = Proposal(kind="install", package="seaborn", command="pip install seaborn")
        ok, log = apply(proposal, python_exe="/no/such/python/binary")
        self.assertFalse(ok)
        self.assertIn("could not run", log)


class TestApplyCodeEdit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_apply_code_edit_")
        self.workspace_path = os.path.join(self.tmpdir, "target.py")
        with open(self.workspace_path, "w") as f:
            f.write("import numpy as np\nx = np.float(3.14)\nprint(x)\n")

    def test_single_edit_applies_correctly(self):
        ok, log = apply_code_edit([{"find": "np.float", "replace": "float"}], self.workspace_path)
        self.assertTrue(ok)
        with open(self.workspace_path) as f:
            content = f.read()
        self.assertIn("x = float(3.14)", content)
        self.assertNotIn("np.float", content)

    def test_multiple_edits_apply_in_order(self):
        with open(self.workspace_path, "w") as f:
            f.write("a = np.float(1)\nb = np.int(2)\n")
        edits = [{"find": "np.float", "replace": "float"}, {"find": "np.int", "replace": "int"}]
        ok, log = apply_code_edit(edits, self.workspace_path)
        self.assertTrue(ok)
        with open(self.workspace_path) as f:
            content = f.read()
        self.assertEqual(content, "a = float(1)\nb = int(2)\n")

    def test_find_not_present_fails_without_crashing_and_does_not_partially_edit(self):
        with open(self.workspace_path) as f:
            original = f.read()
        edits = [{"find": "np.float", "replace": "float"}, {"find": "this text is not in the file", "replace": "x"}]
        ok, log = apply_code_edit(edits, self.workspace_path)
        self.assertFalse(ok)
        self.assertIn("not found verbatim", log)
        # first edit succeeded in-memory but the failed second edit means
        # nothing should have been written back to disk
        with open(self.workspace_path) as f:
            self.assertEqual(f.read(), original)

    def test_no_edits_is_a_clean_failure(self):
        ok, log = apply_code_edit([], self.workspace_path)
        self.assertFalse(ok)

    def test_missing_workspace_file_does_not_crash(self):
        ok, log = apply_code_edit([{"find": "x", "replace": "y"}], "/no/such/file.py")
        self.assertFalse(ok)
        self.assertIn("could not read", log)


class TestInstalledPackagesAndRestore(unittest.TestCase):
    """PHASE5_IMPROVEMENTS_TASK.md #1 -- keeping the two hard-case candidates
    genuinely independent by snapshotting and restoring package state."""

    @patch("subprocess.run")
    def test_installed_packages_parses_pip_freeze_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="numpy==1.23.0\npandas==1.5.3\n", stderr="")
        result = installed_packages("/fake/python")
        self.assertEqual(result, {"numpy==1.23.0", "pandas==1.5.3"})

    @patch("subprocess.run", side_effect=OSError("no such file"))
    def test_installed_packages_never_raises_on_missing_interpreter(self, _mock_run):
        self.assertEqual(installed_packages("/fake/python"), set())

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=5))
    def test_installed_packages_never_raises_on_timeout(self, _mock_run):
        self.assertEqual(installed_packages("/fake/python", timeout=5), set())

    def test_missing_python_exe_does_not_crash(self):
        # Real subprocess call, no mocking, against a genuinely nonexistent
        # interpreter -- proves the OSError path works end to end.
        self.assertEqual(installed_packages("/no/such/python/binary"), set())

    @patch("repair_tool.apply.installed_packages")
    @patch("subprocess.run")
    def test_restore_uninstalls_only_newly_added_packages(self, mock_run, mock_installed):
        before = {"numpy==1.23.0"}
        after = {"numpy==1.23.0", "scipy==1.10.0", "scipy-extra-dep==2.0.0"}
        mock_installed.return_value = after
        mock_run.return_value = MagicMock(returncode=0, stdout="Successfully uninstalled\n", stderr="")

        ok, log = restore_packages("/fake/python", before)

        self.assertTrue(ok)
        uninstall_args = mock_run.call_args[0][0]
        self.assertEqual(uninstall_args[:4], ["/fake/python", "-m", "pip", "uninstall"])
        # transitive deps get removed too, since the diff is against actual
        # installed state, not just the package that was explicitly asked for
        self.assertEqual(set(uninstall_args[5:]), {"scipy", "scipy-extra-dep"})
        self.assertNotIn("numpy", uninstall_args)

    @patch("repair_tool.apply.installed_packages")
    def test_restore_is_a_noop_when_nothing_new_was_added(self, mock_installed):
        before = {"numpy==1.23.0"}
        mock_installed.return_value = before  # nothing changed
        with patch("subprocess.run") as mock_run:
            ok, log = restore_packages("/fake/python", before)
        mock_run.assert_not_called()
        self.assertTrue(ok)

    @patch("repair_tool.apply.installed_packages")
    @patch("subprocess.run", side_effect=OSError("no such file"))
    def test_restore_never_raises_on_missing_interpreter(self, _mock_run, mock_installed):
        mock_installed.return_value = {"numpy==1.23.0", "scipy==1.10.0"}
        ok, log = restore_packages("/fake/python", {"numpy==1.23.0"})
        self.assertFalse(ok)
        self.assertIn("no such file", log)

    @patch("repair_tool.apply.installed_packages")
    @patch("subprocess.run")
    def test_restore_reinstalls_a_downgraded_package_to_its_prior_version(self, mock_run, mock_installed):
        # The real bug this guards against: an env-fix candidate that
        # DOWNGRADES an already-installed package (e.g. PyYAML 6.x -> <5.1)
        # is not a "new" package in the freeze diff -- naively uninstalling
        # it by name would leave it missing entirely, worse than before.
        before = {"PyYAML==6.0.3"}
        after = {"PyYAML==5.0.0"}  # same package, downgraded version
        mock_installed.return_value = after
        mock_run.return_value = MagicMock(returncode=0, stdout="Successfully installed PyYAML-6.0.3\n", stderr="")

        ok, log = restore_packages("/fake/python", before)

        self.assertTrue(ok)
        self.assertEqual(mock_run.call_count, 1)  # no separate uninstall call happened
        install_args = mock_run.call_args[0][0]
        self.assertEqual(install_args, ["/fake/python", "-m", "pip", "install", "PyYAML==6.0.3"])


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
class TestRestorePackagesRealVenv(unittest.TestCase):
    """Real pip, real (temporary) venv: proves the actual mechanism behind
    PHASE5_IMPROVEMENTS_TASK.md #1 -- not just that the right subprocess
    calls happen, but that a losing candidate's packages genuinely vanish.
    """

    def setUp(self):
        import venv as venv_module

        self.tmp_venv_dir = tempfile.mkdtemp(prefix="test_restore_real_venv_")
        venv_module.EnvBuilder(with_pip=True).create(self.tmp_venv_dir)
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        exe = "python.exe" if sys.platform == "win32" else "python"
        self.python_exe = os.path.join(self.tmp_venv_dir, bin_dir, exe)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp_venv_dir, ignore_errors=True)

    def test_a_losing_candidates_install_is_genuinely_removed(self):
        before = installed_packages(self.python_exe)
        self.assertNotIn("six", {p.split("==")[0] for p in before})

        ok, _log = apply(Proposal(kind="install", package="six"), self.python_exe)
        self.assertTrue(ok)
        after_install = installed_packages(self.python_exe)
        self.assertIn("six", {p.split("==")[0] for p in after_install})

        ok, _log = restore_packages(self.python_exe, before)
        self.assertTrue(ok)
        after_restore = installed_packages(self.python_exe)
        self.assertNotIn("six", {p.split("==")[0] for p in after_restore})
        self.assertEqual(after_restore, before)

    def test_a_downgraded_package_is_restored_not_removed(self):
        # Reproduces the real bug found while verifying this fix: an
        # env-fix candidate that downgrades an already-installed package
        # (e.g. broken_examples/08's PyYAML<5.1 attempt) must come back to
        # its exact prior version, not vanish entirely.
        ok, _log = apply(Proposal(kind="install", package="six"), self.python_exe)
        self.assertTrue(ok)
        before = installed_packages(self.python_exe)
        six_before = next(p for p in before if p.startswith("six=="))

        # Downgrade six to an old version, simulating a losing env-fix candidate.
        ok, _log = apply(Proposal(kind="install", package="six==1.15.0"), self.python_exe)
        self.assertTrue(ok)
        after_downgrade = installed_packages(self.python_exe)
        self.assertIn("six==1.15.0", after_downgrade)
        self.assertNotEqual(six_before, "six==1.15.0")  # sanity: this really was a change

        ok, _log = restore_packages(self.python_exe, before)
        self.assertTrue(ok)
        after_restore = installed_packages(self.python_exe)
        # six is back at its original version -- not missing, not still downgraded
        self.assertIn(six_before, after_restore)
        self.assertNotIn("six==1.15.0", after_restore)


if __name__ == "__main__":
    unittest.main()
