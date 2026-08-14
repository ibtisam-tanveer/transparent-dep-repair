"""Orchestrate run -> diagnose -> propose -> apply -> verify, with an
explanation trail for every attempt. This is the closed loop for the one
case Phase 3 handles (a resolvable missing package); everything else stops
and is reported honestly rather than guessed at.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from .apply import apply
from .diagnose import Diagnosis, diagnose_result
from .repair import Proposal, propose
from .runner import run_project
from .venv_manager import get_venv_python

MAX_ATTEMPTS = 5


@dataclass
class Attempt:
    """One iteration of the loop: what was proposed, whether it was applied,
    and what re-running the project afterward actually showed."""

    proposal: Proposal
    applied: bool
    verification: str


@dataclass
class RepairResult:
    target: str
    fixed: bool
    attempts: list[Attempt] = field(default_factory=list)


def _signature(d: Diagnosis) -> tuple[str, str, str, str]:
    """Identity of a diagnosis for progress tracking, excluding `detail`
    (which can vary incidentally, e.g. embedded paths) so only a genuinely
    repeated failure — not just repeated noise — counts as no progress.
    """
    return (d.kind, d.module, d.package, d.symbol)


def _last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return "(no output)"


def repair(path: str, max_attempts: int = MAX_ATTEMPTS) -> RepairResult:
    """Try to fix `path`, inside an isolated venv, verifying by re-running.
    Never raises: any failure to progress just ends the loop with fixed=False
    and an explanation in the last attempt.
    """
    python_exe = get_venv_python(path)
    attempts: list[Attempt] = []
    last_signature: tuple[str, str, str, str] | None = None

    for _ in range(max_attempts):
        result = run_project(path, python_exe=python_exe)

        if result.ok:
            if attempts:
                attempts[-1].verification = "verified: project now runs"
            return RepairResult(target=path, fixed=True, attempts=attempts)

        diagnosis = diagnose_result(result)
        signature = _signature(diagnosis)

        if signature == last_signature:
            attempts.append(
                Attempt(
                    proposal=Proposal(
                        kind="none",
                        reason="no progress: the same diagnosis repeated after the last attempt",
                        confidence="low",
                    ),
                    applied=False,
                    verification="stopped: no progress",
                )
            )
            return RepairResult(target=path, fixed=False, attempts=attempts)
        last_signature = signature

        proposal = propose(diagnosis)

        if proposal.kind != "install":
            attempts.append(Attempt(proposal=proposal, applied=False, verification="not handled yet"))
            return RepairResult(target=path, fixed=False, attempts=attempts)

        ok, log = apply(proposal, python_exe)
        if not ok:
            attempts.append(
                Attempt(proposal=proposal, applied=False, verification=f"install failed: {_last_line(log)}")
            )
            return RepairResult(target=path, fixed=False, attempts=attempts)

        attempts.append(Attempt(proposal=proposal, applied=True, verification="pending re-run"))
        # loop continues: next iteration re-runs the project to verify

    return RepairResult(target=path, fixed=False, attempts=attempts)


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose a target file and attempt to fix it (missing packages only, Phase 3)."
    )
    parser.add_argument("path", help="path to the target .py file")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    args = parser.parse_args()

    result = repair(args.path, max_attempts=args.max_attempts)

    print(f"{'FIXED' if result.fixed else 'NOT FIXED'}: {args.path}")
    for i, attempt in enumerate(result.attempts, start=1):
        p = attempt.proposal
        label = f"{p.kind}" + (f" {p.package}" if p.package else "")
        print(f"  attempt {i}: {label}")
        print(f"    reason: {p.reason}")
        if p.source:
            print(f"    source: {p.source}")
        print(f"    confidence: {p.confidence}")
        print(f"    applied: {attempt.applied}")
        print(f"    verification: {attempt.verification}")

    return 0 if result.fixed else 1


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
