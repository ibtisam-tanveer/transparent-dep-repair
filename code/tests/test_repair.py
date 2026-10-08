import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.diagnose import Diagnosis
from repair_tool.llm import FixCandidate, LLMFixSuggestion
from repair_tool.repair import (
    DEFAULT_STRATEGY_ORDER,
    STRATEGY_ORDER,
    propose,
    propose_hard_case,
    strategy_order_for,
)

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))


class TestProposeOffline(unittest.TestCase):
    def test_required_behaviour_install_proposal(self):
        # "sklearn" is a curated alias, so this needs no network at all.
        p = propose(Diagnosis(kind="missing_module", module="sklearn", package="sklearn"))
        self.assertEqual(p.kind, "install")
        self.assertEqual(p.package, "scikit-learn")
        self.assertEqual(p.source, "curated_alias")
        self.assertEqual(p.confidence, "high")

    @patch("repair_tool.repair.pypi.resolve_package_name", return_value="seaborn")
    def test_required_behaviour_seaborn_via_pypi_match(self, _mock_resolve):
        # Reproduces PHASE3_TASK.md's literal example offline: "seaborn" is
        # not a curated alias, so the real call needs network (see
        # test_repair_network below for the live version).
        p = propose(Diagnosis(kind="missing_module", module="seaborn", package="seaborn"))
        self.assertEqual(p.kind, "install")
        self.assertEqual(p.package, "seaborn")
        self.assertEqual(p.source, "pypi_name_match")
        self.assertEqual(p.confidence, "medium")

    def test_required_behaviour_none_for_module_attribute_removed(self):
        p = propose(Diagnosis(kind="module_attribute_removed", package="numpy", symbol="float"))
        self.assertEqual(p.kind, "none")

    @patch("repair_tool.repair.pypi.resolve_package_name", return_value=None)
    def test_unresolved_missing_module_is_none_not_install(self, _mock_resolve):
        p = propose(Diagnosis(kind="missing_module", module="nonsense_xyz", package="nonsense_xyz"))
        self.assertEqual(p.kind, "none")
        self.assertEqual(p.confidence, "low")
        self.assertIn("could not resolve", p.reason)

    def test_every_non_missing_module_kind_is_none(self):
        for kind in ("import_name", "object_attribute_error", "unknown"):
            with self.subTest(kind=kind):
                p = propose(Diagnosis(kind=kind))
                self.assertEqual(p.kind, "none")
                self.assertEqual(p.confidence, "low")
                self.assertIn(kind, p.reason)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
class TestProposeNetwork(unittest.TestCase):
    def test_required_behaviour_seaborn_real(self):
        p = propose(Diagnosis(kind="missing_module", module="seaborn", package="seaborn"))
        self.assertEqual(p.kind, "install")
        self.assertEqual(p.package, "seaborn")


class TestStrategyOrder(unittest.TestCase):
    def test_attribute_kinds_try_code_first(self):
        self.assertEqual(strategy_order_for("module_attribute_removed"), ("code", "environment"))
        self.assertEqual(strategy_order_for("object_attribute_error"), ("code", "environment"))

    def test_other_kinds_default_to_environment_first(self):
        self.assertEqual(strategy_order_for("import_name"), DEFAULT_STRATEGY_ORDER)
        self.assertEqual(strategy_order_for("unknown"), DEFAULT_STRATEGY_ORDER)
        self.assertEqual(strategy_order_for("some_future_kind"), ("environment", "code"))


class TestProposeHardCase(unittest.TestCase):
    def test_llm_error_propagates_without_crashing(self):
        with patch("repair_tool.repair.llm.request_fix", return_value=LLMFixSuggestion(error="no key")):
            result = propose_hard_case(Diagnosis(kind="module_attribute_removed"), "code", "traceback")
        self.assertEqual(result.error, "no key")
        self.assertIsNone(result.code_fix)
        self.assertIsNone(result.env_fix)

    def test_successful_llm_call_carries_both_candidates_and_order(self):
        suggestion = LLMFixSuggestion(
            understanding="np.float removed",
            code_fix=FixCandidate(applicable=True, reason="use builtin", edits=[{"find": "np.float", "replace": "float"}]),
            env_fix=FixCandidate(applicable=True, reason="pin numpy", package="numpy", constraint="<1.24"),
        )
        with patch("repair_tool.repair.llm.request_fix", return_value=suggestion):
            result = propose_hard_case(
                Diagnosis(kind="module_attribute_removed", package="numpy", symbol="float"), "code", "traceback"
            )
        self.assertEqual(result.error, "")
        self.assertEqual(result.understanding, "np.float removed")
        self.assertTrue(result.code_fix.applicable)
        self.assertTrue(result.env_fix.applicable)
        self.assertEqual(result.order, ("code", "environment"))  # attribute kind -> code first


if __name__ == "__main__":
    unittest.main()
