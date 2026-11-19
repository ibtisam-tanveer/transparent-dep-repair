"""HG2.9K evaluation support (TASK_hg2k_evaluation.md) -- offline tests.

Local dataset source in the harness, dataset preparation, and the analysis' success definitions. No network,
no Docker, nothing from the real dataset is executed."""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import analyze_hg2k as ah
from evaluation import hg2k_prepare as prep
from evaluation import run_eval


def make_dataset(root: str, gists: dict[str, str]) -> None:
    for gist_id, source in gists.items():
        d = os.path.join(root, "hard-gists", gist_id)
        os.makedirs(d)
        with open(os.path.join(d, "snippet.py"), "w", encoding="utf-8") as f:
            f.write(source)


class LocalSource(unittest.TestCase):
    def test_local_entry_is_copied_not_modified(self):
        with tempfile.TemporaryDirectory() as root:
            make_dataset(root, {"abc": "import numpy\n"})
            path, log = run_eval.obtain_source("local:hard-gists/abc", root)
            try:
                self.assertIsNotNone(path, log)
                self.assertTrue(os.path.isfile(os.path.join(path, "snippet.py")))
                with open(os.path.join(path, "snippet.py"), "a") as f:  # editing the copy ...
                    f.write("x = 1\n")
                with open(os.path.join(root, "hard-gists", "abc", "snippet.py")) as f:
                    self.assertEqual(f.read(), "import numpy\n")  # ... never touches the dataset
            finally:
                if path:
                    import shutil

                    shutil.rmtree(path, ignore_errors=True)

    def test_missing_and_escaping_sources_are_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(run_eval.obtain_source("local:hard-gists/nope", root)[0])
            path, log = run_eval.obtain_source("local:../../etc", root)
            self.assertIsNone(path)
            self.assertIn("escapes", log)

    def test_git_urls_still_go_through_clone(self):
        called = {}

        def fake_clone(url, timeout=0, full_history=False):
            called["url"] = url
            return "/tmp/x", "ok"

        original = run_eval.clone_repo
        run_eval.clone_repo = fake_clone
        try:
            self.assertEqual(run_eval.obtain_source("https://github.com/a/b.git", "")[0], "/tmp/x")
        finally:
            run_eval.clone_repo = original
        self.assertEqual(called["url"], "https://github.com/a/b.git")


class Prepare(unittest.TestCase):
    def test_static_facts_flag_python2_syntax(self):
        self.assertTrue(prep.static_facts("import os\nprint('x')\n")["py3_parses"])
        self.assertFalse(prep.static_facts("import os\nprint 'x'\n")["py3_parses"])
        # (`except A, e:` is not asserted here: Python 3.14 accepts it, which is why prepare parses in the
        #  runtime image's 3.11 -- see parse_in_container)
        self.assertFalse(prep.static_facts("exec 'x = 1'\n")["py3_parses"])

    def test_build_rows_one_per_gist_in_stable_order(self):
        with tempfile.TemporaryDirectory() as root:
            make_dataset(root, {"b": "print 1\n", "a": "import os\n"})
            rows = prep.build_rows(root)
        self.assertEqual([r["gist_id"] for r in rows], ["a", "b"])
        self.assertEqual(rows[0]["clone_url"], "local:hard-gists/a")
        self.assertEqual(rows[0]["notebook_path"], "snippet.py")
        self.assertEqual((rows[0]["py3_parses"], rows[1]["py3_parses"]), ("True", "False"))
        self.assertEqual(len(rows[0]["sha256"]), 64)

    def test_csv_is_readable_by_the_harness(self):
        with tempfile.TemporaryDirectory() as root:
            make_dataset(root, {"a": "import os\n"})
            path = os.path.join(root, "out.csv")
            prep.main(["--dataset-root", root, "--out", path, "--no-docker-parse"])
            by_repo = run_eval.load_failures_by_repo(path)
            self.assertEqual(list(by_repo), ["hg2k/a"])
            with open(path, newline="") as f:
                self.assertEqual(list(csv.DictReader(f))[0]["category"], "A")


class Analysis(unittest.TestCase):
    def test_wilson_interval(self):
        lo, hi = ah.wilson(0, 6)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.39, places=2)
        self.assertEqual(ah.wilson(0, 0), (0.0, 0.0))
        lo, hi = ah.wilson(50, 100)
        self.assertTrue(lo < 0.5 < hi)

    def test_exception_name(self):
        self.assertEqual(ah.exception_name("ModuleNotFoundError: No module named 'x'"), "ModuleNotFoundError")
        self.assertEqual(ah.exception_name("Traceback...\nsklearn.exceptions.NotFittedError: foo"), "NotFittedError")
        self.assertEqual(ah.exception_name(""), "")

    def row(self, **kw):
        base = {"repository": "hg2k/x", "discovered": True, "fixed": False, "error": "", "fix_actions": [],
                "runtime_observed_error": "", "runtime_observed_reason": "", "llm_calls": 5}
        base.update(kw)
        return base

    def test_import_error_left_is_a_failure_under_every_definition(self):
        c = ah.classify(self.row(runtime_observed_error="ModuleNotFoundError: No module named 'gtk'",
                                 runtime_observed_reason="dependency:missing_module"), None)
        self.assertEqual(c["bucket"], "unfixed_import_still_failing")
        self.assertFalse(c["strict"] or c["import_resolved"] or c["pllm_style"])

    def test_other_runtime_error_counts_as_import_resolved_but_not_strict(self):
        c = ah.classify(self.row(runtime_observed_error="FileNotFoundError: data.csv",
                                 runtime_observed_reason="missing_data_file"), None)
        self.assertEqual(c["bucket"], "unfixed_other_runtime_error")
        self.assertTrue(c["import_resolved"] and c["pllm_style"])
        self.assertFalse(c["strict"])

    def test_attribute_error_is_import_resolved_but_not_pllm_style(self):
        c = ah.classify(self.row(runtime_observed_error="AttributeError: module 'numpy' has no attribute 'float'",
                                 runtime_observed_reason="dependency:module_attribute_removed"), None)
        self.assertFalse(c["pllm_style"])

    def test_syntax_error_is_never_resolved(self):
        """A SyntaxError means the imports were never reached -- Python-2 files must not inflate the lenient rates."""
        c = ah.classify(self.row(runtime_observed_error="SyntaxError: Missing parentheses in call to 'print'",
                                 runtime_observed_reason="code_error:SyntaxError"), None)
        self.assertEqual(c["bucket"], "unfixed_syntax_error")
        self.assertFalse(c["import_resolved"] or c["pllm_style"])

    def test_timeout_means_it_started_and_passed_its_imports(self):
        c = ah.classify(self.row(runtime_observed_error="TimeoutExpired: process exceeded 60s and was killed",
                                 runtime_observed_reason="cell_timeout"), None)
        self.assertEqual(c["bucket"], "unfixed_runs_but_times_out")
        self.assertTrue(c["import_resolved"])
        self.assertFalse(c["strict"])

    def test_fixed_buckets_and_environment_only(self):
        as_is = ah.classify(self.row(fixed=True, passed_without_agent_action=True), None)
        env = ah.classify(self.row(fixed=True, fix_actions=[{"tool": "install_package", "confidence": "high",
                                                             "grounding": "metadata_grounded"}]), None)
        edit = ah.classify(self.row(fixed=True, fix_actions=[{"tool": "edit_code", "confidence": "low",
                                                              "grounding": "llm_proposed"}]), None)
        self.assertEqual((as_is["bucket"], env["bucket"], edit["bucket"]),
                         ("ran_as_is", "fixed_environment_only", "fixed_with_code_edit"))
        self.assertTrue(edit["edit_used"] and not env["edit_used"])

    def test_agent_errors_are_not_counted_as_repair_failures(self):
        c = ah.classify(self.row(stop_reason="error", error="LLM call failed: connection reset"), None)
        self.assertEqual(c["bucket"], "not_run")
        self.assertTrue(c["skipped"].startswith("not_run: agent error"))
        self.assertFalse(c["import_resolved"])

    def test_denominator_is_the_whole_sample(self):
        rows = [self.row(repository=f"hg2k/{i}", fixed=(i == 0), passed_without_agent_action=(i == 0)) for i in range(4)]
        rows[3]["discovered"] = False
        summary = ah.analyse(rows, {})
        self.assertEqual(summary["n_gists"], 4)
        self.assertEqual(summary["definitions"]["strict"]["k"], 1)
        self.assertEqual(summary["n_not_run"], 1)

    def test_prior_work_numbers_are_internally_consistent(self):
        for name, k, of, note, _ in ah.PRIOR:
            self.assertEqual(of, 2891, name)
            self.assertLess(k, of)
        by = {p[0]: p[1] for p in ah.PRIOR}
        # PLLM's own arithmetic: +218 over ReadPyE and +281 over PyEGo
        self.assertEqual(by["PLLM, 10 runs aggregated"] - by["ReadPyE (PLLM's run)"], 218)
        self.assertEqual(by["PLLM, 10 runs aggregated"] - by["PyEGo (PLLM's re-run)"], 281)


if __name__ == "__main__":
    unittest.main()
