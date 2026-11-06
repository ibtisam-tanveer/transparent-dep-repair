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

    def test_finds_a_dependency_file_living_in_a_subdirectory(self):
        """TASK_version_decay_diagnosis.md Part B: a Binder-convention
        binder/environment.yml (not at the repo root) used to be invisible
        to this tool entirely -- exactly "declared dependencies ignored"."""
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "binder"))
            with open(os.path.join(tmpdir, "binder", "environment.yml"), "w") as f:
                f.write("dependencies: []\n")
            found = find_dependency_files(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(found, [os.path.join("binder", "environment.yml")])

    def test_a_root_level_file_is_preferred_over_a_nested_one_with_the_same_name(self):
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "sub"))
            with open(os.path.join(tmpdir, "requirements.txt"), "w") as f:
                f.write("root\n")
            with open(os.path.join(tmpdir, "sub", "requirements.txt"), "w") as f:
                f.write("nested\n")
            found = find_dependency_files(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(found, ["requirements.txt"])

    def test_does_not_descend_into_excluded_or_venv_directories(self):
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, ".venv"))
            with open(os.path.join(tmpdir, ".venv", "pyvenv.cfg"), "w") as f:
                f.write("home = /usr/bin\n")
            with open(os.path.join(tmpdir, ".venv", "requirements.txt"), "w") as f:
                f.write("vendored, must not be found\n")
            found = find_dependency_files(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(found, [])


class TestRepoLastCommitDate(unittest.TestCase):
    def test_a_real_git_repo_returns_its_head_commit_date(self):
        tmpdir = tempfile.mkdtemp()
        try:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=tmpdir, check=True)
            with open(os.path.join(tmpdir, "a.py"), "w") as f:
                f.write("print(1)\n")
            subprocess.run(["git", "add", "a.py"], cwd=tmpdir, check=True)
            env = {**os.environ, "GIT_AUTHOR_DATE": "2019-05-01T00:00:00", "GIT_COMMITTER_DATE": "2019-05-01T00:00:00"}
            subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=tmpdir, check=True, env=env)

            from repair_tool.repo import repo_last_commit_date

            self.assertEqual(repo_last_commit_date(tmpdir), "2019-05-01")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_not_a_git_repo_returns_none(self):
        from repair_tool.repo import repo_last_commit_date

        tmpdir = tempfile.mkdtemp()
        try:
            self.assertIsNone(repo_last_commit_date(tmpdir))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


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

    def test_pipfile_is_only_reported_never_attempted(self):
        with patch("repair_tool.repo._run_pip_install") as mock_pip:
            ok, log, used = _install_dependencies(
                "/fake/python", "/fake/repo", ["Pipfile"], timeout=30
            )
        mock_pip.assert_not_called()
        self.assertTrue(ok)
        self.assertEqual(used, "")
        self.assertIn("Pipfile", log)

    def test_an_environment_yml_with_no_pip_expressible_entries_is_reported_not_attempted(self):
        """A conda environment.yml whose every pin is conda-only (e.g. a
        build string, or a package with no PyPI equivalent at all -- a
        nonexistent file degrades to the same "nothing found" shape)."""
        with patch("repair_tool.repo._run_pip_install") as mock_pip:
            ok, log, used = _install_dependencies(
                "/fake/python", "/fake/repo", ["environment.yml"], timeout=30
            )
        mock_pip.assert_not_called()
        self.assertTrue(ok)
        self.assertEqual(used, "")
        self.assertIn("environment.yml", log)

    @patch("repair_tool.repo._run_pip_install")
    def test_environment_yml_pip_expressible_entries_are_installed_individually(self, mock_pip):
        """TASK_version_decay_diagnosis.md Part B: a real environment.yml's
        pip-expressible pins (conda name=version entries, plus its own
        `pip:` sub-list) must each be attempted -- one bad, conda-only pin
        (no real PyPI release matching it) must not block the others."""
        tmpdir = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmpdir, "environment.yml"), "w") as f:
                f.write(
                    "name: myenv\n"
                    "dependencies:\n"
                    "  - python=3.7\n"
                    "  - numpy=1.17.3\n"
                    "  - pyferret=7.5.0\n"
                    "  - pip:\n"
                    "    - ipywidgets==7.5.1\n"
                )
            mock_pip.side_effect = [(True, "numpy ok"), (False, "pyferret: no matching distribution"), (True, "ipywidgets ok")]
            ok, log, used = _install_dependencies("/fake/python", tmpdir, ["environment.yml"], timeout=30)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        self.assertTrue(ok)  # at least one spec succeeded
        self.assertEqual(used, "environment.yml")
        self.assertEqual(mock_pip.call_count, 3)
        installed_specs = [call.args[1][0] for call in mock_pip.call_args_list]
        self.assertEqual(installed_specs, ["numpy==1.17.3", "pyferret==7.5.0", "ipywidgets==7.5.1"])

    @patch("repair_tool.repo._run_pip_install")
    def test_requirements_txt_is_used_when_present(self, mock_pip):
        mock_pip.return_value = (True, "installed")
        ok, log, used = _install_dependencies(
            "/fake/python", "/fake/repo", ["requirements.txt", "setup.py"], timeout=30
        )
        self.assertIn("-r", mock_pip.call_args_list[0][0][1])

    @patch("repair_tool.repo._run_pip_install")
    def test_both_requirements_txt_and_setup_py_are_attempted_not_just_the_first(self, mock_pip):
        """TASK_version_decay_diagnosis.md Part B: a package-declaring
        setup.py (installs the repo's own package) and requirements.txt
        (installs its pinned third-party stack) answer different
        questions -- both should be attempted, not just the first found."""
        mock_pip.return_value = (True, "installed")
        ok, log, used = _install_dependencies(
            "/fake/python", "/fake/repo", ["requirements.txt", "setup.py"], timeout=30
        )
        self.assertTrue(ok)
        self.assertEqual(used, "requirements.txt, setup.py")
        self.assertEqual(mock_pip.call_count, 2)

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


class TestEnvironmentYmlPipSpecs(unittest.TestCase):
    def test_extracts_conda_pins_and_the_pip_sublist(self):
        from repair_tool.repo import _environment_yml_pip_specs

        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "environment.yml")
            with open(path, "w") as f:
                f.write(
                    "dependencies:\n"
                    "  - python=3.7\n"
                    "  - numpy=1.17.3\n"
                    "  - pandas=0.25.3\n"
                    "  - pip:\n"
                    "    - ipywidgets==7.5.1\n"
                    "    - ferretmagic==20181001\n"
                )
            specs = _environment_yml_pip_specs(path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(specs, ["numpy==1.17.3", "pandas==0.25.3", "ipywidgets==7.5.1", "ferretmagic==20181001"])

    def test_a_pinned_pip_entry_is_never_installed(self):
        """Regression test for a real bug found pinning this against
        ipython_ferretmagic's own binder/environment.yml: downgrading the
        *running* pip to its old conda-pinned version breaks pip's own
        internals on a modern Python (distutils was removed in 3.12),
        silently failing every install attempted after it in the batch."""
        from repair_tool.repo import _environment_yml_pip_specs

        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "environment.yml")
            with open(path, "w") as f:
                f.write("dependencies:\n  - numpy=1.17.3\n  - pip=19.3.1\n")
            specs = _environment_yml_pip_specs(path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(specs, ["numpy==1.17.3"])

    def test_a_build_string_pin_is_skipped_not_mistranslated(self):
        from repair_tool.repo import _environment_yml_pip_specs

        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "environment.yml")
            with open(path, "w") as f:
                f.write("dependencies:\n  - numpy=1.17.3=py37h2f8d375_0\n")
            specs = _environment_yml_pip_specs(path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(specs, [])

    def test_an_unreadable_file_returns_empty_not_a_crash(self):
        from repair_tool.repo import _environment_yml_pip_specs

        self.assertEqual(_environment_yml_pip_specs("/no/such/file.yml"), [])


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
