import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_tool import interpreter
from repair_tool.interpreter import (
    PythonChoice,
    build_repair_environment,
    detect_required_python,
    provision_interpreter,
)


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def write(self, rel: str, text: str) -> None:
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


class TestDetectDeclared(_RepoCase):
    def test_conda_python_pin_in_binder_environment_yml(self):
        """The real ferretmagic case: binder/environment.yml pins python=3.7."""
        self.write("binder/environment.yml", "dependencies:\n  - python=3.7\n  - numpy=1.17.3\n")
        choice = detect_required_python(self.repo)
        self.assertEqual(choice.version, "3.7")
        self.assertEqual(choice.source, os.path.join("binder", "environment.yml"))
        self.assertEqual(choice.grounding, "metadata_grounded")

    def test_conda_spec_variants(self):
        cases = {
            "python=3.7.*": "3.7", "python==3.8.2": "3.8", "python 3.9.*": "3.9",
            "python=3.7.4=h12345_0": "3.7", "python>=3.6,<3.8": "3.7", "python=3.10": "3.10",
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                self.write("environment.yml", f"dependencies:\n  - {spec}\n")
                self.assertEqual(detect_required_python(self.repo).version, expected)

    def test_a_lower_bound_only_is_not_concrete(self):
        """">=3.6" says nothing about era -- must not be read as "3.6"."""
        self.write("environment.yml", "dependencies:\n  - python>=3.6\n")
        self.assertIsNone(detect_required_python(self.repo).version)

    def test_a_conventional_upper_cap_is_not_concrete(self):
        self.write("setup.py", "setup(name='x', python_requires='>=3.6,<4')\n")
        self.assertIsNone(detect_required_python(self.repo).version)

    def test_a_package_merely_prefixed_python_is_not_the_interpreter(self):
        self.write("environment.yml", "dependencies:\n  - python-dateutil=2.8\n  - python_abi=3.7\n")
        self.assertIsNone(detect_required_python(self.repo).version)

    def test_runtime_txt(self):
        self.write("runtime.txt", "python-3.8.1\n")
        choice = detect_required_python(self.repo)
        self.assertEqual((choice.version, choice.source), ("3.8", "runtime.txt"))

    def test_python_version_file(self):
        self.write(".python-version", "3.9.7\n")
        choice = detect_required_python(self.repo)
        self.assertEqual((choice.version, choice.source), ("3.9", ".python-version"))

    def test_python_requires_with_a_real_upper_bound(self):
        self.write("setup.py", "setup(name='x', python_requires='>=3.6,<3.8')\n")
        self.assertEqual(detect_required_python(self.repo).version, "3.7")

    def test_pyproject_requires_python(self):
        self.write("pyproject.toml", '[project]\nname = "x"\nrequires-python = "==3.8.*"\n')
        self.assertEqual(detect_required_python(self.repo).version, "3.8")

    def test_environment_yml_outranks_runtime_txt(self):
        self.write("environment.yml", "dependencies:\n  - python=3.7\n")
        self.write("runtime.txt", "python-3.9\n")
        self.assertEqual(detect_required_python(self.repo).version, "3.7")

    def test_a_non_concrete_file_falls_through_to_the_next_source(self):
        self.write("environment.yml", "dependencies:\n  - numpy=1.17.3\n")
        self.write("runtime.txt", "python-3.8\n")
        self.assertEqual(detect_required_python(self.repo).source, "runtime.txt")

    def test_python_2_is_flagged_unprovisionable_not_dropped(self):
        self.write("runtime.txt", "python-2.7\n")
        choice = detect_required_python(self.repo)
        self.assertEqual(choice.version, "2.7")
        self.assertTrue(choice.unprovisionable_reason)

    def test_declared_files_in_nested_dirs_are_found(self):
        self.write("some/deep/dir/runtime.txt", "python-3.8\n")
        self.assertEqual(detect_required_python(self.repo).version, "3.8")


class TestEraInference(_RepoCase):
    def test_nothing_declared_infers_from_the_last_commit_date_and_is_tagged_ungrounded(self):
        choice = detect_required_python(self.repo, era_cutoff_date="2019-05-01")
        self.assertEqual(choice.version, "3.7")  # 3.8 was released 2019-10
        self.assertEqual(choice.source, "era-inferred")
        self.assertEqual(choice.grounding, "llm_proposed")

    def test_era_boundaries(self):
        for date, expected in [("2018-06-26", "3.6"), ("2018-06-27", "3.7"), ("2022-08-25", "3.10"), ("2025-12-01", "3.14")]:
            with self.subTest(date=date):
                self.assertEqual(detect_required_python(self.repo, era_cutoff_date=date).version, expected)

    def test_declared_beats_era(self):
        self.write("runtime.txt", "python-3.6\n")
        choice = detect_required_python(self.repo, era_cutoff_date="2022-08-25")
        self.assertEqual((choice.version, choice.grounding), ("3.6", "metadata_grounded"))

    def test_no_declaration_and_no_date_gives_no_opinion(self):
        choice = detect_required_python(self.repo)
        self.assertIsNone(choice.version)
        self.assertEqual(choice.source, "none")


def _completed(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestProvision(_RepoCase):
    def setUp(self):
        super().setUp()
        self.env_dir = os.path.join(self.repo, "_env")

    def _fake_run(self, results: dict, minor_of_exe: str = "3.8"):
        """subprocess.run stand-in: uv/conda keyed by an argv fragment. On a
        "success" it creates the interpreter file so the real existence check
        in provision_interpreter passes; version probes return `minor_of_exe`."""
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if "-c" in cmd:  # _interpreter_minor probe
                return _completed(0, minor_of_exe + "\n")
            for fragment, rc in results.items():
                if fragment in cmd:
                    if rc == 0:
                        exe = interpreter._python_in(self.env_dir)
                        os.makedirs(os.path.dirname(exe), exist_ok=True)
                        open(exe, "w").close()
                    return _completed(rc, "", "boom" if rc else "")
            return _completed(1, "", "no such version")

        return run, calls

    @patch("repair_tool.interpreter.find_uv", return_value="/fake/uv")
    @patch("repair_tool.interpreter.find_conda_tool", return_value=None)
    def test_uv_provides_the_exact_version(self, _conda, _uv):
        run, calls = self._fake_run({"3.8": 0}, minor_of_exe="3.8")
        with patch("repair_tool.interpreter.subprocess.run", side_effect=run):
            prov = provision_interpreter(PythonChoice("3.8", "runtime.txt", "metadata_grounded"), self.env_dir, self.repo)
        self.assertTrue(prov.ok)
        self.assertEqual((prov.mechanism, prov.requested, prov.actual, prov.note), ("uv", "3.8", "3.8", ""))
        self.assertIn("--seed", calls[0])  # pip must exist: every installer here is `python -m pip`

    @patch("repair_tool.interpreter.find_uv", return_value="/fake/uv")
    @patch("repair_tool.interpreter.find_conda_tool", return_value=None)
    def test_unreachable_exact_version_uses_the_nearest_newer_and_says_so(self, _conda, _uv):
        """The real situation on Apple Silicon: no CPython 3.7 exists."""
        run, _ = self._fake_run({"3.8": 0}, minor_of_exe="3.8")
        with patch("repair_tool.interpreter.subprocess.run", side_effect=run):
            prov = provision_interpreter(PythonChoice("3.7", "environment.yml", "metadata_grounded"), self.env_dir, self.repo)
        self.assertTrue(prov.ok)
        self.assertEqual((prov.requested, prov.actual), ("3.7", "3.8"))
        self.assertIn("nearest available 3.8", prov.note)

    @patch("repair_tool.interpreter.find_uv", return_value="/fake/uv")
    @patch("repair_tool.interpreter.find_conda_tool", return_value=None)
    def test_actual_version_comes_from_the_interpreter_not_the_request(self, _conda, _uv):
        run, _ = self._fake_run({"3.8": 0}, minor_of_exe="3.9")
        with patch("repair_tool.interpreter.subprocess.run", side_effect=run):
            prov = provision_interpreter(PythonChoice("3.8", "runtime.txt", "metadata_grounded"), self.env_dir, self.repo)
        self.assertEqual(prov.actual, "3.9")
        self.assertIn("nearest available 3.9", prov.note)

    @patch("repair_tool.interpreter.find_uv", return_value="/fake/uv")
    @patch("repair_tool.interpreter.find_conda_tool", return_value=None)
    def test_nothing_reachable_is_reported_unprovisionable_not_raised(self, _conda, _uv):
        run, _ = self._fake_run({})
        with patch("repair_tool.interpreter.subprocess.run", side_effect=run):
            prov = provision_interpreter(PythonChoice("3.6", "runtime.txt", "metadata_grounded"), self.env_dir, self.repo)
        self.assertFalse(prov.ok)
        self.assertIn("no reachable build", prov.unprovisionable_reason)

    def test_python_2_never_touches_a_provisioner(self):
        choice = PythonChoice("2.7", "runtime.txt", "metadata_grounded", unprovisionable_reason="Python 2 is out of scope")
        with patch("repair_tool.interpreter.subprocess.run") as run:
            prov = provision_interpreter(choice, self.env_dir, self.repo)
        run.assert_not_called()
        self.assertFalse(prov.ok)
        self.assertIn("Python 2", prov.unprovisionable_reason)

    def test_pre_3_6_is_out_of_scope(self):
        with patch("repair_tool.interpreter.subprocess.run") as run:
            prov = provision_interpreter(PythonChoice("3.4", "runtime.txt", "metadata_grounded"), self.env_dir, self.repo)
        run.assert_not_called()
        self.assertFalse(prov.ok)

    @patch("repair_tool.interpreter.find_uv", return_value="/fake/uv")
    @patch("repair_tool.interpreter.find_conda_tool", return_value="/fake/micromamba")
    def test_an_environment_yml_is_built_as_a_conda_recipe(self, _conda, _uv):
        self.write("environment.yml", "dependencies:\n  - python=3.7\n")
        run, calls = self._fake_run({"create": 0}, minor_of_exe="3.7")
        with patch("repair_tool.interpreter.subprocess.run", side_effect=run):
            prov = provision_interpreter(PythonChoice("3.7", "environment.yml", "metadata_grounded"), self.env_dir, self.repo)
        self.assertEqual(prov.mechanism, "conda")
        self.assertIn(os.path.join(self.repo, "environment.yml"), calls[0])
        self.assertFalse(any("/fake/uv" == c[0] for c in calls))

    @patch("repair_tool.interpreter.find_uv", return_value="/fake/uv")
    @patch("repair_tool.interpreter.find_conda_tool", return_value="/fake/micromamba")
    def test_a_failed_conda_solve_falls_back_to_uv(self, _conda, _uv):
        """On Apple Silicon the recipe's python=3.7 / pyferret have no build:
        the whole solve fails, which must not end the run."""
        self.write("environment.yml", "dependencies:\n  - python=3.7\n")
        run, calls = self._fake_run({"create": 1, "3.7": 1, "3.8": 0}, minor_of_exe="3.8")
        with patch("repair_tool.interpreter.subprocess.run", side_effect=run):
            prov = provision_interpreter(PythonChoice("3.7", "environment.yml", "metadata_grounded"), self.env_dir, self.repo)
        self.assertTrue(prov.ok)
        self.assertEqual((prov.mechanism, prov.actual), ("uv", "3.8"))
        self.assertIn("conda", prov.log)

    @patch("repair_tool.interpreter.find_uv", return_value="/fake/uv")
    @patch("repair_tool.interpreter.find_conda_tool", return_value=None)
    def test_conda_is_not_used_for_a_non_conda_recipe(self, _conda, _uv):
        run, calls = self._fake_run({"3.8": 0}, minor_of_exe="3.8")
        with patch("repair_tool.interpreter.subprocess.run", side_effect=run):
            provision_interpreter(PythonChoice("3.8", "runtime.txt", "metadata_grounded"), self.env_dir, self.repo)
        self.assertFalse(any("create" in c for c in calls))

    @patch("repair_tool.interpreter.find_uv", return_value=None)
    @patch("repair_tool.interpreter.find_conda_tool", return_value=None)
    def test_no_provisioner_at_all_is_reported(self, _conda, _uv):
        prov = provision_interpreter(PythonChoice("3.8", "runtime.txt", "metadata_grounded"), self.env_dir, self.repo)
        self.assertFalse(prov.ok)
        self.assertIn("no interpreter provisioner", prov.unprovisionable_reason)

    def test_an_already_provisioned_interpreter_is_reused(self):
        exe = interpreter._python_in(self.env_dir)
        os.makedirs(os.path.dirname(exe))
        open(exe, "w").close()
        with patch("repair_tool.interpreter._interpreter_minor", return_value="3.8"), \
                patch("repair_tool.interpreter.subprocess.run") as run:
            prov = provision_interpreter(PythonChoice("3.8", "runtime.txt", "metadata_grounded"), self.env_dir, self.repo)
        run.assert_not_called()
        self.assertEqual(prov.mechanism, "reused")


class TestBuildRepairEnvironment(_RepoCase):
    def setUp(self):
        super().setUp()
        self.env_dir = os.path.join(self.repo, "_env")

    def test_disabled_returns_the_base_interpreter_untouched(self):
        self.write("runtime.txt", "python-3.7\n")
        env = build_repair_environment(self.repo, self.env_dir, "/base/python", enabled=False)
        self.assertEqual(env.python_exe, "/base/python")
        self.assertEqual(env.meta["python_mechanism"], "default")

    def test_no_signal_keeps_the_base_interpreter(self):
        env = build_repair_environment(self.repo, self.env_dir, "/base/python")
        self.assertEqual(env.python_exe, "/base/python")
        self.assertEqual(env.meta["python_version_source"], "none")

    def test_the_already_running_version_is_not_reprovisioned(self):
        self.write("runtime.txt", f"python-{interpreter._current_minor()}\n")
        with patch("repair_tool.interpreter.provision_interpreter") as prov:
            env = build_repair_environment(self.repo, self.env_dir, "/base/python")
        prov.assert_not_called()
        self.assertEqual(env.python_exe, "/base/python")
        self.assertEqual(env.meta["python_grounding"], "metadata_grounded")

    def test_a_provisioned_interpreter_replaces_the_base_and_records_provenance(self):
        self.write("binder/environment.yml", "dependencies:\n  - python=3.7\n")
        fake = interpreter.Provisioned(ok=True, python_exe="/new/python", requested="3.7", actual="3.8",
                                       mechanism="uv", note="3.7 not reachable; nearest available 3.8 used")
        with patch("repair_tool.interpreter.provision_interpreter", return_value=fake):
            env = build_repair_environment(self.repo, self.env_dir, "/base/python")
        self.assertEqual(env.python_exe, "/new/python")
        self.assertEqual(env.meta["python_version_requested"], "3.7")
        self.assertEqual(env.meta["python_version_actual"], "3.8")
        self.assertEqual(env.meta["python_mechanism"], "uv")
        self.assertEqual(env.meta["python_version_source"], os.path.join("binder", "environment.yml"))
        self.assertFalse(env.meta["interpreter_unprovisionable"])

    def test_a_provisioning_failure_keeps_the_base_and_never_raises(self):
        self.write("runtime.txt", "python-3.6\n")
        fake = interpreter.Provisioned(ok=False, requested="3.6", unprovisionable_reason="no reachable build")
        with patch("repair_tool.interpreter.provision_interpreter", return_value=fake):
            env = build_repair_environment(self.repo, self.env_dir, "/base/python")
        self.assertEqual(env.python_exe, "/base/python")
        self.assertTrue(env.meta["interpreter_unprovisionable"])
        self.assertEqual(env.meta["python_note"], "no reachable build")

    def test_python_2_is_recorded_as_unprovisionable(self):
        self.write("runtime.txt", "python-2.7\n")
        env = build_repair_environment(self.repo, self.env_dir, "/base/python")
        self.assertTrue(env.meta["interpreter_unprovisionable"])
        self.assertEqual(env.python_exe, "/base/python")


if __name__ == "__main__":
    unittest.main()
