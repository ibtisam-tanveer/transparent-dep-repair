import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.diagnose import Diagnosis
from repair_tool.repair import propose

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


if __name__ == "__main__":
    unittest.main()
