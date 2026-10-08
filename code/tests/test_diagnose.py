import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.diagnose import Diagnosis, diagnose, diagnose_result
from repair_tool.runner import RunResult, run_project

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "broken_examples")

# file -> (expected kind, expected field checks)
# Matches broken_examples/MANIFEST.md / PHASE2_TASK.md's table, EXCEPT 04:
# with the scikit-learn version pinned by requirements-dev.txt, sklearn's
# `externals` shim module still exists but no longer re-exports `joblib`,
# so the real error is "ImportError: cannot import name 'joblib' from
# 'sklearn.externals'" (kind=import_name), not a missing-module error.
# broken_examples/04_sklearn_externals_joblib.py's own header comment
# already anticipates this: "EXPECTED ERR : ImportError / ModuleNotFoundError".
BROKEN_EXAMPLES = {
    "01_missing_package.py": {"kind": "missing_module", "package": "seaborn"},
    "02_numpy_float.py": {
        "kind": "module_attribute_removed",
        "package": "numpy",
        "symbol": "float",
    },
    "03_numpy_int_bool.py": {
        "kind": "module_attribute_removed",
        "package": "numpy",
        "symbol": "int",
    },
    "04_sklearn_externals_joblib.py": {
        "kind": "import_name",
        "module": "sklearn.externals",
        "package": "sklearn",
        "symbol": "joblib",
    },
    "05_pandas_append.py": {"kind": "object_attribute_error", "symbol": "append"},
    "06_scipy_imread.py": {
        "kind": "import_name",
        "module": "scipy.misc",
        "symbol": "imread",
    },
    "07_collections_abc.py": {
        "kind": "import_name",
        "module": "collections",
        "symbol": "Mapping",
    },
    "08_pyyaml_load.py": {"kind": "unknown"},
}


class TestDiagnose(unittest.TestCase):
    def test_required_behaviour_module_attribute_removed(self):
        d = diagnose("AttributeError: module 'numpy' has no attribute 'float'")
        self.assertEqual(d.kind, "module_attribute_removed")
        self.assertEqual(d.package, "numpy")
        self.assertEqual(d.symbol, "float")

    def test_required_behaviour_missing_module(self):
        d = diagnose("ModuleNotFoundError: No module named 'seaborn'")
        self.assertEqual(d.kind, "missing_module")
        self.assertEqual(d.package, "seaborn")

    def test_empty_string_is_unknown_not_raising(self):
        d = diagnose("")
        self.assertEqual(d.kind, "unknown")
        self.assertEqual(d.detail, "")

    def test_whitespace_only_is_unknown_not_raising(self):
        d = diagnose("   \n\n   ")
        self.assertEqual(d.kind, "unknown")

    def test_noisy_multiline_message_still_finds_the_real_error(self):
        # NumPy prints several explanatory lines *after* the AttributeError
        # line itself; diagnose() must not just grab the literal last line.
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 1, in <module>\n"
            "    np.float(1)\n"
            "AttributeError: module 'numpy' has no attribute 'float'.\n"
            "`np.float` was a deprecated alias for the builtin `float`.\n"
            "See release notes at:\n"
            "    https://numpy.org/devdocs/release/1.20.0-notes.html\n"
        )
        d = diagnose(stderr)
        self.assertEqual(d.kind, "module_attribute_removed")
        self.assertEqual(d.symbol, "float")

    def test_diagnose_result_ok_run_is_none(self):
        result = run_project(os.path.join(ROOT, "hello.py"))
        d = diagnose_result(result)
        self.assertEqual(d.kind, "none")

    def test_diagnose_result_wraps_diagnose_for_failed_run(self):
        result = RunResult(
            ok=False,
            returncode=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'seaborn'",
        )
        d = diagnose_result(result)
        self.assertEqual(d.kind, "missing_module")
        self.assertEqual(d.package, "seaborn")

    def test_unrecognised_error_text_is_unknown(self):
        d = diagnose("RuntimeError: something bespoke went wrong")
        self.assertEqual(d.kind, "unknown")
        self.assertIn("RuntimeError", d.detail)

    def test_chained_exception_classifies_on_the_final_one(self):
        # "During handling of..." is not itself an exception line (no bare
        # "Identifier:" prefix), and the outer/final exception is what a
        # developer actually needs to act on, so it must win over the one
        # that triggered it.
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 1, in <module>\n"
            "    import seaborn\n"
            "ModuleNotFoundError: No module named 'seaborn'\n"
            "\n"
            "During handling of the above exception, another exception occurred:\n"
            "\n"
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 3, in <module>\n"
            "    raise RuntimeError('wrapped') from e\n"
            "RuntimeError: wrapped\n"
        )
        d = diagnose(stderr)
        self.assertEqual(d.kind, "unknown")
        self.assertIn("RuntimeError: wrapped", d.detail)

    def test_warning_raised_as_error_does_not_crash(self):
        # warnings.filterwarnings("error") turns a Warning into a raised
        # exception; the class name ends in "Warning" rather than "Error"
        # but the shape (unindented "Name: message") is identical.
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 2, in <module>\n"
            "    warnings.warn('thing', DeprecationWarning)\n"
            "DeprecationWarning: thing\n"
        )
        d = diagnose(stderr)
        self.assertEqual(d.kind, "unknown")
        self.assertIn("DeprecationWarning: thing", d.detail)

    def test_replacement_characters_from_bad_decoding_do_not_crash(self):
        # runner.py decodes subprocess output with errors="replace", so
        # diagnose() must tolerate U+FFFD showing up anywhere, including
        # mid-token, without raising.
        garbled_prefix = "��� binary noise before the traceback\n"
        stderr = garbled_prefix + "ModuleNotFoundError: No module named 'seaborn'"
        d = diagnose(stderr)
        self.assertIsInstance(d, Diagnosis)
        self.assertEqual(d.kind, "missing_module")
        self.assertEqual(d.package, "seaborn")

        mid_token_garbage = "ModuleNotFoundError: No module named 'sea�born'"
        d2 = diagnose(mid_token_garbage)
        self.assertIsInstance(d2, Diagnosis)  # must not raise either way
        self.assertIn(d2.kind, ("missing_module", "unknown"))


def _make_broken_example_test(filename, expected):
    def test(self):
        result = run_project(os.path.join(EXAMPLES, filename))
        d = diagnose_result(result)
        self.assertEqual(d.kind, expected["kind"])
        for field in ("module", "package", "symbol"):
            if field in expected:
                self.assertEqual(getattr(d, field), expected[field])
        if expected["kind"] == "unknown":
            self.assertIn("TypeError", d.detail)

    return test


for _filename, _expected in BROKEN_EXAMPLES.items():
    _test_name = f"test_diagnose_{_filename.replace('.py', '')}"
    setattr(TestDiagnose, _test_name, _make_broken_example_test(_filename, _expected))


if __name__ == "__main__":
    unittest.main()
