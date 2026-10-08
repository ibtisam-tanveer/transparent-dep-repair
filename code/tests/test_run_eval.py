import csv
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.run_eval import build_result_row, load_checkpoint, load_failures_by_repo, mark_done, run
from repair_tool.agent import AgentResult, TraceStep


def _write_csv(path: str, rows: list[dict]) -> None:
    fieldnames = ["repository", "clone_url", "notebook_path", "error_type", "error_message", "category", "tier", "python_version", "repository_id"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{k: "" for k in fieldnames}, **row})


class TestLoadFailuresByRepo(unittest.TestCase):
    def test_groups_rows_by_repository(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "failures.csv")
            _write_csv(path, [
                {"repository": "a/repo", "clone_url": "https://github.com/a/repo.git", "notebook_path": "x.ipynb", "category": "A", "tier": "confirmed"},
                {"repository": "a/repo", "clone_url": "https://github.com/a/repo.git", "notebook_path": "y.ipynb", "category": "C", "tier": "candidate"},
                {"repository": "b/repo", "clone_url": "https://github.com/b/repo.git", "notebook_path": "z.ipynb", "category": "A", "tier": "confirmed"},
            ])
            by_repo = load_failures_by_repo(path)
            self.assertEqual(set(by_repo.keys()), {"a/repo", "b/repo"})
            self.assertEqual(len(by_repo["a/repo"]), 2)
            self.assertEqual(len(by_repo["b/repo"]), 1)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestCheckpoint(unittest.TestCase):
    def test_missing_checkpoint_file_is_an_empty_set(self):
        self.assertEqual(load_checkpoint("/no/such/checkpoint/file.txt"), set())

    def test_mark_done_then_load_round_trips(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "done.txt")
            mark_done(path, "a/repo")
            mark_done(path, "b/repo")
            self.assertEqual(load_checkpoint(path), {"a/repo", "b/repo"})
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


def _fix_step(tool: str, grounding: str, verification: str, confidence: str) -> TraceStep:
    return TraceStep(thought="", tool_called=tool, tool_input={}, tool_result={"ok": True},
                      grounding=grounding, verification=verification, confidence=confidence)


def _fake_worker_fixing_everything(repository, clone_url, clone_path, failure_rows, run_id, max_steps_per_file, budget_seconds, timeout_seconds):
    """A run_repo_worker stand-in that reports every one of THIS call's
    own failure_rows as fixed -- unlike a static return_value, this stays
    correct across different repos with different notebook paths."""
    return ([{"notebook_path": f["notebook_path"], "fixed": True, "discovered": True} for f in failure_rows], "ok")


_REPO_META = {"env_setup_ok": True, "dependency_file_used": "requirements.txt"}


class TestBuildResultRow(unittest.TestCase):
    """build_result_row() is now per-file, called once per yield from
    _repo_runner.run_repo_incrementally() -- order-awareness (a free pass
    can only be caused by a fix that already happened) is the caller's
    job, via `any_real_fix_seen_before_this_file`, exercised directly here."""

    def test_a_discovered_fixed_file_reports_its_fix_actions(self):
        failure = {"notebook_path": "a.ipynb", "category": "A", "tier": "confirmed"}
        fixed_file = AgentResult(
            target="a.ipynb", fixed=True,
            trace=[_fix_step("install_package", "metadata_grounded", "verified", "high")],
            final_strategy="install", llm_calls=3, total_tokens=120,
        )

        row = build_result_row("org/repo", "u", failure, fixed_file, _REPO_META, "run1", 30, False)

        self.assertTrue(row["discovered"])
        self.assertTrue(row["fixed"])
        self.assertEqual(row["llm_calls"], 3)
        self.assertEqual(len(row["fix_actions"]), 1)
        self.assertEqual(row["fix_actions"][0]["grounding"], "metadata_grounded")

    def test_a_never_discovered_notebook_is_reported_honestly(self):
        failure = {"notebook_path": "missing.ipynb", "category": "E", "tier": "candidate"}
        row = build_result_row("org/repo", "u", failure, None, {}, "run1", 30, False)
        self.assertFalse(row["discovered"])
        self.assertFalse(row["fixed"])

    def test_max_steps_hit_only_true_when_not_fixed_and_trace_exhausts_budget(self):
        failure = {"notebook_path": "a.ipynb", "category": "C", "tier": "candidate"}
        exhausted = AgentResult(target="a.ipynb", fixed=False, trace=[_fix_step("edit_code", "llm_proposed", "unverified", "low")] * 30)
        row = build_result_row("org/repo", "u", failure, exhausted, _REPO_META, "run1", 30, False)
        self.assertTrue(row["max_steps_hit"])

    def test_cross_file_free_pass_true_when_an_earlier_file_was_genuinely_fixed(self):
        failure = {"notebook_path": "b.ipynb", "category": "A", "tier": "confirmed"}
        free_file = AgentResult(target="b.ipynb", fixed=True, trace=[])
        row = build_result_row("org/repo", "u", failure, free_file, _REPO_META, "run1", 30, True)
        self.assertTrue(row["cross_file_free_pass"])

    def test_cross_file_free_pass_false_without_an_earlier_fix(self):
        failure = {"notebook_path": "b.ipynb", "category": "A", "tier": "confirmed"}
        free_file = AgentResult(target="b.ipynb", fixed=True, trace=[])
        row = build_result_row("org/repo", "u", failure, free_file, _REPO_META, "run1", 30, False)
        self.assertFalse(row["cross_file_free_pass"])

    def test_cross_file_free_pass_requires_order_a_later_fix_does_not_count(self):
        """The caller must only pass any_real_fix_seen_before_this_file
        based on files processed STRICTLY earlier -- this test documents
        that contract: a fix that happens later can't retroactively cause
        an earlier free pass, so the caller passing False here (because,
        chronologically, nothing had been fixed yet) must not be
        overridden by this function itself."""
        failure = {"notebook_path": "a.ipynb", "category": "A", "tier": "confirmed"}
        free_file = AgentResult(target="a.ipynb", fixed=True, trace=[])
        row = build_result_row("org/repo", "u", failure, free_file, _REPO_META, "run1", 30, False)
        self.assertFalse(row["cross_file_free_pass"])


class TestRunResumability(unittest.TestCase):
    """The harness itself: sampling, checkpoint-skip, and budget limits --
    run_repo_worker (the isolated-subprocess per-repo call), clone_repo,
    and count_discovered_files are fully mocked; the subprocess isolation
    itself is covered by TestRunRepoWorker and TestRepoWorkerSubprocess
    below, against the real worker/runner modules."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.failures_csv = os.path.join(self.tmpdir, "failures.csv")
        _write_csv(self.failures_csv, [
            {"repository": "a/repo", "clone_url": "https://github.com/a/repo.git", "notebook_path": "x.ipynb", "category": "A", "tier": "confirmed"},
            {"repository": "b/repo", "clone_url": "https://github.com/b/repo.git", "notebook_path": "y.ipynb", "category": "A", "tier": "confirmed"},
            {"repository": "c/repo", "clone_url": "https://github.com/c/repo.git", "notebook_path": "z.ipynb", "category": "A", "tier": "confirmed"},
        ])
        self.results_dir = os.path.join(self.tmpdir, "results")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("evaluation.run_eval.count_discovered_files", return_value=1)
    @patch("evaluation.run_eval.run_repo_worker")
    @patch("evaluation.run_eval.clone_repo")
    def test_resuming_skips_already_done_repos(self, mock_clone, mock_worker, _mock_count):
        mock_clone.return_value = ("/fake/clone", "ok")
        mock_worker.side_effect = _fake_worker_fixing_everything

        run(self.failures_csv, self.results_dir, sample=0, seed=1, max_steps_per_file=30,
            limit_minutes=None, max_llm_calls=None, tag="t1")
        first_call_count = mock_worker.call_count
        self.assertEqual(first_call_count, 3)

        # Second run: nothing new should be processed.
        run(self.failures_csv, self.results_dir, sample=0, seed=1, max_steps_per_file=30,
            limit_minutes=None, max_llm_calls=None, tag="t1")
        self.assertEqual(mock_worker.call_count, first_call_count)

        with open(os.path.join(self.results_dir, "t1.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 3)

    @patch("evaluation.run_eval.count_discovered_files", return_value=1)
    @patch("evaluation.run_eval.run_repo_worker")
    @patch("evaluation.run_eval.clone_repo")
    def test_sample_limits_how_many_repos_are_processed(self, mock_clone, mock_worker, _mock_count):
        mock_clone.return_value = ("/fake/clone", "ok")
        mock_worker.side_effect = lambda *a, **k: ([], "ok")

        run(self.failures_csv, self.results_dir, sample=1, seed=1, max_steps_per_file=30,
            limit_minutes=None, max_llm_calls=None, tag="t2")
        self.assertEqual(mock_worker.call_count, 1)

    @patch("evaluation.run_eval.count_discovered_files", return_value=1)
    @patch("evaluation.run_eval.run_repo_worker")
    @patch("evaluation.run_eval.clone_repo")
    def test_a_repo_that_raises_is_recorded_as_an_error_not_a_crash(self, mock_clone, mock_worker, _mock_count):
        mock_clone.return_value = ("/fake/clone", "ok")
        mock_worker.side_effect = RuntimeError("boom")

        run(self.failures_csv, self.results_dir, sample=0, seed=1, max_steps_per_file=30,
            limit_minutes=None, max_llm_calls=None, tag="t3")

        with open(os.path.join(self.results_dir, "t3.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("boom" in r["error"] for r in rows))

    @patch("evaluation.run_eval.count_discovered_files", return_value=1)
    @patch("evaluation.run_eval.run_repo_worker")
    @patch("evaluation.run_eval.clone_repo")
    def test_a_failed_clone_is_recorded_without_calling_the_worker(self, mock_clone, mock_worker, _mock_count):
        mock_clone.return_value = (None, "git clone failed: repository not found")

        run(self.failures_csv, self.results_dir, sample=0, seed=1, max_steps_per_file=30,
            limit_minutes=None, max_llm_calls=None, tag="t4")

        mock_worker.assert_not_called()
        with open(os.path.join(self.results_dir, "t4.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertTrue(all("clone failed" in r["error"] for r in rows))

    @patch("evaluation.run_eval.count_discovered_files", return_value=1)
    @patch("evaluation.run_eval.run_repo_worker")
    @patch("evaluation.run_eval.clone_repo")
    def test_a_timed_out_repo_with_no_partial_rows_backfills_every_notebook(self, mock_clone, mock_worker, _mock_count):
        """Regression test for the 43-minute-stuck-repo finding: a timeout
        with zero partial rows must still degrade to one honest 'not
        reached' row per labelled notebook, not abort the rest of the run."""
        mock_clone.return_value = ("/fake/clone", "ok")
        mock_worker.side_effect = lambda *a, **k: ([], "timed out after 900s")

        run(self.failures_csv, self.results_dir, sample=0, seed=1, max_steps_per_file=30,
            limit_minutes=None, max_llm_calls=None, tag="t5")

        self.assertEqual(mock_worker.call_count, 3)
        with open(os.path.join(self.results_dir, "t5.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("timed out" in r["error"] for r in rows))
        self.assertTrue(all(r["discovered"] is False for r in rows))

    @patch("evaluation.run_eval.count_discovered_files", return_value=1)
    @patch("evaluation.run_eval.run_repo_worker")
    @patch("evaluation.run_eval.clone_repo")
    def test_a_timed_out_repo_keeps_whatever_partial_rows_it_already_has(self, mock_clone, mock_worker, _mock_count):
        """The actual fix for the 43-minute finding: a timeout that DID
        produce some rows before the cut-off must keep them, only
        backfilling the notebook(s) genuinely not reached."""
        _write_csv(self.failures_csv, [
            {"repository": "a/repo", "clone_url": "u", "notebook_path": "x1.ipynb", "category": "A", "tier": "confirmed"},
            {"repository": "a/repo", "clone_url": "u", "notebook_path": "x2.ipynb", "category": "A", "tier": "confirmed"},
        ])
        mock_clone.return_value = ("/fake/clone", "ok")
        mock_worker.return_value = (
            [{"notebook_path": "x1.ipynb", "fixed": True, "discovered": True}],
            "timed out after 900s",
        )

        run(self.failures_csv, self.results_dir, sample=0, seed=1, max_steps_per_file=30,
            limit_minutes=None, max_llm_calls=None, tag="t6")

        with open(os.path.join(self.results_dir, "t6.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 2)
        x1 = next(r for r in rows if r["notebook_path"] == "x1.ipynb")
        x2 = next(r for r in rows if r["notebook_path"] == "x2.ipynb")
        self.assertTrue(x1["fixed"])
        self.assertFalse(x2["discovered"])
        self.assertIn("timed out", x2["error"])

    @patch("evaluation.run_eval.count_discovered_files", return_value=7)
    @patch("evaluation.run_eval.run_repo_worker")
    @patch("evaluation.run_eval.clone_repo")
    def test_budget_scales_with_discovered_file_count(self, mock_clone, mock_worker, _mock_count):
        """The real fix for the unfair-fixed-timeout finding: a repo with
        more discovered files gets a proportionally larger time budget."""
        mock_clone.return_value = ("/fake/clone", "ok")
        mock_worker.side_effect = lambda *a, **k: ([], "ok")

        run(self.failures_csv, self.results_dir, sample=1, seed=1, max_steps_per_file=30,
            limit_minutes=None, max_llm_calls=None, tag="t7", per_file_budget_seconds=100)

        call_args = mock_worker.call_args
        budget_seconds = call_args.args[-2] if len(call_args.args) >= 2 else call_args.kwargs.get("budget_seconds")
        self.assertEqual(budget_seconds, 700)  # 7 files * 100s/file


class TestRunRepoWorker(unittest.TestCase):
    """run_repo_worker's own plumbing (temp files, timeout handling, exit
    code, JSONL round-trip, cleanup, partial-row preservation) --
    subprocess.run itself is mocked; _repo_worker.py (the real subprocess
    target) is exercised separately in TestRepoWorkerSubprocess below."""

    def test_timeout_with_nothing_written_yet_returns_empty_rows(self):
        import subprocess as subprocess_module

        from evaluation.run_eval import run_repo_worker

        with patch("evaluation.run_eval.subprocess.run", side_effect=subprocess_module.TimeoutExpired(cmd="x", timeout=1)):
            rows, status = run_repo_worker("x/y", "u", "/fake/path", [{"notebook_path": "a.ipynb"}], "run1", 30, 100, timeout_seconds=1)

        self.assertEqual(rows, [])
        self.assertIn("timed out", status)

    def test_timeout_preserves_whatever_was_already_streamed_to_output(self):
        """The core of the partial-results fix: a kill mid-write must not
        destroy rows the worker already flushed for earlier files."""
        from evaluation.run_eval import run_repo_worker

        def fake_run(cmd, **kwargs):
            output_path = cmd[cmd.index("--output") + 1]
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"notebook_path": "a.ipynb", "fixed": True}) + "\n")
            import subprocess as subprocess_module
            raise subprocess_module.TimeoutExpired(cmd=cmd, timeout=1)

        with patch("evaluation.run_eval.subprocess.run", side_effect=fake_run):
            rows, status = run_repo_worker("x/y", "u", "/fake/path", [], "run1", 30, 100, timeout_seconds=1)

        self.assertEqual(rows, [{"notebook_path": "a.ipynb", "fixed": True}])
        self.assertIn("timed out", status)

    def test_a_nonzero_exit_still_preserves_partial_output(self):
        from evaluation.run_eval import run_repo_worker

        def fake_run(cmd, **kwargs):
            output_path = cmd[cmd.index("--output") + 1]
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"notebook_path": "a.ipynb", "fixed": False}) + "\n")
            return MagicMock(returncode=1, stdout="", stderr="Traceback: boom")

        with patch("evaluation.run_eval.subprocess.run", side_effect=fake_run):
            rows, status = run_repo_worker("x/y", "u", "/fake/path", [], "run1", 30, 100, timeout_seconds=60)

        self.assertEqual(rows, [{"notebook_path": "a.ipynb", "fixed": False}])
        self.assertIn("boom", status)

    def test_temp_files_are_cleaned_up_after_a_successful_run(self):
        from evaluation.run_eval import run_repo_worker

        written_output_paths = []

        def fake_run(cmd, **kwargs):
            output_path = cmd[cmd.index("--output") + 1]
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"notebook_path": "a.ipynb", "fixed": True}) + "\n")
            written_output_paths.append(output_path)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("evaluation.run_eval.subprocess.run", side_effect=fake_run):
            rows, status = run_repo_worker("x/y", "u", "/fake/path", [], "run1", 30, 100, timeout_seconds=60)

        self.assertEqual(status, "ok")
        self.assertEqual(rows, [{"notebook_path": "a.ipynb", "fixed": True}])
        self.assertFalse(os.path.isfile(written_output_paths[0]), "output temp file should be removed after reading")

    def test_a_half_written_trailing_line_is_dropped_not_crashed_on(self):
        from evaluation.run_eval import run_repo_worker

        def fake_run(cmd, **kwargs):
            output_path = cmd[cmd.index("--output") + 1]
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"notebook_path": "a.ipynb", "fixed": True}) + "\n")
                f.write('{"notebook_path": "b.ipynb", "fi')  # truncated mid-write
            import subprocess as subprocess_module
            raise subprocess_module.TimeoutExpired(cmd=cmd, timeout=1)

        with patch("evaluation.run_eval.subprocess.run", side_effect=fake_run):
            rows, status = run_repo_worker("x/y", "u", "/fake/path", [], "run1", 30, 100, timeout_seconds=1)

        self.assertEqual(rows, [{"notebook_path": "a.ipynb", "fixed": True}])


class TestRepoWorkerSubprocess(unittest.TestCase):
    """_repo_worker.py's main() invoked directly (a real --input/--output
    JSON file round-trip, not through subprocess.run) with
    run_repo_incrementally mocked to yield a scripted sequence -- proves
    the worker streams rows as it goes and backfills unseen notebooks."""

    @patch("evaluation._repo_worker.run_repo_incrementally")
    def test_worker_streams_one_row_per_yielded_file_and_flushes(self, mock_incremental):
        mock_incremental.return_value = iter([
            (AgentResult(target="a.ipynb", fixed=True, trace=[]), _REPO_META),
            (AgentResult(target="b.ipynb", fixed=False, trace=[]), _REPO_META),
        ])

        tmpdir = tempfile.mkdtemp()
        try:
            input_path = os.path.join(tmpdir, "input.json")
            output_path = os.path.join(tmpdir, "output.jsonl")
            with open(input_path, "w", encoding="utf-8") as f:
                json.dump({
                    "repository": "x/y", "clone_url": "u", "clone_path": "/fake",
                    "failure_rows": [
                        {"notebook_path": "a.ipynb", "category": "A", "tier": "confirmed"},
                        {"notebook_path": "b.ipynb", "category": "C", "tier": "candidate"},
                    ],
                    "run_id": "run1", "max_steps_per_file": 30, "budget_seconds": 100,
                }, f)

            from evaluation import _repo_worker
            with patch.object(sys, "argv", ["_repo_worker.py", "--input", input_path, "--output", output_path]):
                _repo_worker.main()

            with open(output_path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
            self.assertEqual(len(rows), 2)
            self.assertTrue(next(r for r in rows if r["notebook_path"] == "a.ipynb")["fixed"])
            self.assertFalse(next(r for r in rows if r["notebook_path"] == "b.ipynb")["fixed"])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    @patch("evaluation._repo_worker.run_repo_incrementally")
    def test_a_labelled_notebook_never_yielded_is_backfilled_as_not_discovered(self, mock_incremental):
        mock_incremental.return_value = iter([(AgentResult(target="a.ipynb", fixed=True, trace=[]), _REPO_META)])

        tmpdir = tempfile.mkdtemp()
        try:
            input_path = os.path.join(tmpdir, "input.json")
            output_path = os.path.join(tmpdir, "output.jsonl")
            with open(input_path, "w", encoding="utf-8") as f:
                json.dump({
                    "repository": "x/y", "clone_url": "u", "clone_path": "/fake",
                    "failure_rows": [
                        {"notebook_path": "a.ipynb", "category": "A", "tier": "confirmed"},
                        {"notebook_path": "never_reached.ipynb", "category": "A", "tier": "confirmed"},
                    ],
                    "run_id": "run1", "max_steps_per_file": 30, "budget_seconds": 100,
                }, f)

            from evaluation import _repo_worker
            with patch.object(sys, "argv", ["_repo_worker.py", "--input", input_path, "--output", output_path]):
                _repo_worker.main()

            with open(output_path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
            not_reached = next(r for r in rows if r["notebook_path"] == "never_reached.ipynb")
            self.assertFalse(not_reached["discovered"])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
