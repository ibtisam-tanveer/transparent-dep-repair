import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runner import run_project

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "broken_examples")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# file -> substring expected in stderr, per broken_examples/MANIFEST.md
BROKEN_EXAMPLES = {
    "01_missing_package.py": "ModuleNotFoundError",
    "02_numpy_float.py": "AttributeError",
    "03_numpy_int_bool.py": "AttributeError",
    "04_sklearn_externals_joblib.py": "ImportError",
    "05_pandas_append.py": "AttributeError",
    "06_scipy_imread.py": "ImportError",
    "07_collections_abc.py": "ImportError",
    "08_pyyaml_load.py": "TypeError",
}


class TestRunProject(unittest.TestCase):
    def test_success_path(self):
        hello = os.path.join(ROOT, "hello.py")
        result = run_project(hello)
        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)

    def test_missing_file_does_not_raise(self):
        result = run_project(os.path.join(ROOT, "does_not_exist.py"))
        self.assertFalse(result.ok)
        self.assertIn("not", result.stderr.lower())

    def test_timeout_is_captured_not_raised(self):
        target = os.path.join(FIXTURES, "hangs.py")
        start = time.monotonic()
        result = run_project(target, timeout=2)
        elapsed = time.monotonic() - start
        self.assertFalse(result.ok)
        self.assertIn("Timeout", result.stderr)
        self.assertLess(elapsed, 10)

    def test_required_behaviour_assertion_from_spec(self):
        result = run_project(os.path.join(EXAMPLES, "02_numpy_float.py"))
        self.assertFalse(result.ok)
        self.assertIn("AttributeError", result.stderr)


def _make_broken_example_test(filename, expected_substring):
    def test(self):
        result = run_project(os.path.join(EXAMPLES, filename))
        self.assertFalse(result.ok)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected_substring, result.stderr)

    return test


for _filename, _expected in BROKEN_EXAMPLES.items():
    _test_name = f"test_broken_{_filename.replace('.py', '')}"
    setattr(TestRunProject, _test_name, _make_broken_example_test(_filename, _expected))


if __name__ == "__main__":
    unittest.main()
