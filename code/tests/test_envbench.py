"""EnvBench evaluation support (TASK_envbench_evaluation.md) -- offline tests.

The check file (EnvBench's pyright rule), pinned sources, list preparation, the harness switches and the analysis'
bucket rules. No network, no Docker; the pyright call is replaced by a fake executable, git by a local repository.
"""

import csv
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import analyze_envbench as ae
from evaluation import envbench_check as check
from evaluation import envbench_prepare as prep
from evaluation import envbench_source as src
from evaluation import run_eval
from repair_tool import runtime

SHA = "0123456789abcdef0123456789abcdef01234567"


def pyright_json(*messages: str, rule: str = "reportMissingImports", version: str = "1.1.414") -> dict:
    return {"version": version, "generalDiagnostics": [
        {"file": f"/work/repo/f{i}.py", "severity": "error", "message": m, "rule": rule} for i, m in enumerate(messages)]}


class CheckRule(unittest.TestCase):
    def test_only_missing_imports_count(self):
        parsed = pyright_json('Import "yaml" could not be resolved', 'Import "requests" could not be resolved')
        parsed["generalDiagnostics"].append({"file": "x.py", "severity": "error", "rule": "reportAttributeAccessIssue",
                                             "message": '"x" is not a known attribute'})
        found = check.missing_imports(parsed)
        self.assertEqual([m for m, _ in found], ["yaml", "requests"])  # the same selection as EnvBench's jq filter

    def test_zero_diagnostics_is_a_pass(self):
        self.assertEqual(check.missing_imports({"generalDiagnostics": []}), [])
        self.assertEqual(check.missing_imports({}), [])

    def test_local_modules_are_not_install_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "mypkg"))
            os.makedirs(os.path.join(root, "src", "other"))
            self.assertTrue(check.is_repo_local("mypkg.sub", root))
            self.assertTrue(check.is_repo_local("other", root))
            self.assertTrue(check.is_repo_local(".relative", root))
            self.assertFalse(check.is_repo_local("yaml", root))

    def test_final_line_names_the_most_frequent_installable_module_and_carries_the_count(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "mypkg"))
            found = [("mypkg.core", "a.py")] * 5 + [("yaml", "a.py")] * 2 + [("numpy.linalg", "b.py")]
            issues, ordered, candidates = check.summarize(found, root)
            self.assertEqual(issues, 8)
            text, final = check.render(issues, ordered, candidates, "1.1.414")
            self.assertTrue(final.startswith("ModuleNotFoundError: No module named 'yaml'"))
            self.assertIn("[EnvBench check: 8 missing-import diagnostics, 3 modules]", final)
            self.assertLessEqual(len(final), 300)  # only 300 characters of the last exception line are kept in a row

    def test_stderr_is_short_and_ends_with_the_exception_line(self):
        """The agent's diagnose step reads the LAST line of what the model copies back; a long list there is cut."""
        with tempfile.TemporaryDirectory() as root:
            found = [(f"pkg{i}.mod", "a.py") for i in range(40)]
            issues, ordered, candidates = check.summarize(found, root)
            _text, final = check.render(issues, ordered, candidates, "1")
            message = check.stderr_message(candidates, final)
            self.assertLess(len(message), 500)
            self.assertEqual(message.strip().splitlines()[-1], final)

    def test_report_carries_the_hint_only_when_something_is_installable(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "mypkg"))
            text, _ = check.render(*check.summarize([("yaml", "a.py")], root), "1")
            self.assertIn("lookup_package", text)
            self.assertIn("Next step: lookup_package('yaml')", text)  # the same module the last line names
            text, _ = check.render(*check.summarize([("mypkg.x", "a.py")], root), "1")
            self.assertNotIn("lookup_package", text)

    def test_candidates_not_yet_importable_come_first(self):
        """The stuck-detection compares the module named on the last line; it must move while installs make progress."""
        with tempfile.TemporaryDirectory() as root:
            found = [("sublime", "a.py")] * 9 + [("yaml", "a.py")] * 3 + [("requests", "a.py")]
            _issues, _ordered, candidates = check.summarize(found, root, is_importable=lambda m: m.startswith("sublime"))
            self.assertEqual([m for m, _ in candidates], ["yaml", "requests", "sublime"])  # sublime is "installed" but unresolved
            _issues, _ordered, candidates = check.summarize(found, root, is_importable=lambda m: False)
            self.assertEqual(candidates[0][0], "sublime")  # nothing installed: plain frequency order

    def test_importable_uses_the_running_interpreter(self):
        self.assertTrue(check.importable("os.path"))
        self.assertFalse(check.importable("definitely_not_a_module_xyz"))

    def test_a_module_provided_anywhere_in_the_repository_is_local(self):
        """EnvBench's iss-mimic/mimic: `config` lives in a subfolder; naming it made the install get refused."""
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "Pi", "config"))
            with open(os.path.join(root, "Pi", "helper.py"), "w") as f:
                f.write("x = 1\n")
            os.makedirs(os.path.join(root, ".git", "hooks"))
            names = check.local_names(root)
            self.assertIn("config", names)
            self.assertIn("helper", names)
            self.assertNotIn("hooks", names)  # .git is not part of the repository's code
            self.assertTrue(check.is_repo_local("config.settings", root, names))
            self.assertFalse(check.is_repo_local("yaml", root, names))
            _i, _o, candidates = check.summarize([("config", "a.py"), ("yaml", "a.py")], root, names=names)
            self.assertEqual([m for m, _ in candidates], ["yaml"])

    def test_only_local_imports_left_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "mypkg"))
            issues, ordered, candidates = check.summarize([("mypkg._version", "a.py")], root)
            _text, final = check.render(issues, ordered, candidates, "1")
            self.assertTrue(final.startswith("ImportError: only repository-local"))

    _counter = 0

    def _fake_pyright(self, directory: str, body: str) -> str:
        CheckRule._counter += 1
        path = os.path.join(directory, f"fake_pyright_{CheckRule._counter}")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = self._fake_pyright(tmp, "echo '%s'" % json.dumps(pyright_json()))
            bad = self._fake_pyright(tmp, "echo '%s'" % json.dumps(pyright_json('Import "yaml" could not be resolved')))
            with mock.patch.dict(os.environ, {"PYRIGHT_BIN": ok}):
                with mock.patch("sys.stdout"):
                    self.assertEqual(check.main(), 0)
            with mock.patch.dict(os.environ, {"PYRIGHT_BIN": bad}):
                with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    self.assertEqual(check.main(), 1)
            broken = self._fake_pyright(tmp, "echo not json; exit 5")
            with mock.patch.dict(os.environ, {"PYRIGHT_BIN": broken}):
                with mock.patch("sys.stderr") as err:
                    self.assertEqual(check.main(), 3)
                self.assertTrue(any("EnvBenchCheckError" in str(c) for c in err.write.call_args_list))
            with mock.patch.dict(os.environ, {"PYRIGHT_BIN": os.path.join(tmp, "missing")}):
                with mock.patch("sys.stderr"):
                    self.assertEqual(check.main(), 3)

    def test_uses_the_exact_envbench_command_line(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return mock.Mock(stdout=json.dumps(pyright_json()), stderr="", returncode=0)

        with mock.patch("evaluation.envbench_check.subprocess.run", fake_run):
            check.run_pyright("/repo", "/env/bin/python", "/p/pyright", 10)
        cmd, kw = calls[0]
        self.assertEqual(cmd, ["/p/pyright", "/repo", "--level", "error", "--outputjson", "--pythonpath", "/env/bin/python"])
        self.assertEqual(kw["cwd"], "/repo")


class PinnedSources(unittest.TestCase):
    def test_spec_round_trip_and_validation(self):
        spec = src.spec_for("owner/repo.name", SHA)
        self.assertEqual(src.parse_spec(spec), (SHA, "https://github.com/owner/repo.name.git", "owner/repo.name"))
        for bad in ("pinned:abc:https://github.com/a/b.git", f"pinned:{SHA}:https://evil.example/a/b.git",
                    f"pinned:{SHA}:https://github.com/a/b.git;rm -rf", "local:hard-gists/x", f"pinned:{SHA}"):
            with self.assertRaises(ValueError, msg=bad):
                src.parse_spec(bad)

    def test_obtain_never_raises_on_a_malformed_spec(self):
        path, log = src.obtain("pinned:nonsense")
        self.assertIsNone(path)
        self.assertIn("bad commit", log)

    def _local_repo(self, root: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "GIT_AUTHOR_DATE": "2024-06-20T15:28:55+00:00",
               "GIT_COMMITTER_DATE": "2024-06-20T15:28:55+00:00"}
        run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, env=env)
        run("init", "-q")
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("import os\n")
        run("add", "-A")
        run("commit", "-q", "--no-gpg-sign", "-m", "one")
        first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("import sys\n")
        run("commit", "-q", "--no-gpg-sign", "-am", "two")
        return first

    def test_fetch_pinned_gets_exactly_the_pinned_commit_with_one_commit_of_history(self):
        with tempfile.TemporaryDirectory() as root:
            first = self._local_repo(root)
            path, log = src.fetch_pinned("file://" + root, first, timeout=60)
            try:
                self.assertIsNotNone(path, log)
                with open(os.path.join(path, "a.py")) as f:
                    self.assertEqual(f.read(), "import os\n")  # the pinned commit, not the branch tip
                count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=path, capture_output=True,
                                       text=True).stdout.strip()
                self.assertEqual(count, "1")
                date = subprocess.run(["git", "log", "-1", "--format=%cI"], cwd=path, capture_output=True,
                                      text=True).stdout.strip()
                self.assertTrue(date.startswith("2024-06-20"))  # the era cap needs the commit date
            finally:
                if path:
                    import shutil

                    shutil.rmtree(path, ignore_errors=True)

    def test_fetch_pinned_reports_a_missing_commit(self):
        with tempfile.TemporaryDirectory() as root:
            self._local_repo(root)
            path, log = src.fetch_pinned("file://" + root, "f" * 40, timeout=60)
            self.assertIsNone(path)
            self.assertIn("failed", log)

    def test_squash_keeps_the_commit_date(self):
        with tempfile.TemporaryDirectory() as root:
            self._local_repo(root)
            self.assertEqual(src.squash_to_single_commit(root), "")
            count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
            date = subprocess.run(["git", "log", "-1", "--format=%cI"], cwd=root, capture_output=True, text=True).stdout.strip()
            self.assertEqual(count, "1")
            self.assertTrue(date.startswith("2024-06-20"))

    def test_obtain_falls_back_to_the_archive_when_github_fails(self):
        with mock.patch.object(src, "fetch_pinned", return_value=(None, "not our ref")), \
                mock.patch.object(src, "fetch_hf_archive", return_value=("/tmp/x", "from the EnvBench Hugging Face archive")):
            path, log = src.obtain(src.spec_for("a/b", SHA))
        self.assertEqual(path, "/tmp/x")
        self.assertIn("not our ref", log)

    def test_obtain_reports_both_failures(self):
        with mock.patch.object(src, "fetch_pinned", return_value=(None, "gone")), \
                mock.patch.object(src, "fetch_hf_archive", return_value=(None, "too large")):
            path, log = src.obtain(src.spec_for("a/b", SHA))
        self.assertIsNone(path)
        self.assertIn("gone", log)
        self.assertIn("too large", log)

    def test_inject_check_copies_the_checker_into_the_repository_root(self):
        with tempfile.TemporaryDirectory() as root:
            name = src.inject_check(root)
            self.assertEqual(name, "envbench_check.py")
            with open(os.path.join(root, name)) as f, open(check.__file__) as g:
                self.assertEqual(f.read(), g.read())

    def test_harness_routes_pinned_entries_to_this_module(self):
        with mock.patch.object(src, "obtain", return_value=("/tmp/y", "ok")) as obtain:
            self.assertEqual(run_eval.obtain_source(src.spec_for("a/b", SHA)), ("/tmp/y", "ok"))
        obtain.assert_called_once()


class Prepare(unittest.TestCase):
    ENTRIES = [{"repository": "z/last", "revision": SHA}, {"repository": "a/first", "revision": "f" * 40}]

    def test_rows_are_sorted_one_per_repository_with_the_check_as_target(self):
        rows = prep.build_rows(self.ENTRIES)
        self.assertEqual([r["repository"] for r in rows], ["a/first", "z/last"])
        self.assertEqual({r["notebook_path"] for r in rows}, {"envbench_check.py"})
        self.assertEqual(rows[1]["clone_url"], src.spec_for("z/last", SHA))

    def test_duplicates_and_bad_revisions_are_refused(self):
        with self.assertRaises(ValueError):
            prep.build_rows(self.ENTRIES + [self.ENTRIES[0]])
        with self.assertRaises(ValueError):
            prep.build_rows([{"repository": "a/b", "revision": "main"}])

    def test_fingerprint_depends_on_content_not_order(self):
        a = prep.list_fingerprint(prep.build_rows(self.ENTRIES))
        b = prep.list_fingerprint(prep.build_rows(list(reversed(self.ENTRIES))))
        self.assertEqual(a, b)
        changed = prep.list_fingerprint(prep.build_rows([self.ENTRIES[0], {"repository": "a/first", "revision": "e" * 40}]))
        self.assertNotEqual(a, changed)

    def test_csv_is_readable_by_the_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = os.path.join(tmp, "list.jsonl")
            with open(dataset, "w") as f:
                for e in self.ENTRIES:
                    f.write(json.dumps(e) + "\n")
            out = os.path.join(tmp, "out.csv")
            prep.main(["--dataset-file", dataset, "--out", out, "--no-image-query"])
            by_repo = run_eval.load_failures_by_repo(out)
            self.assertEqual(sorted(by_repo), ["a/first", "z/last"])
            with open(out.replace(".csv", ".meta.json")) as f:
                meta = json.load(f)
            self.assertEqual(meta["repositories"], 2)
            self.assertEqual(len(meta["list_sha256"]), 64)
            with open(out, newline="") as f:
                self.assertEqual(list(csv.DictReader(f))[0]["category"], "A")


class RepositoryCopy(unittest.TestCase):
    def test_a_dangling_symlink_does_not_abort_the_copy(self):
        """EnvBench's internetarchive/brozzler has `brozzler.svg` pointing nowhere; the copy used to raise."""
        from repair_tool.venv_manager import get_fresh_repo_copy

        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("import os\n")
            with open(os.path.join(repo, "real.txt"), "w") as f:
                f.write("data")
            os.symlink("does-not-exist.svg", os.path.join(repo, "dangling.svg"))
            os.symlink("real.txt", os.path.join(repo, "valid_link.txt"))
            copy = get_fresh_repo_copy(repo, tmp)
            self.assertTrue(os.path.isfile(os.path.join(copy, "a.py")))
            with open(os.path.join(copy, "valid_link.txt")) as f:
                self.assertEqual(f.read(), "data")  # valid links are still followed, as before
            self.assertFalse(os.path.lexists(os.path.join(copy, "dangling.svg")))


class RuntimeSwitches(unittest.TestCase):
    def test_defaults_are_unchanged_and_overrides_are_opt_in(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in (runtime.IMAGE_ENV, runtime.RUN_TIMEOUT_ENV, runtime.NO_EDIT_ENV):
                os.environ.pop(name, None)
            rt = runtime.DockerRuntime(timeout_scale=3.0)
            self.assertEqual(rt.image, runtime.image_tag(runtime.DEFAULT_PLATFORM))
            captured = {}
            rt.call = lambda action, payload, timeout: captured.update(payload=payload) or {
                "ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            rt.run_project("x.py", 60)
            self.assertEqual(captured["payload"]["timeout"], 180)  # 60 s x scale 3, as before

    def test_image_and_run_timeout_overrides(self):
        with mock.patch.dict(os.environ, {runtime.IMAGE_ENV: "my/image:1", runtime.RUN_TIMEOUT_ENV: "400"}):
            rt = runtime.DockerRuntime(timeout_scale=3.0)
            self.assertEqual(rt.image, "my/image:1")
            captured = {}
            rt.call = lambda action, payload, timeout: captured.update(payload=payload) or {
                "ok": True, "returncode": 0, "stdout": "", "stderr": ""}
            rt.run_project("x.py", 60)
            self.assertEqual(captured["payload"]["timeout"], 1200)

    def test_protected_target_cannot_be_edited(self):
        with mock.patch.dict(os.environ, {runtime.NO_EDIT_ENV: "envbench_check.py"}):
            rt = runtime.DockerRuntime()
            rt.call = lambda *a, **k: self.fail("an edit of a protected file must not reach the container")
            dispatch = rt.build_dispatch("envbench_check.py", "/py", None)
            result = dispatch["edit_code"](edits=[{"find": "x", "replace": "y"}])
            self.assertFalse(result["ok"])
            self.assertEqual(result["refusal_reason"], "protected_target")


def row(**kw) -> dict:
    base = {"repository": "a/b", "discovered": True, "fixed": False, "error": "", "fix_actions": [],
            "runtime_observed_error": "", "runtime_observed_reason": "", "initial_error": "", "llm_calls": 4}
    base.update(kw)
    return base


MISSING = "ModuleNotFoundError: No module named 'yaml' [EnvBench check: %d missing-import diagnostics, %d modules]"


class Analysis(unittest.TestCase):
    def test_issue_counts_are_read_from_the_final_line(self):
        self.assertEqual(ae.issues_in(MISSING % (17, 6)), (17, 6))
        self.assertEqual(ae.issues_in("TypeError: x"), (None, None))
        self.assertEqual(ae.issues_in(""), (None, None))

    def test_buckets(self):
        passed = ae.classify(row(fixed=True, passed_without_agent_action=True), None)
        fixed = ae.classify(row(fixed=True, fix_actions=[{"tool": "install_package", "confidence": "high",
                                                         "grounding": "metadata_grounded"}]), None)
        missing = ae.classify(row(runtime_observed_error=MISSING % (3, 2), runtime_observed_reason="dependency:missing_module"), None)
        local = ae.classify(row(runtime_observed_error="ImportError: only repository-local or relative imports are "
                                "unresolved (a._v) [EnvBench check: 1 missing-import diagnostics, 1 modules]"), None)
        broken = ae.classify(row(runtime_observed_error="EnvBenchCheckError: the EnvBench check could not run: x"), None)
        timeout = ae.classify(row(runtime_observed_error="TimeoutExpired: process exceeded 1200s and was killed",
                                  runtime_observed_reason="cell_timeout"), None)
        self.assertEqual(passed["bucket"], "passed_as_is")
        self.assertEqual(fixed["bucket"], "fixed_by_tool")
        self.assertEqual(missing["bucket"], "unfixed_missing_imports_remain")
        self.assertEqual(local["bucket"], "unfixed_only_local_or_generated")
        self.assertEqual(broken["bucket"], "unfixed_check_could_not_run")
        self.assertEqual(timeout["bucket"], "unfixed_check_timed_out")
        self.assertTrue(passed["pass"] and fixed["pass"])
        self.assertFalse(missing["pass"] or local["pass"] or broken["pass"] or timeout["pass"])

    def test_not_run_is_a_failure_in_the_denominator_but_its_own_bucket(self):
        gone = ae.classify(row(discovered=False, error="clone failed: GitHub: gone; HF fallback: too large"), None)
        err = ae.classify(row(stop_reason="error", error="LLM call failed: connection reset"), None)
        container = ae.classify(row(error="container unavailable: x"), None)
        for c in (gone, err, container):
            self.assertEqual(c["bucket"], "not_run")
            self.assertFalse(c["pass"])
        summary = ae.analyse([row(repository=f"a/{i}", fixed=(i == 0), passed_without_agent_action=(i == 0)) for i in range(4)] +
                             [row(repository="a/gone", discovered=False, error="clone failed")], {})
        self.assertEqual(summary["n_repositories"], 5)
        self.assertEqual(summary["pass"]["k"], 1)
        self.assertEqual(summary["n_not_run"], 1)

    def test_progress_is_measured_by_the_reduction_of_the_issue_count(self):
        c = ae.classify(row(initial_error=MISSING % (134, 20), runtime_observed_error=MISSING % (4, 2)), None)
        self.assertEqual((c["issues_initial"], c["issues_final"]), (134, 4))

    def test_leaderboard_numbers_are_internally_consistent(self):
        for name, k, of, _pct, _src in ae.LEADERBOARD:
            self.assertEqual(of, 329, name)
            self.assertEqual(round(100 * k / of, 2), _pct, name)


if __name__ == "__main__":
    unittest.main()
