import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.analyze_results import analyze


def _row(**kwargs) -> dict:
    base = {
        "discovered": True, "fixed": False, "taxonomy_category": "A", "taxonomy_tier": "confirmed",
        "max_steps_hit": False, "fix_actions": [], "cross_file_free_pass": False, "repository": "x/y",
    }
    base.update(kwargs)
    return base


class TestAnalyze(unittest.TestCase):
    def test_effectiveness_overall_counts_only_discovered_rows(self):
        rows = [
            _row(fixed=True),
            _row(fixed=False),
            _row(discovered=False, fixed=False),  # excluded from the denominator
        ]
        report = analyze(rows)
        self.assertIn("confirmed only:            50.0% (1/2)", report)

    def test_confirmed_and_candidate_denominators_differ(self):
        rows = [
            _row(fixed=True, taxonomy_tier="confirmed"),
            _row(fixed=False, taxonomy_tier="candidate"),
        ]
        report = analyze(rows)
        self.assertIn("confirmed only:            100.0% (1/1)", report)
        self.assertIn("confirmed + candidate:     50.0% (1/2)", report)

    def test_per_category_breakdown_is_reported_separately(self):
        rows = [
            _row(fixed=True, taxonomy_category="A"),
            _row(fixed=False, taxonomy_category="C"),
        ]
        report = analyze(rows)
        self.assertIn("A: 100.0% (1/1)", report)
        self.assertIn("C: 0.0% (0/1)", report)
        self.assertIn("B: no labelled cases", report)

    def test_grounded_vs_proposed_and_confidence_distribution(self):
        rows = [
            _row(fixed=True, fix_actions=[
                {"grounding": "metadata_grounded", "verification": "verified", "confidence": "high"},
            ]),
            _row(fixed=True, fix_actions=[
                {"grounding": "llm_proposed", "verification": "unverified", "confidence": "low"},
                {"grounding": "llm_proposed", "verification": "verified", "confidence": "medium-high"},
            ]),
        ]
        report = analyze(rows)
        self.assertIn("metadata_grounded: 1", report)
        self.assertIn("llm_proposed:      1", report)
        self.assertIn("high: 1", report)
        self.assertIn("medium-high: 1", report)
        self.assertNotIn("low: 1", report)  # the unverified candidate wasn't the accepted fix

    def test_cap_hit_is_split_from_genuine_failure(self):
        rows = [
            _row(fixed=False, max_steps_hit=True),
            _row(fixed=False, max_steps_hit=False),
        ]
        report = analyze(rows)
        self.assertIn("ran out of budget, not proven unfixable): 1", report)
        self.assertIn("model gave up, or no tool call left): 1", report)

    def test_cross_file_free_pass_count_and_repo_count(self):
        rows = [
            _row(fixed=True, cross_file_free_pass=True, repository="a/repo"),
            _row(fixed=True, cross_file_free_pass=True, repository="a/repo"),
            _row(fixed=True, cross_file_free_pass=True, repository="b/repo"),
            _row(fixed=True, cross_file_free_pass=False),
        ]
        report = analyze(rows)
        self.assertIn("another file was genuinely fixed: 3", report)
        self.assertIn("across 2 distinct repositories", report)

    def test_not_discovered_rows_are_counted_separately_not_hidden(self):
        rows = [_row(discovered=False)]
        report = analyze(rows)
        self.assertIn("not discovered", report)
        self.assertIn(": 1", report)


if __name__ == "__main__":
    unittest.main()
