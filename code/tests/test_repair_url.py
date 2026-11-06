import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.repair_url import build_rows, choose_files, parse_repo_url, summarize


class TestParseRepoUrl(unittest.TestCase):
    def test_accepted_forms(self):
        for url in ("https://github.com/owner/repo", "https://github.com/owner/repo.git", "http://github.com/owner/repo/",
                    "github.com/owner/repo", "https://www.github.com/owner/repo/tree/main/notebooks", "https://github.com/owner/repo#readme"):
            with self.subTest(url=url):
                self.assertEqual(parse_repo_url(url), ("owner/repo", "https://github.com/owner/repo.git"))

    def test_rejected_forms(self):
        for url in ("https://gitlab.com/owner/repo", "https://github.com/owner", "not a url", ""):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    parse_repo_url(url)


class TestChooseFiles(unittest.TestCase):
    FILES = ["a.py", "nb/one.ipynb", "nb/two.ipynb", "z.py"]

    def test_default_is_the_notebooks(self):
        self.assertEqual(choose_files(self.FILES, [], 10)[0], ["nb/one.ipynb", "nb/two.ipynb"])

    def test_scripts_are_used_when_there_are_no_notebooks(self):
        self.assertEqual(choose_files(["a.py", "b.py"], [], 10)[0], ["a.py", "b.py"])

    def test_the_cap_is_applied_and_announced(self):
        files, warnings = choose_files([f"n{i}.ipynb" for i in range(30)], [], 5)
        self.assertEqual(len(files), 5)
        self.assertIn("--max-files", warnings[0])

    def test_requested_files_must_exist(self):
        self.assertEqual(choose_files(self.FILES, ["z.py"], 1)[0], ["z.py"])
        with self.assertRaises(ValueError):
            choose_files(self.FILES, ["missing.ipynb"], 10)


class TestRowsAndSummary(unittest.TestCase):
    def test_rows_have_every_column_the_harness_reads(self):
        rows = build_rows("o/r", "https://github.com/o/r.git", ["a.ipynb"])
        self.assertEqual(rows[0]["category"], "A")
        self.assertEqual(rows[0]["tier"], "confirmed")
        self.assertEqual(rows[0]["notebook_path"], "a.ipynb")

    def test_summary_explains_problem_steps_basis_and_check_in_plain_words(self):
        text = summarize([
            {"notebook_path": "a.ipynb", "discovered": True, "fixed": True, "python_version_actual": "3.8", "python_mechanism": "conda",
             "python_version_source": "era-inferred", "initial_error": "ModuleNotFoundError: No module named 'line_profiler'",
             "fix_actions": [
                 {"tool": "install_package", "resolved_package": "line_profiler==4.0.3", "grounding": "llm_proposed",
                  "verification": "unverified", "confidence": "low", "lever": "", "lever_detail": "", "edit_preview": []},
                 {"tool": "install_package", "resolved_package": "memory_profiler==0.61.0", "grounding": "metadata_grounded",
                  "verification": "verified", "confidence": "high", "lever": "", "lever_detail": "", "edit_preview": []},
                 {"tool": "edit_code", "resolved_package": "", "grounding": "llm_proposed", "verification": "verified",
                  "confidence": "medium-high", "lever": "", "lever_detail": "",
                  "edit_preview": [{"find": "old_call(x)", "replace": "new_call(x)"}]},
             ]},
            {"notebook_path": "b.ipynb", "discovered": True, "fixed": False, "runtime_observed_reason": "missing_data_file",
             "runtime_observed_error": "FileNotFoundError: data.csv", "initial_error": "FileNotFoundError: data.csv", "fix_actions": []},
            {"notebook_path": "c.ipynb", "discovered": False, "error": "notebook not found"},
            {"notebook_path": "d.ipynb", "discovered": True, "fixed": True, "passed_without_agent_action": True, "fix_actions": []},
        ])
        self.assertIn("FIXED         a.ipynb", text)
        self.assertIn("problem it had: ModuleNotFoundError: No module named 'line_profiler'", text)
        self.assertIn("step 1: install line_profiler==4.0.3", text)
        self.assertIn("chosen by the AI model", text)
        self.assertIn("on its own this step did not make the notebook run; a later step did", text)  # the confusing label, explained
        self.assertIn("chosen from a fact in the package registry (PyPI)", text)
        self.assertIn("the notebook ran right after this step", text)
        self.assertIn("changed  'old_call(x)'", text)  # an edit shows WHAT it changed
        self.assertIn("to       'new_call(x)'", text)
        self.assertIn("(labels: llm_proposed / unverified / confidence low)", text)  # raw labels kept for experts
        self.assertIn("python 3.8 (conda, from era-inferred)", text)
        self.assertIn("NOT FIXED     b.ipynb", text)
        self.assertIn("first problem seen: FileNotFoundError: data.csv", text)
        self.assertIn("NOT RUN       c.ipynb", text)
        self.assertIn("ALREADY RUNS  d.ipynb", text)
        self.assertIn("nothing needed repairing", text)


if __name__ == "__main__":
    unittest.main()
