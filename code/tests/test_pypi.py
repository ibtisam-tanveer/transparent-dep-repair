import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import pypi

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))


class TestResolvePackageNameOffline(unittest.TestCase):
    """Curated-alias hits never touch the network (checked before any PyPI
    call), so these run offline unconditionally."""

    def test_known_aliases(self):
        self.assertEqual(pypi.resolve_package_name("sklearn"), "scikit-learn")
        self.assertEqual(pypi.resolve_package_name("cv2"), "opencv-python")
        self.assertEqual(pypi.resolve_package_name("PIL"), "Pillow")
        self.assertEqual(pypi.resolve_package_name("bs4"), "beautifulsoup4")
        self.assertEqual(pypi.resolve_package_name("yaml"), "PyYAML")
        self.assertEqual(pypi.resolve_package_name("skimage"), "scikit-image")

    @patch("repair_tool.pypi.package_exists", return_value=False)
    def test_nonsense_name_returns_none(self, _mock_exists):
        self.assertIsNone(pypi.resolve_package_name("this-package-definitely-does-not-exist-xyz123"))

    @patch("repair_tool.pypi.package_exists", return_value=True)
    def test_non_aliased_name_that_exists_on_pypi_resolves_to_itself(self, _mock_exists):
        self.assertEqual(pypi.resolve_package_name("seaborn"), "seaborn")

    def test_package_exists_never_raises_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            self.assertFalse(pypi.package_exists("seaborn"))

    def test_latest_version_never_raises_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            self.assertIsNone(pypi.latest_version("seaborn"))


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
class TestPypiNetwork(unittest.TestCase):
    """Hits the real PyPI. Reproduces PHASE3_TASK.md's literal 'Required
    behaviour' assertions, which for a non-aliased name genuinely need
    network despite the doc's own inline comment implying otherwise."""

    def test_required_behaviour_resolve_package_name(self):
        self.assertEqual(pypi.resolve_package_name("sklearn"), "scikit-learn")
        self.assertEqual(pypi.resolve_package_name("seaborn"), "seaborn")

    def test_package_exists_real(self):
        self.assertTrue(pypi.package_exists("seaborn"))
        self.assertFalse(pypi.package_exists("this-package-definitely-does-not-exist-xyz123"))

    def test_latest_version_real(self):
        self.assertIsNotNone(pypi.latest_version("seaborn"))


if __name__ == "__main__":
    unittest.main()
