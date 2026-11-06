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
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
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
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertEqual(result.error, "")
        self.assertTrue(result.code_fix.applicable)

    @patch("openai.OpenAI")
    def test_malformed_json_degrades_to_no_fix_not_a_crash(self, mock_openai_cls):
        mock_openai_cls.return_value.chat.completions.create.return_value = _mock_openai_response(
            "sorry, I can't help with that"
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
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
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertFalse(result.code_fix.applicable)
        self.assertTrue(result.env_fix.applicable)

    @patch("openai.OpenAI")
    def test_network_or_sdk_failure_degrades_to_error_not_a_crash(self, mock_openai_cls):
        mock_openai_cls.return_value.chat.completions.create.side_effect = RuntimeError("connection reset")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertIn("LLM call failed", result.error)

    @patch("repair_tool.llm.OPENAI_BASE_URL", None)
    def test_kiste_token_is_used_when_openai_key_is_unset(self):
        """LLM_SETUP_kiste.md: KISTE_API_TOKEN works as the sole credential
        for the self-hosted endpoint, which has no OPENAI_API_KEY at all.

        clear=True is load-bearing, not boilerplate: without it, the real
        ambient OPENAI_API_KEY/KISTE_API_TOKEN from this machine's actual
        .env (loaded once at import time) leaks into the test instead of
        the fake value below -- which is exactly the real bug (and real
        secret exposure) found switching this project to kiste. Every test
        in this class that asserts an exact api_key/base_url value clears
        the environment for the same reason.
        """
        with patch.dict(os.environ, {"KISTE_API_TOKEN": "kiste-fake-for-test"}, clear=True):
            with patch("openai.OpenAI") as mock_openai_cls:
                mock_openai_cls.return_value.chat.completions.create.return_value = _mock_openai_response(VALID_JSON)
                result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertEqual(result.error, "")
        mock_openai_cls.assert_called_once_with(api_key="kiste-fake-for-test", base_url=None)

    @patch("openai.OpenAI")
    @patch("repair_tool.llm.OPENAI_BASE_URL", None)
    def test_kiste_token_takes_priority_when_both_keys_are_set(self, mock_openai_cls):
        """Regression test for a real bug found switching to kiste -- see
        agent.py's matching test for the full story."""
        mock_openai_cls.return_value.chat.completions.create.return_value = _mock_openai_response(VALID_JSON)
        with patch.dict(
            os.environ, {"OPENAI_API_KEY": "sk-openai-fake", "KISTE_API_TOKEN": "kiste-fake-for-test"}, clear=True
        ):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertEqual(result.error, "")
        mock_openai_cls.assert_called_once_with(api_key="kiste-fake-for-test", base_url=None)

    @patch("openai.OpenAI")
    @patch("repair_tool.llm.OPENAI_BASE_URL", "https://kiste.informatik.tu-chemnitz.de/v1")
    def test_base_url_override_is_passed_to_the_client(self, mock_openai_cls):
        mock_openai_cls.return_value.chat.completions.create.return_value = _mock_openai_response(VALID_JSON)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertEqual(result.error, "")
        mock_openai_cls.assert_called_once_with(
            api_key="sk-fake-for-test", base_url="https://kiste.informatik.tu-chemnitz.de/v1"
        )

    @patch("openai.OpenAI")
    def test_response_format_unsupported_falls_back_to_plain_extraction(self, mock_openai_cls):
        """LLM_SETUP_kiste.md gotcha 2: a self-hosted server can reject
        response_format entirely -- the second, plain call must still be
        tried and its (unfenced-but-reasoning-prefixed) text parsed."""
        reasoning_prefixed = f"Let me think about this.\n\n{VALID_JSON}"
        mock_openai_cls.return_value.chat.completions.create.side_effect = [
            RuntimeError("400 - Unrecognized request argument supplied: response_format"),
            _mock_openai_response(reasoning_prefixed),
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-for-test"}, clear=True):
            result = request_fix(DIAGNOSIS, "np.float(3.14)", "AttributeError: ...")
        self.assertEqual(result.error, "")
        self.assertTrue(result.code_fix.applicable)
        self.assertEqual(mock_openai_cls.return_value.chat.completions.create.call_count, 2)


class TestExtractJsonObject(unittest.TestCase):
    """LLM_SETUP_kiste.md gotcha 2: robust extraction when the model isn't
    constrained to strict JSON mode, e.g. a reasoning model's preamble."""

    def test_bare_json_object_passes_through(self):
        from repair_tool.llm import _extract_json_object

        self.assertEqual(_extract_json_object('{"a": 1}'), '{"a": 1}')

    def test_reasoning_preamble_before_the_json_is_stripped(self):
        from repair_tool.llm import _extract_json_object

        text = 'Thinking it over, here is my answer:\n\n{"a": 1}'
        self.assertEqual(_extract_json_object(text), '{"a": 1}')

    def test_code_fenced_json_is_still_stripped_first(self):
        from repair_tool.llm import _extract_json_object

        text = '```json\n{"a": 1}\n```'
        self.assertEqual(_extract_json_object(text), '{"a": 1}')

    def test_text_with_no_json_object_at_all_is_returned_unchanged(self):
        from repair_tool.llm import _extract_json_object

        self.assertEqual(_extract_json_object("sorry, I can't help"), "sorry, I can't help")


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
