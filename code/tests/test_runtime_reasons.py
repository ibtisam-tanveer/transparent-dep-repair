import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.runtime_reasons import classify_runtime_reason, reason_group


def _c(exception_line, stderr="", stdout="", category="E", kind="unknown", local=False):
    return classify_runtime_reason(exception_line, stderr or exception_line or "", stdout, category, kind, local)


class TestClassifyRuntimeReason(unittest.TestCase):
    def test_a_dependency_failure_is_labelled_by_its_diagnosis_kind(self):
        self.assertEqual(_c("ModuleNotFoundError: No module named 'x'", category="A", kind="missing_module"), "dependency:missing_module")

    def test_a_dependency_failure_is_never_relabelled_environmental_for_mentioning_a_url(self):
        line = "AttributeError: module 'numpy' has no attribute 'float'"
        stderr = line + "\n  see https://numpy.org/devdocs/release/1.20.0-notes.html"
        self.assertEqual(_c(line, stderr, category="D", kind="module_attribute_removed"), "dependency:module_attribute_removed")

    def test_local_module_wins(self):
        self.assertEqual(_c("ModuleNotFoundError: No module named 'combat'", category="", local=True), "local_module")

    def test_a_cell_timeout_in_a_download_cell(self):
        stderr = ("CellTimeoutError: A cell timed out while it was being executed, after 60 seconds.\n"
                  "Here is a preview of the cell contents:\n[\"google_data_url = 'https://www.gstatic.com/x.csv'\"]")
        self.assertEqual(_c("CellTimeoutError: A cell timed out", stderr), "cell_timeout_download")

    def test_a_download_announced_only_in_stdout_still_counts(self):
        self.assertEqual(
            _c("CellTimeoutError: A cell timed out while it was being executed", stdout="Downloading data..."),
            "cell_timeout_download",
        )

    def test_a_plain_cell_timeout(self):
        self.assertEqual(_c("CellTimeoutError: A cell timed out while it was being executed, after 60 seconds."), "cell_timeout")

    def test_hard_timeout(self):
        self.assertEqual(_c("NotebookHardTimeout: the notebook exceeded its 300s total budget"), "notebook_hard_timeout")

    def test_missing_data_file(self):
        self.assertEqual(_c("FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'"), "missing_data_file")

    def test_a_line_number_401_in_a_traceback_is_not_credentials(self):
        stderr = "File \"x.py\", line 401, in f\nKeyError: 'a'"
        self.assertEqual(_c("KeyError: 'a'", stderr), "code_error:KeyError")

    def test_network_gpu_credentials_memory(self):
        self.assertEqual(_c("urllib.error.URLError: <urlopen error [Errno 8] nodename nor servname provided>"), "network_error")
        self.assertEqual(_c("RuntimeError: Found no NVIDIA driver on your system."), "gpu_required")
        self.assertEqual(_c("botocore.exceptions.NoCredentialsError: Unable to locate credentials"), "credentials")
        self.assertEqual(_c("MemoryError: Unable to allocate 4.00 GiB"), "out_of_memory")

    def test_dead_kernel(self):
        self.assertEqual(_c("DeadKernelError: Kernel died"), "kernel_died")

    def test_unrecognised_errors_are_reported_as_code_errors_not_forced_into_a_friendlier_bucket(self):
        self.assertEqual(_c("ZeroDivisionError: division by zero"), "code_error:ZeroDivisionError")

    def test_nothing_observed(self):
        self.assertEqual(classify_runtime_reason(None, "", "", "", "", False), "unobserved")


class TestReasonGroup(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(reason_group("dependency:missing_module"), "dependency")
        self.assertEqual(reason_group("local_module"), "local_module")
        self.assertEqual(reason_group("gpu_required"), "needs_data_compute_or_network")
        self.assertEqual(reason_group("cell_timeout_download"), "needs_data_compute_or_network")
        self.assertEqual(reason_group("code_error:KeyError"), "code_error")
        self.assertEqual(reason_group(""), "unobserved")


class TestRowCarriesTheObservedError(unittest.TestCase):
    def _row(self, trace, fixed=False):
        from evaluation.run_eval import build_result_row
        from repair_tool.agent import AgentResult

        result = AgentResult(target="a.ipynb", fixed=fixed, trace=trace)
        return build_result_row(
            "x/y", "u", {"notebook_path": "a.ipynb", "category": "A", "tier": "confirmed"}, result, {}, "run1", 30, False,
            repo_path="/nonexistent",
        )

    def _step(self, stderr, stdout=""):
        from repair_tool.agent import TraceStep

        return TraceStep(
            thought="", tool_called="run_target", tool_input={},
            tool_result={"ok": False, "returncode": -1, "stdout": stdout, "stderr": stderr},
            grounding="deterministic", verification="n/a",
        )

    def test_an_out_of_scope_row_records_what_and_why(self):
        row = self._row([self._step("Traceback...\nFileNotFoundError: [Errno 2] No such file or directory: 'd.csv'")])
        self.assertEqual(row["runtime_observed_category"], "E")
        self.assertEqual(row["runtime_observed_reason"], "missing_data_file")
        self.assertIn("d.csv", row["runtime_observed_error"])

    def test_the_error_is_truncated(self):
        row = self._row([self._step("ValueError: " + "x" * 1000)])
        self.assertLessEqual(len(row["runtime_observed_error"]), 300)

    def test_a_fixed_row_has_no_observed_error(self):
        row = self._row([], fixed=True)
        self.assertEqual((row["runtime_observed_error"], row["runtime_observed_reason"]), ("", ""))

    def test_a_row_with_no_run_records_unobserved(self):
        row = self._row([])
        self.assertEqual(row["runtime_observed_reason"], "unobserved")


class TestNotebookProgressInstrumentation(unittest.TestCase):
    def test_progress_and_stuck_cell_reach_the_row(self):
        from evaluation.run_eval import build_result_row
        from repair_tool.agent import AgentResult, TraceStep

        step = TraceStep(
            thought="", tool_called="run_target", tool_input={},
            tool_result={"ok": False, "returncode": -1, "stdout": "", "stderr": "CellTimeoutError: A cell timed out while it was being executed, after 180 seconds.",
                         "progress": "last started code cell 2 of 40", "stuck_cell": "df = pd.read_csv('https://example.org/big.csv')"},
            grounding="deterministic", verification="n/a",
        )
        row = build_result_row("x/y", "u", {"notebook_path": "a.ipynb", "category": "A", "tier": "confirmed"},
                               AgentResult(target="a.ipynb", fixed=False, trace=[step]), {}, "run1", 30, False, repo_path="/nonexistent")
        self.assertEqual(row["runtime_observed_progress"], "last started code cell 2 of 40")
        self.assertIn("read_csv", row["runtime_observed_cell"])
        self.assertEqual(row["runtime_observed_reason"], "cell_timeout_download")  # the STUCK CELL fetches a URL
        self.assertIn("CellTimeoutError", row["runtime_observed_detail"])

    def test_a_plain_compute_cell_stays_a_plain_timeout(self):
        self.assertEqual(
            classify_runtime_reason("CellTimeoutError: A cell timed out while it was being executed, after 180 seconds.",
                                    "CellTimeoutError: A cell timed out", "", "E", "unknown", False, cell_source="model.fit(X, y, epochs=500)"),
            "cell_timeout",
        )


class TestCloneRetry(unittest.TestCase):
    def _completed(self, rc, err=""):
        import subprocess
        return subprocess.CompletedProcess([], rc, "", err)

    def test_a_transient_failure_is_retried_then_succeeds(self):
        from unittest.mock import patch
        from evaluation import run_eval

        results = [self._completed(128, "error: RPC failed; curl 56 Recv failure: Connection reset by peer\nfatal: early EOF"),
                   self._completed(0)]
        with patch.object(run_eval, "run_group", side_effect=results) as run, patch.object(run_eval.time, "sleep") as sleep:
            path, log = run_eval.clone_repo("https://example.org/r.git")
        self.assertIsNotNone(path)
        self.assertIn("attempt 2", log)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()

    def test_a_missing_repository_is_not_retried(self):
        from unittest.mock import patch
        from evaluation import run_eval

        with patch.object(run_eval, "run_group", return_value=self._completed(128, "remote: Repository not found.")) as run, \
             patch.object(run_eval.time, "sleep"):
            path, log = run_eval.clone_repo("https://example.org/gone.git")
        self.assertIsNone(path)
        self.assertEqual(run.call_count, 1)

    def test_it_gives_up_after_the_configured_attempts(self):
        from unittest.mock import patch
        from evaluation import run_eval

        with patch.object(run_eval, "run_group", return_value=self._completed(128, "fatal: early EOF")) as run, \
             patch.object(run_eval.time, "sleep"):
            path, log = run_eval.clone_repo("https://example.org/flaky.git")
        self.assertIsNone(path)
        self.assertEqual(run.call_count, run_eval.CLONE_ATTEMPTS)
        self.assertIn("after 3 attempts", log)


if __name__ == "__main__":
    unittest.main()


class TestExternalToolReasons(unittest.TestCase):
    def test_a_failed_shell_command_is_an_external_tool_problem_even_under_a_dependency_category(self):
        line = "CalledProcessError: Command 'b'dx download -f /data/H1.fa'' returned non-zero exit status 127."
        self.assertEqual(_c(line, category="D", kind="unknown"), "shell_command_failed")
        self.assertEqual(reason_group("shell_command_failed"), "external_tool")

    def test_a_program_that_is_not_installed(self):
        line = "ValueError: Can't execute `prog` minimap2. Is it installed?"
        self.assertEqual(_c(line, category="E"), "external_tool_missing")
        self.assertEqual(reason_group("external_tool_missing"), "external_tool")

    def test_a_missing_python_module_is_still_a_dependency_not_an_external_tool(self):
        self.assertEqual(
            _c("ModuleNotFoundError: No module named 'tool_is_not_installed_pkg'", category="A", kind="missing_module"),
            "dependency:missing_module",
        )
