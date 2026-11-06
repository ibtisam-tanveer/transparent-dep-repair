import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.analyze_results import analyze


def _row(**kwargs) -> dict:
    base = {
        "discovered": True, "fixed": False, "taxonomy_category": "A", "taxonomy_tier": "confirmed",
        "max_steps_hit": False, "fix_actions": [], "cross_file_free_pass": False, "repository": "x/y",
        "budget_exhausted": False, "too_large_for_context": False, "quota_exhausted": False,
        "runtime_observed_category": "", "stop_reason": "", "local_module_import": False,
        "interpreter_unprovisionable": False, "python_version_source": "none", "python_mechanism": "default",
        "python_version_requested": None, "python_version_actual": None,
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

    def test_stop_reason_breakdown_separates_the_different_non_fix_outcomes(self):
        """TASK_agent_stopping.md: step_cap/no_progress/model_gave_up/
        model_stopped must each be counted separately, not folded into a
        single 'genuine failure' bucket the way max_steps_hit alone did."""
        rows = [
            _row(fixed=False, stop_reason="step_cap"),
            _row(fixed=False, stop_reason="no_progress"),
            _row(fixed=False, stop_reason="no_progress"),
            _row(fixed=False, stop_reason="model_gave_up"),
            _row(fixed=False, stop_reason="model_stopped"),
        ]
        report = analyze(rows)
        self.assertIn("step_cap (ran out of the step budget, not proven unfixable): 1", report)
        self.assertIn("no_progress (stopped early -- the observed failure stopped changing): 2", report)
        self.assertIn("model_gave_up (stopped early -- the model called report_unfixable): 1", report)
        self.assertIn("model_stopped (stopped early -- the model stopped calling tools without verify succeeding): 1", report)

    def test_a_result_row_predating_stop_reason_is_counted_as_unknown(self):
        rows = [_row(fixed=False, stop_reason="")]
        report = analyze(rows)
        self.assertIn("unknown (no stop_reason recorded (result predates TASK_agent_stopping.md)): 1", report)

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


class TestInScopeDenominator(unittest.TestCase):
    """PILOT_DECISIONS.md #4: budget-exhausted, too-large-for-context, and
    runtime-reclassified-E rows must be excluded from the effectiveness
    denominator entirely, reported only as their own separate counts."""

    def test_budget_exhausted_rows_are_excluded_from_effectiveness(self):
        rows = [
            _row(fixed=True),
            _row(fixed=False, budget_exhausted=True),
        ]
        report = analyze(rows)
        self.assertIn("never attempted -- this repo's time budget ran out first: 1", report)
        self.assertIn("in scope and genuinely attempted (the effectiveness denominator below): 1", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_too_large_for_context_rows_are_excluded_from_effectiveness(self):
        rows = [
            _row(fixed=True),
            _row(fixed=False, too_large_for_context=True),
        ]
        report = analyze(rows)
        self.assertIn("too large for the model's context window: 1", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_quota_exhausted_rows_are_excluded_from_effectiveness(self):
        """Found running the real pilot smoke test: an API-key credit
        exhaustion must not silently count as a repair failure."""
        rows = [
            _row(fixed=True),
            _row(fixed=False, quota_exhausted=True),
        ]
        report = analyze(rows)
        self.assertIn("API key's credit/quota ran out mid-attempt: 1", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_local_module_import_rows_are_excluded_from_effectiveness(self):
        """TASK_fix_repo_execution.md Part B: a ModuleNotFoundError for a
        repo-local module must not count as a category-A repair failure."""
        rows = [
            _row(fixed=True),
            _row(fixed=False, local_module_import=True),
        ]
        report = analyze(rows)
        self.assertIn("ModuleNotFoundError for a repo-local module, not a missing PyPI package: 1", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_runtime_reclassified_e_rows_are_excluded_from_effectiveness(self):
        rows = [
            _row(fixed=True),
            _row(fixed=False, runtime_observed_category="E"),
        ]
        report = analyze(rows)
        self.assertIn("reclassified as category E", report)
        self.assertIn("not a dependency problem): 1", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_a_fixed_row_with_leftover_runtime_category_is_never_excluded(self):
        """runtime_observed_category only excludes a NOT-fixed row -- a
        fixed row is in scope regardless of what it says (it shouldn't say
        anything meaningful, since build_result_row only sets this for
        not-fixed results, but analyze_results.py must not rely on that)."""
        rows = [_row(fixed=True, runtime_observed_category="E")]
        report = analyze(rows)
        self.assertIn("reclassified as category E", report)
        self.assertIn("not a dependency problem): 0", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_a_runtime_category_other_than_e_is_not_excluded(self):
        """A/B/C/D at runtime just means the real failure matched its
        static label -- a plain non-fix, not a reclassification."""
        rows = [_row(fixed=False, runtime_observed_category="A")]
        report = analyze(rows)
        self.assertIn("not a dependency problem): 0", report)
        self.assertIn("confirmed only:            0.0% (0/1)", report)

    def test_stop_reason_section_excludes_the_carved_out_rows(self):
        """A budget-exhausted row must not be double-counted in the
        stop-reason breakdown once it's already excluded from in_scope."""
        rows = [
            _row(fixed=False, budget_exhausted=True, stop_reason="step_cap"),
            _row(fixed=False, stop_reason="model_gave_up"),
        ]
        report = analyze(rows)
        self.assertIn("model_gave_up (stopped early -- the model called report_unfixable): 1", report)
        self.assertNotIn("step_cap (ran out of the step budget, not proven unfixable): 1", report)


class TestInterpreterRepairAccounting(unittest.TestCase):
    """TASK_interpreter_repair.md Part D: an unprovisionable interpreter is its
    own explicit residual category, not a repair failure."""

    def test_unprovisionable_rows_are_excluded_and_reported_separately(self):
        rows = [
            _row(fixed=True),
            _row(fixed=False, interpreter_unprovisionable=True),
        ]
        report = analyze(rows)
        self.assertIn("interpreter unprovisionable (Python 2 / no reachable build of the declared version): 1", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_a_fixed_row_is_never_excluded_as_unprovisionable(self):
        report = analyze([_row(fixed=True, interpreter_unprovisionable=True)])
        self.assertIn("interpreter unprovisionable (Python 2 / no reachable build of the declared version): 0", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_provenance_section_separates_declared_inferred_and_substituted(self):
        rows = [
            _row(python_version_source="binder/environment.yml", python_mechanism="uv",
                 python_version_requested="3.7", python_version_actual="3.8"),
            _row(python_version_source="era-inferred", python_mechanism="uv",
                 python_version_requested="3.8", python_version_actual="3.8", fixed=True),
            _row(python_version_source="none"),
        ]
        report = analyze(rows)
        self.assertIn("version declared in the repo's own recipe (grounded): 1", report)
        self.assertIn("version era-inferred from last-commit date (ungrounded): 1", report)
        self.assertIn("no signal (harness default interpreter kept): 1", report)
        self.assertIn("a different interpreter than the harness default was provisioned: 2", report)
        self.assertIn("nearest available used: 1", report)
        self.assertIn("fixed after interpreter provisioning: 50.0% (1/2)", report)


class TestLlmUnreachableAccounting(unittest.TestCase):
    """Found in kiste_smoke_v5: an unreachable model backend produced 27
    'failures' with zero model calls -- infrastructure, not a repair outcome."""

    def test_error_with_zero_model_calls_is_excluded_and_reported(self):
        rows = [
            _row(fixed=True, llm_calls=3),
            _row(fixed=False, stop_reason="error", llm_calls=0),
            _row(fixed=False, stop_reason="error", llm_calls=0),
        ]
        report = analyze(rows)
        self.assertIn("LLM backend unreachable (connection error / timeout, no model call ever completed): 2", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)

    def test_an_error_after_real_work_still_counts_as_an_attempt(self):
        rows = [_row(fixed=False, stop_reason="error", llm_calls=4)]
        report = analyze(rows)
        self.assertIn("no model call ever completed): 0", report)
        self.assertIn("confirmed only:            0.0% (0/1)", report)


if __name__ == "__main__":
    unittest.main()


class TestScopeFindingByReason(unittest.TestCase):
    """TASK_next_steps.md step 1: out-of-scope is explained by reason, and the
    scope finding splits dependency-repairable from needs-data/compute."""

    def test_out_of_scope_rows_are_summarised_by_reason(self):
        rows = [
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="missing_data_file"),
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="missing_data_file"),
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="cell_timeout_download"),
        ]
        report = analyze(rows)
        self.assertIn("missing_data_file: 2", report)
        self.assertIn("cell_timeout_download: 1", report)

    def test_rows_without_a_recorded_reason_are_not_hidden(self):
        report = analyze([_row(fixed=False, runtime_observed_category="E")])
        self.assertIn("reason not recorded: 1", report)

    def test_scope_finding_splits_dependency_from_environmental(self):
        rows = [
            _row(fixed=True),
            _row(fixed=False, runtime_observed_category="A", runtime_observed_reason="dependency:missing_module"),
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="gpu_required"),
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="missing_data_file"),
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="code_error:KeyError"),
            _row(fixed=False, local_module_import=True),
        ]
        report = analyze(rows)
        self.assertIn("failures seen: 6", report)
        self.assertIn("fixed (dependency-repairable): 16.7% (1/6)", report)
        self.assertIn("dependency failure, not fixed: 16.7% (1/6)", report)
        self.assertIn("needs data / compute / network / credentials: 33.3% (2/6)", report)
        self.assertIn("other code error (not a dependency): 16.7% (1/6)", report)
        self.assertIn("repo-local module / path problem: 16.7% (1/6)", report)
        self.assertIn("dependency-related in total (fixed + not fixed): 33.3% (2/6)", report)

    def test_never_attempted_and_backend_down_rows_do_not_dilute_the_scope_finding(self):
        rows = [
            _row(fixed=True),
            _row(fixed=False, budget_exhausted=True),
            _row(fixed=False, stop_reason="error", llm_calls=0),
        ]
        self.assertIn("failures seen: 1", analyze(rows))


class TestContainerUnavailableAccounting(unittest.TestCase):
    def test_a_row_whose_container_could_not_be_provided_is_excluded_and_reported(self):
        rows = [
            _row(fixed=True),
            _row(fixed=False, container_error="image build failed: x"),
            _row(fixed=False, error="container unavailable: could not start the container"),
        ]
        report = analyze(rows)
        self.assertIn("container could not be provided (image/start/copy failed): 2", report)
        self.assertIn("confirmed only:            100.0% (1/1)", report)


class TestTimeoutPositionReporting(unittest.TestCase):
    def test_positions_of_timed_out_notebooks_are_summarised(self):
        rows = [
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="cell_timeout",
                 runtime_observed_progress="last started code cell 2 of 40"),
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="cell_timeout_download",
                 runtime_observed_progress="last started code cell 30 of 40"),
            _row(fixed=False, runtime_observed_category="E", runtime_observed_reason="missing_data_file"),
        ]
        report = analyze(rows)
        self.assertIn("timed-out notebooks: 2; with a recorded position: 2", report)
        self.assertIn("stopped in the first 3 code cells: 1", report)
        self.assertIn("stopped in the second half of the notebook: 1", report)
        self.assertIn("2/40, 30/40", report)

    def test_no_timeouts_is_stated(self):
        self.assertIn("(no timeouts)", analyze([_row(fixed=True)]))


class TestExternalToolScopeBucket(unittest.TestCase):
    def test_shell_command_failures_get_their_own_scope_line(self):
        rows = [
            _row(fixed=False, runtime_observed_category="D", runtime_observed_reason="shell_command_failed"),
            _row(fixed=False, runtime_observed_category="A", runtime_observed_reason="dependency:missing_module"),
        ]
        report = analyze(rows)
        self.assertIn("needs an external / system tool or shell command (not a Python package): 50.0% (1/2)", report)
        self.assertIn("dependency failure, not fixed: 50.0% (1/2)", report)
