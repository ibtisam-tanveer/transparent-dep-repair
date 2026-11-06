"""Repair the Python interpreter version, not just the libraries --
TASK_interpreter_repair.md.

The version-decay diagnosis (VERSION_DECAY_DIAGNOSIS_SUMMARY.md) showed the
dominant blocker for real scientific notebooks is *interpreter* decay: a 2019
repo pins Python 3.7-era wheels (numpy==1.17.3) that cannot install on the
Python 3.14 the evaluation runs. The interpreter is part of the dependency
configuration, so this module reconstructs it the way repo2docker/Binder do:
read the repo's own declared recipe first (grounded), fall back to an
era estimate (ungrounded, tagged as such), provision that interpreter, and
let the existing package/code repair run on top of it.

Three steps, kept separate so each is independently testable:

1. `detect_required_python` -- which version, and *why* (declared file name
   or era-inferred). Pure file reading; never executes repo code.
2. `provision_interpreter` -- obtain it: conda/mamba straight from an
   environment.yml (what that file is for), else `uv venv --python X.Y`.
   Falls back to the nearest *newer* reachable interpreter and says so,
   rather than silently substituting one.
3. `build_repair_environment` -- orchestration used by the repo-scale entry
   points; returns the python executable plus a provenance dict.

Honest limits, reported rather than chased: Python 2 / pre-3.4 repos, and
versions no reachable build exists for. On this project's Apple-Silicon dev
machine neither uv nor conda-forge ships CPython 3.7, so a declared 3.7 is
provisioned as the nearest reachable 3.8 and tagged accordingly.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from . import repo as repo_module
from .agent import GROUNDING_LLM, GROUNDING_METADATA

# Minor versions we reason about. Anything older than the oldest *provisionable*
# one is reported as unprovisionable rather than guessed at.
_KNOWN_MINORS = ["3.4", "3.5", "3.6", "3.7", "3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]
_MIN_PROVISIONABLE_MINOR = (3, 6)
# How far above the requested minor we are willing to go when the exact one
# can't be obtained (3.7 -> 3.8 or 3.9, never silently 3.14).
_MAX_NEAREST_DISTANCE = 2

# CPython feature-release dates, used only for era inference when the repo
# declares nothing: "which Python was current when this repo was last touched".
_PYTHON_RELEASES = [
    ("3.4", "2014-03-16"), ("3.5", "2015-09-13"), ("3.6", "2016-12-23"),
    ("3.7", "2018-06-27"), ("3.8", "2019-10-14"), ("3.9", "2020-10-05"),
    ("3.10", "2021-10-04"), ("3.11", "2022-10-24"), ("3.12", "2023-10-02"),
    ("3.13", "2024-10-07"), ("3.14", "2025-10-07"),
]

UV_TIMEOUT = 600
CONDA_TIMEOUT = 900

# repo2docker consults binder/ then .binder/ then the root; we additionally
# accept the first match anywhere (same rule as repo.find_dependency_files).
_RECIPE_DIRS = ["binder", ".binder", ""]


@dataclass
class PythonChoice:
    """Which interpreter version the repo needs, and the evidence for it."""

    version: str | None  # "3.7", or None when the repo gives no usable signal
    source: str  # relative recipe path, "era-inferred", or "none"
    grounding: str  # GROUNDING_METADATA (declared) / GROUNDING_LLM (inferred) / ""
    detail: str = ""  # the raw spec text the version came from
    unprovisionable_reason: str = ""  # set for Python 2 / pre-3.4


@dataclass
class Provisioned:
    ok: bool
    python_exe: str = ""
    requested: str | None = None
    actual: str = ""  # the interpreter's own reported "X.Y", not what we asked for
    mechanism: str = ""  # "conda" | "uv" | "reused" | ""
    note: str = ""
    log: str = ""
    unprovisionable_reason: str = ""


@dataclass
class RepairedEnvironment:
    """What the repo-scale entry points need: an executable and provenance."""

    python_exe: str
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Part A -- detection
# --------------------------------------------------------------------------

def _current_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _minor_tuple(v: str) -> tuple[int, int]:
    major, minor = v.split(".")[:2]
    return int(major), int(minor)


def _specifier_to_minor(spec_text: str) -> tuple[str | None, str]:
    """Resolve a PEP 440 specifier to ONE minor version, only when the spec is
    actually concrete (an equality, or a range with an upper bound). A bare
    lower bound like ">=3.6" says nothing about era -- it is not concrete and
    must not be treated as "3.6" (the task: first *concrete* version wins).
    Returns (minor | None, reason)."""
    try:
        spec = SpecifierSet(spec_text.replace(" ", ""))
    except InvalidSpecifier:
        return None, f"unparseable specifier {spec_text!r}"
    ops = {s.operator for s in spec}
    # Any patch release counts: "==3.8.2" must resolve to 3.8, so probing only
    # "3.8.0" would reject exactly the most specific (best) declarations.
    matching = [m for m in _KNOWN_MINORS if any(spec.contains(f"{m}.{p}") for p in range(0, 40))]
    if not matching:
        return None, f"{spec_text!r} matches no known minor version"
    if ops & {"==", "==="}:
        return matching[-1], ""
    # An upper bound only carries era information if it actually excludes
    # the newest interpreter: ">=3.6,<4" is a conventional cap, not a signal.
    if matching[-1] == _KNOWN_MINORS[-1]:
        return None, f"{spec_text!r} has no effective upper bound, not concrete"
    return matching[-1], ""


def _conda_python_spec_to_pep440(rest: str) -> str | None:
    """Turn the part of a conda dependency after the name `python` into a
    PEP 440 specifier. Handles `=3.7`, `=3.7.*`, `==3.7.4`, ` 3.7.*`,
    `>=3.6,<3.8`, and drops a trailing build string (`=3.7.4=h12345_0`)."""
    rest = rest.strip()
    if not rest:
        return None
    if rest[0] in "<>!~":
        return rest
    if rest.startswith("=="):
        value = rest[2:].split("=")[0].strip()
    elif rest.startswith("="):
        value = rest[1:].split("=")[0].strip()
    elif rest[0].isdigit():
        value = rest.split("=")[0].strip()
    else:
        return None
    value = value.rstrip(",")
    if not re.match(r"^\d+(\.\d+)*(\.\*)?$", value):
        return None
    if value.endswith(".*") or value.count(".") >= 2:
        return f"=={value}"
    return f"=={value}.*"


def _python_from_environment_yml(path: str) -> tuple[str | None, str, str]:
    """(minor | None, raw_spec, python2_reason)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None, "", ""
    if not isinstance(data, dict):
        return None, "", ""
    for dep in data.get("dependencies") or []:
        if not isinstance(dep, str):
            continue
        m = re.match(r"^\s*python(?![\w\-])(.*)$", dep, re.IGNORECASE)
        if not m:
            continue
        pep = _conda_python_spec_to_pep440(m.group(1))
        if pep is None:
            continue
        py2 = re.match(r"^==\s*2\.", pep)
        if py2:
            return "2.7", dep.strip(), "Python 2 is out of scope"
        minor, _ = _specifier_to_minor(pep)
        if minor:
            return minor, dep.strip(), ""
    return None, "", ""


def _python_from_runtime_txt(path: str) -> tuple[str | None, str, str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^\s*python-(\d+)\.(\d+)(?:\.\d+)?\s*$", line)
                if not m:
                    continue
                major, minor = int(m.group(1)), int(m.group(2))
                if major == 2:
                    return "2.7", line.strip(), "Python 2 is out of scope"
                return f"{major}.{minor}", line.strip(), ""
    except OSError:
        pass
    return None, "", ""


def _python_from_python_version_file(path: str) -> tuple[str | None, str, str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^(\d+)\.(\d+)(?:\.\d+)?", line)
                if not m:
                    return None, "", ""
                major, minor = int(m.group(1)), int(m.group(2))
                if major == 2:
                    return "2.7", line, "Python 2 is out of scope"
                return f"{major}.{minor}", line, ""
    except OSError:
        pass
    return None, "", ""


def _python_from_python_requires(path: str) -> tuple[str | None, str, str]:
    """`python_requires` / `requires-python`, read as text -- setup.py is never
    executed."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None, "", ""
    m = re.search(r"""(?:python_requires|requires-python)\s*[=:]\s*["']([^"']+)["']""", text)
    if not m:
        return None, "", ""
    minor, _ = _specifier_to_minor(m.group(1))
    return (minor, m.group(1), "") if minor else (None, "", "")


# (basename, parser) in the task's priority order.
_DECLARED_SOURCES = [
    ("environment.yml", _python_from_environment_yml),
    ("runtime.txt", _python_from_runtime_txt),
    (".python-version", _python_from_python_version_file),
    ("setup.py", _python_from_python_requires),
    ("pyproject.toml", _python_from_python_requires),
]


def _locate(repo_path: str, basename: str) -> str | None:
    """Relative path of `basename`: binder/, .binder/, root first (repo2docker's
    own lookup order), then the shallowest match anywhere else."""
    for d in _RECIPE_DIRS:
        rel = os.path.join(d, basename) if d else basename
        if os.path.isfile(os.path.join(repo_path, rel)):
            return rel
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [
            d for d in dirs
            if not repo_module._is_excluded_dir(d) and not repo_module._is_venv_dir(os.path.join(root, d))
        ]
        if basename in files:
            return os.path.relpath(os.path.join(root, basename), repo_path)
    return None


def _infer_from_era(date_iso: str) -> str | None:
    """Newest CPython feature release that existed on `date_iso`."""
    chosen = None
    for minor, released in _PYTHON_RELEASES:
        if released <= date_iso[:10]:
            chosen = minor
    return chosen


def detect_required_python(repo_path: str, era_cutoff_date: str | None = None) -> PythonChoice:
    """Declared recipe first (grounded), era estimate second (ungrounded)."""
    for basename, parser in _DECLARED_SOURCES:
        rel = _locate(repo_path, basename)
        if rel is None:
            continue
        version, detail, py2_reason = parser(os.path.join(repo_path, rel))
        if version is None:
            continue
        return PythonChoice(
            version=version, source=rel, grounding=GROUNDING_METADATA,
            detail=detail, unprovisionable_reason=py2_reason,
        )

    if era_cutoff_date:
        version = _infer_from_era(era_cutoff_date)
        if version:
            return PythonChoice(
                version=version, source="era-inferred", grounding=GROUNDING_LLM,
                detail=f"newest CPython as of last commit {era_cutoff_date[:10]}",
            )
    return PythonChoice(version=None, source="none", grounding="")


# --------------------------------------------------------------------------
# Part B -- provisioning
# --------------------------------------------------------------------------

def _python_in(prefix: str) -> str:
    if sys.platform == "win32":
        return os.path.join(prefix, "python.exe")
    return os.path.join(prefix, "bin", "python")


def _interpreter_minor(python_exe: str) -> str | None:
    """Ask the interpreter itself -- never trust the version we requested."""
    try:
        completed = subprocess.run(
            [python_exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = completed.stdout.strip()
    return out if completed.returncode == 0 and re.match(r"^\d+\.\d+$", out) else None


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    sibling = os.path.join(os.path.dirname(sys.executable), "uv")
    return sibling if os.path.isfile(sibling) else None


def find_conda_tool() -> str | None:
    """micromamba/mamba/conda, preferring an explicit override, then this
    project's own .tools/ (see TASK_interpreter_repair.md setup notes)."""
    override = os.environ.get("REPAIR_TOOL_MICROMAMBA")
    if override and os.path.isfile(override):
        return override
    project_local = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tools", "bin", "micromamba")
    if os.path.isfile(project_local):
        return project_local
    for name in ("micromamba", "mamba", "conda"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _mamba_env(tool: str) -> dict:
    env = dict(os.environ)
    if "MAMBA_ROOT_PREFIX" not in env and os.path.basename(os.path.dirname(os.path.dirname(tool))) == ".tools":
        env["MAMBA_ROOT_PREFIX"] = os.path.join(os.path.dirname(os.path.dirname(tool)), "mamba_root")
    return env


def _nearest_candidates(requested: str) -> list[str]:
    req = _minor_tuple(requested)
    out = [requested]
    for m in _KNOWN_MINORS:
        t = _minor_tuple(m)
        if t[0] == req[0] and 0 < t[1] - req[1] <= _MAX_NEAREST_DISTANCE:
            out.append(m)
    return out


def _try_conda(tool: str, env_file: str, prefix: str, timeout: int) -> tuple[bool, str]:
    cmd = [tool, "create", "-y", "-p", prefix, "-f", env_file]
    if os.path.basename(tool).startswith("conda"):
        cmd = [tool, "env", "create", "-y", "-p", prefix, "-f", env_file]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=_mamba_env(tool),
        )
    except subprocess.TimeoutExpired:
        return False, f"conda env build timed out after {timeout}s"
    except OSError as exc:
        return False, f"could not run {tool!r}: {exc}"
    return completed.returncode == 0, (completed.stdout + completed.stderr)[-4000:]


def _try_uv(uv: str, minor: str, prefix: str, timeout: int) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [uv, "venv", "--seed", "--clear", "--python", minor, prefix],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"uv venv for Python {minor} timed out after {timeout}s"
    except OSError as exc:
        return False, f"could not run {uv!r}: {exc}"
    return completed.returncode == 0, (completed.stdout + completed.stderr)[-2000:]


def prefer_conda_for_exact_python() -> bool:
    """True inside the runtime container (REPAIR_TOOL_PREFER_CONDA=1): there
    micromamba on linux-64 can supply old CPython (3.6/3.7) that uv cannot, so
    it is tried for the EXACT requested minor before uv's nearest-newer
    fallback. Off on the host, where behaviour is unchanged."""
    return os.environ.get("REPAIR_TOOL_PREFER_CONDA", "") not in ("", "0")


def _try_bare_conda(tool: str, minor: str, prefix: str, timeout: int) -> tuple[bool, str]:
    """A conda env holding just `python=<minor>` (+pip) from conda-forge --
    the interpreter only, none of a recipe's pins."""
    spec = os.path.join(os.path.dirname(prefix.rstrip(os.sep)) or ".", f"_python_{minor}_env.yml")
    try:
        os.makedirs(os.path.dirname(spec), exist_ok=True)
        with open(spec, "w", encoding="utf-8") as f:
            f.write(f"name: interp\nchannels:\n  - conda-forge\ndependencies:\n  - python={minor}\n  - pip\n")
    except OSError as exc:
        return False, f"could not write the interpreter spec: {exc}"
    return _try_conda(tool, spec, prefix, timeout)


def provision_interpreter(
    choice: PythonChoice, env_dir: str, repo_root: str,
    *, conda_timeout: int = CONDA_TIMEOUT, uv_timeout: int = UV_TIMEOUT,
) -> Provisioned:
    """Obtain an interpreter satisfying `choice` at `env_dir`. Never raises.

    Dispatch mirrors repo2docker: a conda recipe is built *as a conda recipe*
    (that is what environment.yml is for -- the pinned Python and the pinned
    stack are one solve); otherwise `uv venv`. If neither can supply the exact
    minor, the nearest newer reachable one is used and the substitution is
    recorded in `note` -- it is a different, weaker claim than "the declared
    version" and the report must say so.
    """
    requested = choice.version
    if choice.unprovisionable_reason or requested is None:
        return Provisioned(
            ok=False, requested=requested,
            unprovisionable_reason=choice.unprovisionable_reason or "no version requested",
            note=choice.unprovisionable_reason,
        )
    if _minor_tuple(requested) < _MIN_PROVISIONABLE_MINOR:
        reason = f"Python {requested} predates the oldest provisionable interpreter (3.{_MIN_PROVISIONABLE_MINOR[1]})"
        return Provisioned(ok=False, requested=requested, unprovisionable_reason=reason, note=reason)

    existing = _python_in(env_dir)
    if os.path.isfile(existing):
        actual = _interpreter_minor(existing)
        if actual:
            return Provisioned(ok=True, python_exe=existing, requested=requested, actual=actual,
                               mechanism="reused", note="reused a previously provisioned interpreter")

    logs: list[str] = []

    if choice.source.endswith("environment.yml"):
        tool = find_conda_tool()
        if tool:
            ok, log = _try_conda(tool, os.path.join(repo_root, choice.source), env_dir, conda_timeout)
            logs.append(f"conda ({os.path.basename(tool)}): {'ok' if ok else 'FAILED'}\n{log}")
            exe = _python_in(env_dir)
            if ok and os.path.isfile(exe):
                actual = _interpreter_minor(exe)
                if actual:
                    return Provisioned(ok=True, python_exe=exe, requested=requested, actual=actual,
                                       mechanism="conda", log="\n".join(logs))
            shutil.rmtree(env_dir, ignore_errors=True)  # a failed solve can leave a partial prefix
        else:
            logs.append("conda: no micromamba/mamba/conda available; falling back to uv")

    if prefer_conda_for_exact_python():
        tool = find_conda_tool()
        if tool:
            ok, log = _try_bare_conda(tool, requested, env_dir, conda_timeout)
            logs.append(f"conda python={requested} (interpreter only): {'ok' if ok else 'FAILED'}\n{log}")
            exe = _python_in(env_dir)
            if ok and os.path.isfile(exe):
                actual = _interpreter_minor(exe)
                if actual == requested:
                    note = ""
                    if choice.source.endswith("environment.yml"):
                        note = (f"the repo's own environment.yml could not be solved here; Python {requested} was "
                                "provisioned on its own and the packages are installed on top")
                    return Provisioned(ok=True, python_exe=exe, requested=requested, actual=actual,
                                       mechanism="conda", note=note, log="\n".join(logs))
            shutil.rmtree(env_dir, ignore_errors=True)

    uv = find_uv()
    if uv is None:
        return Provisioned(
            ok=False, requested=requested, log="\n".join(logs),
            unprovisionable_reason="no interpreter provisioner available (install uv or micromamba)",
        )
    for minor in _nearest_candidates(requested):
        ok, log = _try_uv(uv, minor, env_dir, uv_timeout)
        logs.append(f"uv --python {minor}: {'ok' if ok else 'FAILED'}\n{log}")
        exe = _python_in(env_dir)
        if ok and os.path.isfile(exe):
            actual = _interpreter_minor(exe)
            if actual:
                note = ""
                if actual != requested:
                    note = f"Python {requested} not reachable on this platform; nearest available {actual} used"
                return Provisioned(ok=True, python_exe=exe, requested=requested, actual=actual,
                                   mechanism="uv", note=note, log="\n".join(logs))
        shutil.rmtree(env_dir, ignore_errors=True)

    reason = f"no reachable build of Python {requested} (or nearest {_MAX_NEAREST_DISTANCE} newer minors)"
    return Provisioned(ok=False, requested=requested, log="\n".join(logs),
                       unprovisionable_reason=reason, note=reason)


# --------------------------------------------------------------------------
# Orchestration + provenance (Part C)
# --------------------------------------------------------------------------

def build_repair_environment(
    repo_root: str, env_dir: str, base_python_exe: str,
    era_cutoff_date: str | None = None, enabled: bool = True,
) -> RepairedEnvironment:
    """Decide and, if needed, provision the interpreter for one repo.

    `base_python_exe` is the already-built default environment; it is returned
    unchanged when repair is disabled, the repo gives no signal, the right
    version is already running, or provisioning fails (the failure is recorded,
    never raised -- one unprovisionable repo must not abort a run).
    """
    current = _current_minor()
    meta = {
        "python_version_requested": None,
        "python_version_actual": current,
        "python_version_source": "none",
        "python_grounding": "",
        "python_mechanism": "default",
        "python_note": "",
        "interpreter_unprovisionable": False,
    }
    if not enabled:
        meta["python_note"] = "interpreter repair disabled"
        return RepairedEnvironment(base_python_exe, meta)

    choice = detect_required_python(repo_root, era_cutoff_date)
    meta.update(
        python_version_requested=choice.version,
        python_version_source=choice.source,
        python_grounding=choice.grounding,
    )
    if choice.version is None:
        return RepairedEnvironment(base_python_exe, meta)

    if choice.version == current and not choice.unprovisionable_reason:
        meta["python_mechanism"] = "default"
        return RepairedEnvironment(base_python_exe, meta)

    prov = provision_interpreter(choice, env_dir, repo_root)
    if prov.ok:
        meta.update(python_version_actual=prov.actual, python_mechanism=prov.mechanism, python_note=prov.note)
        return RepairedEnvironment(prov.python_exe, meta)

    meta.update(
        interpreter_unprovisionable=True,
        python_note=prov.unprovisionable_reason or prov.note,
    )
    return RepairedEnvironment(base_python_exe, meta)
