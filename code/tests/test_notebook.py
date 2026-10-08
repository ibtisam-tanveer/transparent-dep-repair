import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nbformat
from nbformat.v4 import new_code_cell, new_notebook

from repair_tool import venv_manager
from repair_tool.apply import apply, apply_code_edit
from repair_tool.diagnose import diagnose_result
from repair_tool.loop import repair
from repair_tool.notebook import (
    _collect_stdout,
    _first_error_output,
    _format_error,
    _strip_ansi,
    edit_notebook_cells,
    run_notebook,
)
from repair_tool.repair import Proposal
from repair_tool.runner import run_project

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTEBOOKS = os.path.join(ROOT, "broken_examples", "notebooks")


def _write_notebook(path, cells_source):
    nb = new_notebook(cells=[new_code_cell(src) for src in cells_source])
    nbformat.write(nb, path)


class TestDispatch(unittest.TestCase):
    """run_project/apply_code_edit must dispatch to notebook.py for .ipynb
    and leave the .py path completely untouched."""

    @patch("repair_tool.notebook.run_notebook")
    def test_run_project_dispatches_ipynb_to_notebook_module(self, mock_run_notebook):
        from repair_tool.runner import RunResult

        mock_run_notebook.return_value = RunResult(ok=True, returncode=0, stdout="", stderr="")
        tmpdir = tempfile.mkdtemp()
        try:
            fake_nb = os.path.join(tmpdir, "x.ipynb")
            _write_notebook(fake_nb, ["print(1)"])
            run_project(fake_nb, timeout=30, python_exe="/fake/python")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        mock_run_notebook.assert_called_once_with(fake_nb, timeout=30, python_exe="/fake/python")

    def test_run_project_missing_ipynb_reported_before_dispatch(self):
        # The shared file-existence check in run_project must catch this
        # before ever reaching notebook.py -- same contract as .py targets.
        result = run_project(os.path.join(NOTEBOOKS, "does_not_exist.ipynb"))
        self.assertFalse(result.ok)
        self.assertIn("no such file", result.stderr.lower())

    @patch("repair_tool.notebook.edit_notebook_cells")
    def test_apply_code_edit_dispatches_ipynb_to_notebook_module(self, mock_edit):
        mock_edit.return_value = (True, "ok")
        edits = [{"find": "a", "replace": "b"}]
        apply_code_edit(edits, "some_workspace_copy.ipynb")
        mock_edit.assert_called_once_with(edits, "some_workspace_copy.ipynb")

    def test_apply_code_edit_py_path_unaffected(self):
        # A .py workspace path must still go through the plain-text branch,
        # not get accidentally routed to the notebook module.
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "x.py")
            with open(path, "w") as f:
                f.write("a = 1\n")
            ok, log = apply_code_edit([{"find": "a = 1", "replace": "a = 2"}], path)
            self.assertTrue(ok)
            with open(path) as f:
                self.assertEqual(f.read(), "a = 2\n")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestErrorExtractionHelpers(unittest.TestCase):
    """Fast, deterministic tests of the small pure helpers -- no kernel,
    no execution needed."""

    def test_strip_ansi_removes_color_codes(self):
        colored = "\x1b[31mModuleNotFoundError\x1b[39m: \x1b[36mNo module named 'x'\x1b[39m"
        self.assertEqual(_strip_ansi(colored), "ModuleNotFoundError: No module named 'x'")

    def test_strip_ansi_leaves_plain_text_untouched(self):
        plain = "AttributeError: module 'numpy' has no attribute 'float'"
        self.assertEqual(_strip_ansi(plain), plain)

    def test_format_error_produces_a_classifiable_clean_final_line(self):
        # A real, ANSI-colorized notebook error output, exactly the shape
        # nbclient actually produces (confirmed against the installed version).
        error = {
            "ename": "\x1b[31mModuleNotFoundError\x1b[39m",
            "evalue": "No module named 'seaborn'",
            "traceback": [
                "\x1b[31m---------------------------------------------------------------------------\x1b[39m",
                "\x1b[31mModuleNotFoundError\x1b[39m  Traceback (most recent call last)",
                "\x1b[36mCell\x1b[39m\x1b[36m \x1b[39m\x1b[32mIn[2]\x1b[39m, line 1",
                "\x1b[31mModuleNotFoundError\x1b[39m: No module named 'seaborn'",
            ],
        }
        stderr = _format_error(error)
        self.assertNotIn("\x1b", stderr)
        from repair_tool.diagnose import diagnose

        d = diagnose(stderr)
        self.assertEqual(d.kind, "missing_module")
        self.assertEqual(d.package, "seaborn")

    def test_collect_stdout_joins_stream_outputs_in_order(self):
        nb = new_notebook(
            cells=[
                {
                    "cell_type": "code",
                    "id": "cell-1",
                    "source": "print(1)",
                    "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}],
                    "execution_count": 1,
                    "metadata": {},
                },
                {
                    "cell_type": "code",
                    "id": "cell-2",
                    "source": "print(2)",
                    "outputs": [{"output_type": "stream", "name": "stdout", "text": "2\n"}],
                    "execution_count": 2,
                    "metadata": {},
                },
            ]
        )
        self.assertEqual(_collect_stdout(nb), "1\n2\n")

    def test_first_error_output_finds_the_first_one_in_order(self):
        nb = new_notebook(
            cells=[
                {"cell_type": "code", "id": "cell-1", "source": "ok", "outputs": [], "execution_count": 1, "metadata": {}},
                {
                    "cell_type": "code",
                    "id": "cell-2",
                    "source": "bad",
                    "outputs": [{"output_type": "error", "ename": "ValueError", "evalue": "x", "traceback": []}],
                    "execution_count": 2,
                    "metadata": {},
                },
            ]
        )
        error = _first_error_output(nb)
        self.assertIsNotNone(error)
        self.assertEqual(error["ename"], "ValueError")

    def test_first_error_output_none_when_no_cell_errors(self):
        nb = new_notebook(cells=[new_code_cell("print(1)")])
        self.assertIsNone(_first_error_output(nb))


class TestRunNotebookOffline(unittest.TestCase):
    """Real execution, but no network needed (ipykernel is already present
    in this dev .venv -- see pyproject.toml's dev extra)."""

    def test_clean_notebook_is_ok(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "clean.ipynb")
            _write_notebook(path, ["x = 1 + 1", "print(x)"])
            result = run_notebook(path, timeout=15)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertIn("2", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_stops_at_first_failing_cell(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "two_errors.ipynb")
            # cell 2 fails; cell 3 would also fail differently if it ever ran
            _write_notebook(path, ["print('cell1')", "raise ValueError('first')", "raise TypeError('second')"])
            result = run_notebook(path, timeout=15)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertFalse(result.ok)
        self.assertIn("cell1", result.stdout)
        self.assertIn("ValueError", result.stderr)
        self.assertNotIn("TypeError", result.stderr)  # cell 3 never ran

    def test_unparseable_notebook_file_is_a_clean_failure(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "not_a_notebook.ipynb")
            with open(path, "w") as f:
                f.write("{ this is not valid notebook json")
            result = run_notebook(path, timeout=10)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertFalse(result.ok)
        self.assertIn("NotebookReadError", result.stderr)

    def test_missing_notebook_file_does_not_crash(self):
        result = run_notebook("/no/such/notebook.ipynb", timeout=10)
        self.assertFalse(result.ok)
        self.assertIn("NotebookReadError", result.stderr)

    def test_missing_package_notebook_classifies_correctly(self):
        r = run_project(os.path.join(NOTEBOOKS, "missing_package.ipynb"))
        d = diagnose_result(r)
        self.assertFalse(r.ok)
        self.assertEqual(d.kind, "missing_module")
        self.assertEqual(d.package, "seaborn")

    def test_numpy_float_notebook_classifies_correctly(self):
        r = run_project(os.path.join(NOTEBOOKS, "numpy_float.ipynb"))
        d = diagnose_result(r)
        self.assertFalse(r.ok)
        self.assertEqual(d.kind, "module_attribute_removed")
        self.assertEqual(d.symbol, "float")

    def test_missing_data_file_notebook_fails_honestly_not_a_crash(self):
        r = run_project(os.path.join(NOTEBOOKS, "missing_data_file.ipynb"))
        self.assertFalse(r.ok)
        self.assertIn("FileNotFoundError", r.stderr)


class TestEditNotebookCells(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "target.ipynb")
        _write_notebook(self.path, ["import numpy as np", "x = np.float(3.14)\nprint(x)"])

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cell_sources(self):
        nb = nbformat.read(self.path, as_version=4)
        return [c["source"] for c in nb.cells]

    def test_edit_applies_to_the_correct_cell(self):
        ok, log = edit_notebook_cells([{"find": "np.float", "replace": "float"}], self.path)
        self.assertTrue(ok)
        sources = self._cell_sources()
        self.assertIn("float(3.14)", sources[1])
        self.assertNotIn("np.float", sources[1])
        self.assertEqual(sources[0], "import numpy as np")  # untouched

    def test_find_not_present_in_any_cell_is_a_clean_failure(self):
        original = self._cell_sources()
        ok, log = edit_notebook_cells([{"find": "this text is nowhere", "replace": "x"}], self.path)
        self.assertFalse(ok)
        self.assertIn("not found verbatim", log)
        self.assertEqual(self._cell_sources(), original)  # nothing written

    def test_no_partial_write_when_a_later_edit_fails(self):
        original = self._cell_sources()
        edits = [{"find": "np.float", "replace": "float"}, {"find": "not present anywhere", "replace": "x"}]
        ok, log = edit_notebook_cells(edits, self.path)
        self.assertFalse(ok)
        self.assertEqual(self._cell_sources(), original)  # first edit not persisted either

    def test_no_edits_is_a_clean_failure(self):
        ok, log = edit_notebook_cells([], self.path)
        self.assertFalse(ok)

    def test_missing_file_does_not_crash(self):
        ok, log = edit_notebook_cells([{"find": "x", "replace": "y"}], "/no/such/notebook.ipynb")
        self.assertFalse(ok)
        self.assertIn("could not read", log)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
class TestKernelIsolation(unittest.TestCase):
    """Proves execution actually happens under the *target* venv's kernel,
    not the host's -- the exact bug this test class exists to catch (see
    PHASE5 notebook-support debugging: a naive `km.kernel_cmd = [...]`
    attribute assignment was silently ignored, and every notebook secretly
    ran under this tool's own dev .venv instead)."""

    def setUp(self):
        self.tmp_venv_root = tempfile.mkdtemp(prefix="test_notebook_kernel_isolation_")
        self._orig_venv_root = venv_manager.VENV_ROOT
        venv_manager.VENV_ROOT = self.tmp_venv_root
        self.tmp_target_dir = tempfile.mkdtemp(prefix="test_notebook_kernel_isolation_target_")

    def tearDown(self):
        venv_manager.VENV_ROOT = self._orig_venv_root
        shutil.rmtree(self.tmp_venv_root, ignore_errors=True)
        shutil.rmtree(self.tmp_target_dir, ignore_errors=True)

    def test_package_present_only_in_target_venv_is_importable_there_and_not_on_host(self):
        marker_notebook = os.path.join(self.tmp_target_dir, "marker.ipynb")
        _write_notebook(marker_notebook, ["import seaborn; print(seaborn.__version__)"])

        # Fails under the host interpreter (this dev .venv never has seaborn --
        # see README.md, deliberately, so example 01 stays a real missing package).
        host_result = run_notebook(marker_notebook, timeout=30, python_exe=sys.executable)
        self.assertFalse(host_result.ok)
        self.assertIn("ModuleNotFoundError", host_result.stderr)

        # Now install seaborn into a real, fresh target venv...
        target_python = venv_manager.get_venv_python(marker_notebook)
        ok, log = apply(Proposal(kind="install", package="seaborn"), target_python)
        self.assertTrue(ok, msg=log)

        # ...and confirm the SAME notebook now succeeds when run against
        # THAT venv's kernel -- proving execution follows python_exe.
        target_result = run_notebook(marker_notebook, timeout=30, python_exe=target_python)
        self.assertTrue(target_result.ok, msg=target_result.stderr)
        self.assertIn(".", target_result.stdout)  # a version string was printed


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
class TestRepairNotebookIntegration(unittest.TestCase):
    """Real venv, real PyPI installs, and (for the numpy case) a real LLM
    call. Mirrors PHASE3/5's own guarded integration tests."""

    def setUp(self):
        self.tmp_venv_root = tempfile.mkdtemp(prefix="test_notebook_repair_venvs_")
        self._orig_venv_root = venv_manager.VENV_ROOT
        venv_manager.VENV_ROOT = self.tmp_venv_root

    def tearDown(self):
        venv_manager.VENV_ROOT = self._orig_venv_root
        shutil.rmtree(self.tmp_venv_root, ignore_errors=True)

    def test_required_behaviour_missing_package_notebook(self):
        target = os.path.join(NOTEBOOKS, "missing_package.ipynb")
        r = run_project(target)
        self.assertFalse(r.ok)
        self.assertEqual(diagnose_result(r).kind, "missing_module")

        result = repair(target)
        self.assertTrue(result.fixed)

    @unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
    def test_numpy_float_notebook_fixed_end_to_end(self):
        target = os.path.join(NOTEBOOKS, "numpy_float.ipynb")
        with open(target, encoding="utf-8") as f:
            original_content = f.read()

        result = repair(target)

        self.assertTrue(result.fixed)
        self.assertEqual(result.attempts[-1].proposal.strategy_won, "code")

        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), original_content)  # original .ipynb never touched

    def test_missing_data_file_notebook_reported_honestly(self):
        target = os.path.join(NOTEBOOKS, "missing_data_file.ipynb")
        result = repair(target)
        self.assertFalse(result.fixed)
        # Never crashed getting here, and never claimed a false success --
        # that's the actual requirement; the exact wording may vary by
        # whichever candidate the LLM tries first.
        self.assertGreaterEqual(len(result.attempts), 1)


if __name__ == "__main__":
    unittest.main()
