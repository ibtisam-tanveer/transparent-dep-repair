"""TASK_prepilot_fixes.md #2 (refuse name-collision installs) and #3
(working-directory resolution for repo-local imports)."""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nbformat
from nbformat.v4 import new_code_cell, new_notebook

from repair_tool import agent_tools, workdir
from repair_tool.localmods import find_local_module
from repair_tool.runner import run_project


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_nb(path, source):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    nbformat.write(new_notebook(cells=[new_code_cell(source)]), path)


class TestFindLocalModule(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_finds_a_py_file_anywhere_and_returns_its_relative_path(self):
        _write(os.path.join(self.repo, "src", "combat.py"))
        self.assertEqual(find_local_module("combat", self.repo), os.path.join("src", "combat.py"))

    def test_matches_requirement_strings_and_dotted_names(self):
        _write(os.path.join(self.repo, "src", "combat.py"))
        self.assertIsNotNone(find_local_module("combat==0.3.0", self.repo))
        self.assertIsNotNone(find_local_module("combat.pycombat", self.repo))

    def test_pypi_dash_names_match_underscore_modules(self):
        _write(os.path.join(self.repo, "my_helper.py"))
        self.assertIsNotNone(find_local_module("my-helper", self.repo))

    def test_strict_mode_ignores_a_data_directory_that_merely_shares_a_name(self):
        os.makedirs(os.path.join(self.repo, "figures", "seaborn"))
        _write(os.path.join(self.repo, "figures", "seaborn", "plot1.png"))
        self.assertIsNotNone(find_local_module("seaborn", self.repo))  # broad (classification) form
        self.assertIsNone(find_local_module("seaborn", self.repo, require_python_content=True))

    def test_strict_mode_accepts_a_real_package_directory(self):
        _write(os.path.join(self.repo, "mypkg", "__init__.py"))
        self.assertIsNotNone(find_local_module("mypkg", self.repo, require_python_content=True))

    def test_no_match_returns_none(self):
        self.assertIsNone(find_local_module("nothing_here", self.repo))


class TestInstallGuard(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        _write(os.path.join(self.repo, "src", "combat.py"))

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_installing_a_name_the_repo_provides_is_refused_without_calling_pip(self):
        with patch("repair_tool.agent_tools.apply_module.apply") as apply_mock:
            result = agent_tools.install_package("combat", "/fake/python", repo_root=self.repo)
        apply_mock.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertTrue(result["refused"])
        self.assertEqual(result["refusal_reason"], "repo_local_module")
        self.assertEqual(result["local_module_path"], os.path.join("src", "combat.py"))
        self.assertIn("Do not retry", result["log"])

    def test_a_version_pinned_request_for_the_same_name_is_refused_too(self):
        with patch("repair_tool.agent_tools.apply_module.apply") as apply_mock:
            result = agent_tools.install_package("combat==0.3.0", "/fake/python", repo_root=self.repo)
        apply_mock.assert_not_called()
        self.assertTrue(result["refused"])

    def test_a_real_dependency_is_still_installed(self):
        with patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "ok")) as apply_mock:
            result = agent_tools.install_package("pandas", "/fake/python", repo_root=self.repo)
        apply_mock.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("refused"))

    def test_without_a_repo_root_nothing_is_checked(self):
        with patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "ok")) as apply_mock:
            agent_tools.install_package("combat", "/fake/python")
        apply_mock.assert_called_once()

    def test_the_dispatch_answers_a_repeated_refused_install_without_rechecking(self):
        dispatch = agent_tools.build_dispatch("/fake/ws.py", "/fake/python", repo_root=self.repo)
        with patch("repair_tool.agent_tools.apply_module.apply") as apply_mock:
            first = dispatch["install_package"](package="combat")
            second = dispatch["install_package"](package="Combat")
        apply_mock.assert_not_called()
        self.assertTrue(first["refused"])
        self.assertTrue(second["refused"])
        self.assertIn("Already refused", second["log"])

    def test_a_local_foo_py_and_a_notebook_importing_foo_never_pip_installs_foo(self):
        """The task's regression: repo with a local foo.py + `import foo`."""
        _write(os.path.join(self.repo, "foo.py"), "X = 1\n")
        dispatch = agent_tools.build_dispatch("/fake/ws.py", "/fake/python", repo_root=self.repo)
        with patch("repair_tool.agent_tools.apply_module.apply") as apply_mock:
            for _ in range(3):  # the agent hammering the same install, as it did 22 times
                dispatch["install_package"](package="foo")
        apply_mock.assert_not_called()


class TestWorkdirParsing(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        workdir.reset()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        workdir.reset()

    def test_relative_sys_path_entries_are_extracted_from_a_notebook(self):
        nb = os.path.join(self.repo, "notebooks", "old", "a.ipynb")
        _write_nb(nb, 'import sys\nsys.path.append("../src/")\nsys.path.insert(0, "../lib")\nsys.path.append("/abs/path")')
        self.assertEqual(workdir.relative_sys_path_entries(nb), ["../src/", "../lib"])

    def test_candidates_prefer_the_ancestor_whose_sys_path_entry_reaches_the_module(self):
        _write(os.path.join(self.repo, "src", "combat.py"))
        nb = os.path.join(self.repo, "notebooks", "old", "a.ipynb")
        _write_nb(nb, 'import sys\nsys.path.append("../src/")\nimport combat')
        candidates = workdir.candidate_working_dirs(nb, self.repo, "combat")
        self.assertEqual(candidates[0][0], os.path.realpath(os.path.join(self.repo, "notebooks")))
        self.assertEqual(candidates[0][1], workdir.SOURCE_SYS_PATH)

    def test_no_candidates_when_nothing_could_make_the_module_reachable(self):
        _write(os.path.join(self.repo, "src", "combat.py"))
        nb = os.path.join(self.repo, "notebooks", "a.ipynb")
        _write_nb(nb, "import combat")
        self.assertEqual(workdir.candidate_working_dirs(nb, self.repo, "combat"), [])


class TestWorkdirResolutionEndToEnd(unittest.TestCase):
    """Real kernels / real subprocesses: the covid layout in miniature."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        workdir.reset()
        _write(os.path.join(self.repo, "src", "combat.py"), "VALUE = 42\n")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        workdir.reset()

    def _covid_notebook(self, name="old/a.ipynb"):
        path = os.path.join(self.repo, "notebooks", name)
        _write_nb(path, 'import sys\nsys.path.append("../src/")\nimport combat\nprint("VALUE", combat.VALUE)')
        return path

    def test_the_notebook_fails_from_its_own_folder_without_resolution(self):
        """Control: this is the pre-fix behaviour -- the false 'local module' failure."""
        result = run_project(self._covid_notebook(), timeout=60)
        self.assertFalse(result.ok)
        self.assertIn("No module named 'combat'", result.stderr)

    def test_it_runs_once_given_the_directory_its_sys_path_entries_expect(self):
        path = self._covid_notebook()
        result = run_project(path, timeout=60, repo_root=self.repo)
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("VALUE 42", result.stdout)
        self.assertEqual(result.cwd, os.path.realpath(os.path.join(self.repo, "notebooks")))
        self.assertEqual(result.cwd_source, workdir.SOURCE_SYS_PATH)
        info = workdir.describe(path, self.repo)
        self.assertTrue(info["working_directory_recovered"])
        self.assertEqual(info["working_directory"], "notebooks")
        self.assertEqual(info["working_directory_module"], "combat")

    def test_the_resolution_is_remembered_so_later_runs_do_not_search_again(self):
        path = self._covid_notebook()
        run_project(path, timeout=60, repo_root=self.repo)  # searches + caches
        with patch("repair_tool.workdir.candidate_working_dirs") as candidates:
            again = run_project(path, timeout=60, repo_root=self.repo)
        candidates.assert_not_called()
        self.assertTrue(again.ok, again.stderr)

    def test_a_script_gets_the_same_treatment(self):
        script = os.path.join(self.repo, "scripts", "old", "s.py")
        _write(script, 'import sys\nsys.path.append("../src/")\nimport combat\nprint(combat.VALUE)\n')
        # scripts/old -> ../src is scripts/src (missing); scripts -> ../src is <repo>/src
        result = run_project(script, timeout=60, python_exe=sys.executable, repo_root=self.repo)
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.cwd, os.path.realpath(os.path.join(self.repo, "scripts")))

    def test_an_unresolvable_local_import_stays_failing_with_the_real_reason(self):
        """Honest limit: nothing makes this resolvable, so it is NOT
        recovered and the caller keeps classifying it as repo-local."""
        path = os.path.join(self.repo, "notebooks", "b.ipynb")
        _write_nb(path, "import combat")
        result = run_project(path, timeout=60, repo_root=self.repo)
        self.assertFalse(result.ok)
        self.assertIn("No module named 'combat'", result.stderr)
        info = workdir.describe(path, self.repo)
        self.assertFalse(info["working_directory_recovered"])

    def test_a_non_local_missing_module_triggers_no_directory_search(self):
        path = os.path.join(self.repo, "notebooks", "c.ipynb")
        _write_nb(path, "import definitely_not_a_real_package_xyz")
        with patch("repair_tool.workdir.candidate_working_dirs") as candidates:
            result = run_project(path, timeout=60, repo_root=self.repo)
        candidates.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(workdir.describe(path, self.repo), {})

    def test_a_passing_notebook_is_untouched(self):
        path = os.path.join(self.repo, "notebooks", "ok.ipynb")
        _write_nb(path, "print('fine')")
        result = run_project(path, timeout=60, repo_root=self.repo)
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.cwd_source, workdir.SOURCE_FILE_DIR)


class TestResultRowCarriesTheNewFields(unittest.TestCase):
    def test_build_result_row_records_working_directory_and_refused_installs(self):
        from evaluation.run_eval import build_result_row
        from repair_tool.agent import AgentResult, TraceStep

        step = TraceStep(
            thought="", tool_called="install_package", tool_input={"package": "combat"},
            tool_result={"ok": False, "refused": True}, grounding="metadata_grounded",
            verification="unverified", confidence="medium",
        )
        result = AgentResult(
            target="notebooks/old/a.ipynb", fixed=True, trace=[step],
            working_directory={
                "working_directory": "notebooks", "working_directory_source": "inferred_from_sys_path",
                "working_directory_recovered": True, "working_directory_module": "combat",
            },
        )
        row = build_result_row(
            "x/y", "u", {"notebook_path": "notebooks/old/a.ipynb", "category": "A", "tier": "confirmed"},
            result, {}, "run1", 30, False,
        )
        self.assertEqual(row["refused_local_module_installs"], 1)
        self.assertEqual(row["working_directory"], "notebooks")
        self.assertTrue(row["working_directory_recovered"])
        self.assertEqual(row["working_directory_source"], "inferred_from_sys_path")


if __name__ == "__main__":
    unittest.main()


class TestPostBuild(unittest.TestCase):
    """TASK_prepilot_fixes.md #4: binder/postBuild, as repo2docker runs it."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def _script(self, body, where=("binder", "postBuild")):
        _write(os.path.join(self.repo, *where), "#!/bin/bash\n" + textwrap.dedent(body))

    def test_finds_binder_then_dot_binder_then_root(self):
        from repair_tool.repo import find_postbuild

        self.assertIsNone(find_postbuild(self.repo))
        _write(os.path.join(self.repo, "postBuild"), "")
        self.assertTrue(find_postbuild(self.repo).endswith(os.path.join(self.repo, "postBuild")))
        _write(os.path.join(self.repo, ".binder", "postBuild"), "")
        self.assertIn(".binder", find_postbuild(self.repo))
        _write(os.path.join(self.repo, "binder", "postBuild"), "")
        self.assertIn(os.sep + "binder" + os.sep, find_postbuild(self.repo))

    def test_no_postbuild_is_reported_as_not_found(self):
        from repair_tool.repo import run_postbuild

        result = run_postbuild(self.repo, sys.executable, self.home)
        self.assertEqual((result["found"], result["ran"], result["ok"]), (False, False, False))

    def test_it_runs_from_the_repo_root_with_the_envs_python_first_and_a_throwaway_home(self):
        from repair_tool.repo import run_postbuild

        self._script('pwd > "$PWD/ran_here.txt"\necho "$PATH" | cut -d: -f1 > "$PWD/first_path.txt"\ntouch "$HOME/marker"\n')
        result = run_postbuild(self.repo, "/some/env/bin/python", self.home)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["path"], os.path.join("binder", "postBuild"))
        self.assertEqual(os.path.realpath(open(os.path.join(self.repo, "ran_here.txt")).read().strip()), os.path.realpath(self.repo))
        self.assertEqual(open(os.path.join(self.repo, "first_path.txt")).read().strip(), "/some/env/bin")
        self.assertTrue(os.path.exists(os.path.join(self.home, "marker")), "must write under the throwaway HOME")
        self.assertFalse(os.path.exists(os.path.join(os.path.expanduser("~"), "marker_from_postbuild_test")))

    def test_a_failing_script_is_recorded_not_raised(self):
        from repair_tool.repo import run_postbuild

        self._script("echo about to fail >&2\nexit 7\n")
        result = run_postbuild(self.repo, sys.executable, self.home)
        self.assertEqual((result["found"], result["ran"], result["ok"], result["returncode"]), (True, True, False, 7))
        self.assertIn("about to fail", result["log_tail"])

    def test_a_hanging_script_is_killed_at_the_timeout_with_no_orphan(self):
        from repair_tool.repo import run_postbuild
        from tests.test_proc import _read_pid, _wait_dead

        pidfile = os.path.join(self.repo, "child.pid")
        self._script(f"sleep 300 &\necho $! > {pidfile}\nwait\n")
        started = __import__("time").time()
        result = run_postbuild(self.repo, sys.executable, self.home, timeout=2)
        self.assertLess(__import__("time").time() - started, 30)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["log_tail"])
        self.assertTrue(_wait_dead(_read_pid(pidfile)))


class TestBudgetSizedByLabelledNotebooks(unittest.TestCase):
    """TASK_prepilot_fixes.md #4: a one-notebook target must not get a
    whole-repo-sized budget, and unlabelled notebooks must not eat it."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        for name in ("a.ipynb", "b.ipynb", "c.ipynb", "d.ipynb"):
            _write_nb(os.path.join(self.repo, "notebooks", name), "print(1)")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_count_target_files_counts_only_the_labelled_notebooks(self):
        from evaluation._repo_runner import count_discovered_files, count_target_files

        self.assertEqual(count_discovered_files(self.repo), 4)
        self.assertEqual(count_target_files(self.repo, ["notebooks/b.ipynb"]), 1)
        self.assertEqual(count_target_files(self.repo, ["notebooks/b.ipynb", "notebooks/zzz.ipynb"]), 1)

    def test_only_paths_restricts_the_run_to_the_labelled_notebooks(self):
        from evaluation._repo_runner import run_repo_incrementally
        from repair_tool.agent import AgentResult

        tmp_root = tempfile.mkdtemp()
        try:
            with patch("evaluation._repo_runner.get_fresh_venv_python", return_value=sys.executable), \
                 patch("evaluation._repo_runner.build_repair_environment") as env, \
                 patch("evaluation._repo_runner.repo_module._install_dependencies", return_value=(True, "", "")), \
                 patch("evaluation._repo_runner.run_project") as run:
                env.return_value.python_exe = sys.executable
                env.return_value.meta = {}
                run.return_value.ok = True
                seen = [r.target for r, _ in run_repo_incrementally(
                    self.repo, tmp_root, 5, 1000, only_paths={os.path.join("notebooks", "b.ipynb")},
                )]
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
        self.assertEqual(seen, [os.path.join("notebooks", "b.ipynb")])
        self.assertEqual(run.call_count, 1)


class TestDescribeException(unittest.TestCase):
    def test_includes_the_innermost_frame(self):
        def boom():
            return None["x"]

        try:
            boom()
        except TypeError as exc:
            text = workdir.describe_exception(exc)
        self.assertTrue(text.startswith("TypeError:"))
        self.assertIn("test_prepilot_fixes.py", text)
        self.assertIn("in boom", text)


class TestImportabilityAwareLocalModules(unittest.TestCase):
    """Found in the pilot: TeachOpenCADD's T014 notebook needs the PyPI package
    `nglview`, but the repo has `T018/.../utils/helpers/nglview.py` -- unreachable
    from T014 -- and the whole-repo name match both misclassified the notebook as
    repo-local and made the install guard refuse the install."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        _write(os.path.join(self.repo, "pkg", "T018", "utils", "helpers", "nglview.py"), "X = 1\n")
        self.nb = os.path.join(self.repo, "pkg", "T014", "t14.ipynb")
        _write_nb(self.nb, "import nglview")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_a_same_named_file_in_an_unrelated_subtree_is_not_importable_from_the_notebook(self):
        self.assertIsNotNone(find_local_module("nglview", self.repo))  # the broad whole-repo match
        self.assertIsNone(find_local_module("nglview", self.repo, from_file=self.nb))

    def test_the_install_guard_lets_the_real_package_through(self):
        with patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "ok")) as apply_mock:
            result = agent_tools.install_package("nglview", "/fake/python", repo_root=self.repo, target_path=self.nb)
        apply_mock.assert_called_once()
        self.assertFalse(result.get("refused"))

    def test_a_module_in_the_notebooks_own_folder_or_an_ancestor_is_local(self):
        _write(os.path.join(self.repo, "pkg", "T014", "sibling.py"))
        _write(os.path.join(self.repo, "pkg", "shared.py"))
        _write(os.path.join(self.repo, "rootmod.py"))
        for name in ("sibling", "shared", "rootmod"):
            with self.subTest(name=name):
                self.assertIsNotNone(find_local_module(name, self.repo, from_file=self.nb))

    def test_a_module_reached_through_the_notebooks_own_sys_path_entry_is_local(self):
        _write(os.path.join(self.repo, "src", "combat.py"))
        nb = os.path.join(self.repo, "notebooks", "old", "a.ipynb")
        _write_nb(nb, 'import sys\nsys.path.append("../../src/")\nimport combat')
        self.assertIsNotNone(find_local_module("combat", self.repo, from_file=nb))
        with patch("repair_tool.agent_tools.apply_module.apply") as apply_mock:
            result = agent_tools.install_package("combat", "/fake/python", repo_root=self.repo, target_path=nb)
        apply_mock.assert_not_called()
        self.assertTrue(result["refused"])

    def test_without_a_path_to_it_the_same_src_module_is_not_importable(self):
        _write(os.path.join(self.repo, "src", "combat.py"))
        nb = os.path.join(self.repo, "notebooks", "b.ipynb")
        _write_nb(nb, "import combat")
        self.assertIsNone(find_local_module("combat", self.repo, from_file=nb))

    def test_a_file_outside_the_repo_falls_back_to_the_broad_match(self):
        self.assertIsNotNone(find_local_module("nglview", self.repo, from_file="/somewhere/else/ws.ipynb"))

    def test_the_runtime_classification_uses_the_same_rule(self):
        from evaluation.run_eval import _looks_like_local_module

        self.assertFalse(_looks_like_local_module("nglview", self.repo, os.path.join("pkg", "T014", "t14.ipynb")))
        self.assertTrue(_looks_like_local_module("nglview", self.repo))  # no target -> broad (old behaviour)
