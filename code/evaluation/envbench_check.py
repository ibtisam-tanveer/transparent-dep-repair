"""EnvBench's own Python check, as a run target (TASK_envbench_evaluation.md).

EnvBench (Eliseeva et al., arXiv:2503.14443; code github.com/JetBrains-Research/EnvBench) scores a Python
repository like this (evaluation/scripts/python_build.sh in their repository): after the setup script has run in
the container, run

    python -m pyright /data/project --level error --outputjson

and count the diagnostics whose rule is `reportMissingImports`. A repository PASSES when that count is zero (the
benchmark's "pass@1": exit code 0 and 0 issues).

This file is the same check, run in place against the environment the repair tool built: the harness copies it into
the repository root as `envbench_check.py` and the tool treats it like any other target -- run it, read what is
still missing, install/repair, run it again -- so "fixed" means exactly "EnvBench's check finds no missing import".
It is NOT code of the repository under repair, and the harness refuses edits to it (REPAIR_TOOL_NO_EDIT).

Deliberately only the standard library and syntax that Python 3.6 accepts: it runs under whatever interpreter the
tool selected for the repository. pyright itself lives in its own environment of the container image
(code/docker/Dockerfile.envbench) and is pointed at the repository's interpreter with `--pythonpath`, which is how
pyright's own `python -m pyright` finds the active environment.

Exit codes: 0 = no missing import (PASS); 1 = missing imports remain; 3 = the check itself could not run.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys

TARGET_NAME = "envbench_check.py"
RULE = "reportMissingImports"
PYRIGHT_BIN_DEFAULT = "/opt/conda/envs/pyright/bin/pyright"
TIMEOUT_DEFAULT = 1100  # seconds for pyright itself: below the in-container run limit (REPAIR_TOOL_RUN_TIMEOUT x container scale = 1200 s); EnvBench allows its whole evaluation container 600 s on native hardware
_IMPORT_RE = re.compile(r'Import "([^"]+)" could not be resolved')
MAX_LISTED = 25


def run_pyright(root, python_exe, pyright_bin, timeout):
    """(parsed json | None, error text). EnvBench's command; the interpreter is passed explicitly."""
    cmd = [pyright_bin, root, "--level", "error", "--outputjson", "--pythonpath", python_exe]
    try:
        completed = subprocess.run(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "pyright did not finish within %d s" % timeout
    except OSError as exc:
        return None, "could not run pyright (%s): %s" % (pyright_bin, exc)
    try:
        return json.loads(completed.stdout), ""
    except ValueError:
        tail = (completed.stderr or completed.stdout or "")[-400:].strip()
        return None, "pyright produced no JSON (exit %s): %s" % (completed.returncode, tail)


def missing_imports(parsed):
    """Every `reportMissingImports` diagnostic of a pyright --outputjson document, as (module, file) pairs --
    the same selection as EnvBench's jq filter `select(.rule == "reportMissingImports")`."""
    found = []
    for diag in parsed.get("generalDiagnostics") or []:
        if diag.get("rule") != RULE:
            continue
        match = _IMPORT_RE.search(diag.get("message", ""))
        found.append((match.group(1) if match else diag.get("message", "").strip(), diag.get("file", "")))
    return found


SKIP_DIRS = frozenset([".git", "node_modules", "__pycache__", ".tox", ".venv", "venv", ".eggs"])


def local_names(root):
    """Every directory name and .py module name anywhere in the repository (not its dependencies' directories).
    A module the repository itself provides is not a PyPI package: installing one of that name would shadow it, and the
    repair tool refuses to (it says so and the agent stops) -- so the check must not name it as the thing to install."""
    names = set()
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        names.update(dirnames)
        names.update(f[:-3] for f in filenames if f.endswith(".py"))
    return names


def is_repo_local(module, root, names=None):
    """True when the repository itself provides the first component of `module` (see local_names)."""
    if module.startswith("."):
        return True  # a relative import names a sibling of the importing file: nothing to install
    top = module.split(".", 1)[0]
    if not top:
        return True
    if names is not None and top in names:
        return True
    for base in (root, os.path.join(root, "src")):
        if os.path.isdir(os.path.join(base, top)) or os.path.isfile(os.path.join(base, top + ".py")):
            return True
    return False


def importable(module):
    """Whether the interpreter running this check can already import the first component of `module`."""
    try:
        return importlib.util.find_spec(module.split(".", 1)[0]) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def summarize(found, root, is_importable=importable, names=None):
    """(issues, ordered [(module, occurrences)], installable candidates in the order they should be named).

    Candidates are the unresolved modules that are not the repository's own, most frequent first -- but the ones whose
    top-level package is NOT yet importable in this environment come before those that are (an install that did not
    help, or a missing submodule). The first candidate is the one the last line of the output names, and the repair
    loop's stuck-detection looks at that name: naming the next not-yet-installed module keeps the signal moving while
    the agent works through the list, and it stays on one module only when nothing else is left to try."""
    counts = {}
    for module, _file in found:
        counts[module] = counts.get(module, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    candidates = [(m, n) for m, n in ordered if not is_repo_local(m, root, names)]
    candidates = [c for c in candidates if not is_importable(c[0])] + [c for c in candidates if is_importable(c[0])]
    return len(found), ordered, candidates


HINT = ("These are the imports pyright could not resolve in the current environment. Look each third-party name up "
        "with lookup_package, then install the real package behind it (one install_package call per package; several "
        "calls in one turn are fine) and run the target again. Names that are this repository's own modules cannot be "
        "fixed by installing anything.")


def render(issues, ordered, candidates, version):
    """(report for stdout, the final exception line). The final line carries the count: only the last
    exception line of a failing run is kept in the evaluation rows (<= 300 characters)."""
    lines = ["EnvBench check (pyright %s): %d reportMissingImports diagnostic(s) in %d distinct module(s)."
             % (version or "?", issues, len(ordered))]
    if ordered:
        lines.append("Unresolved imports (module: occurrences), most frequent first:")
        for module, n in ordered[:MAX_LISTED]:
            lines.append("  %s: %d" % (module, n))
        if len(ordered) > MAX_LISTED:
            lines.append("  ... and %d more" % (len(ordered) - MAX_LISTED))
    if candidates:
        lines.append(HINT)
        first = candidates[0][0].split(".", 1)[0]
        lines.append("Next step: lookup_package('%s'), then install_package for the package it resolves to, then verify." % first)
    suffix = " [EnvBench check: %d missing-import diagnostics, %d modules]" % (issues, len(ordered))
    if candidates:
        final = "ModuleNotFoundError: No module named '%s'%s" % (candidates[0][0].split(".", 1)[0], suffix)
    else:
        local = ", ".join(m for m, _n in ordered[:5])
        final = "ImportError: only repository-local or relative imports are unresolved (%s)%s" % (local, suffix)
    return "\n".join(lines), final


def stderr_message(candidates, final):
    """What goes to stderr on a failure: SHORT, and ending with the exception line -- the agent's diagnose step
    reads the last line of what it is given, and a long list there is cut off when the model copies the result."""
    names = ", ".join(m.split(".", 1)[0] for m, _n in candidates[:6])
    head = "EnvBench check failed; most frequent unresolved third-party modules: %s\n" % names if names else ""
    return head + final + "\n"


def main(argv=None):
    root = os.path.dirname(os.path.abspath(__file__))
    pyright_bin = os.environ.get("PYRIGHT_BIN", PYRIGHT_BIN_DEFAULT)
    timeout = int(os.environ.get("ENVBENCH_PYRIGHT_TIMEOUT", str(TIMEOUT_DEFAULT)))
    parsed, error = run_pyright(root, sys.executable, pyright_bin, timeout)
    if parsed is None:
        sys.stderr.write("EnvBenchCheckError: the EnvBench check could not run: %s\n" % error)
        return 3
    found = missing_imports(parsed)
    issues, ordered, candidates = summarize(found, root, names=local_names(root))
    text, final = render(issues, ordered, candidates, parsed.get("version"))
    if issues == 0:
        print(text)
        print("EnvBench check PASSED: 0 reportMissingImports (python %s)" % sys.version.split()[0])
        return 0
    print(text)
    sys.stderr.write(stderr_message(candidates, final))
    return 1


if __name__ == "__main__":
    sys.exit(main())
