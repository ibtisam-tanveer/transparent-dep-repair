"""TASK_improve_fix_rate.md: the six repair levers, each individually toggleable.
Everything here is offline (fakes for PyPI, pip, conda and the model)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import agent_tools, condafallback, levers, pyladder, versions
from repair_tool.agent import MAX_FALLBACK_NUDGES, agent_repair, system_prompt
from repair_tool.runner import RunResult
from tests.test_agent import EXAMPLES, _response, _tool_call

API_ERR = "ImportError: cannot import name 'get_nearest_node' from 'osmnx.distance' (/x/osmnx/distance.py)"


def _history(*series):
    """Newest-first release history with one release per 'major.minor' string."""
    return [{"version": f"{s}.0", "series": s, "date": "2020-01-01"} for s in series]


class TestLeverFlags(unittest.TestCase):
    def test_parse_all_none_and_lists(self):
        self.assertEqual(levers.parse("all"), frozenset(levers.ALL_LEVERS))
        self.assertEqual(levers.parse(""), frozenset())
        self.assertEqual(levers.parse("none"), frozenset())
        self.assertEqual(levers.parse("downgrade, code_forward"), frozenset({"downgrade", "code_forward"}))

    def test_a_typo_is_an_error_not_a_silently_different_experiment(self):
        with self.assertRaises(ValueError):
            levers.parse("dowgrade")

    def test_the_default_is_off_and_the_environment_switches_levers_on(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(levers.active(), frozenset())
            self.assertFalse(levers.enabled(levers.DOWNGRADE))
        with patch.dict(os.environ, {levers.ENV: "downgrade"}, clear=True):
            self.assertTrue(levers.enabled(levers.DOWNGRADE))
            self.assertFalse(levers.enabled(levers.CONDA_FALLBACK))

    def test_tool_specs_and_dispatch_expose_a_lever_tool_only_when_its_lever_is_on(self):
        base = {s["function"]["name"] for s in agent_tools.TOOL_SPECS}
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual({s["function"]["name"] for s in agent_tools.tool_specs()}, base)
            self.assertEqual(set(agent_tools.build_dispatch("/f.py", "/p")), base)
        with patch.dict(os.environ, {levers.ENV: "downgrade"}, clear=True):
            self.assertEqual({s["function"]["name"] for s in agent_tools.tool_specs()}, base | {"downgrade_search"})
            self.assertEqual(set(agent_tools.build_dispatch("/f.py", "/p")), base | {"downgrade_search"})
        with patch.dict(os.environ, {levers.ENV: "all"}, clear=True):
            names = {s["function"]["name"] for s in agent_tools.tool_specs()}
            self.assertEqual(names, base | {"downgrade_search", "lower_python"})
            self.assertEqual(set(agent_tools.build_dispatch("/f.py", "/p")), names)

    def test_the_prompt_is_the_baseline_unless_a_lever_is_on(self):
        from repair_tool.agent import _SYSTEM_PROMPT

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(system_prompt(), _SYSTEM_PROMPT)
        with patch.dict(os.environ, {levers.ENV: "code_forward"}, clear=True):
            self.assertIn("migrate", system_prompt())
            self.assertNotIn("downgrade_search", system_prompt())


class TestProbeSchedule(unittest.TestCase):
    def test_walks_back_through_older_series_geometrically(self):
        history = _history("2.1", "2.0", "1.9", "1.8", "1.7", "1.6", "1.5", "1.4", "1.3", "1.2", "1.1", "1.0")
        picks = versions.probe_schedule(history, "2.1.0", max_tries=6)
        self.assertEqual([p["series"] for p in picks], ["2.0", "1.9", "1.8", "1.6", "1.3", "1.0"][:len(picks)])
        self.assertLessEqual(len(picks), 6)

    def test_only_series_older_than_the_installed_one_are_candidates(self):
        picks = versions.probe_schedule(_history("3.0", "2.0", "1.0"), "2.0.5")
        self.assertEqual([p["series"] for p in picks], ["1.0"])

    def test_the_latest_patch_of_each_series_is_used(self):
        history = [{"version": "1.9.3", "series": "1.9", "date": ""}, {"version": "1.9.0", "series": "1.9", "date": ""},
                   {"version": "1.8.2", "series": "1.8", "date": ""}]
        self.assertEqual([p["version"] for p in versions.probe_schedule(history, "2.0.0")], ["1.9.3", "1.8.2"])

    def test_nothing_older_means_no_candidates(self):
        self.assertEqual(versions.probe_schedule(_history("1.0"), "1.0.0"), [])
        self.assertEqual(versions.probe_schedule(_history("1.0"), "not-a-version"), [])


class TestDowngradeSearch(unittest.TestCase):
    def _search(self, fixing_series=None, new_error=None, install_fails=(), history=None, installed="2.1.0", **kw):
        installed_now = {"v": installed}
        calls = []

        def install_fn(spec):
            version = spec.split("==")[1]
            calls.append(version)
            if any(version.startswith(s) for s in install_fails):
                return False, "ERROR: no matching distribution"
            installed_now["v"] = version
            return True, "ok"

        def run_fn():
            v = installed_now["v"]
            if fixing_series and v.startswith(fixing_series):
                return {"ok": True, "stdout": "", "stderr": ""}
            if new_error and v.startswith(new_error[0]):
                return {"ok": False, "stderr": new_error[1]}
            return {"ok": False, "stderr": API_ERR}

        hist = history or _history("2.1", "2.0", "1.9", "1.8", "1.7", "1.6", "1.5", "1.4", "1.3", "1.2", "1.1", "1.0")
        result = versions.downgrade_search(
            "osmnx", "/p", install_fn, run_fn, initial_failure={"ok": False, "stderr": API_ERR},
            history_fn=lambda name: hist, version_fn=lambda py, pkg: installed_now["v"] if installed else None, **kw,
        )
        return result, calls, installed_now

    def test_finds_the_version_where_the_project_passes_and_records_every_try(self):
        result, calls, _ = self._search(fixing_series="1.8")
        self.assertTrue(result["ok"])
        self.assertEqual(result["final_version"], "1.8.0")
        self.assertEqual(result["stopped_because"], "passes")
        self.assertEqual([t["outcome"] for t in result["tried"]], ["same_error", "same_error", "passes"])
        self.assertEqual([t["version"] for t in result["tried"]], ["2.0.0", "1.9.0", "1.8.0"])
        self.assertEqual(result["original_version"], "2.1.0")

    def test_when_nothing_works_the_original_version_is_restored_and_edit_code_is_suggested(self):
        result, calls, installed_now = self._search()
        self.assertFalse(result["ok"])
        self.assertEqual(result["final_version"], "2.1.0")
        self.assertEqual(installed_now["v"], "2.1.0")
        self.assertTrue(result["restored_original"])
        self.assertIn("edit_code", result["message"])
        self.assertLessEqual(len(result["tried"]), versions.DEFAULT_MAX_TRIES)

    def test_a_different_error_keeps_that_version_and_hands_back_the_new_layer(self):
        result, _, installed_now = self._search(new_error=("1.9", "ModuleNotFoundError: No module named 'shapely'"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["stopped_because"], "error_changed")
        self.assertEqual(result["final_version"], "1.9.0")
        self.assertEqual(installed_now["v"], "1.9.0")
        self.assertIn("shapely", result["message"])

    def test_a_still_this_packages_api_error_keeps_searching(self):
        other_api = "ImportError: cannot import name 'other_fn' from 'osmnx.utils'"
        result, _, _ = self._search(fixing_series="1.6", new_error=("1.9", other_api))
        self.assertTrue(result["ok"])
        self.assertEqual(result["tried"][1]["outcome"], "still_this_packages_api")

    def test_a_failed_install_is_skipped_not_fatal(self):
        result, _, _ = self._search(fixing_series="1.8", install_fails=("1.9",))
        self.assertTrue(result["ok"])
        self.assertEqual(result["tried"][1]["outcome"], "install_failed")

    def test_the_try_budget_is_bounded(self):
        result, calls, _ = self._search(max_tries=2)
        self.assertEqual(len(result["tried"]), 2)

    def test_the_time_budget_is_bounded(self):
        result, _, _ = self._search(time_budget_seconds=-1)
        self.assertEqual(result["stopped_because"], "time_budget")

    def test_a_package_that_is_not_installed_or_has_no_history_is_reported_not_crashed(self):
        result, _, _ = self._search(installed=None)
        self.assertEqual(result["stopped_because"], "package_not_installed")
        empty = versions.downgrade_search("x", "/p", lambda s: (True, ""), lambda: {"ok": False, "stderr": ""},
                                          history_fn=lambda n: [], version_fn=lambda p, k: "1.0.0")
        self.assertEqual(empty["stopped_because"], "no_release_history")


class TestCondaFallback(unittest.TestCase):
    def test_build_failure_detection_distinguishes_a_build_from_a_missing_package(self):
        self.assertTrue(condafallback.looks_like_build_failure("ERROR: Failed building wheel for abutils"))
        self.assertTrue(condafallback.looks_like_build_failure("error: command 'gcc' failed with exit status 1"))
        self.assertFalse(condafallback.looks_like_build_failure("ERROR: No matching distribution found for abutils"))
        self.assertFalse(condafallback.looks_like_build_failure(""))

    def test_pin_translation_and_names(self):
        self.assertEqual(condafallback.pip_spec_to_conda("cooltools==0.3.2"), "cooltools=0.3.2")
        self.assertEqual(condafallback.pip_spec_to_conda("numpy<1.24"), "numpy<1.24")
        self.assertEqual(condafallback.base_name("Rpy2==3.2.2"), "rpy2")
        self.assertEqual(condafallback.system_dependency_specs("rpy2==3.2.2"), ["r-base", "rpy2"])
        self.assertIsNone(condafallback.system_dependency_specs("pandas"))

    def test_a_plain_venv_cannot_take_conda_packages_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bin"))
            result = condafallback.conda_install(os.path.join(tmp, "bin", "python"), ["x"])
        self.assertFalse(result["ok"])
        self.assertIn("plain venv", result["unavailable"])

    def _conda_env(self, tmp):
        os.makedirs(os.path.join(tmp, "conda-meta"))
        os.makedirs(os.path.join(tmp, "bin"))
        return os.path.join(tmp, "bin", "python")

    def test_conda_install_runs_micromamba_against_the_environment_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = self._conda_env(tmp)
            with patch("repair_tool.interpreter.find_conda_tool", return_value="/usr/bin/micromamba"), \
                 patch("repair_tool.condafallback.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, "done", "")) as run:
                result = condafallback.conda_install(python, ["abutils=0.2.4"])
            cmd = run.call_args.args[0]
        self.assertTrue(result["ok"])
        self.assertEqual(cmd[:5], ["/usr/bin/micromamba", "install", "-y", "-p", tmp])
        self.assertIn("conda-forge", cmd)
        self.assertIn("abutils=0.2.4", cmd)

    def test_an_unavailable_exact_build_retries_unpinned_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = self._conda_env(tmp)
            outcomes = [subprocess.CompletedProcess([], 1, "", "PackagesNotFoundError"), subprocess.CompletedProcess([], 0, "ok", "")]
            with patch("repair_tool.interpreter.find_conda_tool", return_value="/m"), \
                 patch("repair_tool.condafallback.subprocess.run", side_effect=outcomes):
                result = condafallback.conda_install_with_unpinned_retry(python, "abutils==0.2.4")
        self.assertTrue(result["ok"])
        self.assertFalse(result["pinned"])


class TestInstallPackageLevers(unittest.TestCase):
    PIP_BUILD_FAIL = "Building wheel for abutils (pyproject.toml): finished with status 'error'\nERROR: Failed to build abutils"

    def test_default_off_never_touches_conda(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("repair_tool.agent_tools.apply_module.apply", return_value=(False, self.PIP_BUILD_FAIL)), \
             patch("repair_tool.agent_tools.condafallback.conda_install_with_unpinned_retry") as conda:
            result = agent_tools.install_package("abutils", "/p")
        conda.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertNotIn("lever", result)

    def test_conda_fallback_rescues_a_pip_build_failure_and_credits_the_lever(self):
        with patch.dict(os.environ, {levers.ENV: "conda_fallback"}, clear=True), \
             patch("repair_tool.agent_tools.apply_module.apply", return_value=(False, self.PIP_BUILD_FAIL)), \
             patch("repair_tool.agent_tools.condafallback.conda_install_with_unpinned_retry",
                   return_value={"ok": True, "specs": ["abutils"], "log": "installed", "pinned": False, "mechanism": "conda"}):
            result = agent_tools.install_package("abutils", "/p")
        self.assertTrue(result["ok"])
        self.assertEqual((result["mechanism"], result["lever"]), ("conda", "conda_fallback"))

    def test_a_failed_conda_fallback_is_recorded_but_not_credited(self):
        with patch.dict(os.environ, {levers.ENV: "conda_fallback"}, clear=True), \
             patch("repair_tool.agent_tools.apply_module.apply", return_value=(False, self.PIP_BUILD_FAIL)), \
             patch("repair_tool.agent_tools.condafallback.conda_install_with_unpinned_retry",
                   return_value={"ok": False, "specs": ["abutils"], "log": "", "unavailable": "plain venv"}):
            result = agent_tools.install_package("abutils", "/p")
        self.assertFalse(result["ok"])
        self.assertNotIn("lever", result)  # attempted, not credited
        self.assertEqual(result["conda_fallback"]["unavailable"], "plain venv")

    def test_a_non_build_pip_failure_does_not_trigger_the_fallback(self):
        with patch.dict(os.environ, {levers.ENV: "conda_fallback"}, clear=True), \
             patch("repair_tool.agent_tools.apply_module.apply", return_value=(False, "ERROR: No matching distribution found")), \
             patch("repair_tool.agent_tools.condafallback.conda_install_with_unpinned_retry") as conda:
            agent_tools.install_package("nonexistent", "/p")
        conda.assert_not_called()

    def test_system_deps_provisions_r_for_rpy2_through_conda_without_calling_pip(self):
        with patch.dict(os.environ, {levers.ENV: "system_deps"}, clear=True), \
             patch("repair_tool.agent_tools.condafallback.provision_system_dependency",
                   return_value={"ok": True, "specs": ["r-base", "rpy2"], "log": "ok", "pinned": False}), \
             patch("repair_tool.agent_tools.apply_module.apply") as pip:
            result = agent_tools.install_package("rpy2", "/p")
        pip.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual((result["mechanism"], result["lever"]), ("conda", "system_deps"))
        self.assertEqual(result["system_dependency"]["specs"], ["r-base", "rpy2"])

    def test_system_deps_that_cannot_be_provided_fall_back_to_pip_and_record_why(self):
        with patch.dict(os.environ, {levers.ENV: "system_deps"}, clear=True), \
             patch("repair_tool.agent_tools.condafallback.provision_system_dependency",
                   return_value={"ok": False, "specs": ["r-base", "rpy2"], "log": "", "unavailable": "plain venv"}), \
             patch("repair_tool.agent_tools.apply_module.apply", return_value=(False, "R_HOME not set")) as pip:
            result = agent_tools.install_package("rpy2", "/p")
        pip.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertEqual(result["system_dependency"]["unavailable"], "plain venv")
        self.assertNotIn("lever", result)

    def test_a_package_outside_the_table_never_asks_conda_for_system_deps(self):
        with patch.dict(os.environ, {levers.ENV: "system_deps"}, clear=True), \
             patch("repair_tool.agent_tools.condafallback.conda_install") as conda, \
             patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "ok")):
            agent_tools.install_package("pandas", "/p")
        conda.assert_not_called()


class TestPythonLadder(unittest.TestCase):
    def test_next_lower_steps_down_the_ladder_and_stops_at_the_floor(self):
        self.assertEqual(pyladder.next_lower("3.9"), "3.8")
        self.assertEqual(pyladder.next_lower("3.14"), "3.13")
        self.assertEqual(pyladder.next_lower("3.7"), "3.6")
        self.assertIsNone(pyladder.next_lower("3.6"))
        self.assertIsNone(pyladder.next_lower(None))

    def test_hints_recognise_a_too_new_interpreter(self):
        for text in (
            "ERROR: Package 'x' requires a different Python: 3.9.1 not in '<3.8'",
            "ERROR: No matching distribution found for numpy==1.17.3",
            "ImportError: cannot import name 'Mapping' from 'collections'",
            "ModuleNotFoundError: No module named 'distutils.msvccompiler'",
            "AttributeError: module 'inspect' has no attribute 'getargspec'",
        ):
            with self.subTest(text=text[:50]):
                self.assertIsNotNone(pyladder.python_too_new_hint(text))
        self.assertIsNone(pyladder.python_too_new_hint("FileNotFoundError: data.csv"))
        self.assertIsNone(pyladder.python_too_new_hint(""))

    def _prov(self, **kw):
        from repair_tool.interpreter import Provisioned
        base = dict(ok=True, python_exe="/envs/agent_py38/bin/python", requested="3.8", actual="3.8", mechanism="conda")
        base.update(kw)
        return Provisioned(**base)

    def test_lower_python_rebuilds_on_the_next_lower_version_and_replays_installs(self):
        with patch("repair_tool.interpreter._interpreter_minor", return_value="3.9"), \
             patch("repair_tool.interpreter.provision_interpreter", return_value=self._prov()) as prov, \
             patch("repair_tool.repo.find_dependency_files", return_value=["requirements.txt"]), \
             patch("repair_tool.repo._install_dependencies", return_value=(True, "", "requirements.txt")) as deps, \
             patch("repair_tool.pyladder.apply_module.apply", side_effect=[(True, ""), (False, "boom")]) as pip:
            result = pyladder.lower_python("/envs/interp/bin/python", "/repo", ["a==1", "b==2"], "scvi needs older python")
        self.assertTrue(result["ok"])
        self.assertEqual((result["from_version"], result["to_version"]), ("3.9", "3.8"))
        self.assertEqual(result["python_exe"], "/envs/agent_py38/bin/python")
        self.assertEqual(prov.call_args.args[0].version, "3.8")
        self.assertEqual(prov.call_args.args[0].source, "agent-requested")
        deps.assert_called_once()
        self.assertEqual(result["replayed_installs"], ["a==1"])
        self.assertEqual(result["replay_failed"], ["b==2"])
        self.assertEqual(pip.call_count, 2)

    def test_a_substitution_that_is_not_lower_is_refused(self):
        with patch("repair_tool.interpreter._interpreter_minor", return_value="3.8"), \
             patch("repair_tool.interpreter.provision_interpreter", return_value=self._prov(actual="3.8", requested="3.7")):
            result = pyladder.lower_python("/envs/interp/bin/python", "/repo", [])
        self.assertFalse(result["ok"])
        self.assertIn("not lower", result["message"])

    def test_an_unprovisionable_target_and_the_floor_are_reported_not_crashed(self):
        with patch("repair_tool.interpreter._interpreter_minor", return_value="3.9"), \
             patch("repair_tool.interpreter.provision_interpreter",
                   return_value=self._prov(ok=False, python_exe="", unprovisionable_reason="no build")):
            self.assertFalse(pyladder.lower_python("/p/bin/python", "/repo", [])["ok"])
        with patch("repair_tool.interpreter._interpreter_minor", return_value="3.6"):
            result = pyladder.lower_python("/p/bin/python", "/repo", [])
        self.assertFalse(result["ok"])
        self.assertIn("oldest", result["message"])


class TestDispatchWithLevers(unittest.TestCase):
    def test_lower_python_switches_the_interpreter_for_every_later_tool_call(self):
        with patch.dict(os.environ, {levers.ENV: "interpreter_down"}, clear=True), \
             patch("repair_tool.agent_tools.pyladder.lower_python",
                   return_value={"ok": True, "python_exe": "/new/bin/python", "to_version": "3.8"}), \
             patch("repair_tool.agent_tools.run_project",
                   return_value=RunResult(ok=True, returncode=0, stdout="", stderr="")) as run:
            dispatch = agent_tools.build_dispatch("/f.py", "/old/bin/python")
            dispatch["lower_python"](reason="too new")
            dispatch["run_target"]()
        self.assertEqual(run.call_args.kwargs["python_exe"], "/new/bin/python")

    def test_installs_are_remembered_so_they_can_be_replayed_onto_a_lower_interpreter(self):
        seen = {}

        def fake_lower(python, repo_root, installed, reason, **_kw):
            seen["installed"] = installed
            return {"ok": False}

        with patch.dict(os.environ, {levers.ENV: "interpreter_down"}, clear=True), \
             patch("repair_tool.agent_tools.apply_module.apply", return_value=(True, "ok")), \
             patch("repair_tool.agent_tools.pyladder.lower_python", side_effect=fake_lower):
            dispatch = agent_tools.build_dispatch("/f.py", "/p")
            dispatch["install_package"](package="seaborn==0.9.0")
            dispatch["lower_python"](reason="x")
        self.assertEqual(seen["installed"], ["seaborn==0.9.0"])

    def test_a_too_new_hint_is_attached_only_when_the_interpreter_lever_is_on(self):
        failing = RunResult(ok=False, returncode=1, stdout="", stderr="ImportError: cannot import name 'Mapping' from 'collections'")
        with patch("repair_tool.agent_tools.run_project", return_value=failing):
            with patch.dict(os.environ, {levers.ENV: "interpreter_down"}, clear=True):
                self.assertIn("hint", agent_tools.build_dispatch("/f.py", "/p")["run_target"]())
            with patch.dict(os.environ, {}, clear=True):
                self.assertNotIn("hint", agent_tools.build_dispatch("/f.py", "/p")["run_target"]())

    def test_downgrade_search_uses_the_last_failing_run_as_its_baseline(self):
        failing = RunResult(ok=False, returncode=1, stdout="", stderr=API_ERR)
        captured = {}

        def fake_search(package, python, install_fn, run_fn, initial_failure=None, **kw):
            captured.update(package=package, initial=initial_failure)
            return {"ok": False, "final_version": "2.1.0", "original_version": "2.1.0"}

        with patch.dict(os.environ, {levers.ENV: "downgrade"}, clear=True), \
             patch("repair_tool.agent_tools.run_project", return_value=failing), \
             patch("repair_tool.agent_tools.versions.downgrade_search", side_effect=fake_search):
            dispatch = agent_tools.build_dispatch("/f.py", "/p")
            dispatch["run_target"]()
            dispatch["downgrade_search"](package="osmnx")
        self.assertEqual(captured["package"], "osmnx")
        self.assertIn("get_nearest_node", captured["initial"]["stderr"])


@patch("repair_tool.agent.get_workspace_copy", return_value=os.path.join(EXAMPLES, "01_missing_package.py"))
@patch("repair_tool.agent.get_venv_python", return_value="/fake/python")
class TestAgentLoopLevers(unittest.TestCase):
    """The loop itself: bounded fallback nudges (lever 5), code-forward (6),
    and a passing downgrade search being accepted as a verified fix (1)."""

    def _run(self, levers_value, runs, scripted, max_steps=30, no_progress_limit=3):
        env = {"OPENAI_API_KEY": "sk-fake-for-test"}
        if levers_value is not None:
            env[levers.ENV] = levers_value
        with patch("repair_tool.agent_tools.run_project", side_effect=runs), \
             patch("openai.OpenAI") as client, patch.dict(os.environ, env, clear=True):
            client.return_value.chat.completions.create.side_effect = scripted
            result = agent_repair(os.path.join(EXAMPLES, "01_missing_package.py"), max_steps=max_steps,
                                  no_progress_limit=no_progress_limit)
            return result, client.return_value.chat.completions.create

    def _failing(self, n, err=API_ERR):
        return [RunResult(ok=False, returncode=1, stdout="", stderr=err) for _ in range(n)]

    def test_without_the_lever_the_run_stops_exactly_as_before(self, _v, _w):
        result, _create = self._run("downgrade", self._failing(10),
                                    [_response([_tool_call(f"c{i}", "run_target", {})]) for i in range(10)])
        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(result.fallback_nudges, [])

    def test_bounded_fallback_nudges_toward_the_untried_downgrade_then_continues(self, _v, _w):
        scripted = [_response([_tool_call(f"c{i}", "run_target", {})]) for i in range(4)]
        scripted.append(_response([_tool_call("d", "downgrade_search", {"package": "osmnx"})]))
        with patch("repair_tool.agent_tools.versions.downgrade_search",
                   return_value={"ok": True, "package": "osmnx", "original_version": "2.1.0", "final_version": "1.4.0",
                                 "stopped_because": "passes", "tried": []}):
            result, create = self._run("downgrade,bounded_fallback", self._failing(10), scripted)
        self.assertTrue(result.fixed, result.stop_reason)
        self.assertEqual(len(result.fallback_nudges), 1)
        self.assertEqual(result.fallback_nudges[0]["lever"], "downgrade")
        # the nudge reached the model as a user message naming the exact package
        sent = create.call_args_list[4].kwargs["messages"]
        self.assertTrue(any(m["role"] == "user" and "downgrade_search" in m["content"] and "osmnx" in m["content"] for m in sent))
        # and the accepted fix is the deterministic search: verified, grounded in PyPI metadata
        step = result.trace[-1]
        self.assertEqual((step.tool_called, step.verification, step.grounding), ("downgrade_search", "verified", "metadata_grounded"))
        self.assertEqual(result.final_strategy, "downgrade")

    def test_nudges_are_bounded_and_a_genuinely_stuck_run_still_stops(self, _v, _w):
        # all levers on, but the model never follows any advice
        scripted = [_response([_tool_call(f"c{i}", "run_target", {})]) for i in range(40)]
        result, _ = self._run("all", self._failing(40), scripted, max_steps=40)
        self.assertEqual(result.stop_reason, "no_progress")
        self.assertLessEqual(len(result.fallback_nudges), MAX_FALLBACK_NUDGES)
        self.assertGreaterEqual(len(result.fallback_nudges), 1)
        levers_suggested = [n["lever"] for n in result.fallback_nudges]
        self.assertEqual(len(levers_suggested), len(set(levers_suggested)), "the same advice is never repeated")

    def test_bounded_fallback_alone_has_nothing_to_suggest_and_changes_nothing(self, _v, _w):
        result, _ = self._run("bounded_fallback", self._failing(10),
                              [_response([_tool_call(f"c{i}", "run_target", {})]) for i in range(10)])
        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(result.fallback_nudges, [])

    def test_code_forward_nudges_toward_edit_code_for_a_removed_api(self, _v, _w):
        scripted = [_response([_tool_call(f"c{i}", "run_target", {})]) for i in range(10)]
        result, create = self._run("code_forward,bounded_fallback", self._failing(10), scripted, max_steps=12)
        self.assertEqual([n["lever"] for n in result.fallback_nudges][:1], ["code_forward"])
        self.assertTrue(any("edit_code" in n["message"] for n in result.fallback_nudges))

    def test_interpreter_nudge_is_offered_when_the_error_is_not_an_api_error(self, _v, _w):
        err = "ModuleNotFoundError: No module named 'scvi'"
        scripted = [_response([_tool_call(f"c{i}", "run_target", {})]) for i in range(10)]
        result, _ = self._run("interpreter_down,bounded_fallback", self._failing(10, err), scripted, max_steps=12)
        self.assertEqual(result.fallback_nudges[0]["lever"], "interpreter_down")

    def test_lower_python_step_is_unverified_until_a_later_verify_passes(self, _v, _w):
        runs = self._failing(2, "ImportError: cannot import name 'Mapping' from 'collections'") + [
            RunResult(ok=True, returncode=0, stdout="", stderr="")]
        scripted = [
            _response([_tool_call("a", "run_target", {})]),
            _response([_tool_call("b", "lower_python", {"reason": "too new"})]),
            _response([_tool_call("c", "verify", {})]),
        ]
        with patch("repair_tool.agent_tools.pyladder.lower_python",
                   return_value={"ok": True, "python_exe": "/new/python", "to_version": "3.8", "from_version": "3.9"}):
            result, _ = self._run("interpreter_down", runs[:1] + runs[2:], scripted)
        self.assertTrue(result.fixed)
        lower = [s for s in result.trace if s.tool_called == "lower_python"][0]
        self.assertEqual((lower.grounding, lower.verification), ("llm_proposed", "verified"))
        self.assertEqual(result.final_strategy, "interpreter")


class TestLeverProvenanceOnRows(unittest.TestCase):
    def _row(self, trace, nudges=None, fixed=True):
        from evaluation.run_eval import build_result_row
        from repair_tool.agent import AgentResult

        result = AgentResult(target="a.ipynb", fixed=fixed, trace=trace, fallback_nudges=nudges or [])
        return build_result_row("x/y", "u", {"notebook_path": "a.ipynb", "category": "A", "tier": "confirmed"},
                                result, {}, "run1", 30, False, repo_path="/nonexistent")

    def _step(self, tool, result, grounding="metadata_grounded"):
        from repair_tool.agent import TraceStep

        return TraceStep(thought="", tool_called=tool, tool_input={}, tool_result=result, grounding=grounding,
                         verification="verified", confidence="high")

    def test_a_downgrade_step_is_a_fix_action_with_its_lever_and_a_readable_detail(self):
        step = self._step("downgrade_search", {
            "ok": True, "package": "osmnx", "original_version": "2.1.0", "final_version": "1.4.0",
            "stopped_because": "passes", "tried": [{"version": "2.0.0", "outcome": "same_error"}, {"version": "1.4.0", "outcome": "passes"}]})
        with patch.dict(os.environ, {levers.ENV: "downgrade,bounded_fallback"}, clear=True):
            row = self._row([step], nudges=[{"lever": "downgrade", "after_step": 4, "message": "m"}])
        action = row["fix_actions"][0]
        self.assertEqual(action["lever"], "downgrade")
        self.assertIn("osmnx 2.1.0 -> 1.4.0", action["lever_detail"])
        self.assertIn("2.0.0:same_error", action["lever_detail"])
        self.assertEqual(row["levers"], "downgrade,bounded_fallback")
        self.assertEqual(len(row["fallback_nudges"]), 1)

    def test_a_lower_python_step_records_the_final_interpreter(self):
        step = self._step("lower_python", {"ok": True, "from_version": "3.9", "to_version": "3.8", "mechanism": "conda", "message": "m"},
                          grounding="llm_proposed")
        row = self._row([step])
        self.assertEqual(row["python_version_final"], "3.8")
        self.assertEqual(row["fix_actions"][0]["lever"], "interpreter_down")

    def test_an_attempted_but_failed_conda_fallback_is_not_credited_as_a_lever(self):
        step = self._step("install_package", {"ok": False, "resolved_package": "abutils", "conda_fallback": {"ok": False, "specs": ["abutils"]}})
        row = self._row([step], fixed=False)
        self.assertEqual(row["fix_actions"][0]["lever"], "")
        self.assertIn("pip build failed -> conda", row["fix_actions"][0]["lever_detail"])

    def test_the_baseline_row_records_no_levers(self):
        with patch.dict(os.environ, {}, clear=True):
            row = self._row([])
        self.assertEqual((row["levers"], row["fallback_nudges"], row["python_version_final"]), ("", [], None))


class TestAnalysisReportsLevers(unittest.TestCase):
    def test_lever_attribution_section(self):
        from evaluation.analyze_results import analyze

        base = {"discovered": True, "fixed": False, "taxonomy_category": "A", "taxonomy_tier": "confirmed", "repository": "x/y",
                "fix_actions": [], "stop_reason": "no_progress", "levers": "all"}
        rows = [
            dict(base, repository="a/one", fixed=True, fix_actions=[{"tool": "downgrade_search", "lever": "downgrade", "grounding": "metadata_grounded",
                                                                       "verification": "verified", "confidence": "high"}]),
            dict(base, repository="a/two", fallback_nudges=[{"lever": "downgrade"}], python_version_final="3.8"),
        ]
        report = analyze(rows)
        self.assertIn("lever actions taken: downgrade x1", report)
        self.assertIn("fixed with a downgrade step in the accepted fix: 1 (a/one)", report)
        self.assertIn("bounded-fallback nudges given: 1 across 1 notebooks", report)
        self.assertIn("interpreter stepped down by the agent: 1", report)


if __name__ == "__main__":
    unittest.main()


class TestCondaActivation(unittest.TestCase):
    """Found measuring lever 3: rpy2 installed through conda, but R_HOME was unset because the
    environment's python ran without conda's activation."""

    def _conda_env(self, tmp, with_r=False):
        os.makedirs(os.path.join(tmp, "conda-meta"))
        os.makedirs(os.path.join(tmp, "bin"))
        if with_r:
            os.makedirs(os.path.join(tmp, "lib", "R"))
        return os.path.join(tmp, "bin", "python")

    def test_a_plain_venv_is_untouched(self):
        from repair_tool import condaenv

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bin"))
            self.assertIsNone(condaenv.activation_env(os.path.join(tmp, "bin", "python")))

    def test_the_activated_environment_comes_from_micromamba_run_and_is_cached(self):
        from repair_tool import condaenv

        activated = {"PATH": "/env/bin:/usr/bin", "R_HOME": "/env/lib/R", "CONDA_PREFIX": "/env"}
        out = "noise\nACTENV:" + json.dumps(activated) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            python = self._conda_env(tmp)
            with patch("repair_tool.interpreter.find_conda_tool", return_value="/m"), \
                 patch("repair_tool.condaenv.subprocess.run", return_value=subprocess.CompletedProcess([], 0, out, "")) as run:
                first = condaenv.activation_env(python)
                second = condaenv.activation_env(python)
        self.assertEqual(first["R_HOME"], "/env/lib/R")
        self.assertEqual(second, first)
        self.assertEqual(run.call_count, 1, "the second call must come from the cache")
        self.assertEqual(run.call_args.args[0][:4], ["/m", "run", "-p", tmp])

    def test_a_package_install_invalidates_the_cache(self):
        from repair_tool import condaenv

        with tempfile.TemporaryDirectory() as tmp:
            python = self._conda_env(tmp)
            out = "ACTENV:" + json.dumps({"V": "1"}) + "\n"
            with patch("repair_tool.interpreter.find_conda_tool", return_value="/m"), \
                 patch("repair_tool.condaenv.subprocess.run", return_value=subprocess.CompletedProcess([], 0, out, "")) as run:
                condaenv.activation_env(python)
                future = os.path.getmtime(os.path.join(tmp, "conda-meta")) + 100
                os.utime(os.path.join(tmp, "conda-meta"), (future, future))  # conda-meta changed after the snapshot
                condaenv.activation_env(python)
        self.assertEqual(run.call_count, 2)

    def test_when_micromamba_cannot_run_the_essentials_are_still_set_including_r_home(self):
        from repair_tool import condaenv

        with tempfile.TemporaryDirectory() as tmp:
            python = self._conda_env(tmp, with_r=True)
            with patch("repair_tool.interpreter.find_conda_tool", return_value=None):
                env = condaenv.activation_env(python)
            self.assertEqual(env["R_HOME"], os.path.join(tmp, "lib", "R"))
            self.assertEqual(env["CONDA_PREFIX"], tmp)
            self.assertTrue(env["PATH"].startswith(os.path.join(tmp, "bin")))

    def test_the_notebook_executor_and_script_runner_pass_the_activated_environment(self):
        from repair_tool import notebook, runner

        sentinel = {"R_HOME": "/env/lib/R"}
        with tempfile.TemporaryDirectory() as tmp:
            nb = os.path.join(tmp, "a.ipynb")
            open(nb, "w").write(json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
            py = os.path.join(tmp, "s.py")
            open(py, "w").write("print(1)")
            done = subprocess.CompletedProcess([], 0, "\nINPROC_RESULT:" + json.dumps({"ok": True, "stdout": "", "stderr": ""}) + "\n", "")
            with patch("repair_tool.condaenv.activation_env", return_value=sentinel), \
                 patch("repair_tool.notebook.ensure_ipython", return_value=(True, "")), \
                 patch("repair_tool.notebook.run_group", return_value=done) as nb_run:
                notebook._run_notebook_inproc(nb, 10, "/env/bin/python", None, None)
            self.assertEqual(nb_run.call_args.kwargs["env"], sentinel)
            with patch("repair_tool.condaenv.activation_env", return_value=sentinel), \
                 patch("repair_tool.runner.run_group", return_value=subprocess.CompletedProcess([], 0, "1\n", "")) as py_run:
                runner._run_once(py, 10, "/env/bin/python", None)
            self.assertEqual(py_run.call_args.kwargs["env"], sentinel)


class TestRowRecordsWhatWasChanged(unittest.TestCase):
    """Transparency: the row keeps the problem the tool was asked to fix, and WHAT an edit changed."""

    def _row(self, trace, fixed=True):
        from evaluation.run_eval import build_result_row
        from repair_tool.agent import AgentResult

        return build_result_row("x/y", "u", {"notebook_path": "a.ipynb", "category": "A", "tier": "confirmed"},
                                AgentResult(target="a.ipynb", fixed=fixed, trace=trace), {}, "run1", 30, False, repo_path="/nonexistent")

    def _step(self, tool, tool_input, result):
        from repair_tool.agent import TraceStep

        return TraceStep(thought="", tool_called=tool, tool_input=tool_input, tool_result=result, grounding="llm_proposed",
                         verification="verified", confidence="medium-high")

    def test_the_first_error_and_the_edit_contents_are_kept(self):
        trace = [
            self._step("run_target", {}, {"ok": False, "stderr": "Traceback...\nModuleNotFoundError: No module named 'x'"}),
            self._step("edit_code", {"edits": [{"find": "np.float", "replace": "float"}]}, {"ok": True}),
            self._step("verify", {}, {"ok": True}),
        ]
        row = self._row(trace)
        self.assertEqual(row["initial_error"], "ModuleNotFoundError: No module named 'x'")
        edit = [a for a in row["fix_actions"] if a["tool"] == "edit_code"][0]
        self.assertEqual(edit["edit_preview"], [{"find": "np.float", "replace": "float"}])

    def test_a_notebook_that_passed_untouched_has_no_initial_error(self):
        self.assertEqual(self._row([]) ["initial_error"], "")

    def test_edit_previews_are_truncated_and_capped(self):
        edits = [{"find": "f" * 1000, "replace": "r" * 1000} for _ in range(9)]
        row = self._row([self._step("edit_code", {"edits": edits}, {"ok": True})])
        preview = row["fix_actions"][0]["edit_preview"]
        self.assertEqual(len(preview), 5)
        self.assertEqual(len(preview[0]["find"]), 240)
