import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataset.taxonomy import CATEGORIES, TIERS, classify, split_reason


class TestSplitReason(unittest.TestCase):
    def test_bare_class_name_has_no_message(self):
        self.assertEqual(split_reason("ModuleNotFoundError"), ("ModuleNotFoundError", ""))

    def test_class_name_and_message_split_on_first_colon(self):
        self.assertEqual(
            split_reason("FileNotFoundError: [Errno 2] No such file or directory"),
            ("FileNotFoundError", "[Errno 2] No such file or directory"),
        )

    def test_message_itself_containing_a_colon_is_preserved(self):
        class_name, message = split_reason("HTTPError: 401 Client Error: UNAUTHORIZED for url")
        self.assertEqual(class_name, "HTTPError")
        self.assertEqual(message, "401 Client Error: UNAUTHORIZED for url")

    def test_none_and_empty_are_handled_without_raising(self):
        self.assertEqual(split_reason(None), ("", ""))
        self.assertEqual(split_reason(""), ("", ""))


class TestClassify(unittest.TestCase):
    """Every case here is a real `reason` value observed in the 2023
    GigaScience db (dataset/NOTES_2023.md), not a hypothetical."""

    def test_bare_module_not_found_is_confirmed_missing_dependency(self):
        category, tier, _, _ = classify("ModuleNotFoundError")
        self.assertEqual((category, tier), ("A", "confirmed"))

    def test_import_error_with_no_module_named_message_is_confirmed_a(self):
        category, tier, _, _ = classify("ImportError: No module named joblib")
        self.assertEqual((category, tier), ("A", "confirmed"))

    def test_bare_import_error_is_candidate_a(self):
        category, tier, _, _ = classify("ImportError")
        self.assertEqual((category, tier), ("A", "candidate"))

    def test_bare_attribute_error_is_candidate_removed_api(self):
        category, tier, _, _ = classify("AttributeError")
        self.assertEqual((category, tier), ("C", "candidate"))

    def test_file_not_found_with_does_not_exist_message_is_confirmed_e(self):
        category, tier, _, _ = classify("FileNotFoundError: [Errno 2] File all.txt does not exist")
        self.assertEqual((category, tier), ("E", "confirmed"))

    def test_io_error_no_such_file_is_confirmed_e(self):
        category, tier, _, _ = classify("IOError: [Errno 2] No such file or directory")
        self.assertEqual((category, tier), ("E", "confirmed"))

    def test_runtime_error_please_install_is_candidate_a(self):
        category, tier, _, _ = classify("RuntimeError: Please install TensorFlow")
        self.assertEqual((category, tier), ("A", "candidate"))

    def test_connection_pool_message_is_confirmed_e(self):
        category, tier, _, _ = classify(
            "ConnectionError: HTTPConnectionPool(host='localhost', port=1234): "
            "Max retries exceeded with url: /v1/session"
        )
        self.assertEqual((category, tier), ("E", "confirmed"))

    def test_undefined_symbol_import_error_is_candidate_version_conflict(self):
        category, tier, _, _ = classify(
            "ImportError: /home/ti89cos/anaconda3/envs/work/lib/python3.7/"
            "site-packages/IcePy.cpython-37m-x86_64-linux-gnu.so: undefined symbol"
        )
        self.assertEqual((category, tier), ("D", "candidate"))

    def test_name_error_defaults_to_candidate_e_not_a_dependency_bug(self):
        category, tier, _, _ = classify("NameError")
        self.assertEqual((category, tier), ("E", "candidate"))

    def test_syntax_error_is_excluded(self):
        category, tier, _, _ = classify("SyntaxError")
        self.assertEqual((category, tier), ("E", "excluded"))

    def test_called_process_error_defaults_to_candidate_version_conflict(self):
        category, tier, _, _ = classify("CalledProcessError")
        self.assertEqual((category, tier), ("D", "candidate"))

    def test_an_entirely_unrecognised_class_name_defaults_to_excluded_e(self):
        category, tier, class_name, message = classify("SomeExceptionNeverSeenBefore")
        self.assertEqual((category, tier), ("E", "excluded"))
        self.assertEqual(class_name, "SomeExceptionNeverSeenBefore")

    def test_none_reason_is_handled_without_raising(self):
        category, tier, class_name, message = classify(None)
        self.assertEqual((category, tier), ("E", "excluded"))
        self.assertEqual(class_name, "")

    def test_every_classification_uses_a_defined_category_and_tier(self):
        samples = [
            "ModuleNotFoundError", "ImportError", "AttributeError", "FileNotFoundError: x does not exist",
            "CalledProcessError", "NameError", "TypeError", "ValueError", "RuntimeError: Please install X",
            None, "", "WeirdOneOffError",
        ]
        for reason in samples:
            category, tier, _, _ = classify(reason)
            self.assertIn(category, CATEGORIES)
            self.assertIn(tier, TIERS)


if __name__ == "__main__":
    unittest.main()
