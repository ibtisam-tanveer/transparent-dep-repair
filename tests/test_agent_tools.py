import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.agent_tools import (
    TOOL_SPECS,
    build_dispatch,
    diagnose_error,
    edit_code,
    install_package,
    lookup_package,
    run_target,
    verify,
)
from repair_tool.runner import RunResult

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "broken_examples")


class TestRunTarget(unittest.TestCase):
    @patch("repair_tool.agent_tools.run_project")
    def test_wraps_run_project_as_a_plain_dict(self, mock_run):
        mock_run.return_value = RunResult(ok=False, returncode=1, stdout="", stderr="boom")
        result = run_target("some/path.py", python_exe="/fake/python", timeout=30)
        self.assertEqual(result, {"ok": False, "returncode": 1, "stdout": "", "stderr": "boom"})
        mock_run.assert_called_once_with("some/path.py", timeout=30, python_exe="/fake/python")


class TestVerify(unittest.TestCase):
    @patch("repair_tool.agent_tools.run_project")
    def test_verify_is_just_another_run_target_call(self, mock_run):
        mock_run.return_value = RunResult(ok=True, returncode=0, stdout="ok\n", stderr="")
        result = verify("some/path.py", python_exe="/fake/python")
        self.assertTrue(result["ok"])


class TestDiagnoseError(unittest.TestCase):
    def test_classifies_a_real_run_result_dict(self):
        run_result = {
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "ModuleNotFoundError: No module named 'seaborn'",
        }
        diagnosis = diagnose_error(run_result)
        self.assertEqual(diagnosis["kind"], "missing_module")
        self.assertEqual(diagnosis["module"], "seaborn")

    def test_an_ok_run_result_diagnoses_as_none(self):
        run_result = {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
        diagnosis = diagnose_error(run_result)
        self.assertEqual(diagnosis["kind"], "none")


class TestLookupPackage(unittest.TestCase):
    @patch("repair_tool.agent_tools.pypi.latest_version", return_value="0.13.0")
    @patch("repair_tool.agent_tools.pypi.resolve_package_name", return_value="seaborn")
    def test_resolved_package_reports_exists_and_latest_version(self, mock_resolve, mock_latest):
        result = lookup_package("seaborn")
        self.assertEqual(
            result,
            {"import_name": "seaborn", "resolved_package": "seaborn", "exists": True, "latest_version": "0.13.0"},
        )

    @patch("repair_tool.agent_tools.pypi.resolve_package_name", return_value=None)
    def test_unresolved_package_reports_exists_false_without_a_version_lookup(self, mock_resolve):
        result = lookup_package("not_a_real_package_xyz")
        self.assertEqual(
            result,
            {"import_name": "not_a_real_package_xyz", "resolved_package": None, "exists": False, "latest_version": None},
        )


class TestInstallPackage(unittest.TestCase):
    @patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "Successfully installed seaborn"))
    def test_wraps_apply_with_an_install_proposal(self, mock_apply):
        result = install_package("seaborn", "/fake/python")
        self.assertEqual(result, {"ok": True, "log": "Successfully installed seaborn"})
        proposal = mock_apply.call_args.args[0]
        self.assertEqual(proposal.kind, "install")
        self.assertEqual(proposal.package, "seaborn")
        self.assertEqual(mock_apply.call_args.args[1], "/fake/python")


class TestEditCode(unittest.TestCase):
    def test_applies_a_real_find_replace_edit_to_a_real_file(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "target.py")
            with open(path, "w") as f:
                f.write("x = np.float(3.14)\n")

            result = edit_code([{"find": "np.float", "replace": "float"}], path)

            self.assertTrue(result["ok"])
            with open(path) as f:
                self.assertEqual(f.read(), "x = float(3.14)\n")
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_a_find_string_not_present_fails_cleanly(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "target.py")
            with open(path, "w") as f:
                f.write("x = 1\n")
            result = edit_code([{"find": "not there", "replace": "x"}], path)
            self.assertFalse(result["ok"])
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)


class TestToolSpecs(unittest.TestCase):
    def test_every_spec_is_a_well_formed_openai_function_tool(self):
        for spec in TOOL_SPECS:
            self.assertEqual(spec["type"], "function")
            fn = spec["function"]
            self.assertTrue(fn["name"])
            self.assertTrue(fn["description"])
            self.assertIn("parameters", fn)

    def test_dispatch_has_a_callable_for_every_tool_spec(self):
        dispatch = build_dispatch("/fake/workspace.py", "/fake/python")
        spec_names = {spec["function"]["name"] for spec in TOOL_SPECS}
        self.assertEqual(spec_names, set(dispatch.keys()))


class TestBuildDispatch(unittest.TestCase):
    @patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "ok"))
    def test_install_package_dispatch_binds_the_session_python_exe(self, mock_apply):
        dispatch = build_dispatch("/fake/workspace.py", "/fake/python")
        dispatch["install_package"](package="seaborn")
        self.assertEqual(mock_apply.call_args.args[1], "/fake/python")

    @patch("repair_tool.agent_tools.run_project")
    def test_run_target_dispatch_binds_the_session_workspace_path(self, mock_run):
        mock_run.return_value = RunResult(ok=True, returncode=0, stdout="", stderr="")
        dispatch = build_dispatch("/fake/workspace.py", "/fake/python")
        dispatch["run_target"]()
        mock_run.assert_called_once_with("/fake/workspace.py", timeout=60, python_exe="/fake/python")


if __name__ == "__main__":
    unittest.main()
