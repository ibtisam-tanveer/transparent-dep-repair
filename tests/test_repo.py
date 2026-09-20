import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import venv_manager
from repair_tool.repo import (
    RepoResult,
    _install_dependencies,
    _is_venv_dir,
    _looks_like_a_git_url,
    _pyproject_declares_a_package,
    analyze_repo,
    analyze_repo_url,
    discover_runnable_files,
    find_dependency_files,
)

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_REPO = os.path.join(ROOT, "tests", "fixtures", "sample_repo")
SAMPLE_REPO_WITH_DEPS = os.path.join(ROOT, "tests", "fixtures", "sample_repo_with_deps")


class TestDiscovery(unittest.TestCase):
    def test_finds_py_and_ipynb_excludes_tests_and_setup(self):
        found = discover_runnable_files(SAMPLE_REPO)
        self.assertIn("good.py", found)
        self.assertIn("bad_missing_import.py", found)
        self.assertIn(os.path.join("notebooks", "good.ipynb"), found)
        self.assertNotIn(os.path.join("tests", "test_should_be_excluded.py"), found)
        self.assertEqual(len(found), 3)

    def test_excludes_hidden_and_venv_directories(self):
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, ".git"))
            os.makedirs(os.path.join(tmpdir, ".venv", "lib"))
            os.makedirs(os.path.join(tmpdir, "__pycache__"))
            with open(os.path.join(tmpdir, ".git", "hook.py"), "w") as f:
                f.write("raise RuntimeError('must not run')")
            with open(os.path.join(tmpdir, ".venv", "lib", "site.py"), "w") as f:
                f.write("raise RuntimeError('must not run')")
            with open(os.path.join(tmpdir, "__pycache__", "cached.py"), "w") as f:
                f.write("raise RuntimeError('must not run')")
            with open(os.path.join(tmpdir, "real.py"), "w") as f:
                f.write("print('ok')")
            found = discover_runnable_files(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(found, ["real.py"])

    def test_excludes_setup_py_itself(self):
        tmpdir = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmpdir, "setup.py"), "w") as f:
                f.write("raise RuntimeError('must not run as a discovered file')")
            found = discover_runnable_files(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(found, [])

    def test_excludes_a_custom_named_venv_by_structure_not_name(self):
        # Reproduces a real bug found manually: a venv folder named
        # "myenv" (not one of the hardcoded names) got walked into, and
        # every vendored library file inside it got treated as a
        # discoverable project file. Every real venv has a pyvenv.cfg
        # directly inside it, regardless of what the folder is called --
        # that's what _is_venv_dir checks instead of the name.
        tmpdir = tempfile.mkdtemp()
        try:
            venv_dir = os.path.join(tmpdir, "myenv")
            vendored = os.path.join(venv_dir, "Lib", "site-packages", "pip", "_vendor", "urllib3")
            os.makedirs(vendored)
            with open(os.path.join(venv_dir, "pyvenv.cfg"), "w") as f:
                f.write("home = /usr/bin\n")
            with open(os.path.join(vendored, "poolmanager.py"), "w") as f:
                f.write("raise RuntimeError('vendored library code, must never be discovered')")
            with open(os.path.join(tmpdir, "training.py"), "w") as f:
                f.write("print('real project file')")

            found = discover_runnable_files(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        self.assertEqual(found, ["training.py"])

    def test_is_venv_dir_true_only_with_pyvenv_cfg_present(self):
        tmpdir = tempfile.mkdtemp()
        try:
            self.assertFalse(_is_venv_dir(tmpdir))
            with open(os.path.join(tmpdir, "pyvenv.cfg"), "w") as f:
                f.write("home = /usr/bin\n")
            self.assertTrue(_is_venv_dir(tmpdir))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_excludes_site_packages_and_conda_meta_by_name_as_a_second_safety_net(self):
        tmpdir = tempfile.mkdtemp()
        try:
            for name in ["site-packages", "conda-meta"]:
                d = os.path.join(tmpdir, name)
                os.makedirs(d)
                with open(os.path.join(d, "vendored.py"), "w") as f:
                    f.write("raise RuntimeError('must not run')")
            with open(os.path.join(tmpdir, "real.py"), "w") as f:
                f.write("print('ok')")

            found = discover_runnable_files(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        self.assertEqual(found, ["real.py"])


class TestFindDependencyFiles(unittest.TestCase):
    def test_none_found_in_a_bare_repo(self):
        self.assertEqual(find_dependency_files(SAMPLE_REPO), [])

    def test_priority_order_when_several_exist(self):
        tmpdir = tempfile.mkdtemp()
        try:
            for name in ["setup.py", "requirements.txt", "environment.yml"]:
                open(os.path.join(tmpdir, name), "w").close()
            found = find_dependency_files(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        # requirements.txt outranks setup.py outranks environment.yml
        self.assertEqual(found, ["requirements.txt", "setup.py", "environment.yml"])

    def test_finds_requirements_txt(self):
        self.assertEqual(find_dependency_files(SAMPLE_REPO_WITH_DEPS), ["requirements.txt"])


class TestPyprojectDetection(unittest.TestCase):
    def test_tool_config_only_is_not_a_package(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "pyproject.toml")
            with open(path, "w") as f:
                f.write("[tool.ruff]\nline-length = 100\n")
            self.assertFalse(_pyproject_declares_a_package(path))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_project_section_is_a_package(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "pyproject.toml")
            with open(path, "w") as f:
                f.write('[project]\nname = "x"\nversion = "0.1"\n')
            self.assertTrue(_pyproject_declares_a_package(path))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_poetry_section_is_a_package(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "pyproject.toml")
            with open(path, "w") as f:
                f.write('[tool.poetry]\nname = "x"\n')
            self.assertTrue(_pyproject_declares_a_package(path))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestInstallDependenciesOffline(unittest.TestCase):
    def test_nothing_found_is_a_clean_no_op(self):
        ok, log, used = _install_dependencies("/fake/python", "/fake/repo", [], timeout=30)
        self.assertTrue(ok)
        self.assertEqual(used, "")

    def test_unsupported_formats_only_reported_not_attempted(self):
        with patch("repair_tool.repo._run_pip_install") as mock_pip:
            ok, log, used = _install_dependencies(
                "/fake/python", "/fake/repo", ["environment.yml", "Pipfile"], timeout=30
            )
        mock_pip.assert_not_called()
        self.assertTrue(ok)
        self.assertEqual(used, "")
        self.assertIn("environment.yml", log)

    @patch("repair_tool.repo._run_pip_install")
    def test_requirements_txt_is_used_when_present(self, mock_pip):
        mock_pip.return_value = (True, "installed")
        ok, log, used = _install_dependencies(
            "/fake/python", "/fake/repo", ["requirements.txt", "setup.py"], timeout=30
        )
        self.assertEqual(used, "requirements.txt")
        mock_pip.assert_called_once()
        self.assertIn("-r", mock_pip.call_args[0][1])

    def test_pyproject_without_package_falls_through_to_next_candidate(self):
        tmpdir = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
                f.write("[tool.ruff]\n")
            with patch("repair_tool.repo._run_pip_install") as mock_pip:
                mock_pip.return_value = (True, "installed")
                ok, log, used = _install_dependencies(
                    "/fake/python", tmpdir, ["pyproject.toml", "requirements.txt"], timeout=30
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(used, "requirements.txt")  # skipped the tool-config pyproject.toml


class TestAnalyzeRepoOffline(unittest.TestCase):
    def test_bad_repo_path_is_a_clean_failure(self):
        result = analyze_repo("/no/such/repo/path")
        self.assertFalse(result.env_setup_ok)
        self.assertEqual(result.files, [])

    @patch("repair_tool.repo.get_venv_python", return_value=sys.executable)
    def test_one_bad_file_does_not_abort_the_others(self, _mock_venv):
        result = analyze_repo(SAMPLE_REPO)
        self.assertEqual(len(result.files), 3)
        by_path = {f.path: f for f in result.files}
        self.assertTrue(by_path["good.py"].run_result.ok)
        self.assertFalse(by_path["bad_missing_import.py"].run_result.ok)
        self.assertEqual(by_path["bad_missing_import.py"].diagnosis.kind, "missing_module")
        self.assertIsNone(by_path["good.py"].diagnosis)

    @patch("repair_tool.repo.get_venv_python", return_value=sys.executable)
    def test_summary_counts_are_correct(self, _mock_venv):
        result = analyze_repo(SAMPLE_REPO)
        self.assertEqual(result.summary["total"], 3)
        self.assertEqual(result.summary["ran"], 2)
        self.assertEqual(result.summary["failed"], 1)
        self.assertEqual(result.summary["failures_by_kind"], {"missing_module": 1})

    @patch("repair_tool.repo.get_venv_python", return_value=sys.executable)
    def test_original_repo_files_are_never_modified(self, _mock_venv):
        before = {}
        for root, _, files in os.walk(SAMPLE_REPO):
            for name in files:
                path = os.path.join(root, name)
                before[path] = os.path.getmtime(path)

        analyze_repo(SAMPLE_REPO)

        for path, mtime in before.items():
            self.assertEqual(os.path.getmtime(path), mtime, msg=f"{path} was modified")

    @patch("repair_tool.repo.get_venv_python", return_value=sys.executable)
    def test_pass_rule_is_configurable_not_hard_coded(self, _mock_venv):
        strict = analyze_repo(SAMPLE_REPO, pass_rule=lambda r: r.summary["failed"] == 0)
        self.assertFalse(strict.passed)

        lenient = analyze_repo(SAMPLE_REPO, pass_rule=lambda r: r.summary["ran"] > 0)
        self.assertTrue(lenient.passed)

        no_rule = analyze_repo(SAMPLE_REPO)
        self.assertIsNone(no_rule.passed)  # summary/per-file results still fully available
        self.assertEqual(no_rule.summary["total"], 3)

    @patch("repair_tool.repo.get_venv_python", return_value=sys.executable)
    def test_a_pass_rule_that_raises_does_not_crash_the_analysis(self, _mock_venv):
        def broken_rule(r):
            raise ValueError("oops")

        result = analyze_repo(SAMPLE_REPO, pass_rule=broken_rule)
        self.assertIsNone(result.passed)
        self.assertEqual(len(result.files), 3)  # analysis itself still completed


class TestLooksLikeAGitUrl(unittest.TestCase):
    def test_recognises_urls(self):
        self.assertTrue(_looks_like_a_git_url("https://github.com/x/y.git"))
        self.assertTrue(_looks_like_a_git_url("https://github.com/x/y"))
        self.assertTrue(_looks_like_a_git_url("git@github.com:x/y.git"))
        self.assertTrue(_looks_like_a_git_url("ssh://git@example.com/x/y.git"))

    def test_does_not_flag_a_local_path(self):
        self.assertFalse(_looks_like_a_git_url("/some/local/repo"))
        self.assertFalse(_looks_like_a_git_url("tests/fixtures/sample_repo"))


def _make_local_git_repo(with_file_content: str) -> str:
    """A tiny real git repo on disk, so analyze_repo_url's `git clone` path
    can be exercised offline (git clones a local path just as well as a
    remote URL -- no network needed for this)."""
    path = tempfile.mkdtemp(prefix="test_repo_clone_source_")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    with open(os.path.join(path, "good.py"), "w") as f:
        f.write(with_file_content)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)
    return path


class TestAnalyzeRepoUrlOffline(unittest.TestCase):
    """Clones from a local git repo, not a remote one -- exercises the real
    `git clone` + analyze + cleanup path without needing network."""

    @patch("repair_tool.repo.get_venv_python", return_value=sys.executable)
    def test_clones_analyzes_and_reports_the_original_identifier(self, _mock_venv):
        source = _make_local_git_repo("print('hello')\n")
        try:
            result = analyze_repo_url(source)
        finally:
            shutil.rmtree(source, ignore_errors=True)

        self.assertEqual(result.repo_path, source)  # not the (deleted) temp clone path
        self.assertEqual(len(result.files), 1)
        self.assertTrue(result.files[0].run_result.ok)

    @patch("repair_tool.repo.get_venv_python", return_value=sys.executable)
    def test_clone_is_deleted_after_analysis(self, _mock_venv):
        controlled_tmpdir = tempfile.mkdtemp(prefix="test_repo_clone_controlled_")
        source = _make_local_git_repo("print('hi')\n")
        try:
            with patch("repair_tool.repo.tempfile.mkdtemp", return_value=controlled_tmpdir):
                analyze_repo_url(source)
            self.assertFalse(os.path.isdir(controlled_tmpdir))
        finally:
            shutil.rmtree(source, ignore_errors=True)
            shutil.rmtree(controlled_tmpdir, ignore_errors=True)

    def test_bad_source_fails_gracefully_and_still_cleans_up(self):
        controlled_tmpdir = tempfile.mkdtemp(prefix="test_repo_clone_controlled_bad_")
        try:
            with patch("repair_tool.repo.tempfile.mkdtemp", return_value=controlled_tmpdir):
                result = analyze_repo_url("/no/such/repo/anywhere")
            self.assertFalse(result.env_setup_ok)
            self.assertEqual(result.files, [])
            self.assertFalse(os.path.isdir(controlled_tmpdir))
        finally:
            shutil.rmtree(controlled_tmpdir, ignore_errors=True)

    def test_clone_timeout_is_a_clean_failure_not_a_crash(self):
        with patch(
            "repair_tool.repo.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git clone", timeout=5),
        ):
            result = analyze_repo_url("https://example.com/whatever.git", clone_timeout=5)
        self.assertFalse(result.env_setup_ok)
        self.assertIn("timed out", result.env_setup_log)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
class TestAnalyzeRepoUrlNetwork(unittest.TestCase):
    def test_required_behaviour_real_github_url(self):
        # A tiny, extremely stable public repo (GitHub's own demo repo).
        result = analyze_repo_url("https://github.com/octocat/Hello-World.git")
        self.assertEqual(result.repo_path, "https://github.com/octocat/Hello-World.git")
        self.assertIn(result.env_setup_ok, (True, False))
        self.assertIsInstance(result.summary, dict)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
class TestAnalyzeRepoIntegration(unittest.TestCase):
    """Real venv, real pip installs against a real requirements.txt and a
    real minimal installable package."""

    def setUp(self):
        self.tmp_venv_root = tempfile.mkdtemp(prefix="test_repo_venvs_")
        self._orig_venv_root = venv_manager.VENV_ROOT
        venv_manager.VENV_ROOT = self.tmp_venv_root

    def tearDown(self):
        venv_manager.VENV_ROOT = self._orig_venv_root
        shutil.rmtree(self.tmp_venv_root, ignore_errors=True)

    def test_required_behaviour_requirements_txt_repo(self):
        result = analyze_repo(SAMPLE_REPO_WITH_DEPS)

        self.assertIn(result.env_setup_ok, (True, False))
        self.assertGreaterEqual(len(result.files), 1)
        self.assertEqual(result.dependency_file_used, "requirements.txt")
        self.assertTrue(result.env_setup_ok)
        for f in result.files:
            self.assertTrue(hasattr(f, "run_result"))
            if not f.run_result.ok:
                self.assertIsNotNone(f.diagnosis)
        self.assertTrue(result.summary)
        # the file needing seaborn actually succeeded once it was installed
        by_path = {f.path: f for f in result.files}
        self.assertTrue(by_path["uses_seaborn.py"].run_result.ok)

    def test_pyproject_toml_package_is_installed_and_importable(self):
        tmpdir = tempfile.mkdtemp(prefix="test_repo_pyproject_")
        try:
            with open(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
                f.write(
                    "[build-system]\n"
                    'requires = ["setuptools>=68"]\n'
                    'build-backend = "setuptools.build_meta"\n\n'
                    "[project]\n"
                    'name = "repo-fixture-pkg"\n'
                    'version = "0.1.0"\n'
                    'dependencies = ["seaborn"]\n'
                )
            with open(os.path.join(tmpdir, "check.py"), "w") as f:
                f.write("import seaborn as sns\nprint(sns.__version__)\n")

            result = analyze_repo(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        self.assertEqual(result.dependency_file_used, "pyproject.toml")
        self.assertTrue(result.env_setup_ok, msg=result.env_setup_log)
        by_path = {f.path: f for f in result.files}
        self.assertTrue(by_path["check.py"].run_result.ok, msg=by_path["check.py"].run_result.stderr)


if __name__ == "__main__":
    unittest.main()
