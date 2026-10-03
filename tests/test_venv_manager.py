import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import venv_manager


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


class TestRepoFileWorkspaceCopy(unittest.TestCase):
    def setUp(self):
        self.venv_dir = tempfile.mkdtemp()
        self.repo_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.venv_dir, ignore_errors=True)
        shutil.rmtree(self.repo_dir, ignore_errors=True)

    def test_copies_a_file_preserving_its_relative_path(self):
        nested = os.path.join(self.repo_dir, "sub", "dir")
        os.makedirs(nested)
        with open(os.path.join(nested, "a.py"), "w") as f:
            f.write("print('a')\n")

        copy_path = venv_manager.get_repo_file_workspace_copy(self.venv_dir, self.repo_dir, "sub/dir/a.py")

        self.assertTrue(os.path.isfile(copy_path))
        self.assertTrue(copy_path.startswith(self.venv_dir))
        with open(copy_path) as f:
            self.assertEqual(f.read(), "print('a')\n")

    def test_two_files_with_the_same_basename_in_different_dirs_do_not_collide(self):
        for sub in ("pkg_a", "pkg_b"):
            os.makedirs(os.path.join(self.repo_dir, sub))
            with open(os.path.join(self.repo_dir, sub, "main.py"), "w") as f:
                f.write(f"print('{sub}')\n")

        copy_a = venv_manager.get_repo_file_workspace_copy(self.venv_dir, self.repo_dir, "pkg_a/main.py")
        copy_b = venv_manager.get_repo_file_workspace_copy(self.venv_dir, self.repo_dir, "pkg_b/main.py")

        self.assertNotEqual(copy_a, copy_b)
        with open(copy_a) as f:
            self.assertIn("pkg_a", f.read())
        with open(copy_b) as f:
            self.assertIn("pkg_b", f.read())

    def test_reuses_an_existing_copy_rather_than_overwriting_edits(self):
        os.makedirs(os.path.join(self.repo_dir, "sub"))
        with open(os.path.join(self.repo_dir, "sub", "a.py"), "w") as f:
            f.write("original\n")

        copy_path = venv_manager.get_repo_file_workspace_copy(self.venv_dir, self.repo_dir, "sub/a.py")
        with open(copy_path, "w") as f:
            f.write("edited by the agent\n")

        copy_path_again = venv_manager.get_repo_file_workspace_copy(self.venv_dir, self.repo_dir, "sub/a.py")

        self.assertEqual(copy_path, copy_path_again)
        with open(copy_path_again) as f:
            self.assertEqual(f.read(), "edited by the agent\n")


if __name__ == "__main__":
    unittest.main()
