"""Orchestrate run -> diagnose -> propose -> apply -> verify, with an
explanation trail for every attempt.

Phase 3's case (a resolvable missing package) stays exactly as it was:
propose() decides, apply() installs, the next run verifies. Phase 5 adds
the hard cases (removed/changed APIs): propose_hard_case() asks an LLM for
two candidates in one call, and this module applies + verifies each in
turn, keeps whichever one makes the project run, and records the other as
a rejected alternative. Every case, easy or hard, is verified by actually
re-running the project — never by trusting an install or an LLM's word.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from .apply import apply, apply_code_edit, installed_packages, restore_packages
from .diagnose import Diagnosis, diagnose_result
from .repair import HARD_CASE_KINDS, Proposal, propose, propose_hard_case
from .runner import run_project
from .venv_manager import get_venv_python, get_workspace_copy

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


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as exc:
        return f"<could not read {path}: {exc}>"


def _resolve_hard_case(diagnosis, workspace_path, python_exe, source_before, error_text):
    """Ask the LLM for both candidates, apply+verify each in order, and
    return (winning_proposal_or_None, rejection_notes).

    The two candidates are kept genuinely independent: a failed code edit is
    reverted back to `source_before`, and a failed environment fix has its
    installed packages (including transitive dependencies) removed by
    diffing against a pre-attempt snapshot — see
    PHASE5_IMPROVEMENTS_TASK.md #1. Without this, a losing candidate's
    leftover state could make the *next* candidate pass or fail for reasons
    unrelated to its own merits, corrupting the strategy_won measurement.
    Never raises. Returns (winner_or_None, rejection_notes, model_identifier)
    -- the model is surfaced even when there's no winner, so a "not fixed"
    result still records which model was tried (PHASE5_IMPROVEMENTS_TASK.md #2).
    """
    hard = propose_hard_case(diagnosis, source_before, error_text)

    if hard.error:
        return None, [f"LLM unavailable: {hard.error}"], hard.model

    candidates = {"code": hard.code_fix, "environment": hard.env_fix}
    notes: list[str] = []

    for strategy in hard.order:
        candidate = candidates.get(strategy)
        if candidate is None or not candidate.applicable:
            reason = candidate.reason if candidate else "no candidate returned"
            notes.append(f"{strategy} fix: not applicable ({reason})")
            continue

        packages_before = installed_packages(python_exe) if strategy == "environment" else None

        if strategy == "code":
            ok, log = apply_code_edit(candidate.edits, workspace_path)
        else:
            pkg_spec = f"{candidate.package}{candidate.constraint}"
            ok, log = apply(Proposal(kind="install", package=pkg_spec), python_exe)

        if not ok:
            notes.append(f"{strategy} fix: failed to apply ({_last_line(log)})")
            if strategy == "code":
                _write_text(workspace_path, source_before)  # nothing applied; keep tidy
            elif packages_before is not None:
                restore_packages(python_exe, packages_before)  # e.g. a partial/broken install
            continue

        verify_result = run_project(workspace_path, python_exe=python_exe)
        if verify_result.ok:
            winner = Proposal(
                kind="code_edit" if strategy == "code" else "install",
                package=candidate.package if strategy == "environment" else "",
                edits=candidate.edits if strategy == "code" else [],
                reason=candidate.reason,
                source="LLM suggestion, verified by re-running",
                confidence="high",
                strategy_won=strategy,
                alternatives="; ".join(notes) or "(only one strategy was applicable)",
                model=hard.model,
            )
            return winner, notes, hard.model

        notes.append(f"{strategy} fix: applied but the project still failed after re-run")
        if strategy == "code":
            _write_text(workspace_path, source_before)  # revert before trying the other
        elif packages_before is not None:
            restore_packages(python_exe, packages_before)  # revert before trying the other

    return None, notes, hard.model


def _write_text(path: str, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass  # best-effort revert; a failed revert still can't crash the tool


def repair(path: str, max_attempts: int = MAX_ATTEMPTS) -> RepairResult:
    """Try to fix `path`, inside an isolated venv, verifying by re-running.
    Never raises: any failure to progress just ends the loop with fixed=False
    and an explanation in the last attempt.

    `path` itself is read once (to make the working copy) and never modified;
    every run and edit from here on targets that copy.
    """
    python_exe = get_venv_python(path)
    workspace_path = get_workspace_copy(path)
    attempts: list[Attempt] = []
    last_signature: tuple[str, str, str, str] | None = None

    for _ in range(max_attempts):
        result = run_project(workspace_path, python_exe=python_exe)

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

        if diagnosis.kind in HARD_CASE_KINDS:
            source_before = _read_text(workspace_path)
            winner, notes, model_tried = _resolve_hard_case(
                diagnosis, workspace_path, python_exe, source_before, result.stderr
            )

            if winner is None:
                attempts.append(
                    Attempt(
                        proposal=Proposal(
                            kind="none",
                            reason="neither the code fix nor the environment fix verified",
                            alternatives="; ".join(notes),
                            confidence="low",
                            model=model_tried,
                        ),
                        applied=False,
                        verification="not fixed: no candidate verified by re-running",
                    )
                )
                return RepairResult(target=path, fixed=False, attempts=attempts)

            attempts.append(Attempt(proposal=winner, applied=True, verification="verified: project now runs"))
            # No need to loop again: the winning candidate's own verification
            # run inside _resolve_hard_case already showed the whole project
            # runs end-to-end (ok=True), which is strictly stronger than
            # "this one error is gone" -- there is no next layer to reveal.
            return RepairResult(target=path, fixed=True, attempts=attempts)

        # Phase 3 path: unchanged.
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
        description="Diagnose a target file and attempt to fix it (missing packages, "
        "plus LLM-based code/environment fixes for removed or changed APIs)."
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
        if p.model:
            print(f"    model: {p.model}")
        if p.strategy_won:
            print(f"    strategy_won: {p.strategy_won}")
        if p.alternatives:
            print(f"    alternatives: {p.alternatives}")
        print(f"    confidence: {p.confidence}")
        print(f"    applied: {attempt.applied}")
        print(f"    verification: {attempt.verification}")

    return 0 if result.fixed else 1


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
