"""Classify the error text captured by runner.py into a structured Diagnosis.

Phase 2 of the dependency-repair loop: run -> read error -> (later phases)
propose fix -> apply -> re-verify -> explain. This module only reads and
classifies stderr; it changes nothing and fixes nothing.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

from runner import RunResult, run_project

# The line that actually names the exception, e.g.:
#   AttributeError: module 'numpy' has no attribute 'float'
# It is unindented (unlike "  File ..." / code lines in a traceback), which
# is what lets us find it even when noisy explanatory text follows it (as
# NumPy's deprecation messages do).
_EXCEPTION_LINE_RE = re.compile(r"^([A-Za-z_][\w.]*):\s*(.*)$")

# 1. missing_module — e.g. "ModuleNotFoundError: No module named 'seaborn'"
_MISSING_MODULE_RE = re.compile(r"No module named ['\"]([\w.]+)['\"]")

# 2. import_name — e.g. "ImportError: cannot import name 'Mapping' from 'collections'"
_IMPORT_NAME_RE = re.compile(
    r"cannot import name ['\"]([^'\"]+)['\"] from ['\"]([\w.]+)['\"]"
)

# 3. module_attribute_removed — e.g. "AttributeError: module 'numpy' has no attribute 'float'"
_MODULE_ATTR_RE = re.compile(
    r"module ['\"]([\w.]+)['\"] has no attribute ['\"]([^'\"]+)['\"]"
)

# 4. object_attribute_error — e.g. "AttributeError: 'DataFrame' object has no attribute 'append'"
_OBJECT_ATTR_RE = re.compile(
    r"['\"]([\w.]+)['\"] object has no attribute ['\"]([^'\"]+)['\"]"
)


@dataclass
class Diagnosis:
    """Structured classification of a captured error, for later phases to act on."""

    kind: str
    module: str = ""
    package: str = ""
    symbol: str = ""
    detail: str = ""


def _top_level(module: str) -> str:
    """'sklearn.externals' -> 'sklearn'; 'seaborn' -> 'seaborn'."""
    return module.split(".", 1)[0]


def _find_exception_line(stderr: str) -> str | None:
    """Return the last unindented 'ExceptionName: message' line, if any.

    Scanning from the end (rather than just taking the last line) matters
    because some libraries print extra explanatory text after the real
    exception line (e.g. NumPy's deprecation notes).
    """
    for line in reversed(stderr.splitlines()):
        if _EXCEPTION_LINE_RE.match(line):
            return line
    return None


def diagnose(stderr: str) -> Diagnosis:
    """Classify captured stderr text into a Diagnosis. Never raises."""
    stderr = stderr or ""
    exception_line = _find_exception_line(stderr)

    if exception_line is None:
        last_line = next(
            (line for line in reversed(stderr.splitlines()) if line.strip()), ""
        )
        return Diagnosis(kind="unknown", detail=last_line)

    _, message = _EXCEPTION_LINE_RE.match(exception_line).groups()

    if m := _MISSING_MODULE_RE.search(message):
        module = m.group(1)
        return Diagnosis(
            kind="missing_module",
            module=module,
            package=_top_level(module),
            detail=exception_line,
        )

    if m := _IMPORT_NAME_RE.search(message):
        symbol, module = m.group(1), m.group(2)
        return Diagnosis(
            kind="import_name",
            module=module,
            package=_top_level(module),
            symbol=symbol,
            detail=exception_line,
        )

    if m := _MODULE_ATTR_RE.search(message):
        module, symbol = m.group(1), m.group(2)
        return Diagnosis(
            kind="module_attribute_removed",
            module=module,
            package=_top_level(module),
            symbol=symbol,
            detail=exception_line,
        )

    if m := _OBJECT_ATTR_RE.search(message):
        symbol = m.group(2)
        return Diagnosis(kind="object_attribute_error", symbol=symbol, detail=exception_line)

    return Diagnosis(kind="unknown", detail=exception_line)


def diagnose_result(result: RunResult) -> Diagnosis:
    """Convenience wrapper: diagnose a Phase 1 RunResult directly."""
    if result.ok:
        return Diagnosis(kind="none")
    return diagnose(result.stderr)


def _describe(d: Diagnosis) -> str:
    """One-line human summary of a Diagnosis, for the CLI."""
    if d.kind == "none":
        return "none (ran fine)"
    if d.kind == "missing_module":
        return f"missing_module: {d.module}"
    if d.kind == "import_name":
        return f"import_name: {d.symbol} from {d.module}"
    if d.kind == "module_attribute_removed":
        return f"module_attribute_removed: {d.package}.{d.symbol}"
    if d.kind == "object_attribute_error":
        return f"object_attribute_error: .{d.symbol}"
    return f"unknown: {d.detail}"


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a target file and diagnose the resulting error, if any."
    )
    parser.add_argument("path", help="path to the target .py file")
    parser.add_argument(
        "--timeout", type=int, default=60, help="seconds before the run is killed"
    )
    args = parser.parse_args()

    result = run_project(args.path, timeout=args.timeout)
    d = diagnose_result(result)

    print(_describe(d))
    for field in ("module", "package", "symbol", "detail"):
        value = getattr(d, field)
        if value:
            print(f"  {field}: {value}")

    return 0 if d.kind == "none" else 1


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
