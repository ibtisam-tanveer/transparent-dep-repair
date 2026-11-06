"""repair_tool/inproc_runner.py: the ZeroMQ-free notebook executor used in the
linux/amd64 container (a kernel cannot spawn a child process there)."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nbformat
from nbformat.v4 import new_code_cell, new_notebook

from repair_tool.diagnose import diagnose
from repair_tool.notebook import EXECUTOR_ENV, run_notebook


def _nb(tmp, sources, name="t.ipynb", stale=False):
    cells = [new_code_cell(s) for s in sources]
    if stale:
        for i, c in enumerate(cells, 1):
            c["execution_count"] = 100 + i
    path = os.path.join(tmp, name)
    nbformat.write(new_notebook(cells=cells), path)
    return path


def _run(path, timeout=20, **kw):
    with patch.dict(os.environ, {EXECUTOR_ENV: "inproc"}):
        return run_notebook(path, timeout=timeout, **kw)


class TestInprocExecutor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_passing_notebook_shares_a_namespace_across_cells(self):
        result = _run(_nb(self.tmp, ["x = 41", "print('answer', x + 1)"]))
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("answer 42", result.stdout)

    def test_an_error_has_the_same_final_line_shape_the_diagnoser_parses(self):
        result = _run(_nb(self.tmp, ["print('before')", "import definitely_not_installed_xyz", "print('never')"]))
        self.assertFalse(result.ok)
        self.assertIn("before", result.stdout)
        self.assertNotIn("never", result.stdout)
        self.assertEqual(result.stderr.strip().splitlines()[-1], "ModuleNotFoundError: No module named 'definitely_not_installed_xyz'")
        diagnosis = diagnose(result.stderr)
        self.assertEqual((diagnosis.kind, diagnosis.module), ("missing_module", "definitely_not_installed_xyz"))
        self.assertEqual(result.progress, "last started code cell 2 of 3")
        self.assertIn("definitely_not_installed_xyz", result.stuck_cell)

    def test_a_syntax_error_is_reported_not_crashed(self):
        result = _run(_nb(self.tmp, ["def broken(:"]))
        self.assertFalse(result.ok)
        self.assertIn("SyntaxError", result.stderr)

    def test_a_hung_cell_times_out_at_the_cell_limit_with_the_cell_preview(self):
        started = time.time()
        result = _run(_nb(self.tmp, ["x = 1", "import time\nmarker_hung_cell = 1\ntime.sleep(60)"]), timeout=2)
        self.assertLess(time.time() - started, 30)
        self.assertFalse(result.ok)
        self.assertIn("CellTimeoutError", result.stderr)
        self.assertIn("marker_hung_cell", result.stderr)  # the same preview nbclient puts in its message
        self.assertEqual(result.progress, "last started code cell 2 of 2")

    def test_user_code_cannot_swallow_the_timeout(self):
        src = "import time\ntry:\n    time.sleep(60)\nexcept Exception:\n    print('swal' + 'lowed')"
        result = _run(_nb(self.tmp, [src]), timeout=2)
        self.assertFalse(result.ok)
        self.assertIn("CellTimeoutError", result.stderr)
        self.assertNotIn("swallowed", result.stdout)
        self.assertNotIn("Traceback", result.stdout)  # the traceback goes to the error text, not stdout
        self.assertNotIn("\x1b[", result.stdout)

    def test_child_processes_work_the_thing_a_kernel_cannot_do_under_rosetta(self):
        result = _run(_nb(self.tmp, [
            "!echo from_shell_escape",
            "%%bash\necho from_bash_magic",
            "import subprocess; print(subprocess.run(['echo','from_subprocess'], capture_output=True, text=True).stdout)",
            "import os; os.system('echo from_os_system')",
        ]))
        self.assertTrue(result.ok, result.stderr)
        for expected in ("from_shell_escape", "from_bash_magic", "from_subprocess"):
            self.assertIn(expected, result.stdout)

    def test_matplotlib_runs_headless(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("matplotlib not installed in the test environment")
        result = _run(_nb(self.tmp, ["import matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.show()\nprint('plotted')"]))
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("plotted", result.stdout)

    def test_the_working_directory_and_local_imports_behave_like_a_kernel(self):
        os.makedirs(os.path.join(self.tmp, "pkgdir"))
        with open(os.path.join(self.tmp, "pkgdir", "helper_mod.py"), "w") as f:
            f.write("VALUE = 7\n")
        path = _nb(self.tmp, ["import os, sys\nsys.path.append('pkgdir')\nimport helper_mod\nprint('VALUE', helper_mod.VALUE, os.path.basename(os.getcwd()))"])
        self.assertTrue(_run(path).ok)
        result = _run(path, cwd=self.tmp)
        self.assertIn("VALUE 7", result.stdout)

    def test_stale_counts_and_outputs_saved_in_the_notebook_are_irrelevant(self):
        path = _nb(self.tmp, ["x = 1", "import time\nreal_stuck = 1\ntime.sleep(60)", "later = 1"], stale=True)
        result = _run(path, timeout=2)
        self.assertEqual(result.progress, "last started code cell 2 of 3")
        self.assertIn("real_stuck", result.stuck_cell)

    def test_a_cell_that_ignores_the_alarm_is_killed_by_the_group_backstop(self):
        """A C-level hang cannot be interrupted from inside; the caller's
        process-group kill at the total budget is what ends it, and the
        position is still known from the progress file."""
        src = "import signal, time\nsignal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})\nblocked_cell = 1\ntime.sleep(120)"
        started = time.time()
        result = _run(_nb(self.tmp, ["x = 1", src]), timeout=2, total_timeout=8)
        self.assertLess(time.time() - started, 60)
        self.assertFalse(result.ok)
        self.assertIn("NotebookHardTimeout", result.stderr)
        self.assertEqual(result.progress, "last started code cell 2 of 2")
        self.assertIn("blocked_cell", result.stuck_cell)

    def test_a_missing_notebook_file_is_a_clean_failure(self):
        result = _run(os.path.join(self.tmp, "nope.ipynb"))
        self.assertFalse(result.ok)

    def test_the_default_executor_is_still_the_kernel(self):
        with patch.dict(os.environ, {}, clear=True), patch("repair_tool.notebook._run_notebook_inproc") as inproc:
            path = _nb(self.tmp, ["print(1)"])
            result = run_notebook(path, timeout=30)
        inproc.assert_not_called()
        self.assertTrue(result.ok, result.stderr)


if __name__ == "__main__":
    unittest.main()


class TestMatplotlibMagics(unittest.TestCase):
    """Found in the first docker pilot: three notebooks failed with
    'NotImplementedError: Implement enable_gui in a subclass' -- the base
    InteractiveShell cannot activate a GUI the way a kernel's shell can."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("matplotlib not installed in the test environment")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matplotlib_inline_magic_does_not_fail(self):
        result = _run(_nb(self.tmp, ["%matplotlib inline", "import matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.show()\nprint('ok')"]))
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_other_matplotlib_magics_do_not_fail(self):
        for magic in ("%matplotlib notebook", "%matplotlib widget", "%matplotlib", "%pylab inline"):
            with self.subTest(magic=magic):
                result = _run(_nb(self.tmp, [magic, "print('after')"], name=f"m{abs(hash(magic))}.ipynb"))
                self.assertNotIn("enable_gui", result.stderr)


class TestExceptionNameMatchesAKernel(unittest.TestCase):
    def test_a_non_builtin_exception_is_reported_with_its_bare_name_like_a_kernel(self):
        tmp = tempfile.mkdtemp()
        try:
            result = _run(_nb(tmp, ["import subprocess\nsubprocess.run(['sh','-c','exit 3'], check=True)"]))
            self.assertFalse(result.ok)
            last = result.stderr.strip().splitlines()[-1]
            self.assertTrue(last.startswith("CalledProcessError: "), last)
            self.assertNotIn("subprocess.CalledProcessError", last)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_builtin_exceptions_are_unchanged(self):
        tmp = tempfile.mkdtemp()
        try:
            result = _run(_nb(tmp, ["raise ValueError('boom')"]))
            self.assertEqual(result.stderr.strip().splitlines()[-1], "ValueError: boom")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
