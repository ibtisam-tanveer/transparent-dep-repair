"""TASK_prepilot_fixes.md #1 regression tests: a hung notebook/worker must be
killed at its budget, a result must still come back, and no orphan process
may survive. These use real processes (sleep, a real Jupyter kernel) because
the bug was precisely about processes outliving their parent."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nbformat
from nbformat.v4 import new_code_cell, new_notebook

from repair_tool import proc
from repair_tool.notebook import run_notebook


def _is_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # Signal 0 also "succeeds" for a zombie; treat a zombie as dead.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return out == "" or out.startswith("Z")


def _wait_dead(pid: int, seconds: float = 8.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if _is_dead(pid):
            return True
        time.sleep(0.1)
    return _is_dead(pid)


def _read_pid(path: str, seconds: float = 10.0) -> int:
    end = time.time() + seconds
    while time.time() < end:
        try:
            text = open(path).read().strip()
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError(f"no pid appeared in {path}")


class TestRunGroup(unittest.TestCase):
    def test_a_normal_run_returns_output_and_exit_code(self):
        completed = proc.run_group([sys.executable, "-c", "print('hi'); import sys; sys.exit(3)"], text=True, timeout=30)
        self.assertEqual(completed.returncode, 3)
        self.assertIn("hi", completed.stdout)

    def test_timeout_kills_a_grandchild_not_just_the_direct_child(self):
        """subprocess.run(timeout=...) kills only the direct child and
        leaves whatever it spawned running; run_group kills the group."""
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "pid")
            script = f"sleep 300 & echo $! > {pidfile}; wait"
            started = time.time()
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.run_group(["sh", "-c", script], text=True, timeout=2)
            self.assertLess(time.time() - started, 20, "run_group must return promptly after the timeout")
            self.assertTrue(_wait_dead(_read_pid(pidfile)), "the grandchild survived the timeout")

    def test_never_signals_its_own_process_group(self):
        self.assertFalse(proc.kill_process_group(os.getpgrp()))
        self.assertFalse(proc.kill_process_group(None))
        self.assertFalse(proc.kill_process_group(1))


class TestKernelRegistry(unittest.TestCase):
    def test_reap_kills_registered_groups_and_spares_unregistered_ones(self):
        victim = subprocess.Popen(["sleep", "300"], start_new_session=True)
        spared = subprocess.Popen(["sleep", "300"], start_new_session=True)
        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "reg")
            with patch.dict(os.environ, {proc.REGISTRY_ENV: registry}):
                proc.register_group(victim.pid)
                proc.register_group(spared.pid)
                proc.unregister_group(spared.pid)  # shut down cleanly -> must not be touched
            try:
                self.assertEqual(proc.reap_registered(registry), 1)
                self.assertTrue(_wait_dead(victim.pid))
                victim.wait(timeout=5)
                self.assertIsNone(spared.poll(), "an unregistered group must be left alone")
            finally:
                spared.kill()
                spared.wait(timeout=5)
                victim.kill() if victim.poll() is None else None

    def test_reap_tolerates_a_missing_or_garbled_registry(self):
        self.assertEqual(proc.reap_registered(None), 0)
        self.assertEqual(proc.reap_registered("/nonexistent/path"), 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reg")
            open(path, "w").write("garbage\n+notanumber\n+\n")
            self.assertEqual(proc.reap_registered(path), 0)


def _write_nb(path, sources):
    nbformat.write(new_notebook(cells=[new_code_cell(s) for s in sources]), path)


class TestHungNotebookIsKilled(unittest.TestCase):
    def test_a_hanging_cell_is_killed_and_its_child_process_does_not_survive(self):
        """A cell spawns a child then hangs. run_notebook must return a
        failed result at the cell budget AND leave no orphan behind."""
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "child.pid")
            nb = os.path.join(tmp, "hang.ipynb")
            _write_nb(nb, [
                textwrap.dedent(f"""
                    import subprocess, time
                    p = subprocess.Popen(["sleep", "300"])
                    open({pidfile!r}, "w").write(str(p.pid))
                    time.sleep(300)
                """),
            ])
            started = time.time()
            result = run_notebook(nb, timeout=5)
            elapsed = time.time() - started

            self.assertFalse(result.ok)
            self.assertIn("Timeout", result.stderr)
            self.assertLess(elapsed, 60)
            self.assertTrue(_wait_dead(_read_pid(pidfile)), "the notebook's child process survived run_notebook")

    def test_the_whole_notebook_deadline_holds_even_when_every_cell_is_within_its_own_budget(self):
        """Each cell finishes inside the per-cell limit, so nbclient never
        times out -- only the whole-notebook watchdog can stop this."""
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "child.pid")
            nb = os.path.join(tmp, "slow.ipynb")
            _write_nb(nb, [
                f"import subprocess; p = subprocess.Popen(['sleep','300']); open({pidfile!r},'w').write(str(p.pid))",
            ] + ["import time; time.sleep(2)"] * 10)
            started = time.time()
            result = run_notebook(nb, timeout=30, total_timeout=4)
            elapsed = time.time() - started

            self.assertFalse(result.ok)
            self.assertIn("NotebookHardTimeout", result.stderr)
            self.assertLess(elapsed, 40, "the watchdog must stop it long before 10 x 2s + startup would")
            self.assertTrue(_wait_dead(_read_pid(pidfile)))

    def test_a_timed_out_notebook_reports_how_far_it_got_and_which_cell_stuck(self):
        """TASK_next_steps.md step 1: 'timed out' must say where."""
        with tempfile.TemporaryDirectory() as tmp:
            nb = os.path.join(tmp, "progress.ipynb")
            _write_nb(nb, ["x = 1", "import time\nmarker_slow_cell = True\ntime.sleep(300)", "y = 2"])
            result = run_notebook(nb, timeout=5)
            self.assertFalse(result.ok)
            self.assertEqual(result.progress, "last started code cell 2 of 3")
            self.assertIn("marker_slow_cell", result.stuck_cell)

    def test_stale_counts_and_outputs_saved_in_the_notebook_do_not_leak_into_the_report(self):
        """Regression (found by comparing the pilot's recorded stuck cells with
        nbclient's own timeout message): execution counts and outputs the AUTHOR
        saved on cells that never ran in this run must not be reported."""
        import nbformat
        from nbformat.v4 import new_code_cell, new_notebook, new_output

        cells = [new_code_cell("x = 1"), new_code_cell("import time\nreal_stuck_cell = True\ntime.sleep(300)"),
                 new_code_cell("stale_later_cell = True")]
        for i, c in enumerate(cells, start=1):
            c["execution_count"] = i * 10  # saved by the author's run
        cells[2]["outputs"] = [new_output("stream", name="stdout", text="STALE OUTPUT FROM THE AUTHOR\n")]
        with tempfile.TemporaryDirectory() as tmp:
            nb = os.path.join(tmp, "stale.ipynb")
            nbformat.write(new_notebook(cells=cells), nb)
            result = run_notebook(nb, timeout=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.progress, "last started code cell 2 of 3")  # not 3 of 3
        self.assertIn("real_stuck_cell", result.stuck_cell)
        self.assertNotIn("STALE OUTPUT", result.stdout)

    def test_a_clean_notebook_is_unaffected_by_the_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            nb = os.path.join(tmp, "ok.ipynb")
            _write_nb(nb, ["print('fine')"])
            result = run_notebook(nb, timeout=30)
            self.assertTrue(result.ok, result.stderr)
            self.assertIn("fine", result.stdout)


class TestHungWorkerIsKilled(unittest.TestCase):
    def test_a_sigkilled_worker_still_has_its_kernel_group_reaped(self):
        """The worker registers a kernel-like group (own session, as
        jupyter_client starts them), then hangs. The harness kills the
        worker at the timeout; the registry reap must take the kernel
        group down too, and the partial row must still be returned."""
        from evaluation import run_eval

        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "kernel.pid")
            stub = os.path.join(tmp, "hanging_worker_stub.py")
            open(stub, "w").write(textwrap.dedent(f"""
                import argparse, json, os, subprocess, time
                ap = argparse.ArgumentParser(); ap.add_argument("--input"); ap.add_argument("--output")
                a = ap.parse_args()
                with open(a.output, "w") as f:
                    f.write(json.dumps({{"notebook_path": "a.ipynb", "fixed": True}}) + "\\n")
                # a "kernel": its own session, so a group-kill of the worker misses it
                k = subprocess.Popen(["sleep", "300"], start_new_session=True)
                open({pidfile!r}, "w").write(str(k.pid))
                with open(os.environ["REPAIR_TOOL_KERNEL_REGISTRY"], "a") as r:
                    r.write("+" + str(k.pid) + "\\n")
                time.sleep(300)
            """))
            env = dict(os.environ, PYTHONPATH=tmp + os.pathsep + os.environ.get("PYTHONPATH", ""))
            started = time.time()
            with patch.dict(os.environ, env, clear=True), patch.object(run_eval, "WORKER_MODULE", "hanging_worker_stub"):
                rows, status = run_eval.run_repo_worker("x/y", "u", "/fake", [], "run1", 30, 100, timeout_seconds=4)
            elapsed = time.time() - started

            self.assertIn("timed out", status)
            self.assertEqual(rows, [{"notebook_path": "a.ipynb", "fixed": True}])
            self.assertLess(elapsed, 30)
            self.assertTrue(_wait_dead(_read_pid(pidfile)), "the kernel-like process survived the worker timeout")


if __name__ == "__main__":
    unittest.main()
