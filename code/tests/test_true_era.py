"""TASK_true_era_dating.md -- true-era dating (era_date) and strict out-of-window refusal (era_strict). Offline."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import _repo_runner, add_paper_dates
from repair_tool import era, eradate, levers


def _git(cwd, *args, date=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    if date:
        env.update(GIT_AUTHOR_DATE=f"{date}T12:00:00", GIT_COMMITTER_DATE=f"{date}T12:00:00")
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, env=env)


def make_repo(d):
    _git(d, "init", "-q")
    open(os.path.join(d, "nb.ipynb"), "w").write("1")
    _git(d, "add", "."); _git(d, "commit", "-qm", "a", date="2019-03-01")
    open(os.path.join(d, "other.py"), "w").write("x")
    _git(d, "add", "."); _git(d, "commit", "-qm", "b", date="2020-06-10")
    open(os.path.join(d, "nb.ipynb"), "w").write("2")
    _git(d, "add", "."); _git(d, "commit", "-qm", "c", date="2021-01-05")
    open(os.path.join(d, "other.py"), "w").write("y")
    _git(d, "add", "."); _git(d, "commit", "-qm", "d", date="2023-02-27")


class GitDates(unittest.TestCase):
    def test_file_and_repo_dates_from_full_history(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            g = eradate.git_dates(d, "nb.ipynb")
        self.assertEqual((g["repo_first_commit"], g["repo_last_commit"]), ("2019-03-01", "2023-02-27"))
        self.assertEqual((g["file_first_commit"], g["file_last_change"]), ("2019-03-01", "2021-01-05"))
        self.assertFalse(g["shallow"])

    def test_a_shallow_clone_yields_no_file_or_first_dates(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as dst:
            make_repo(d)
            subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{d}", os.path.join(dst, "c")], check=True, capture_output=True)
            g = eradate.git_dates(os.path.join(dst, "c"), "nb.ipynb")
        self.assertTrue(g["shallow"])
        self.assertNotIn("file_last_change", g)
        self.assertNotIn("repo_first_commit", g)
        self.assertEqual(g["repo_last_commit"], "2023-02-27")


class ChooseDate(unittest.TestCase):
    GIT = {"repo_last_commit": "2023-02-27", "repo_first_commit": "2020-09-22", "file_last_change": "2021-01-05"}

    def test_paper_date_wins_when_not_after_the_last_commit(self):
        e = eradate.choose_one("2021-09-16", "received", self.GIT)
        self.assertEqual((e.date, e.source, e.grounded, e.capped), ("2021-09-16", "paper_date", True, False))
        self.assertEqual(e.candidates["file_git_date"], "2021-01-05", "other candidates stay visible")

    def test_paper_date_after_the_last_commit_is_capped_and_says_so(self):
        e = eradate.choose_one("2022-09-02", "published", {"repo_last_commit": "2019-11-26"})
        self.assertEqual((e.date, e.source, e.capped), ("2019-11-26", "paper_date", True))

    def test_file_date_is_next_and_grounded(self):
        e = eradate.choose_one(None, "published", self.GIT)
        self.assertEqual((e.date, e.source, e.grounded), ("2021-01-05", "file_git_date", True))

    def test_exploratory_file_first_ordering_is_opt_in(self):
        git = dict(self.GIT, file_first_commit="2020-10-15")
        e = eradate.choose_one("2022-05-25", "accepted", git)
        self.assertEqual((e.date, e.source), ("2022-05-25", "paper_date"))  # the default order is unchanged
        e = eradate.choose_one("2022-05-25", "accepted", git, prefer="file_first")
        self.assertEqual((e.date, e.source, e.grounded), ("2020-10-15", "file_first_commit", True))
        e = eradate.choose_one("2022-05-25", "accepted", self.GIT, prefer="file_first")  # no file history: falls back to paper
        self.assertEqual(e.source, "paper_date")

    def test_first_commit_then_last_commit_are_ungrounded_fallbacks(self):
        e = eradate.choose_one(None, "published", {"repo_last_commit": "2023-02-27", "repo_first_commit": "2020-09-22"})
        self.assertEqual((e.date, e.source, e.grounded), ("2020-09-22", "repo_first_commit", False))
        e = eradate.choose_one(None, "published", {"repo_last_commit": "2023-02-27", "shallow": True})
        self.assertEqual((e.date, e.source, e.grounded), ("2023-02-27", "repo_last_commit", False))
        self.assertIn("shallow", e.note)

    def test_nothing_available_is_none_never_invented(self):
        e = eradate.choose_one(None, "published", {})
        self.assertIsNone(e.date)

    def test_several_targets_use_the_latest_date(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            e = eradate.resolve(d, ["nb.ipynb", "other.py"], {})
        self.assertEqual(e.date, "2023-02-27")  # other.py was last changed in 2023


class BaselineIsUnchanged(unittest.TestCase):
    def test_lever_off_reports_repo_last_commit(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {levers.ENV: ""}):
            make_repo(d)
            date, meta = _repo_runner._era_date_for(d, d, {"nb.ipynb"}, None)
        self.assertEqual(date, "2023-02-27")
        self.assertEqual(meta["era_date_source"], "repo_last_commit")
        self.assertFalse(meta["era_date_grounded"])

    def test_lever_on_uses_the_hint(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {levers.ENV: "era_date"}):
            make_repo(d)
            date, meta = _repo_runner._era_date_for(d, d, {"nb.ipynb"}, {"nb.ipynb": {"paper_date": "2021-09-16"}})
        self.assertEqual((date, meta["era_date_source"], meta["era_date_grounded"]), ("2021-09-16", "paper_date", True))


class PaperDates(unittest.TestCase):
    def test_published_then_accepted_then_received(self):
        c = sqlite3.connect(":memory:")
        c.execute("create table article (id integer, published_date text, accepted_date text, received_date text)")
        c.execute("create table repositories (id integer, article_id integer)")
        c.executemany("insert into article values (?,?,?,?)", [(1, "2022-01-06", "2021-08-06", "2021-05-14"), (2, "", "2022-05-25", "2021-09-16"), (3, "", "", "2021-09-16"), (4, "", "", "")])
        c.executemany("insert into repositories values (?,?)", [(10, 1), (20, 2), (30, 3), (40, 4)])
        self.assertEqual(add_paper_dates.paper_date_for(c, "10"), ("2022-01-06", "published"))
        self.assertEqual(add_paper_dates.paper_date_for(c, "20"), ("2022-05-25", "accepted"))
        self.assertEqual(add_paper_dates.paper_date_for(c, "30"), ("2021-09-16", "received"))
        self.assertEqual(add_paper_dates.paper_date_for(c, "40"), ("", ""))
        self.assertEqual(add_paper_dates.paper_date_for(c, "99"), ("", ""))


RELEASES = [  # newest first, like pypi.release_history
    {"version": "3.7.0", "series": "3.7", "date": "2022-02-01"},
    {"version": "3.4.3", "series": "3.4", "date": "2021-08-12"},
    {"version": "3.3.4", "series": "3.3", "date": "2021-01-28"},
    {"version": "3.3.3", "series": "3.3", "date": "2020-11-11"},
]


class StrictWindow(unittest.TestCase):
    def setUp(self):
        self.rel = mock.patch.object(era, "_releases", return_value=RELEASES)
        self.rel.start()
        self.env = mock.patch.dict(os.environ, {levers.ENV: "era_strict"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.rel.stop()

    def test_a_spec_no_release_by_the_date_satisfies_is_refused_with_advice(self):
        r = era.out_of_window("matplotlib>=3.6", "2021-02-01")
        self.assertTrue(r["refused"] and r["refusal_reason"] == "era_window")
        self.assertEqual(r["newest_as_of_era"], "3.3.4")
        self.assertIn("newest as of then was 3.3.4", r["log"])
        self.assertIn("2021-02-01", r["log"])

    def test_acceptable_specs_pass(self):
        for spec in ("matplotlib", "matplotlib==3.3.3", "matplotlib<3.4", "matplotlib>=3.3,<3.5", "matplotlib[extra]>=3.0"):
            self.assertIsNone(era.out_of_window(spec, "2021-02-01"), spec)

    def test_exact_pin_after_the_date_is_refused(self):
        self.assertIsNotNone(era.out_of_window("matplotlib==3.4.3", "2021-02-01"))
        self.assertIsNone(era.out_of_window("matplotlib==3.4.3", "2021-09-01"))

    def test_package_that_did_not_exist_yet(self):
        r = era.out_of_window("matplotlib", "2019-01-01")
        self.assertIn("no release of matplotlib existed at all", r["log"])

    def test_unjudgeable_things_are_allowed(self):
        self.assertIsNone(era.out_of_window("git+https://example.com/x.git", "2021-02-01"))
        self.assertIsNone(era.out_of_window("matplotlib>=3.6", None))
        with mock.patch.object(era, "_releases", return_value=[]):
            self.assertIsNone(era.out_of_window("unknownpkg>=1", "2021-02-01"))

    def test_gate_counts_and_bounds_refusals_and_never_installs(self):
        state = {}
        for i in range(era.MAX_ERA_REFUSALS):
            g = era.strict_gate("matplotlib>=3.6", "2021-02-01", state)
            self.assertTrue(g["refused"])
        self.assertFalse(state.get("era_refusal_limit_reached"))
        g = era.strict_gate("matplotlib>=3.6", "2021-02-01", state)
        self.assertTrue(g["refused"] and state["era_refusal_limit_reached"])
        self.assertIn("limit", g["log"])
        self.assertEqual(state["era_refusals"], era.MAX_ERA_REFUSALS + 1)

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {levers.ENV: ""}):
            self.assertIsNone(era.strict_gate("matplotlib>=3.6", "2021-02-01", {}))

    def test_dispatch_refuses_without_calling_install(self):
        from repair_tool import agent_tools

        with mock.patch.object(agent_tools, "install_package") as inst:
            d = agent_tools.build_dispatch("/w/nb.ipynb", "/p/python", era_cutoff_date="2021-02-01")
            out = d["install_package"](package="matplotlib>=3.6")
        inst.assert_not_called()
        self.assertEqual(out["refusal_reason"], "era_window")


class StrictNoFallback(unittest.TestCase):
    def test_resolver_failure_is_a_refusal_not_a_modern_install(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "x No solution found when resolving dependencies:\n Because foo was not found in the package registry")

        with mock.patch.dict(os.environ, {levers.ENV: "era_resolve,era_strict"}), mock.patch.object(era, "_find_uv", return_value="/bin/uv"), \
                mock.patch("subprocess.run", side_effect=fake):
            ok, log, meta = era.pip_install("/p/python", ["foo"], "2015-01-01", 60, allow_fallback=False)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1, "plain pip must NOT be tried")
        self.assertTrue(meta["strict_refused"])
        self.assertFalse(meta.get("fallback"))
        self.assertIn("Refused under the era date 2015-01-01", log)


class LeverFlags(unittest.TestCase):
    def test_flags_parse_default_off(self):
        self.assertEqual(levers.parse("era_resolve,era_date,era_strict"), frozenset({"era_resolve", "era_date", "era_strict"}))
        self.assertEqual(levers.parse(""), frozenset())
        self.assertEqual(set(levers.SIX_LEVERS) & {"era_resolve", "era_date", "era_strict"}, set())


if __name__ == "__main__":
    unittest.main()
