import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.diagnose import Diagnosis
from repair_tool.llm import LLMFixSuggestion, request_fix

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))

DIAGNOSIS = Diagnosis(kind="module_attribute_removed", module="numpy", package="numpy", symbol="float")

VALID_JSON = """{
  "understanding": "np.float was removed in NumPy 1.24",
  "code_fix": {"applicable": true, "reason": "use the builtin", "edits": [{"find": "np.float", "replace": "float"}]},
  "env_fix": {"applicable": true, "reason": "pin an older numpy", "package": "numpy", "constraint": "<1.24"}
}"""


def _mock_openai_response(content: str):
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


class TestRequestFixOffline(unittest.TestCase):
    def test_missing_api_key_gives_a_clear_error_not_a_crash(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertIsInstance(result, LLMFixSuggestion)
        self.assertIn("OPENAI_API_KEY", result.error)
        self.assertIsNone(result.code_fix)
        self.assertIsNone(result.env_fix)

    @patch("openai.OpenAI")
    def test_valid_json_response_parses_both_candidates(self, mock_openai_cls):
        mock_openai_cls.return_value.chat.completions.create.return_value = _mock_openai_response(VALID_JSON)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")

        self.assertEqual(result.error, "")
        self.assertTrue(result.code_fix.applicable)
        self.assertEqual(result.code_fix.edits, [{"find": "np.float", "replace": "float"}])
        self.assertTrue(result.env_fix.applicable)
        self.assertEqual(result.env_fix.package, "numpy")
        self.assertEqual(result.env_fix.constraint, "<1.24")

    @patch("openai.OpenAI")
    def test_response_wrapped_in_code_fences_still_parses(self, mock_openai_cls):
        fenced = f"```json\n{VALID_JSON}\n```"
        mock_openai_cls.return_value.chat.completions.create.return_value = _mock_openai_response(fenced)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertEqual(result.error, "")
        self.assertTrue(result.code_fix.applicable)

    @patch("openai.OpenAI")
    def test_malformed_json_degrades_to_no_fix_not_a_crash(self, mock_openai_cls):
        mock_openai_cls.return_value.chat.completions.create.return_value = _mock_openai_response(
            "sorry, I can't help with that"
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertIsInstance(result, LLMFixSuggestion)
        self.assertNotEqual(result.error, "")
        self.assertIsNone(result.code_fix)

    @patch("openai.OpenAI")
    def test_one_candidate_not_applicable_is_parsed_correctly(self, mock_openai_cls):
        content = """{
          "understanding": "x",
          "code_fix": {"applicable": false, "reason": "no code change can help"},
          "env_fix": {"applicable": true, "reason": "pin it", "package": "numpy", "constraint": "<1.24"}
        }"""
        mock_openai_cls.return_value.chat.completions.create.return_value = _mock_openai_response(content)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertFalse(result.code_fix.applicable)
        self.assertTrue(result.env_fix.applicable)

    @patch("openai.OpenAI")
    def test_network_or_sdk_failure_degrades_to_error_not_a_crash(self, mock_openai_cls):
        mock_openai_cls.return_value.chat.completions.create.side_effect = RuntimeError("connection reset")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertIn("LLM call failed", result.error)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
@unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
class TestRequestFixNetwork(unittest.TestCase):
    def test_real_call_returns_a_usable_suggestion(self):
        result = request_fix(
            DIAGNOSIS,
            "import numpy as np\nx = np.float(3.14)\nprint(x)\n",
            "AttributeError: module 'numpy' has no attribute 'float'",
        )
        self.assertEqual(result.error, "", msg=result.error)
        self.assertTrue(result.code_fix is not None or result.env_fix is not None)


if __name__ == "__main__":
    unittest.main()
