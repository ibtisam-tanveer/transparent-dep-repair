import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import venv_manager
from repair_tool.runner import run_project


class TestVenvDirFor(unittest.TestCase):
    def test_matches_get_venv_pythons_own_directory(self):
        tmp_venv_root = tempfile.mkdtemp()
        orig = venv_manager.VENV_ROOT
        venv_manager.VENV_ROOT = tmp_venv_root
        try:
            target = os.path.join(tmp_venv_root, "some_target.py")
            python_exe = venv_manager.get_venv_python(target)
            self.assertTrue(python_exe.startswith(venv_manager.venv_dir_for(target)))
        finally:
            venv_manager.VENV_ROOT = orig
            shutil.rmtree(tmp_venv_root, ignore_errors=True)

    def test_is_read_only_and_creates_nothing(self):
        tmp_venv_root = tempfile.mkdtemp()
        orig = venv_manager.VENV_ROOT
        venv_manager.VENV_ROOT = tmp_venv_root
        try:
            venv_dir = venv_manager.venv_dir_for("/some/never/used/path.py")
            self.assertFalse(os.path.exists(venv_dir))
        finally:
            venv_manager.VENV_ROOT = orig
            shutil.rmtree(tmp_venv_root, ignore_errors=True)


class TestFreshVenvAndWorkspace(unittest.TestCase):
    """Real (but fast, offline) venv creation -- no network needed for
    `venv.EnvBuilder`, just a local interpreter and pip bootstrap."""

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp(prefix="repair_tool_test_fresh_")

    def tearDown(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_fresh_venv_is_created_at_the_given_root_not_the_persistent_store(self):
        python_exe = venv_manager.get_fresh_venv_python(self.tmp_root)
        self.assertTrue(os.path.isfile(python_exe))
        self.assertTrue(python_exe.startswith(self.tmp_root))

    def test_fresh_workspace_copy_is_a_real_untouched_copy(self):
        source_dir = tempfile.mkdtemp()
        try:
            source = os.path.join(source_dir, "target.py")
            with open(source, "w") as f:
                f.write("print('original')\n")

            copy_path = venv_manager.get_fresh_workspace_copy(source, self.tmp_root)

            self.assertNotEqual(copy_path, source)
            with open(copy_path) as f:
                self.assertEqual(f.read(), "print('original')\n")
        finally:
            shutil.rmtree(source_dir, ignore_errors=True)

    def test_two_fresh_calls_for_the_same_target_do_not_collide(self):
        source_dir = tempfile.mkdtemp()
        try:
            source = os.path.join(source_dir, "target.py")
            with open(source, "w") as f:
                f.write("print(1)\n")

            root_a = tempfile.mkdtemp(prefix="repair_tool_test_fresh_a_")
            root_b = tempfile.mkdtemp(prefix="repair_tool_test_fresh_b_")
            try:
                copy_a = venv_manager.get_fresh_workspace_copy(source, root_a)
                copy_b = venv_manager.get_fresh_workspace_copy(source, root_b)
                self.assertNotEqual(copy_a, copy_b)
                self.assertTrue(os.path.isfile(copy_a))
                self.assertTrue(os.path.isfile(copy_b))
            finally:
                shutil.rmtree(root_a, ignore_errors=True)
                shutil.rmtree(root_b, ignore_errors=True)
        finally:
            shutil.rmtree(source_dir, ignore_errors=True)


class TestFreshRepoCopy(unittest.TestCase):
    """TASK_fix_repo_execution.md Part A: a whole-repo copy, not a
    stripped single-file workspace -- a local sibling import or a
    relative-path data read needs its sibling files physically present."""

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp()
        self.repo_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)
        shutil.rmtree(self.repo_dir, ignore_errors=True)

    def test_copies_the_whole_tree_not_just_one_file(self):
        nested = os.path.join(self.repo_dir, "sub", "dir")
        os.makedirs(nested)
        with open(os.path.join(nested, "a.py"), "w") as f:
            f.write("print('a')\n")
        with open(os.path.join(self.repo_dir, "sibling_helper.py"), "w") as f:
            f.write("def greet():\n    return 'hi'\n")

        copy_root = venv_manager.get_fresh_repo_copy(self.repo_dir, self.tmp_root)

        self.assertTrue(copy_root.startswith(self.tmp_root))
        self.assertTrue(os.path.isfile(os.path.join(copy_root, "sub", "dir", "a.py")))
        self.assertTrue(os.path.isfile(os.path.join(copy_root, "sibling_helper.py")))

    def test_sibling_import_resolves_under_a_fresh_repo_copy_but_a_real_missing_package_still_fails(self):
        """The decisive proof for Part A: under the old per-file-stripped
        workspace, `import sibling_helper` would raise ModuleNotFoundError
        even though the file is right there in the real repo -- a false
        failure the harness itself caused. Under a whole-repo copy it
        resolves, while a genuinely missing PyPI package is still
        correctly detected."""
        with open(os.path.join(self.repo_dir, "sibling_helper.py"), "w") as f:
            f.write("def greet():\n    return 'hi'\n")
        with open(os.path.join(self.repo_dir, "uses_sibling.py"), "w") as f:
            f.write("import sibling_helper\nprint(sibling_helper.greet())\n")
        with open(os.path.join(self.repo_dir, "uses_fake_package.py"), "w") as f:
            f.write("import a_totally_fake_nonexistent_package_xyz\n")

        copy_root = venv_manager.get_fresh_repo_copy(self.repo_dir, self.tmp_root)

        sibling_result = run_project(os.path.join(copy_root, "uses_sibling.py"))
        self.assertTrue(sibling_result.ok, msg=sibling_result.stderr)
        self.assertIn("hi", sibling_result.stdout)

        fake_result = run_project(os.path.join(copy_root, "uses_fake_package.py"))
        self.assertFalse(fake_result.ok)
        self.assertIn("No module named 'a_totally_fake_nonexistent_package_xyz'", fake_result.stderr)

    def test_fresh_copy_never_touches_the_original_repo(self):
        with open(os.path.join(self.repo_dir, "a.py"), "w") as f:
            f.write("original\n")

        copy_root = venv_manager.get_fresh_repo_copy(self.repo_dir, self.tmp_root)
        with open(os.path.join(copy_root, "a.py"), "w") as f:
            f.write("edited by the agent\n")

        with open(os.path.join(self.repo_dir, "a.py")) as f:
            self.assertEqual(f.read(), "original\n")


class TestPersistentRepoCopy(unittest.TestCase):
    def setUp(self):
        self.venv_dir = tempfile.mkdtemp()
        self.repo_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.venv_dir, ignore_errors=True)
        shutil.rmtree(self.repo_dir, ignore_errors=True)

    def test_copies_the_whole_tree(self):
        os.makedirs(os.path.join(self.repo_dir, "sub"))
        with open(os.path.join(self.repo_dir, "sub", "a.py"), "w") as f:
            f.write("print('a')\n")

        copy_root = venv_manager.get_persistent_repo_copy(self.repo_dir, self.venv_dir)

        self.assertTrue(copy_root.startswith(self.venv_dir))
        self.assertTrue(os.path.isfile(os.path.join(copy_root, "sub", "a.py")))

    def test_reuses_an_existing_copy_rather_than_overwriting_edits(self):
        with open(os.path.join(self.repo_dir, "a.py"), "w") as f:
            f.write("original\n")

        copy_root = venv_manager.get_persistent_repo_copy(self.repo_dir, self.venv_dir)
        with open(os.path.join(copy_root, "a.py"), "w") as f:
            f.write("edited by the agent\n")

        copy_root_again = venv_manager.get_persistent_repo_copy(self.repo_dir, self.venv_dir)

        self.assertEqual(copy_root, copy_root_again)
        with open(os.path.join(copy_root_again, "a.py")) as f:
            self.assertEqual(f.read(), "edited by the agent\n")


if __name__ == "__main__":
    unittest.main()
