import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.agent import AgentResult, TraceStep
from repair_tool.report import build_report

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "broken_examples")


def _obs_step(tool_called: str, tool_input=None, tool_result=None) -> TraceStep:
    return TraceStep(
        thought="",
        tool_called=tool_called,
        tool_input=tool_input or {},
        tool_result=tool_result or {"ok": False},
        grounding="deterministic",
        verification="n/a",
        confidence="",
    )


class TestBuildReport(unittest.TestCase):
    def test_reports_the_target_and_outcome(self):
        result = AgentResult(target="broken_examples/01_missing_package.py", fixed=True, trace=[])
        report = build_report(result)
        self.assertIn("broken_examples/01_missing_package.py", report)
        self.assertIn("FIXED", report)

    def test_an_llm_error_is_reported_without_a_trace(self):
        result = AgentResult(target="x.py", fixed=False, error="OPENAI_API_KEY is not set")
        report = build_report(result)
        self.assertIn("OPENAI_API_KEY is not set", report)

    def test_no_fix_action_taken_is_reported_honestly(self):
        result = AgentResult(target="x.py", fixed=False, trace=[_obs_step("run_target")])
        report = build_report(result)
        self.assertIn("No fix action was taken", report)

    def test_names_every_fix_action_with_its_tags(self):
        install_step = TraceStep(
            thought="numpy is missing",
            tool_called="install_package",
            tool_input={"package": "numpy"},
            tool_result={"ok": True, "log": "Successfully installed numpy"},
            grounding="metadata_grounded",
            verification="unverified",
            confidence="medium",
        )
        edit_step = TraceStep(
            thought="np.float was removed in numpy >= 1.24",
            tool_called="edit_code",
            tool_input={"edits": [{"find": "np.float", "replace": "float"}]},
            tool_result={"ok": True, "log": "applied"},
            grounding="llm_proposed",
            verification="verified",
            confidence="medium-high",
        )
        result = AgentResult(
            target="broken_examples/02_numpy_float.py",
            fixed=True,
            trace=[_obs_step("run_target"), install_step, _obs_step("verify"), edit_step, _obs_step("verify")],
            final_strategy="code_edit",
        )

        report = build_report(result)

        self.assertIn("install numpy", report)
        self.assertIn("metadata_grounded", report)
        self.assertIn("numpy is missing", report)
        self.assertIn("edit code: np.float -> float", report)
        self.assertIn("llm_proposed", report)
        self.assertIn("np.float was removed", report)
        self.assertIn("medium-high", report)
        # the necessary-but-insufficient install is not hidden just because
        # it wasn't the step that finally fixed things
        self.assertIn("unverified", report)
        self.assertIn("2 fix actions", report)
        self.assertIn("1 grounded", report)
        self.assertIn("1 model-proposed", report)

    def test_a_failed_edit_is_shown_as_unverified_not_hidden(self):
        failed_edit = TraceStep(
            thought="try removing the call",
            tool_called="edit_code",
            tool_input={"edits": [{"find": "not in the source", "replace": "x"}]},
            tool_result={"ok": False, "log": "edit 1 failed to apply: not found verbatim in source"},
            grounding="llm_proposed",
            verification="unverified",
            confidence="low",
        )
        result = AgentResult(target="x.py", fixed=False, trace=[failed_edit])
        report = build_report(result)
        self.assertIn("edit code:", report)
        self.assertIn("failed to apply", report)
        self.assertIn("low", report)

    def test_accepted_fix_confidence_flags_an_unreviewed_model_proposal(self):
        edit_step = TraceStep(
            thought="rewrite",
            tool_called="edit_code",
            tool_input={"edits": [{"find": "a", "replace": "b"}]},
            tool_result={"ok": True, "log": "applied"},
            grounding="llm_proposed",
            verification="verified",
            confidence="medium-high",
        )
        result = AgentResult(target="x.py", fixed=True, trace=[edit_step])
        report = build_report(result)
        self.assertIn("Accepted fix confidence: medium-high", report)
        self.assertIn("reviewer may wish to check", report)

    def test_accepted_fix_confidence_has_no_review_flag_when_grounded(self):
        install_step = TraceStep(
            thought="install it",
            tool_called="install_package",
            tool_input={"package": "seaborn"},
            tool_result={"ok": True, "log": "installed"},
            grounding="metadata_grounded",
            verification="verified",
            confidence="high",
        )
        result = AgentResult(target="x.py", fixed=True, trace=[install_step])
        report = build_report(result)
        self.assertIn("Accepted fix confidence: high", report)
        self.assertNotIn("reviewer may wish to check", report)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
@unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
class TestBuildReportNetwork(unittest.TestCase):
    def test_required_behaviour_real_report_tells_the_two_step_story(self):
        # A different file from test_agent.py's own real end-to-end test
        # (02_numpy_float.py), deliberately: venv_manager caches one venv +
        # workspace copy per absolute target path, so reusing the same file
        # here would find it already fixed by the other test (if it ran
        # first in the same process) and report "no fix action was taken"
        # instead of exercising the two-step story this test checks for.
        from repair_tool.agent import agent_repair

        result = agent_repair(os.path.join(EXAMPLES, "03_numpy_int_bool.py"))
        self.assertTrue(result.fixed)

        report = build_report(result)

        self.assertIn("03_numpy_int_bool.py", report)
        self.assertIn("FIXED", report)
        self.assertIn("grounding:", report)
        self.assertIn("verification:", report)
        self.assertIn("confidence:", report)
        self.assertIn("Summary:", report)


if __name__ == "__main__":
    unittest.main()
