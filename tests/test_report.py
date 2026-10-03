import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool.agent import AgentResult, TraceStep
from repair_tool.agent_repo import RepoAgentResult
from repair_tool.report import build_report, build_repo_report

SKIP_NETWORK = bool(os.environ.get("SKIP_NETWORK_TESTS"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "broken_examples")
FIXTURE_REPO = os.path.join(ROOT, "tests", "fixtures", "agent_repo")


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


class TestBuildRepoReport(unittest.TestCase):
    def test_reports_the_repo_and_overall_outcome(self):
        result = RepoAgentResult(
            repo_path=FIXTURE_REPO,
            files=[
                AgentResult(target="good.py", fixed=True, trace=[]),
                AgentResult(target="a.py", fixed=True, trace=[_obs_step("run_target")]),
            ],
            summary={"total": 2, "already_passing": 1, "fixed": 1, "still_failing": 0},
        )
        report = build_repo_report(result)
        self.assertIn(FIXTURE_REPO, report)
        self.assertIn("2 of 2 files now run", report)

    def test_an_error_is_reported_without_a_file_list(self):
        result = RepoAgentResult(repo_path="/no/such/path", error="not a directory: '/no/such/path'")
        report = build_repo_report(result)
        self.assertIn("not a directory", report)

    def test_names_every_file_and_does_not_hide_a_still_broken_one(self):
        install_step = TraceStep(
            thought="",
            tool_called="install_package",
            tool_input={"package": "seaborn"},
            tool_result={"ok": True, "log": "installed"},
            grounding="metadata_grounded",
            verification="verified",
            confidence="high",
        )
        fixed_file = AgentResult(target="a.py", fixed=True, trace=[install_step], final_strategy="install")
        already_passing_file = AgentResult(target="b.py", fixed=True, trace=[])
        broken_file = AgentResult(target="c.py", fixed=False, trace=[_obs_step("run_target")])

        result = RepoAgentResult(
            repo_path=FIXTURE_REPO,
            files=[fixed_file, already_passing_file, broken_file],
            summary={"total": 3, "already_passing": 1, "fixed": 1, "still_failing": 1},
        )

        report = build_repo_report(result)

        self.assertIn("a.py: FIXED (confidence: high)", report)
        self.assertIn("b.py: already passing (no repair needed)", report)
        self.assertIn("c.py: NOT FIXED", report)
        self.assertIn("1 file(s) remain broken", report)
        # the fixed file's per-file detail is reused, not re-derived
        self.assertIn("install seaborn", report)
        self.assertIn("metadata_grounded", report)

    def test_a_file_that_raised_is_reported_as_an_error_not_hidden(self):
        errored_file = AgentResult(target="d.py", fixed=False, error="RuntimeError: boom")
        result = RepoAgentResult(
            repo_path=FIXTURE_REPO,
            files=[errored_file],
            summary={"total": 1, "already_passing": 0, "fixed": 0, "still_failing": 1},
        )
        report = build_repo_report(result)
        self.assertIn("d.py: ERROR (RuntimeError: boom)", report)


@unittest.skipIf(SKIP_NETWORK, "SKIP_NETWORK_TESTS set")
@unittest.skipUnless(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
class TestBuildReportNetwork(unittest.TestCase):
    def test_required_behaviour_real_report_names_grounding_verification_and_confidence(self):
        # 07_collections_abc.py, deliberately: a pure stdlib import_name fix
        # (from collections import Mapping -> from collections.abc import
        # Mapping) needs no install and only one diagnose/edit/verify round,
        # so it's not shared with any other real test's cache in this suite
        # (unlike 01/02, used by test_agent.py) and isn't tight against
        # MAX_STEPS the way 03_numpy_int_bool.py's *two* separate removed
        # attributes can be (install + two edit/verify rounds came within
        # one step of exhausting the default budget in practice). The
        # two-step grounded-then-proposed story itself is already asserted
        # deterministically offline, in test_agent.py's numpy-style test;
        # this test only needs to prove build_report renders a real trace.
        from repair_tool.agent import agent_repair

        result = agent_repair(os.path.join(EXAMPLES, "07_collections_abc.py"))
        self.assertTrue(result.fixed, msg=result.error)

        report = build_report(result)

        self.assertIn("07_collections_abc.py", report)
        self.assertIn("FIXED", report)
        self.assertIn("grounding:", report)
        self.assertIn("verification:", report)
        self.assertIn("confidence:", report)
        self.assertIn("Summary:", report)


if __name__ == "__main__":
    unittest.main()
