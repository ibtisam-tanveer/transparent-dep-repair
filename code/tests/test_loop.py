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
from repair_tool.llm import FixCandidate
from repair_tool.loop import repair
from repair_tool.repair import HardCaseProposal, Proposal
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
    def test_missing_module_unhandled_kind_no_install_attempted(self, mock_run, mock_apply, _mock_venv):
        # A resolvable missing_module is the only kind the Phase 3 rule-based
        # path can ever say "not handled yet" for now (module_attribute_removed
        # etc. moved to the Phase 5 LLM path -- see the hard-case tests below).
        mock_run.return_value = _fail("ModuleNotFoundError: No module named 'nonexistent_xyz123'")

        with patch("repair_tool.pypi.package_exists", return_value=False):
            result = repair(os.path.join(EXAMPLES, "01_missing_package.py"))

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


@patch("repair_tool.loop.get_venv_python", return_value="/fake/python")
class TestRepairLoopHardCasesOffline(unittest.TestCase):
    """Phase 5's dual-candidate path, fully mocked: no real LLM call, no
    real venv, no real pip install. apply_code_edit and get_workspace_copy
    stay real (they're pure file operations) so file-safety guarantees are
    genuinely exercised, not just assumed.
    """

    ATTR_STDERR = "AttributeError: module 'numpy' has no attribute 'float'"

    @patch("repair_tool.loop.apply_code_edit")
    @patch("repair_tool.loop.propose_hard_case")
    @patch("repair_tool.loop.run_project")
    def test_code_fix_wins_on_first_try(self, mock_run, mock_propose_hard, mock_edit, _mock_venv):
        mock_run.side_effect = [_fail(self.ATTR_STDERR), _ok("3.14\n")]
        mock_propose_hard.return_value = HardCaseProposal(
            understanding="np.float removed",
            code_fix=FixCandidate(applicable=True, reason="use the builtin", edits=[{"find": "a", "replace": "b"}]),
            env_fix=FixCandidate(applicable=True, reason="pin numpy", package="numpy", constraint="<1.24"),
            order=("code", "environment"),
            model="gpt-4o-mini",
        )
        mock_edit.return_value = (True, "applied")

        result = repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        self.assertTrue(result.fixed)
        self.assertEqual(len(result.attempts), 1)
        winner = result.attempts[0].proposal
        self.assertEqual(winner.strategy_won, "code")
        self.assertEqual(winner.kind, "code_edit")
        self.assertNotEqual(winner.alternatives, "")  # the untried env candidate is recorded
        self.assertEqual(winner.model, "gpt-4o-mini")  # PHASE5_IMPROVEMENTS_TASK.md #2

    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.apply_code_edit")
    @patch("repair_tool.loop.propose_hard_case")
    @patch("repair_tool.loop.run_project")
    def test_code_fix_fails_to_apply_falls_back_to_environment(
        self, mock_run, mock_propose_hard, mock_edit, mock_apply, _mock_venv
    ):
        mock_run.side_effect = [_fail(self.ATTR_STDERR), _ok("3.14\n")]
        mock_propose_hard.return_value = HardCaseProposal(
            code_fix=FixCandidate(applicable=True, reason="use the builtin", edits=[{"find": "a", "replace": "b"}]),
            env_fix=FixCandidate(applicable=True, reason="pin numpy", package="numpy", constraint="<1.24"),
            order=("code", "environment"),
        )
        mock_edit.return_value = (False, "edit 1 failed to apply: 'a' not found verbatim in source")
        mock_apply.return_value = (True, "installed numpy<1.24")

        result = repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        self.assertTrue(result.fixed)
        winner = result.attempts[0].proposal
        self.assertEqual(winner.strategy_won, "environment")
        self.assertEqual(winner.package, "numpy")
        self.assertIn("code fix: failed to apply", winner.alternatives)
        # apply() was called with the constraint folded into the package spec
        apply_args = mock_apply.call_args[0]
        self.assertEqual(apply_args[0].package, "numpy<1.24")

    @patch("repair_tool.loop.restore_packages")
    @patch("repair_tool.loop.installed_packages")
    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.apply_code_edit")
    @patch("repair_tool.loop.propose_hard_case")
    @patch("repair_tool.loop.run_project")
    def test_losing_environment_candidate_is_restored_before_the_next_candidate(
        self, mock_run, mock_propose_hard, mock_edit, mock_apply, mock_installed, mock_restore, _mock_venv
    ):
        # PHASE5_IMPROVEMENTS_TASK.md #1: environment tried first (order),
        # fails verification -> must be restored *before* code is tried, so
        # code's own result can never be attributed to leftover env changes.
        mock_run.side_effect = [_fail(self.ATTR_STDERR), _fail(self.ATTR_STDERR), _ok("3.14\n")]
        mock_propose_hard.return_value = HardCaseProposal(
            code_fix=FixCandidate(applicable=True, reason="use the builtin", edits=[{"find": "a", "replace": "b"}]),
            env_fix=FixCandidate(applicable=True, reason="pin numpy", package="numpy", constraint="<1.24"),
            order=("environment", "code"),
            model="gpt-4o-mini",
        )
        snapshot_before_env_attempt = {"numpy==1.26.0"}
        mock_installed.return_value = snapshot_before_env_attempt
        mock_apply.return_value = (True, "installed numpy<1.24")
        mock_edit.return_value = (True, "applied")

        result = repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        self.assertTrue(result.fixed)
        self.assertEqual(result.attempts[0].proposal.strategy_won, "code")
        # the snapshot was taken before the env attempt, and restore was
        # called with exactly that snapshot once it failed verification --
        # not called at all for the code attempt, which doesn't touch packages
        mock_installed.assert_called_once_with(_mock_venv.return_value)
        mock_restore.assert_called_once_with(_mock_venv.return_value, snapshot_before_env_attempt)

    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.apply_code_edit")
    @patch("repair_tool.loop.propose_hard_case")
    @patch("repair_tool.loop.run_project")
    def test_neither_candidate_verifies_reports_not_fixed_honestly(
        self, mock_run, mock_propose_hard, mock_edit, mock_apply, _mock_venv
    ):
        mock_run.side_effect = [_fail(self.ATTR_STDERR), _fail(self.ATTR_STDERR), _fail(self.ATTR_STDERR)]
        mock_propose_hard.return_value = HardCaseProposal(
            code_fix=FixCandidate(applicable=True, reason="use the builtin", edits=[{"find": "a", "replace": "b"}]),
            env_fix=FixCandidate(applicable=True, reason="pin numpy", package="numpy", constraint="<1.24"),
            order=("code", "environment"),
        )
        mock_edit.return_value = (True, "applied")
        mock_apply.return_value = (True, "installed numpy<1.24")

        result = repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        self.assertFalse(result.fixed)
        self.assertEqual(len(result.attempts), 1)
        p = result.attempts[0].proposal
        self.assertEqual(p.kind, "none")
        self.assertFalse(result.attempts[0].applied)
        self.assertIn("code fix", p.alternatives)
        self.assertIn("environment fix", p.alternatives)
        self.assertIn("still failed after re-run", p.alternatives)

    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.apply_code_edit")
    @patch("repair_tool.loop.propose_hard_case")
    @patch("repair_tool.loop.run_project")
    def test_llm_unavailable_reports_not_fixed_no_candidates_attempted(
        self, mock_run, mock_propose_hard, mock_edit, mock_apply, _mock_venv
    ):
        mock_run.return_value = _fail(self.ATTR_STDERR)
        mock_propose_hard.return_value = HardCaseProposal(error="OPENAI_API_KEY is not set.")

        result = repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        mock_edit.assert_not_called()
        mock_apply.assert_not_called()
        self.assertFalse(result.fixed)
        self.assertIn("LLM unavailable", result.attempts[0].proposal.alternatives)

    @patch("repair_tool.loop.propose_hard_case")
    @patch("repair_tool.loop.run_project")
    def test_original_input_file_is_never_mutated_by_a_code_fix(self, mock_run, mock_propose_hard, _mock_venv):
        # Real apply_code_edit + real get_workspace_copy, against a scratch
        # copy of a broken example (never the repo's own file), with a
        # temporary VENV_ROOT so nothing leaks into the real .repair_venvs/.
        tmp_target_dir = tempfile.mkdtemp(prefix="test_no_mutation_target_")
        tmp_venv_root = tempfile.mkdtemp(prefix="test_no_mutation_venvs_")
        target = os.path.join(tmp_target_dir, "target.py")
        shutil.copyfile(os.path.join(EXAMPLES, "02_numpy_float.py"), target)
        with open(target, encoding="utf-8") as f:
            original_content = f.read()

        mock_run.side_effect = [_fail(self.ATTR_STDERR), _ok("3.14\n")]
        mock_propose_hard.return_value = HardCaseProposal(
            code_fix=FixCandidate(applicable=True, reason="use the builtin", edits=[{"find": "np.float", "replace": "float"}]),
            env_fix=FixCandidate(applicable=False, reason="not needed"),
            order=("code", "environment"),
        )

        try:
            with patch.object(venv_manager, "VENV_ROOT", tmp_venv_root):
                result = repair(target)
            # The real check: re-read the original input file *after*
            # repair() ran, while it still exists, and compare to what was
            # there before -- this is what actually proves it was never
            # touched (comparing against a pre-captured string proves nothing,
            # since that string can't change regardless of what repair() did).
            with open(target, encoding="utf-8") as f:
                content_after = f.read()
        finally:
            shutil.rmtree(tmp_target_dir, ignore_errors=True)
            shutil.rmtree(tmp_venv_root, ignore_errors=True)

        self.assertTrue(result.fixed)
        self.assertEqual(content_after, original_content)
        # And confirm the fix genuinely happened -- just not to this file.
        self.assertEqual(result.attempts[0].proposal.edits, [{"find": "np.float", "replace": "float"}])

    @patch("repair_tool.loop.apply_code_edit")
    @patch("repair_tool.loop.propose_hard_case")
    @patch("repair_tool.loop.apply")
    @patch("repair_tool.loop.propose")
    @patch("repair_tool.loop.run_project")
    def test_layered_fix_install_then_llm_fix_the_error_revealed_underneath(
        self, mock_run, mock_propose, mock_apply, mock_propose_hard, mock_edit, _mock_venv
    ):
        # Exactly the scenario PHASE5_TASK.md calls out: numpy itself is
        # missing at first (Phase 3's rule-based path installs it), and only
        # once that's done does the real np.float AttributeError show up
        # (Phase 5's LLM path fixes that one).
        mock_run.side_effect = [
            _fail("ModuleNotFoundError: No module named 'numpy'"),  # iteration 1: layer 1
            _fail("AttributeError: module 'numpy' has no attribute 'float'"),  # iteration 2 top: layer 2
            _ok("3.14\n"),  # verification inside _resolve_hard_case
        ]
        mock_propose.return_value = Proposal(kind="install", package="numpy", confidence="medium")
        mock_apply.return_value = (True, "installed numpy")
        mock_propose_hard.return_value = HardCaseProposal(
            code_fix=FixCandidate(applicable=True, reason="use the builtin", edits=[{"find": "a", "replace": "b"}]),
            env_fix=FixCandidate(applicable=False, reason="not needed"),
            order=("code", "environment"),
        )
        mock_edit.return_value = (True, "applied")

        result = repair(os.path.join(EXAMPLES, "02_numpy_float.py"))

        self.assertTrue(result.fixed)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.attempts[0].proposal.kind, "install")  # layer 1: Phase 3 path
        self.assertEqual(result.attempts[1].proposal.strategy_won, "code")  # layer 2: Phase 5 path


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

    @unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
    def test_llm_fixes_removed_numpy_api_end_to_end(self):
        # PHASE5_TASK.md's literal "Required behaviour" example: a real
        # LLM call, a real (temporary) venv, a real edited working copy.
        target = os.path.join(EXAMPLES, "02_numpy_float.py")
        with open(target, encoding="utf-8") as f:
            original_content = f.read()

        result = repair(target)

        self.assertIs(result.fixed, True)
        self.assertIn(result.attempts[-1].proposal.strategy_won, ("code", "environment"))
        self.assertTrue(result.attempts[-1].proposal.alternatives)

        # The original example file in the repo must be untouched.
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), original_content)

    @unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
    def test_llm_fixes_non_numpy_pandas_case_end_to_end(self):
        # PHASE5_IMPROVEMENTS_TASK.md #3: a generalisation smoke test outside
        # the numpy/pandas cases -- dict.has_key(), a language-level (not
        # library-level) removed API with no sensible environment fix.
        result = repair(os.path.join(EXAMPLES, "09_dict_has_key.py"))

        self.assertIs(result.fixed, True)
        self.assertEqual(result.attempts[-1].proposal.strategy_won, "code")


if __name__ == "__main__":
    unittest.main()
