"""Lever 7, era-consistent resolution (TASK_era_consistent_resolution.md) -- offline tests.

No network, no real uv/pip: subprocess.run is replaced and the commands that WOULD run are inspected."""

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import apply as apply_module
from repair_tool import era, levers, repo
from repair_tool.repair import Proposal


def completed(returncode=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=out, stderr=err)


class EraLeverFlag(unittest.TestCase):
    def test_parse_and_default_off(self):
        self.assertIn("era_resolve", levers.ALL_LEVERS)
        self.assertEqual(levers.parse("era_resolve"), frozenset({"era_resolve"}))
        self.assertEqual(levers.parse(None), frozenset())
        self.assertNotIn("era_resolve", levers.SIX_LEVERS, "the six-lever set the earlier results used is unchanged")
        self.assertEqual(levers.parse("all"), frozenset(levers.ALL_LEVERS))

    def test_stamp_is_end_of_day(self):
        self.assertEqual(era.stamp("2019-05-17"), "2019-05-17T23:59:59Z")
        self.assertEqual(era.stamp("2019-05-17 10:00:00 +0200"), "2019-05-17T23:59:59Z")


class EraPipInstall(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {levers.ENV: "era_resolve"})
        self.env.start()
        self.uv = mock.patch.object(era, "_find_uv", return_value="/bin/uv")
        self.uv.start()

    def tearDown(self):
        self.uv.stop()
        self.env.stop()

    def test_uses_uv_with_the_repo_date(self):
        with mock.patch("subprocess.run", return_value=completed()) as run:
            ok, _log, meta = era.pip_install("/env/bin/python", ["osmnx"], "2019-05-17", 60)
        cmd = run.call_args[0][0]
        self.assertTrue(ok)
        self.assertEqual(cmd[:3], ["/bin/uv", "pip", "install"])
        self.assertEqual(cmd[cmd.index("--python") + 1], "/env/bin/python")
        self.assertEqual(cmd[cmd.index("--exclude-newer") + 1], "2019-05-17T23:59:59Z")
        self.assertEqual(cmd[-1], "osmnx")
        self.assertTrue(meta["applied"])
        self.assertEqual((meta["mechanism"], meta["date"]), ("uv --exclude-newer", "2019-05-17"))

    def test_no_repo_date_runs_plain_pip_and_says_why(self):
        with mock.patch("subprocess.run", return_value=completed()) as run:
            ok, _log, meta = era.pip_install("/env/bin/python", ["osmnx"], None, 60)
        self.assertEqual(run.call_args[0][0][1:4], ["-m", "pip", "install"])
        self.assertTrue(ok)
        self.assertFalse(meta["applied"])
        self.assertEqual(meta["reason"], "no_repo_date")

    def test_resolution_failure_falls_back_to_plain_pip_and_is_recorded(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "/bin/uv":
                return completed(1, err="  x No solution found when resolving dependencies:\n  Because foo was not found in the package registry")
            return completed(0, out="Successfully installed foo-9.9")

        with mock.patch("subprocess.run", side_effect=fake):
            ok, log, meta = era.pip_install("/env/bin/python", ["foo"], "2015-01-01", 60)
        self.assertTrue(ok)
        self.assertIn("Successfully installed", log)
        self.assertTrue(meta["fallback"])
        self.assertTrue(meta["fallback_ok"])
        self.assertEqual(meta["mechanism"], "pip (fallback)")
        self.assertIn("No solution found", meta["era_failure_tail"])
        self.assertEqual(len(calls), 2)

    def test_build_failure_is_not_a_resolution_failure(self):
        """A package that fails to BUILD would fail the same under plain pip: no fallback, the failure is reported as is."""
        with mock.patch("subprocess.run", return_value=completed(1, err="error: Failed to build `rtree==0.9.4`")) as run:
            ok, log, meta = era.pip_install("/env/bin/python", ["rtree"], "2020-06-01", 60)
        self.assertFalse(ok)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(meta.get("fallback"))
        self.assertIn("Failed to build", log)

    def test_uv_missing_degrades_to_plain_pip(self):
        with mock.patch.object(era, "_find_uv", return_value=None), mock.patch("subprocess.run", return_value=completed()):
            ok, _log, meta = era.pip_install("/env/bin/python", ["x"], "2019-01-01", 60)
        self.assertTrue(ok)
        self.assertEqual(meta["reason"], "uv_unavailable")

    def test_summarize(self):
        s = era.summarize([{"applied": True}, {"applied": True, "fallback": True}, {"applied": False, "reason": "conda_install"}, {}])
        self.assertEqual(s, {"installs": 3, "applied": 1, "fallback": 1, "not_applied": 1, "reasons": ["conda_install"]})


class EraOffChangesNothing(unittest.TestCase):
    def test_apply_with_lever_off_is_the_old_pip_command_and_records_nothing(self):
        with mock.patch.dict(os.environ, {levers.ENV: ""}), mock.patch("subprocess.run", return_value=completed()) as run:
            ok, _log, meta = apply_module.apply_ex(Proposal(kind="install", package="numpy"), "/env/bin/python", era_date="2019-01-01")
        self.assertTrue(ok)
        self.assertEqual(run.call_args[0][0], ["/env/bin/python", "-m", "pip", "install", "numpy"])
        self.assertEqual(meta, {})

    def test_apply_with_lever_on_goes_through_uv(self):
        with mock.patch.dict(os.environ, {levers.ENV: "era_resolve"}), \
                mock.patch.object(era, "_find_uv", return_value="/bin/uv"), \
                mock.patch("subprocess.run", return_value=completed()) as run:
            ok, _log, meta = apply_module.apply_ex(Proposal(kind="install", package="numpy"), "/env/bin/python", era_date="2019-01-01")
        self.assertTrue(ok)
        self.assertIn("--exclude-newer", run.call_args[0][0])
        self.assertTrue(meta["applied"])

    def test_declared_requirements_use_the_date_and_report_it(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {levers.ENV: "era_resolve"}), \
                mock.patch.object(era, "_find_uv", return_value="/bin/uv"), \
                mock.patch("subprocess.run", return_value=completed()) as run:
            with open(os.path.join(d, "requirements.txt"), "w") as f:
                f.write("pandas\n")
            metas: list = []
            ok, _log, used = repo._install_dependencies("/env/bin/python", d, ["requirements.txt"], 60, era_date="2018-03-04", era_meta_out=metas)
        self.assertTrue(ok)
        self.assertEqual(used, "requirements.txt")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--exclude-newer") + 1], "2018-03-04T23:59:59Z")
        self.assertEqual(len(metas), 1)
        self.assertTrue(metas[0]["applied"])

    def test_setup_record(self):
        with mock.patch.dict(os.environ, {levers.ENV: ""}):
            self.assertEqual(era.setup_record("2019-01-01", [], {}), {})
        with mock.patch.dict(os.environ, {levers.ENV: "era_resolve"}), mock.patch.object(era, "_find_uv", return_value="/bin/uv"):
            rec = era.setup_record("2019-01-01", [{"applied": True}], {"python_mechanism": "conda"})
        self.assertTrue(rec["applied"])
        self.assertEqual(rec["declared_installs"]["installs"], 1)
        self.assertIn("conda", rec["note"])


if __name__ == "__main__":
    unittest.main()
