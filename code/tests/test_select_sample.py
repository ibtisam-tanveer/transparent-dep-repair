import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.select_sample import select


def _row(repo, nb, cat="A", tier="confirmed", py="3.7"):
    return {"repository": repo, "notebook_path": nb, "category": cat, "tier": tier, "python_version": py,
            "clone_url": f"https://x/{repo}.git", "error_type": "E", "error_message": "", "repository_id": "1"}


def _rows():
    rows = []
    for i in range(30):
        rows += [_row(f"a/repo{i}", "n1.ipynb"), _row(f"a/repo{i}", "n2.ipynb")]
    for i in range(10):
        rows.append(_row(f"c/repo{i}", "c.ipynb", cat="C", tier="candidate"))
        rows.append(_row(f"d/repo{i}", "d.ipynb", cat="D", tier="candidate"))
    rows.append(_row("a/repo0", "x.ipynb", cat="C", tier="confirmed"))  # a confirmed C must NOT be drawn into the candidate pool
    return rows


class TestSelect(unittest.TestCase):
    def test_quotas_one_notebook_per_repo_and_distinct_repos(self):
        chosen, audit = select(_rows(), {"A": 8, "C": 3, "D": 3}, 42, 60)
        self.assertEqual(audit["selected_by_category"], {"A": 8, "C": 3, "D": 3})
        repos = [r["repository"] for r in chosen]
        self.assertEqual(len(repos), len(set(repos)), "every repository must appear once")

    def test_it_is_reproducible_and_seed_dependent(self):
        a, _ = select(_rows(), {"A": 8, "C": 3, "D": 3}, 42, 60)
        b, _ = select(_rows(), {"A": 8, "C": 3, "D": 3}, 42, 60)
        c, _ = select(_rows(), {"A": 8, "C": 3, "D": 3}, 7, 60)
        self.assertEqual([r["notebook_path"] + r["repository"] for r in a], [r["notebook_path"] + r["repository"] for r in b])
        self.assertNotEqual([r["repository"] for r in a], [r["repository"] for r in c])

    def test_pools_use_the_right_tier(self):
        chosen, audit = select(_rows(), {"A": 5, "C": 3, "D": 3}, 42, 60)
        self.assertEqual({r["tier"] for r in chosen if r["category"] == "A"}, {"confirmed"})
        self.assertEqual({r["tier"] for r in chosen if r["category"] in "CD"}, {"candidate"})
        self.assertEqual(audit["pools"]["C"]["tier"], "candidate")

    def test_very_large_repos_are_excluded(self):
        rows = _rows() + [_row("big/repo", f"n{i}.ipynb") for i in range(100)]
        chosen, audit = select(rows, {"A": 40, "C": 0, "D": 0}, 42, 60)
        self.assertNotIn("big/repo", [r["repository"] for r in chosen])
        self.assertEqual(audit["repositories_excluded_as_too_large"], 1)

    def test_a_quota_larger_than_the_pool_returns_what_exists(self):
        chosen, audit = select(_rows(), {"A": 0, "C": 50, "D": 0}, 42, 60)
        self.assertEqual(audit["selected_by_category"], {"C": 10})


class TestVerifiedAgainstTheCurrentTree(unittest.TestCase):
    def test_stale_and_unreachable_repositories_are_skipped_and_replaced(self):
        rows = _rows()

        def lookup(url):
            if "repo0" in url or "repo1." in url:
                return None  # unreachable
            if "repo2" in url:
                return set()  # cloned, but the labelled notebooks are gone
            return {"n1.ipynb", "n2.ipynb", "c.ipynb", "d.ipynb"}

        chosen, audit = select(rows, {"A": 8, "C": 0, "D": 0}, 42, 60, tree_lookup=lookup)
        self.assertEqual(audit["selected_by_category"], {"A": 8}, "skipped repos must be replaced, not left as holes")
        names = [r["repository"] for r in chosen]
        self.assertNotIn("a/repo2", names)
        self.assertNotIn("a/repo0", names)
        self.assertTrue(audit["verified_against_current_tree"])

    def test_only_notebooks_that_still_exist_can_be_picked(self):
        rows = [_row("a/r", "old.ipynb"), _row("a/r", "new.ipynb")]
        for seed in range(10):
            chosen, _ = select(rows, {"A": 1, "C": 0, "D": 0}, seed, 60, tree_lookup=lambda url: {"new.ipynb"})
            self.assertEqual(chosen[0]["notebook_path"], "new.ipynb")

    def test_the_audit_counts_what_was_skipped(self):
        rows = [_row("a/r1", "n.ipynb"), _row("a/r2", "n.ipynb"), _row("a/r3", "n.ipynb")]

        def lookup(url):
            return None if "r1" in url else (set() if "r2" in url else {"n.ipynb"})

        _chosen, audit = select(rows, {"A": 3, "C": 0, "D": 0}, 42, 60, tree_lookup=lookup)
        self.assertEqual(audit["skipped_repositories"], {"repository_unreachable": 1, "labelled_notebooks_no_longer_exist": 1})
        self.assertEqual(audit["selected"], 1)


if __name__ == "__main__":
    unittest.main()
